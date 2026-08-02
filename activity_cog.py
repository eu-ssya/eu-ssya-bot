import asyncio
import io
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

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
        self.dirty_guilds = set()
        self._collection_generations = defaultdict(int)
        self._disconnect_epochs = {}
        self._startup_recovered_guild_ids = set()
        self._lifecycle_tasks = set()
        self._store_worker_tasks = set()
        self.recovery_task = None
        self.checkpoint_task = None

    async def _store_call(self, method, *args, **kwargs):
        async with self.store_lock:
            worker = asyncio.create_task(
                asyncio.to_thread(method, *args, **kwargs)
            )
            self._store_worker_tasks.add(worker)
            worker.add_done_callback(self._store_worker_tasks.discard)
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError as cancellation:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                try:
                    worker.result()
                except BaseException:
                    pass
                raise cancellation

    def _track_lifecycle_task(self, task):
        self._lifecycle_tasks.add(task)
        task.add_done_callback(self._lifecycle_tasks.discard)
        return task

    def _suspend_collection(self, guilds) -> dict[int, int]:
        generations = {}
        for guild in guilds:
            guild_id = guild.id
            self._collection_generations[guild_id] += 1
            generations[guild_id] = self._collection_generations[guild_id]
            self.collection_gates[guild_id].clear()
            self.dirty_guilds.add(guild_id)
        return generations

    async def _recover_suspended_guild_locked(
        self,
        guild,
        effective_at_epoch: int,
        close_reason: str,
        generation: int,
    ) -> bool:
        if self._collection_generations[guild.id] != generation:
            return False
        snapshot = await self._store_call(
            self.store.snapshot_open_row_ids,
            guild.id,
        )
        await self._store_call(
            self.store.close_snapshot_rows_at_checkpoint,
            snapshot,
            close_reason,
        )
        await self._full_reconcile_guild_locked(
            guild,
            effective_at_epoch,
            expected_generation=generation,
        )
        return self._collection_generations[guild.id] == generation

    async def recover_after_ready(self) -> None:
        guilds = [
            guild
            for guild in list(self.bot.guilds)
            if guild.id not in self._startup_recovered_guild_ids
        ]
        generations = self._suspend_collection(guilds)
        await self.bot.wait_until_ready()
        effective_at_epoch = utc_now_epoch()
        for guild in list(self.bot.guilds):
            if guild.id in self._startup_recovered_guild_ids:
                continue
            if guild.id not in generations:
                generations.update(self._suspend_collection((guild,)))
            try:
                async with self.guild_locks[guild.id]:
                    if guild.id in self._startup_recovered_guild_ids:
                        continue
                    close_reason = (
                        "gateway_disconnect"
                        if guild.id in self._disconnect_epochs
                        else "restart_checkpoint"
                    )
                    recovered = await self._recover_suspended_guild_locked(
                        guild,
                        effective_at_epoch,
                        close_reason,
                        generations[guild.id],
                    )
                    if recovered:
                        self._startup_recovered_guild_ids.add(guild.id)
                        self._disconnect_epochs.pop(guild.id, None)
            except Exception:
                logger.exception(
                    "activity startup recovery failed for guild %s",
                    guild.id,
                )

    @tasks.loop(seconds=60.0)
    async def checkpoint_loop(self) -> None:
        await self._checkpoint_open_rows_once()

    async def _checkpoint_open_rows_once(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                async with self.guild_locks[guild.id]:
                    if not self.collection_gates[guild.id].is_set():
                        continue
                    checkpoint_epoch = utc_now_epoch()
                    await self._store_call(
                        self.store.checkpoint_open_rows,
                        guild.id,
                        checkpoint_epoch,
                    )
            except Exception:
                logger.exception(
                    "activity checkpoint failed for guild %s",
                    guild.id,
                )

    async def cog_load(self) -> None:
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = self._track_lifecycle_task(
                asyncio.create_task(self.recover_after_ready())
            )
        if not self.checkpoint_loop.is_running():
            self.checkpoint_task = self._track_lifecycle_task(
                self.checkpoint_loop.start()
            )

    async def cog_unload(self) -> None:
        tasks_to_wait = set(self._lifecycle_tasks)
        if self.checkpoint_loop.is_running():
            self.checkpoint_loop.cancel()
        if self.recovery_task is not None and not self.recovery_task.done():
            self.recovery_task.cancel()
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
        self._lifecycle_tasks.difference_update(tasks_to_wait)

        guilds = list(self.bot.guilds)
        self._suspend_collection(guilds)
        effective_at_epoch = utc_now_epoch()
        for guild in guilds:
            try:
                async with self.guild_locks[guild.id]:
                    await self._store_call(
                        self.store.close_open_rows,
                        guild.id,
                        effective_at_epoch,
                        "graceful_shutdown",
                    )
            except Exception:
                logger.exception(
                    "activity graceful cleanup failed for guild %s",
                    guild.id,
                )

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

    @staticmethod
    def desired_kind_for_member(member, config) -> str | None:
        role_ids = {getattr(role, "id", None) for role in member.roles}
        if member.bot or config.target_role_id not in role_ids:
            return None
        voice = getattr(member, "voice", None)
        channel = None if voice is None else getattr(voice, "channel", None)
        category_id = None if channel is None else getattr(channel, "category_id", None)
        if category_id == config.reading_category_id:
            return "reading_room"
        if category_id == config.study_category_id:
            return "study"
        return None

    async def reconcile_member(
        self,
        member,
        effective_at_epoch: int,
        close_reason: str = "reconciled",
        *,
        allow_closed_gate: bool = False,
    ) -> None:
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        if (
            not allow_closed_gate
            and not self.collection_gates[guild.id].is_set()
        ):
            self.dirty_guilds.add(guild.id)
            return
        config = await self._store_call(self.store.get_config, guild.id)
        desired_kind = (
            self.desired_kind_for_member(member, config)
            if config.voice_is_complete
            else None
        )
        await self._store_call(
            self.store.reconcile_session,
            guild.id,
            member.id,
            desired_kind,
            effective_at_epoch,
            close_reason,
        )

    async def full_reconcile_guild(self, guild, effective_at_epoch: int) -> None:
        """Serialize and fully reconcile one guild."""
        async with self.guild_locks[guild.id]:
            await self._full_reconcile_guild_locked(guild, effective_at_epoch)

    async def _full_reconcile_guild_locked(
        self,
        guild,
        effective_at_epoch: int,
        *,
        invalidations: list[tuple[str, str]] | None = None,
        expected_generation: int | None = None,
    ) -> tuple[object, list[str]]:
        """Fully reconcile while the caller owns this guild's lock."""
        gate = self.collection_gates[guild.id]
        gate.clear()
        try:
            warnings = []
            if invalidations:
                config, applied_warnings = await self._apply_resource_invalidations_locked(
                    guild,
                    effective_at_epoch,
                    invalidations,
                )
                warnings.extend(applied_warnings)
            config, discovered_warnings = (
                await self._invalidate_configured_resources_locked(
                    guild, effective_at_epoch
                )
            )
            warnings.extend(discovered_warnings)
            if not config.voice_is_complete:
                await self._store_call(
                    self.store.abort_full_reconcile,
                    guild.id,
                    effective_at_epoch=effective_at_epoch,
                )
                self.dirty_guilds.discard(guild.id)
                return config, warnings
            await self._store_call(
                self.store.open_collection_run, guild.id, effective_at_epoch
            )
            members = list(guild.members)
            current_member_ids = {member.id for member in members}
            for member in members:
                await self.reconcile_member(
                    member,
                    effective_at_epoch,
                    allow_closed_gate=True,
                )
            open_user_ids = await self._store_call(
                self.store.list_open_session_user_ids, guild.id
            )
            for user_id in open_user_ids:
                if user_id in current_member_ids:
                    continue
                await self._store_call(
                    self.store.reconcile_session,
                    guild.id,
                    user_id,
                    None,
                    effective_at_epoch,
                    "reconciled",
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
        if (
            expected_generation is not None
            and self._collection_generations[guild.id] != expected_generation
        ):
            await self._store_call(
                self.store.abort_full_reconcile,
                guild.id,
                effective_at_epoch=effective_at_epoch,
            )
            self.dirty_guilds.add(guild.id)
            return config, warnings
        self.dirty_guilds.discard(guild.id)
        gate.set()
        return config, warnings

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
                await self._full_reconcile_guild_locked(guild, now_epoch)

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

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        effective_at_epoch = utc_now_epoch()
        guild = member.guild
        async with self.guild_locks[guild.id]:
            await self.reconcile_member(member, effective_at_epoch)

    @commands.Cog.listener()
    async def on_member_update(self, before, after) -> None:
        effective_at_epoch = utc_now_epoch()
        guild = after.guild
        async with self.guild_locks[guild.id]:
            if after.id == getattr(guild.me, "id", None):
                await self._revalidate_configured_resources_locked(
                    guild, effective_at_epoch
                )
                return
            config = await self._store_call(self.store.get_config, guild.id)
            target_role_id = config.target_role_id
            if target_role_id is None:
                return
            before_has_role = target_role_id in {
                getattr(role, "id", None) for role in before.roles
            }
            after_has_role = target_role_id in {
                getattr(role, "id", None) for role in after.roles
            }
            if before_has_role == after_has_role:
                return
            close_reason = (
                "role_removed"
                if before_has_role and not after_has_role
                else "reconciled"
            )
            await self.reconcile_member(
                after,
                effective_at_epoch,
                close_reason=close_reason,
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        effective_at_epoch = utc_now_epoch()
        guild = member.guild
        async with self.guild_locks[guild.id]:
            if not self.collection_gates[guild.id].is_set():
                self.dirty_guilds.add(guild.id)
                return
            await self._store_call(
                self.store.reconcile_session,
                guild.id,
                member.id,
                None,
                effective_at_epoch,
                "reconciled",
            )

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        observed_epoch = utc_now_epoch()
        guilds = list(self.bot.guilds)
        self._suspend_collection(guilds)
        for guild in guilds:
            self._disconnect_epochs.setdefault(
                guild.id,
                observed_epoch,
            )
        for guild in guilds:
            disconnect_epoch = self._disconnect_epochs[guild.id]
            try:
                async with self.guild_locks[guild.id]:
                    await self._store_call(
                        self.store.close_open_rows,
                        guild.id,
                        disconnect_epoch,
                        "gateway_disconnect",
                    )
            except Exception:
                logger.exception(
                    "activity disconnect close failed for guild %s",
                    guild.id,
                )

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        guilds = list(self.bot.guilds)
        generations = self._suspend_collection(guilds)
        effective_at_epoch = utc_now_epoch()
        for guild in guilds:
            try:
                async with self.guild_locks[guild.id]:
                    close_reason = (
                        "gateway_disconnect"
                        if guild.id in self._disconnect_epochs
                        else "restart_checkpoint"
                    )
                    recovered = await self._recover_suspended_guild_locked(
                        guild,
                        effective_at_epoch,
                        close_reason,
                        generations[guild.id],
                    )
                    if recovered:
                        self._disconnect_epochs.pop(guild.id, None)
            except Exception:
                logger.exception(
                    "activity resume recovery failed for guild %s",
                    guild.id,
                )

    @commands.Cog.listener()
    async def on_guild_available(self, guild) -> None:
        had_pending_disconnect = guild.id in self._disconnect_epochs
        generation = self._suspend_collection((guild,))[guild.id]
        effective_at_epoch = utc_now_epoch()
        try:
            async with self.guild_locks[guild.id]:
                if self._collection_generations[guild.id] != generation:
                    return
                if had_pending_disconnect:
                    recovered = await self._recover_suspended_guild_locked(
                        guild,
                        effective_at_epoch,
                        "gateway_disconnect",
                        generation,
                    )
                else:
                    await self._full_reconcile_guild_locked(
                        guild,
                        effective_at_epoch,
                        expected_generation=generation,
                    )
                    recovered = (
                        self._collection_generations[guild.id] == generation
                    )
                if recovered and had_pending_disconnect:
                    self._disconnect_epochs.pop(guild.id, None)
        except Exception:
            logger.exception(
                "activity guild-available recovery failed for guild %s",
                guild.id,
            )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role) -> None:
        effective_at_epoch = utc_now_epoch()
        guild = role.guild
        async with self.guild_locks[guild.id]:
            config = await self._store_call(self.store.get_config, guild.id)
            if config.target_role_id != role.id:
                await self._revalidate_configured_resources_locked(
                    guild, effective_at_epoch
                )
                return
            await self._full_reconcile_guild_locked(
                guild,
                effective_at_epoch,
                invalidations=[
                    ("target_role_id", "대상 역할을 찾을 수 없습니다.")
                ],
            )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after) -> None:
        effective_at_epoch = utc_now_epoch()
        guild = after.guild
        bot_role_ids = {
            getattr(role, "id", None)
            for role in getattr(guild.me, "roles", ())
        }
        if after.id not in bot_role_ids:
            return
        async with self.guild_locks[guild.id]:
            await self._revalidate_configured_resources_locked(
                guild, effective_at_epoch
            )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel) -> None:
        effective_at_epoch = utc_now_epoch()
        guild = channel.guild
        async with self.guild_locks[guild.id]:
            config = await self._store_call(self.store.get_config, guild.id)
            invalidations = []
            for field in ("reading_category_id", "study_category_id"):
                if getattr(config, field) == channel.id:
                    label = "독서실" if field == "reading_category_id" else "스터디"
                    invalidations.append(
                        (field, f"{label} 카테고리를 찾을 수 없습니다.")
                    )
            if config.sod_eod_channel_id == channel.id:
                invalidations.append(
                    (
                        "sod_eod_channel_id",
                        "SoD/EoD 텍스트 채널을 찾을 수 없습니다.",
                    )
                )
            if any(field != "sod_eod_channel_id" for field, _ in invalidations):
                await self._full_reconcile_guild_locked(
                    guild,
                    effective_at_epoch,
                    invalidations=invalidations,
                )
            elif invalidations:
                await self._apply_resource_invalidations_locked(
                    guild, effective_at_epoch, invalidations
                )

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after) -> None:
        effective_at_epoch = utc_now_epoch()
        guild = after.guild
        async with self.guild_locks[guild.id]:
            config = await self._store_call(self.store.get_config, guild.id)
            invalidations = []
            for field in ("reading_category_id", "study_category_id"):
                if getattr(config, field) != after.id:
                    continue
                valid_category = self._same_guild_resource(
                    after, discord.CategoryChannel, guild
                ) and self._category_is_accessible(after, guild)
                if not valid_category:
                    label = "독서실" if field == "reading_category_id" else "스터디"
                    invalidations.append(
                        (field, f"{label} 카테고리에 접근할 수 없습니다.")
                    )
            if config.sod_eod_channel_id == after.id:
                valid_text_channel = self._same_guild_resource(
                    after, discord.TextChannel, guild
                ) and self._text_channel_is_accessible(after, guild)
                if not valid_text_channel:
                    invalidations.append(
                        (
                            "sod_eod_channel_id",
                            "SoD/EoD 텍스트 채널에 접근할 수 없습니다.",
                        )
                    )
            if any(field != "sod_eod_channel_id" for field, _ in invalidations):
                await self._full_reconcile_guild_locked(
                    guild,
                    effective_at_epoch,
                    invalidations=invalidations,
                )
            elif invalidations:
                await self._apply_resource_invalidations_locked(
                    guild, effective_at_epoch, invalidations
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
        async with self.guild_locks[guild.id]:
            return await self._revalidate_configured_resources_locked(
                guild, effective_at_epoch
            )

    async def _configured_resource_invalidations_locked(
        self, guild, effective_at_epoch: int
    ) -> tuple[object, list[tuple[str, str]]]:
        config = await self._store_call(self.store.get_config, guild.id)
        invalidations = []
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
            invalidations.append((field, warning))
        return config, invalidations

    async def _apply_resource_invalidations_locked(
        self,
        guild,
        effective_at_epoch: int,
        invalidations: list[tuple[str, str]],
    ) -> tuple[object, list[str]]:
        config = await self._store_call(self.store.get_config, guild.id)
        warnings = []
        for field, warning in invalidations:
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
            warnings.append(warning)
        return config, warnings

    async def _invalidate_configured_resources_locked(
        self, guild, effective_at_epoch: int
    ) -> tuple[object, list[str]]:
        """Apply invalid resources while the full-reconcile gate is owned."""
        config, invalidations = await self._configured_resource_invalidations_locked(
            guild, effective_at_epoch
        )
        if not invalidations:
            return config, []
        return await self._apply_resource_invalidations_locked(
            guild, effective_at_epoch, invalidations
        )

    async def _revalidate_configured_resources_locked(
        self, guild, effective_at_epoch: int
    ) -> tuple[object, list[str]]:
        config, invalidations = await self._configured_resource_invalidations_locked(
            guild, effective_at_epoch
        )
        if not invalidations:
            return config, []
        if any(field != "sod_eod_channel_id" for field, _ in invalidations):
            return await self._full_reconcile_guild_locked(
                guild,
                effective_at_epoch,
                invalidations=invalidations,
            )
        return await self._apply_resource_invalidations_locked(
            guild, effective_at_epoch, invalidations
        )

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
