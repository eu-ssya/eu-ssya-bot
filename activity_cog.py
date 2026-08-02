import asyncio
import io
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from activity_store import ActivityStore


logger = logging.getLogger(__name__)


def utc_now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


async def require_admin(interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if interaction.guild is not None and bool(
        permissions and permissions.administrator
    ):
        return True
    kwargs = {"ephemeral": True}
    if interaction.response.is_done():
        await interaction.followup.send("서버 관리자만 사용할 수 있습니다.", **kwargs)
    else:
        await interaction.response.send_message(
            "서버 관리자만 사용할 수 있습니다.", **kwargs
        )
    return False


class ActivityCog(commands.Cog):
    settings_group = app_commands.Group(
        name="활동설정",
        description="활동 현황 수집 설정",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.store_lock = asyncio.Lock()
        self.guild_locks = defaultdict(asyncio.Lock)
        self.collection_gates = defaultdict(asyncio.Event)

    async def _store_call(self, method, *args, **kwargs):
        async with self.store_lock:
            return await asyncio.to_thread(method, *args, **kwargs)

    @staticmethod
    def _same_guild_resource(resource, resource_type, guild) -> bool:
        resource_guild = getattr(resource, "guild", None)
        return isinstance(resource, resource_type) and (
            getattr(resource_guild, "id", None) == guild.id
        )

    @staticmethod
    async def _complete_ephemeral(
        interaction,
        content: str,
        *,
        attachments: list[discord.File] | None = None,
    ) -> None:
        kwargs = {"content": content}
        if attachments is not None:
            kwargs["attachments"] = attachments
        await interaction.edit_original_response(**kwargs)

    @staticmethod
    def _category_is_accessible(category, guild) -> bool:
        permissions = category.permissions_for(guild.me)
        return bool(getattr(permissions, "view_channel", False))

    @staticmethod
    def _text_channel_is_accessible(channel, guild) -> bool:
        permissions = channel.permissions_for(guild.me)
        return bool(
            getattr(permissions, "view_channel", False)
            and getattr(permissions, "read_message_history", False)
        )

    async def reconcile_member(
        self,
        member,
        effective_at_epoch: int,
        *,
        collection_active: bool | None = None,
    ) -> None:
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        config = await self._store_call(self.store.get_config, guild.id)
        desired_kind = None
        active = (
            self.collection_gates[guild.id].is_set()
            if collection_active is None
            else collection_active
        )
        if active and config.voice_is_complete:
            role_ids = {getattr(role, "id", None) for role in member.roles}
            voice = getattr(member, "voice", None)
            channel = None if voice is None else getattr(voice, "channel", None)
            category_id = None if channel is None else getattr(channel, "category_id", None)
            if not member.bot and config.target_role_id in role_ids:
                if category_id == config.reading_category_id:
                    desired_kind = "reading_room"
                elif category_id == config.study_category_id:
                    desired_kind = "study"
        await self._store_call(
            self.store.reconcile_session,
            guild.id,
            member.id,
            desired_kind,
            effective_at_epoch,
        )

    async def full_reconcile_guild(self, guild, effective_at_epoch: int) -> None:
        """Reconcile one guild while the caller holds its guild lock."""
        if not self.guild_locks[guild.id].locked():
            raise RuntimeError("full_reconcile_guild requires the guild lock")
        config = await self._store_call(self.store.get_config, guild.id)
        role = (
            None
            if config.target_role_id is None
            else guild.get_role(config.target_role_id)
        )
        reading = (
            None
            if config.reading_category_id is None
            else guild.get_channel(config.reading_category_id)
        )
        study = (
            None
            if config.study_category_id is None
            else guild.get_channel(config.study_category_id)
        )
        valid = (
            config.voice_is_complete
            and self._same_guild_resource(role, discord.Role, guild)
            and self._same_guild_resource(reading, discord.CategoryChannel, guild)
            and self._same_guild_resource(study, discord.CategoryChannel, guild)
            and reading.id != study.id
            and self._category_is_accessible(reading, guild)
            and self._category_is_accessible(study, guild)
        )
        if not valid:
            self.collection_gates[guild.id].clear()
            return
        self.collection_gates[guild.id].clear()
        try:
            await self._store_call(
                self.store.open_collection_run, guild.id, effective_at_epoch
            )
            for member in list(guild.members):
                await self.reconcile_member(
                    member,
                    effective_at_epoch,
                    collection_active=True,
                )
        except Exception:
            try:
                await self._store_call(
                    self.store.abort_full_reconcile,
                    guild.id,
                    effective_at_epoch=effective_at_epoch,
                )
            except Exception:
                logger.exception("failed to abort activity full reconcile")
            raise
        self.collection_gates[guild.id].set()

    async def _change_voice_setting(self, guild, now_epoch: int, **change) -> None:
        async with self.guild_locks[guild.id]:
            before = await self._store_call(self.store.get_config, guild.id)
            after = await self._store_call(
                self.store.apply_config_change,
                guild.id,
                effective_at_epoch=now_epoch,
                **change,
            )
            before_core = (
                before.target_role_id,
                before.reading_category_id,
                before.study_category_id,
            )
            after_core = (
                after.target_role_id,
                after.reading_category_id,
                after.study_category_id,
            )
            needs_recovery = (
                after.voice_is_complete
                and not self.collection_gates[guild.id].is_set()
            )
            if before_core != after_core or needs_recovery:
                await self.full_reconcile_guild(guild, now_epoch)

    async def _change_sod_setting(
        self, guild, now_epoch: int, channel_id: int
    ) -> None:
        async with self.guild_locks[guild.id]:
            await self._store_call(
                self.store.apply_config_change,
                guild.id,
                sod_eod_channel_id=channel_id,
                effective_at_epoch=now_epoch,
            )

    async def _validate_distinct_category(
        self, guild, *, field: str, category_id: int
    ) -> bool:
        config = await self._store_call(self.store.get_config, guild.id)
        other_id = (
            config.study_category_id
            if field == "reading_category_id"
            else config.reading_category_id
        )
        return category_id != other_id

    async def _report_command_error(self, interaction) -> None:
        logger.exception("activity settings command failed")
        await self._complete_ephemeral(
            interaction, "활동 설정 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."
        )

    @settings_group.command(name="대상역할", description="수집 대상 역할을 설정합니다.")
    async def set_target_role(
        self, interaction: discord.Interaction, 역할: discord.Role
    ) -> None:
        if not await require_admin(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            if not self._same_guild_resource(역할, discord.Role, guild):
                await self._complete_ephemeral(
                    interaction, "이 서버의 역할만 설정할 수 있습니다."
                )
                return
            await self._change_voice_setting(
                guild, utc_now_epoch(), target_role_id=역할.id
            )
            await self._complete_ephemeral(
                interaction, f"대상 역할을 {역할.id}(으)로 설정했습니다."
            )
        except Exception:
            await self._report_command_error(interaction)

    @settings_group.command(name="독서실", description="독서실 음성 카테고리를 설정합니다.")
    async def set_reading_category(
        self, interaction: discord.Interaction, 카테고리: discord.CategoryChannel
    ) -> None:
        if not await require_admin(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            if not self._same_guild_resource(
                카테고리, discord.CategoryChannel, guild
            ):
                await self._complete_ephemeral(
                    interaction, "이 서버의 카테고리만 설정할 수 있습니다."
                )
                return
            if not self._category_is_accessible(카테고리, guild):
                await self._complete_ephemeral(
                    interaction, "봇이 이 카테고리에 접근할 수 없습니다."
                )
                return
            if not await self._validate_distinct_category(
                guild, field="reading_category_id", category_id=카테고리.id
            ):
                await self._complete_ephemeral(
                    interaction, "독서실과 스터디 카테고리는 서로 달라야 합니다."
                )
                return
            await self._change_voice_setting(
                guild, utc_now_epoch(), reading_category_id=카테고리.id
            )
            await self._complete_ephemeral(
                interaction, f"독서실 카테고리를 {카테고리.id}(으)로 설정했습니다."
            )
        except Exception:
            await self._report_command_error(interaction)

    @settings_group.command(name="스터디", description="스터디 음성 카테고리를 설정합니다.")
    async def set_study_category(
        self, interaction: discord.Interaction, 카테고리: discord.CategoryChannel
    ) -> None:
        if not await require_admin(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            if not self._same_guild_resource(
                카테고리, discord.CategoryChannel, guild
            ):
                await self._complete_ephemeral(
                    interaction, "이 서버의 카테고리만 설정할 수 있습니다."
                )
                return
            if not self._category_is_accessible(카테고리, guild):
                await self._complete_ephemeral(
                    interaction, "봇이 이 카테고리에 접근할 수 없습니다."
                )
                return
            if not await self._validate_distinct_category(
                guild, field="study_category_id", category_id=카테고리.id
            ):
                await self._complete_ephemeral(
                    interaction, "독서실과 스터디 카테고리는 서로 달라야 합니다."
                )
                return
            await self._change_voice_setting(
                guild, utc_now_epoch(), study_category_id=카테고리.id
            )
            await self._complete_ephemeral(
                interaction, f"스터디 카테고리를 {카테고리.id}(으)로 설정했습니다."
            )
        except Exception:
            await self._report_command_error(interaction)

    @settings_group.command(name="sod_eod", description="SoD/EoD 텍스트 채널을 설정합니다.")
    async def set_sod_eod_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        if not await require_admin(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            if not self._same_guild_resource(채널, discord.TextChannel, guild):
                await self._complete_ephemeral(
                    interaction, "이 서버의 텍스트 채널만 설정할 수 있습니다."
                )
                return
            if not self._text_channel_is_accessible(채널, guild):
                await self._complete_ephemeral(
                    interaction, "봇이 이 텍스트 채널의 이력을 읽을 수 없습니다."
                )
                return
            await self._change_sod_setting(guild, utc_now_epoch(), 채널.id)
            await self._complete_ephemeral(
                interaction, f"SoD/EoD 채널을 {채널.id}(으)로 설정했습니다."
            )
        except Exception:
            await self._report_command_error(interaction)

    async def _invalidate_configured_resources(
        self, guild, effective_at_epoch: int
    ) -> tuple[object, list[str]]:
        warnings = []
        async with self.guild_locks[guild.id]:
            config = await self._store_call(self.store.get_config, guild.id)
            checks = (
                (
                    "target_role_id",
                    discord.Role,
                    guild.get_role,
                    "대상 역할을 찾을 수 없습니다.",
                    None,
                    None,
                ),
                (
                    "reading_category_id",
                    discord.CategoryChannel,
                    guild.get_channel,
                    "독서실 카테고리를 찾을 수 없습니다.",
                    self._category_is_accessible,
                    "독서실 카테고리에 접근할 수 없습니다.",
                ),
                (
                    "study_category_id",
                    discord.CategoryChannel,
                    guild.get_channel,
                    "스터디 카테고리를 찾을 수 없습니다.",
                    self._category_is_accessible,
                    "스터디 카테고리에 접근할 수 없습니다.",
                ),
                (
                    "sod_eod_channel_id",
                    discord.TextChannel,
                    guild.get_channel,
                    "SoD/EoD 텍스트 채널을 찾을 수 없습니다.",
                    self._text_channel_is_accessible,
                    "SoD/EoD 텍스트 채널에 접근할 수 없습니다.",
                ),
            )
            for (
                field,
                resource_type,
                resolver,
                missing_warning,
                access_check,
                access_warning,
            ) in checks:
                resource_id = getattr(config, field)
                if resource_id is None:
                    continue
                resource = resolver(resource_id)
                if not self._same_guild_resource(resource, resource_type, guild):
                    warning = missing_warning
                elif access_check is not None and not access_check(resource, guild):
                    warning = access_warning
                else:
                    continue
                if field == "sod_eod_channel_id":
                    config = await self._store_call(
                        self.store.invalidate_sod_eod_channel,
                        guild.id,
                        effective_at_epoch=effective_at_epoch,
                    )
                else:
                    config = await self._store_call(
                        self.store.invalidate_voice_config,
                        guild.id,
                        field=field,
                        effective_at_epoch=effective_at_epoch,
                    )
                    self.collection_gates[guild.id].clear()
                warnings.append(warning)
        return config, warnings

    @settings_group.command(name="상태", description="활동 수집 설정 상태를 표시합니다.")
    async def activity_status(self, interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            config, warnings = await self._invalidate_configured_resources(
                guild, utc_now_epoch()
            )
            runs = await self._store_call(self.store.list_runs, guild.id)
            periods = await self._store_call(
                self.store.list_channel_periods, guild.id
            )
            open_sessions = await self._store_call(
                self.store.count_open_sessions, guild.id
            )
            lines = [
                f"대상 역할 ID: {config.target_role_id or '미설정'}",
                f"독서실 카테고리 ID: {config.reading_category_id or '미설정'}",
                f"스터디 카테고리 ID: {config.study_category_id or '미설정'}",
                f"SoD/EoD 채널 ID: {config.sod_eod_channel_id or '미설정'}",
                f"열린 음성 세션: {open_sessions}",
                f"열린 수집 run: {sum(ended is None for _, _, ended, _ in runs)}",
            ]
            if runs:
                lines.append("음성 수집 run 이력:")
                lines.extend(
                    "- "
                    f"시작={started}, checkpoint={checkpoint}, "
                    f"종료={ended if ended is not None else '진행 중'}, "
                    f"사유={reason or '-'}"
                    for started, checkpoint, ended, reason in runs
                )
            if periods:
                lines.append("SoD/EoD 채널 기간 및 동기화:")
                for channel_id, started, ended in periods:
                    state = await self._store_call(
                        self.store.get_sync_state, guild.id, channel_id
                    )
                    history_from = (
                        None if state is None else state.history_from_epoch
                    )
                    completed = None if state is None else state.completed_epoch
                    lines.append(
                        "- "
                        f"채널={channel_id}, 시작={started}, "
                        f"종료={ended if ended is not None else '진행 중'}, "
                        f"history_from={history_from if history_from is not None else '-'}, "
                        f"completed={completed if completed is not None else '-'}"
                    )
            if warnings:
                lines.append("경고:")
                lines.extend(f"- {warning}" for warning in warnings)
            detail = "\n".join(lines)
            if len(detail) <= 2000:
                await self._complete_ephemeral(interaction, detail)
            else:
                summary = "\n".join(
                    lines[:6]
                    + ["상세 상태가 길어 전체 내용을 TXT 파일로 첨부했습니다."]
                )
                attachment = discord.File(
                    io.BytesIO(detail.encode("utf-8")),
                    filename=f"activity-status-{guild.id}.txt",
                )
                await self._complete_ephemeral(
                    interaction,
                    summary,
                    attachments=[attachment],
                )
        except Exception:
            await self._report_command_error(interaction)


async def setup(bot):
    store = ActivityStore(os.getenv("ACTIVITY_DB_PATH", "/data/activity.db"))
    await asyncio.to_thread(store.initialize)
    await bot.add_cog(ActivityCog(bot, store))
