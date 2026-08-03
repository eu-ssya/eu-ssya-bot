import asyncio
import dataclasses
import io
import logging
import os
import re
import sqlite3
import unicodedata
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from activity_store import (
    ActivityReport,
    ActivityStore,
    ChannelChanged,
    CoverageWarning,
    KST,
    ReportMember,
    ReportRow,
    kst_range_to_epoch,
)


logger = logging.getLogger(__name__)


SOD_EOD_PATTERN = re.compile(r"(?<![a-z0-9])(sod|eod)(?![a-z0-9])")
AUTHOR_ELIGIBILITY_CACHE_LIMIT = 64
REPORT_PAGE_SIZE = 15
REPORT_CONTENT_LIMIT = 2000
REPORT_VIEW_TIMEOUT_SECONDS = 600
REPORT_WARNING_SUMMARY_LIMIT = 120
TEXT_SYNC_RETRY_DELAY_SECONDS = 30.0
_TEXT_RECOVERY_PREPARE_FAILED = object()

_TABLE_COLUMNS = (
    ("순위", 4, "right"),
    ("이름", 14, "left"),
    ("최근 활동", 16, "left"),
    ("독서실", 16, "right"),
    ("스터디", 16, "right"),
    ("SoD", 3, "right"),
    ("EoD", 3, "right"),
    ("활동일", 6, "right"),
)


def detect_sod_eod(content: str) -> set[str]:
    return {match.group(1) for match in SOD_EOD_PATTERN.finditer(content.casefold())}


@dataclass(frozen=True)
class BackfillResult:
    processed_count: int
    event_count: int


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


def _strict_iso_date(value: str) -> date:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("날짜는 유효한 YYYY-MM-DD 형식이어야 합니다.") from error


def _is_zero_width_extension(character: str) -> bool:
    codepoint = ord(character)
    return bool(
        character == "\u200d"
        or unicodedata.combining(character)
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
    )


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _terminal_clusters(value: str) -> list[str]:
    clusters: list[str] = []
    for character in value:
        if (
            _is_regional_indicator(character)
            and clusters
            and len(clusters[-1]) == 1
            and _is_regional_indicator(clusters[-1])
        ):
            clusters[-1] += character
        elif (
            not clusters
            or (
                not _is_zero_width_extension(character)
                and not clusters[-1].endswith("\u200d")
            )
        ):
            clusters.append(character)
        else:
            clusters[-1] += character
    return clusters


def _cluster_cell_width(cluster: str) -> int:
    for character in cluster:
        if _is_zero_width_extension(character):
            continue
        codepoint = ord(character)
        if (
            unicodedata.east_asian_width(character) in {"W", "F"}
            or 0x2600 <= codepoint <= 0x27BF
            or 0x1F000 <= codepoint <= 0x1FAFF
        ):
            return 2
        return 1
    return 0


def _terminal_cell_width(value: str) -> int:
    return sum(_cluster_cell_width(cluster) for cluster in _terminal_clusters(value))


def _fit_terminal_cell(
    value: str,
    width: int,
    *,
    align: str = "left",
) -> str:
    if align not in {"left", "right"}:
        raise ValueError("align must be 'left' or 'right'")
    if width < 3:
        raise ValueError("terminal cell width must be at least 3")

    normalized = " ".join(str(value).split())
    clusters = _terminal_clusters(normalized)
    rendered = normalized
    rendered_width = _terminal_cell_width(rendered)
    if rendered_width > width:
        kept: list[str] = []
        kept_width = 0
        for cluster in clusters:
            cluster_width = _cluster_cell_width(cluster)
            if kept_width + cluster_width + 3 > width:
                break
            kept.append(cluster)
            kept_width += cluster_width
        rendered = "".join(kept) + "..."
        rendered_width = kept_width + 3

    padding = " " * (width - rendered_width)
    return padding + rendered if align == "right" else rendered + padding


_WARNING_EPOCH_RANGE = re.compile(r"(?P<start>\d+)~(?P<end>\d+) UTC epoch")
_WARNING_EPOCH_POINT = re.compile(r"(?P<epoch>\d+) UTC epoch")


def _format_kst_minute(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, KST).strftime("%Y-%m-%d %H:%M KST")


def _format_recent_activity(epoch: int | None) -> str:
    if epoch is None:
        return "없음"
    try:
        return datetime.fromtimestamp(epoch, KST).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "표시 불가"


def _format_duration(seconds: int) -> str:
    total_minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}분"
    return f"{hours}시간 {minutes}분"


def _humanize_warning(warning: CoverageWarning) -> str:
    if warning.code == "sod_history_unavailable":
        return "SoD/EoD 이력 시작점을 확인할 수 없어 이 조회의 값은 부분 데이터입니다."
    if warning.code == "sod_history_partial":
        return "SoD/EoD 이력 일부에 접근할 수 없어 이 조회의 값은 부분 데이터입니다."

    text = " ".join(str(warning.text).split())
    text = _WARNING_EPOCH_RANGE.sub(
        lambda match: (
            f"{_format_kst_minute(int(match.group('start')))} ~ "
            f"{_format_kst_minute(int(match.group('end')))}"
        ),
        text,
    )
    text = _WARNING_EPOCH_POINT.sub(
        lambda match: _format_kst_minute(int(match.group("epoch"))),
        text,
    )
    if warning.code == "voice_gap":
        text = text.replace("음성 수집 누락 구간", "음성 수집 공백")
    return text


def _format_timestamp(epoch: int | None, target_timezone) -> str:
    if epoch is None:
        return "기록 없음"
    try:
        return datetime.fromtimestamp(epoch, target_timezone).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    except (OverflowError, OSError, ValueError):
        return f"UTC epoch {epoch}"


def _warning_summary_lines(
    report: ActivityReport,
    *,
    character_limit: int,
) -> list[str]:
    if character_limit <= 0:
        return []
    if not report.warnings:
        return ["경고: 없음"] if len("경고: 없음") <= character_limit else []
    warning_count = len(report.warnings)
    warning_kind_count = len({warning.code for warning in report.warnings})
    lines = [f"경고: 총 {warning_count}건/{warning_kind_count}종"]
    if len(lines[0]) > character_limit:
        compact = f"경고: 총 {warning_count}건 · 상세는 전체 TXT 참조."
        return [compact] if len(compact) <= character_limit else []
    used = len(lines[0])
    for index, warning in enumerate(report.warnings):
        line = f"- {_humanize_warning(warning)}"
        remaining = len(report.warnings) - index
        omitted = f"- 나머지 {remaining}건 상세는 전체 TXT 참조."
        reserve = 1 + len(omitted) if remaining else 0
        if used + 1 + len(line) + reserve > character_limit:
            if used + 1 + len(omitted) <= character_limit:
                lines.append(omitted)
                return lines
            compact = f"경고: 총 {warning_count}건 · 상세는 전체 TXT 참조."
            return [compact] if len(compact) <= character_limit else []
        lines.append(line)
        used += 1 + len(line)
    return lines


def _txt_warning_summary_lines(report: ActivityReport) -> list[str]:
    if not report.warnings:
        return ["경고: 없음"]
    warning_count = len(report.warnings)
    warning_kind_count = len({warning.code for warning in report.warnings})
    lines = [f"경고: 총 {warning_count}건/{warning_kind_count}종"]
    used = len(lines[0])
    for index, warning in enumerate(report.warnings):
        text = " ".join(str(warning.text).split())
        line = f"- {text}"
        remaining = len(report.warnings) - index
        omitted = f"- 나머지 {remaining}건 상세는 전체 TXT 참조."
        reserve = 1 + len(omitted) if remaining else 0
        if used + 1 + len(line) + reserve > REPORT_WARNING_SUMMARY_LIMIT:
            lines.append(omitted)
            break
        lines.append(line)
        used += 1 + len(line)
    return lines


def _table_line(values: tuple[str, ...]) -> str:
    return " ".join(
        _fit_terminal_cell(value, width, align=align)
        for value, (_label, width, align) in zip(values, _TABLE_COLUMNS)
    )


def _report_table_lines(
    rows: list[ReportRow],
    *,
    start_rank: int,
) -> list[str]:
    lines = [_table_line(tuple(label for label, _width, _align in _TABLE_COLUMNS))]
    for offset, row in enumerate(rows):
        name = " ".join(str(row.display_name).split()) or "이름 없음"
        lines.append(
            _table_line(
                (
                    str(start_rank + offset),
                    name,
                    _format_recent_activity(row.last_activity_epoch),
                    _format_duration(row.reading_seconds),
                    _format_duration(row.study_seconds),
                    str(row.sod_days),
                    str(row.eod_days),
                    str(row.combined_days),
                )
            )
        )
    return lines


def format_report_page(report: ActivityReport, page_index: int) -> str:
    page_count = max(1, report.page_count)
    page_index = min(max(0, page_index), page_count - 1)
    start = page_index * REPORT_PAGE_SIZE
    rows = report.rows[start : start + REPORT_PAGE_SIZE]
    table_lines = _report_table_lines(rows, start_rank=start + 1)
    if not rows:
        table_lines.append("표시할 대상 멤버가 없습니다.")

    fixed_lines = [
        (
            f"활동 현황 · {report.start_date} ~ {report.end_date} "
            f"· {page_index + 1}/{page_count}"
        ),
        "정렬: 활동일 ↓ · 음성시간 ↓ · 최근활동 ↓",
        "```text",
        *table_lines,
        "```",
    ]
    fixed_content = "\n".join(fixed_lines)
    warning_budget = REPORT_CONTENT_LIMIT - len(fixed_content) - 1
    warning_lines = _warning_summary_lines(
        report,
        character_limit=min(REPORT_WARNING_SUMMARY_LIMIT, warning_budget),
    )
    content = "\n".join([*fixed_lines, *warning_lines])
    if len(content) > REPORT_CONTENT_LIMIT:
        raise ValueError("activity report page budget exceeded")
    return content


def build_report_txt(report: ActivityReport) -> str:
    generated = _format_recent_activity(report.generated_epoch)
    lines = [
        "활동 현황 보고서",
        report.period_label,
        f"생성: {generated} KST",
        "정렬: 활동일 ↓ · 음성시간 ↓ · 최근활동 ↓",
        "",
        *_report_table_lines(report.rows, start_rank=1),
    ]
    if not report.rows:
        lines.append("표시할 대상 멤버가 없습니다.")

    lines.extend(["", "전체 coverage 경고:"])
    if report.warnings:
        lines.extend(
            f"- [{warning.code}] {_humanize_warning(warning)}"
            for warning in report.warnings
        )
    else:
        lines.append("- 없음")
    return "\n".join(lines) + "\n"


class ActivityReportView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        report: ActivityReport,
        original_response_editor,
        *,
        guild_id: int | None = None,
    ) -> None:
        super().__init__(timeout=REPORT_VIEW_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.report = report
        self.page_index = 0
        self.original_response_editor = original_response_editor
        self.last_authorized_interaction = None
        self._sync_page_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        accepted = bool(
            interaction.guild is not None
            and (self.guild_id is None or interaction.guild.id == self.guild_id)
            and interaction.user.id == self.owner_id
            and permissions
            and permissions.administrator
        )
        if not accepted:
            message = "이 보고서를 실행한 현재 서버 관리자만 사용할 수 있습니다."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False
        if self.guild_id is None:
            self.guild_id = interaction.guild.id
        self.last_authorized_interaction = interaction
        self.original_response_editor = interaction.edit_original_response
        return True

    async def _move_page(self, interaction: discord.Interaction, delta: int) -> None:
        last_page = max(0, self.report.page_count - 1)
        self.page_index = min(max(0, self.page_index + delta), last_page)
        self._sync_page_buttons()
        await interaction.response.edit_message(
            content=format_report_page(self.report, self.page_index),
            view=self,
        )

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._move_page(interaction, -1)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._move_page(interaction, 1)

    @discord.ui.button(label="전체 TXT", style=discord.ButtonStyle.primary)
    async def full_txt_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(thinking=False)
        data = io.BytesIO(build_report_txt(self.report).encode("utf-8"))
        attachment = discord.File(data, filename=self.report.txt_filename)
        await interaction.followup.send(file=attachment, ephemeral=True)

    def _sync_page_buttons(self) -> None:
        last_page = max(0, self.report.page_count - 1)
        self.page_index = min(max(0, self.page_index), last_page)
        self.previous_page.disabled = self.page_index == 0
        self.next_page.disabled = self.page_index == last_page

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            await self.original_response_editor(view=self)
        except discord.DiscordException:
            logger.warning("activity report timeout edit failed", exc_info=True)


class ActivityCog(commands.Cog):
    settings_group = app_commands.Group(
        name="활동설정",
        description="활동 현황 수집 설정",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )
    report_group = app_commands.Group(
        name="활동현황",
        description="활동 현황을 조회합니다.",
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
        self._guild_unavailable_epochs = {}
        self._guild_unavailable_generations = {}
        self._guild_available_recovery_requests = set()
        self._startup_recovered_guild_ids = set()
        self._lifecycle_tasks = set()
        self._store_worker_tasks = set()
        self._text_sync_retry_tasks = {}
        self._text_recovery_pending_guild_ids = set()
        self.text_sync_retry_delay_seconds = TEXT_SYNC_RETRY_DELAY_SECONDS
        self._unloading = False
        self.recovery_task = None
        self.checkpoint_task = None

    async def _send_report(self, interaction, period_factory) -> None:
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        generated_epoch = utc_now_epoch()
        terminal_kwargs = None
        try:
            start_date, end_date = period_factory(generated_epoch)
            if start_date > end_date:
                raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
            guild = interaction.guild
            config, invalid_warnings = await self._invalidate_configured_resources(
                guild,
                generated_epoch,
            )
            missing = [
                label
                for value, label in (
                    (config.target_role_id, "대상 역할"),
                    (config.reading_category_id, "독서실 카테고리"),
                    (config.study_category_id, "스터디 카테고리"),
                    (config.sod_eod_channel_id, "SoD/EoD 채널"),
                )
                if value is None
            ]
            if missing:
                detail = " ".join(invalid_warnings)
                message = "활동 보고서 설정이 완전하지 않습니다: " + ", ".join(missing)
                if detail:
                    message += f". {detail}"
                terminal_kwargs = {"content": message}
            else:
                start_epoch, end_epoch = kst_range_to_epoch(start_date, end_date)
                members = [
                    ReportMember(member.id, member.display_name)
                    for member in guild.members
                    if self._member_has_target_role(member, config)
                ]
                report = await self._store_call(
                    self.store.build_report,
                    guild_id=guild.id,
                    members=members,
                    start_epoch=start_epoch,
                    end_epoch=end_epoch,
                    as_of_epoch=generated_epoch,
                )
                if (
                    guild.id in self._text_recovery_pending_guild_ids
                    and "sod_backfill_incomplete"
                    not in {warning.code for warning in report.warnings}
                ):
                    report = dataclasses.replace(
                        report,
                        warnings=[
                            *report.warnings,
                            CoverageWarning(
                                code="sod_backfill_incomplete",
                                text=(
                                    "SoD/EoD 자동 동기화 준비가 완료되지 않았습니다; "
                                    "이 조회의 값은 부분 데이터입니다."
                                ),
                            ),
                        ],
                    )
                report = dataclasses.replace(
                    report,
                    generated_epoch=generated_epoch,
                )
                view = ActivityReportView(
                    owner_id=interaction.user.id,
                    guild_id=guild.id,
                    report=report,
                    original_response_editor=interaction.edit_original_response,
                )
                terminal_kwargs = {
                    "content": format_report_page(report, 0),
                    "view": view,
                }
        except ValueError as error:
            terminal_kwargs = {"content": str(error)}
        except Exception:
            logger.exception("activity report build failed")
            terminal_kwargs = {
                "content": (
                    "활동 현황을 만들지 못했습니다. "
                    "설정과 로그를 확인해 주세요."
                )
            }

        try:
            await interaction.edit_original_response(**terminal_kwargs)
        except discord.DiscordException:
            logger.exception("activity report terminal edit failed")

    @report_group.command(name="최근", description="오늘을 포함한 최근 N일을 조회합니다.")
    async def recent_report(
        self,
        interaction: discord.Interaction,
        일수: app_commands.Range[int, 1],
    ) -> None:
        def period(generated_epoch: int) -> tuple[date, date]:
            if 일수 < 1:
                raise ValueError("일수는 1 이상이어야 합니다.")
            today = datetime.fromtimestamp(generated_epoch, KST).date()
            return today - timedelta(days=일수 - 1), today

        await self._send_report(interaction, period)

    @report_group.command(name="기간", description="KST 날짜 범위를 조회합니다.")
    async def period_report(
        self,
        interaction: discord.Interaction,
        시작일: str,
        종료일: str,
    ) -> None:
        def period(_generated_epoch: int) -> tuple[date, date]:
            return _strict_iso_date(시작일), _strict_iso_date(종료일)

        await self._send_report(interaction, period)

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

    async def _wait_for_text_sync_retry(self) -> None:
        await asyncio.sleep(self.text_sync_retry_delay_seconds)

    def _schedule_text_sync_retry(self, guild):
        if self._unloading:
            return None
        existing = self._text_sync_retry_tasks.get(guild.id)
        if existing is not None and not existing.done():
            return existing
        task = self._track_lifecycle_task(
            asyncio.create_task(self._text_sync_retry_worker(guild))
        )
        self._text_sync_retry_tasks[guild.id] = task

        def discard_retry(completed_task):
            if self._text_sync_retry_tasks.get(guild.id) is completed_task:
                self._text_sync_retry_tasks.pop(guild.id, None)

        task.add_done_callback(discard_retry)
        return task

    async def _complete_text_recovery_success(self, guild_id: int) -> None:
        self._text_recovery_pending_guild_ids.discard(guild_id)
        retry_task = self._text_sync_retry_tasks.get(guild_id)
        if retry_task is None:
            return
        if self._text_sync_retry_tasks.get(guild_id) is retry_task:
            self._text_sync_retry_tasks.pop(guild_id, None)
        if retry_task is asyncio.current_task():
            return
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)

    async def _text_sync_retry_worker(self, guild) -> None:
        while not self._unloading:
            await self._wait_for_text_sync_retry()
            try:
                async with self.guild_locks[guild.id]:
                    state = await self._store_call(
                        self.store.begin_current_channel_recovery,
                        guild.id,
                        utc_now_epoch(),
                    )
                    if (
                        guild.id in self._disconnect_epochs
                        or guild.id in self._guild_unavailable_epochs
                    ):
                        return
                    if await self._finish_current_text_recovery_locked(
                        guild,
                        state,
                    ):
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "activity automatic SoD/EoD retry failed for guild %s",
                    guild.id,
                )

    def _suspend_collection(self, guilds) -> dict[int, int]:
        generations = {}
        for guild in guilds:
            guild_id = guild.id
            self._collection_generations[guild_id] += 1
            generations[guild_id] = self._collection_generations[guild_id]
            self.collection_gates[guild_id].clear()
            self.dirty_guilds.add(guild_id)
        return generations

    def _record_guild_outage(
        self,
        guild_id: int,
        observed_epoch: int,
        generation: int,
    ) -> tuple[int, int]:
        previous_epoch = self._guild_unavailable_epochs.get(guild_id)
        outage_epoch = (
            observed_epoch
            if previous_epoch is None
            else max(previous_epoch, observed_epoch)
        )
        self._guild_unavailable_epochs[guild_id] = outage_epoch
        self._guild_unavailable_generations[guild_id] = generation
        return outage_epoch, generation

    def _guild_outage_state(self, guild_id: int) -> tuple[int, int] | None:
        outage_epoch = self._guild_unavailable_epochs.get(guild_id)
        outage_generation = self._guild_unavailable_generations.get(guild_id)
        if outage_epoch is None or outage_generation is None:
            return None
        return outage_epoch, outage_generation

    def _clear_guild_outage_if_current(
        self,
        guild_id: int,
        expected_state: tuple[int, int] | None,
    ) -> bool:
        if expected_state is None or self._guild_outage_state(guild_id) != expected_state:
            return False
        self._guild_unavailable_epochs.pop(guild_id, None)
        self._guild_unavailable_generations.pop(guild_id, None)
        return True

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

    async def _close_for_outage_locked(
        self,
        guild,
        outage_epoch: int,
    ) -> None:
        if guild.id in self._startup_recovered_guild_ids:
            await self._store_call(
                self.store.close_open_rows_through_epoch,
                guild.id,
                outage_epoch,
                "gateway_disconnect",
            )
            return
        snapshot = await self._store_call(
            self.store.snapshot_open_row_ids,
            guild.id,
        )
        await self._store_call(
            self.store.close_snapshot_rows_at_checkpoint,
            snapshot,
            "gateway_disconnect",
        )

    async def _recover_startup_guild_locked(
        self,
        guild,
        effective_at_epoch: int,
        generation: int,
    ) -> bool:
        if guild.id in self._startup_recovered_guild_ids:
            return True
        guild_outage_state = self._guild_outage_state(guild.id)
        text_state = await self._prepare_current_text_recovery_locked(
            guild,
            effective_at_epoch,
        )
        close_reason = (
            "gateway_disconnect"
            if (
                guild.id in self._disconnect_epochs
                or guild.id in self._guild_unavailable_epochs
            )
            else "restart_checkpoint"
        )
        recovered = await self._recover_suspended_guild_locked(
            guild,
            effective_at_epoch,
            close_reason,
            generation,
        )
        if not recovered:
            return False
        self._startup_recovered_guild_ids.add(guild.id)
        self._disconnect_epochs.pop(guild.id, None)
        self._clear_guild_outage_if_current(guild.id, guild_outage_state)
        self._guild_available_recovery_requests.discard(guild.id)
        if not await self._finish_current_text_recovery_locked(guild, text_state):
            self._schedule_text_sync_retry(guild)
        return True

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
                    await self._recover_startup_guild_locked(
                        guild,
                        effective_at_epoch,
                        generations[guild.id],
                    )
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
        self._unloading = False
        if self.recovery_task is None or self.recovery_task.done():
            self.recovery_task = self._track_lifecycle_task(
                asyncio.create_task(self.recover_after_ready())
            )
        if not self.checkpoint_loop.is_running():
            self.checkpoint_task = self._track_lifecycle_task(
                self.checkpoint_loop.start()
            )

    async def cog_unload(self) -> None:
        self._unloading = True
        tasks_to_wait = set(self._lifecycle_tasks)
        if self.checkpoint_loop.is_running():
            self.checkpoint_loop.cancel()
        for task in tasks_to_wait:
            if not task.done():
                task.cancel()
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
        self._lifecycle_tasks.difference_update(tasks_to_wait)
        self._text_sync_retry_tasks.clear()

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
    def _member_has_target_role(member, config) -> bool:
        return bool(
            member is not None
            and not member.bot
            and config.target_role_id
            in {getattr(role, "id", None) for role in member.roles}
        )

    @staticmethod
    def _message_envelope_is_eligible(message, guild, config) -> bool:
        message_guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        return bool(
            message_guild is not None
            and getattr(message_guild, "id", None) == guild.id
            and isinstance(channel, discord.TextChannel)
            and getattr(channel, "id", None) == config.sod_eod_channel_id
            and getattr(message, "type", None) is discord.MessageType.default
            and getattr(message, "webhook_id", None) is None
        )

    @classmethod
    def _message_is_eligible(cls, message, guild, config, member) -> bool:
        return bool(
            cls._message_envelope_is_eligible(message, guild, config)
            and cls._member_has_target_role(member, config)
        )

    @staticmethod
    def _event_member_or_cached(message, guild):
        author = getattr(message, "author", None)
        author_guild = getattr(author, "guild", None)
        if (
            getattr(author_guild, "id", None) == guild.id
            and hasattr(author, "roles")
            and hasattr(author, "bot")
        ):
            return author
        author_id = getattr(author, "id", None)
        return None if author_id is None else guild.get_member(author_id)

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
            owns_generation = (
                expected_generation is None
                or self._collection_generations[guild.id] == expected_generation
            )
            if owns_generation:
                try:
                    await self._store_call(
                        self.store.abort_full_reconcile,
                        guild.id,
                        effective_at_epoch=effective_at_epoch,
                    )
                except Exception:
                    logger.exception("failed to abort activity full reconcile")
            else:
                # A newer lifecycle event owns cleanup at its own epoch.
                # Keep the gate closed and never sweep from this stale attempt.
                self.dirty_guilds.add(guild.id)
            raise
        if (
            expected_generation is not None
            and self._collection_generations[guild.id] != expected_generation
        ):
            # The newer lifecycle owner also acquires this guild lock and must
            # close or reconcile rows at its own epoch; stale owners never sweep.
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
    async def on_message(self, message) -> None:
        guild = getattr(message, "guild", None)
        if guild is None:
            return
        try:
            config = await self._store_call(self.store.get_config, guild.id)
            member = self._event_member_or_cached(message, guild)
            if not self._message_is_eligible(message, guild, config, member):
                return
            event_types = detect_sod_eod(message.content)
            if not event_types:
                return
            message_created_epoch = int(message.created_at.timestamp())
            await self._store_call(
                self.store.record_live_message,
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                user_id=member.id,
                message_created_epoch=message_created_epoch,
                event_types=event_types,
                updated_epoch=utc_now_epoch(),
                expected_current_channel_id=config.sod_eod_channel_id,
            )
        except Exception:
            logger.exception(
                "activity live SoD/EoD collection failed for guild %s",
                guild.id,
            )

    async def backfill_current_channel(
        self,
        guild,
        *,
        channel: discord.TextChannel | None = None,
    ) -> BackfillResult:
        async with self.guild_locks[guild.id]:
            return await self._backfill_current_channel_locked(
                guild,
                channel=channel,
            )

    async def _prepare_current_text_recovery_locked(
        self,
        guild,
        updated_epoch: int,
    ):
        try:
            return await self._store_call(
                self.store.begin_current_channel_recovery,
                guild.id,
                updated_epoch,
            )
        except Exception:
            self._text_recovery_pending_guild_ids.add(guild.id)
            logger.exception(
                "activity SoD/EoD recovery preparation failed for guild %s",
                guild.id,
            )
            self._schedule_text_sync_retry(guild)
            return _TEXT_RECOVERY_PREPARE_FAILED

    async def _finish_current_text_recovery_locked(
        self,
        guild,
        state,
    ) -> bool:
        if state is _TEXT_RECOVERY_PREPARE_FAILED:
            return False
        if state is None or state.initialized_epoch is None:
            await self._complete_text_recovery_success(guild.id)
            return True
        try:
            await self._backfill_current_channel_locked(
                guild,
                automatic_state=state,
            )
            return True
        except Exception:
            logger.exception(
                "activity automatic SoD/EoD delta backfill failed for guild %s",
                guild.id,
            )
            return False

    async def _backfill_current_channel_locked(
        self,
        guild,
        *,
        channel: discord.TextChannel | None = None,
        automatic_state=None,
    ) -> BackfillResult:
        config = await self._store_call(self.store.get_config, guild.id)
        channel_id = config.sod_eod_channel_id
        if channel_id is None:
            raise ValueError("SoD/EoD 채널이 설정되지 않았습니다.")
        if automatic_state is None:
            state = await self._store_call(
                self.store.get_sync_state,
                guild.id,
                channel_id,
            )
            await self._store_call(
                self.store.mark_backfill_started,
                guild.id,
                channel_id,
                utc_now_epoch(),
            )
        else:
            state = automatic_state
            if state.channel_id != channel_id:
                raise ChannelChanged(channel_id)
            if state.initialized_epoch is None:
                return BackfillResult(0, 0)
            if state.newest_processed_message_id is None:
                raise RuntimeError("initialized sync state has no delta cursor")
        after = (
            None
            if state is None or state.newest_processed_message_id is None
            else discord.Object(id=state.newest_processed_message_id)
        )
        if channel is None:
            channel = guild.get_channel(channel_id)
        if (
            not self._same_guild_resource(channel, discord.TextChannel, guild)
            or channel.id != channel_id
        ):
            raise ChannelChanged(channel_id)
        if not self._text_channel_is_accessible(channel, guild):
            raise ValueError("SoD/EoD 텍스트 채널 이력을 읽을 수 없습니다.")

        scan_barrier_epoch = utc_now_epoch()
        processed_count = 0
        event_count = 0
        author_eligibility = OrderedDict()
        async for message in channel.history(
            limit=None,
            oldest_first=True,
            after=after,
        ):
            author_id = message.author.id
            member = guild.get_member(author_id)
            if member is not None:
                author_eligibility.pop(author_id, None)
                eligible_author = self._member_has_target_role(member, config)
            else:
                try:
                    eligible_author = author_eligibility.pop(author_id)
                except KeyError:
                    try:
                        member = await guild.fetch_member(author_id)
                    except discord.NotFound:
                        member = None
                    eligible_author = self._member_has_target_role(member, config)
                author_eligibility[author_id] = eligible_author
                if len(author_eligibility) > AUTHOR_ELIGIBILITY_CACHE_LIMIT:
                    author_eligibility.popitem(last=False)

            event_types = set()
            if (
                eligible_author
                and self._message_envelope_is_eligible(message, guild, config)
            ):
                event_types = detect_sod_eod(message.content)
            message_created_epoch = int(message.created_at.timestamp())
            await self._store_call(
                self.store.record_backfill_message_and_advance_cursor,
                guild_id=guild.id,
                channel_id=channel_id,
                message_id=message.id,
                user_id=author_id,
                message_created_epoch=message_created_epoch,
                event_types=event_types,
                newest_processed_message_created_epoch=message_created_epoch,
                updated_epoch=utc_now_epoch(),
                expected_current_channel_id=channel_id,
            )
            processed_count += 1
            event_count += len(event_types)

        current = await self._store_call(self.store.get_config, guild.id)
        if current.sod_eod_channel_id != channel_id:
            raise ChannelChanged(channel_id)
        await self._store_call(
            self.store.mark_backfill_completed,
            guild.id,
            channel_id,
            utc_now_epoch(),
            empty_scan_cursor_epoch=scan_barrier_epoch,
        )
        await self._complete_text_recovery_success(guild.id)
        return BackfillResult(processed_count, event_count)

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
                    await self._prepare_current_text_recovery_locked(
                        guild,
                        disconnect_epoch,
                    )
                    await self._close_for_outage_locked(
                        guild,
                        disconnect_epoch,
                    )
            except Exception:
                logger.exception(
                    "activity disconnect close failed for guild %s",
                    guild.id,
                )

    @commands.Cog.listener()
    async def on_guild_unavailable(self, guild) -> None:
        observed_epoch = utc_now_epoch()
        self._guild_available_recovery_requests.discard(guild.id)
        generation = self._suspend_collection((guild,))[guild.id]
        outage_epoch, outage_generation = self._record_guild_outage(
            guild.id,
            observed_epoch,
            generation,
        )
        try:
            async with self.guild_locks[guild.id]:
                if (
                    self._collection_generations[guild.id] != generation
                    or self._guild_outage_state(guild.id)
                    != (outage_epoch, outage_generation)
                ):
                    return
                await self._prepare_current_text_recovery_locked(
                    guild,
                    outage_epoch,
                )
                await self._close_for_outage_locked(
                    guild,
                    outage_epoch,
                )
        except Exception:
            logger.exception(
                "activity guild-unavailable close failed for guild %s",
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
                    generation = generations[guild.id]
                    if self._collection_generations[guild.id] != generation:
                        continue
                    guild_outage_state = self._guild_outage_state(guild.id)
                    guild_outage_epoch = (
                        None if guild_outage_state is None else guild_outage_state[0]
                    )
                    needs_startup_recovery = (
                        guild.id not in self._startup_recovered_guild_ids
                    )
                    if guild_outage_epoch is not None and bool(
                        getattr(guild, "unavailable", False)
                    ):
                        await self._prepare_current_text_recovery_locked(
                            guild,
                            effective_at_epoch,
                        )
                        await self._close_for_outage_locked(
                            guild,
                            guild_outage_epoch,
                        )
                        self._disconnect_epochs.pop(guild.id, None)
                        self._guild_available_recovery_requests.discard(guild.id)
                        continue
                    if needs_startup_recovery:
                        recovered = await self._recover_startup_guild_locked(
                            guild,
                            effective_at_epoch,
                            generation,
                        )
                    else:
                        text_state = await self._prepare_current_text_recovery_locked(
                            guild,
                            effective_at_epoch,
                        )
                        close_reason = (
                            "gateway_disconnect"
                            if (
                                guild.id in self._disconnect_epochs
                                or guild_outage_epoch is not None
                            )
                            else "restart_checkpoint"
                        )
                        if guild_outage_epoch is not None:
                            await self._close_for_outage_locked(
                                guild,
                                guild_outage_epoch,
                            )
                        recovered = await self._recover_suspended_guild_locked(
                            guild,
                            effective_at_epoch,
                            close_reason,
                            generation,
                        )
                    if recovered and not needs_startup_recovery:
                        self._disconnect_epochs.pop(guild.id, None)
                        self._clear_guild_outage_if_current(
                            guild.id,
                            guild_outage_state,
                        )
                        self._guild_available_recovery_requests.discard(guild.id)
                        if not await self._finish_current_text_recovery_locked(
                            guild,
                            text_state,
                        ):
                            self._schedule_text_sync_retry(guild)
            except Exception:
                logger.exception(
                    "activity resume recovery failed for guild %s",
                    guild.id,
                )

    @commands.Cog.listener()
    async def on_guild_available(self, guild) -> None:
        self._guild_available_recovery_requests.add(guild.id)
        had_pending_disconnect = guild.id in self._disconnect_epochs
        guild_outage_state = self._guild_outage_state(guild.id)
        had_guild_outage = guild_outage_state is not None
        guild_outage_epoch = (
            None if guild_outage_state is None else guild_outage_state[0]
        )
        needs_startup_recovery = (
            guild.id not in self._startup_recovered_guild_ids
        )
        generation = self._suspend_collection((guild,))[guild.id]
        effective_at_epoch = utc_now_epoch()
        try:
            async with self.guild_locks[guild.id]:
                if self._collection_generations[guild.id] != generation:
                    return
                if needs_startup_recovery:
                    recovered = await self._recover_startup_guild_locked(
                        guild,
                        effective_at_epoch,
                        generation,
                    )
                else:
                    text_state = await self._prepare_current_text_recovery_locked(
                        guild,
                        effective_at_epoch,
                    )
                    if guild_outage_epoch is not None:
                        await self._close_for_outage_locked(
                            guild,
                            guild_outage_epoch,
                        )
                    if had_pending_disconnect or had_guild_outage:
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
                    if recovered:
                        self._disconnect_epochs.pop(guild.id, None)
                        self._clear_guild_outage_if_current(
                            guild.id,
                            guild_outage_state,
                        )
                        self._guild_available_recovery_requests.discard(guild.id)
                        if not await self._finish_current_text_recovery_locked(
                            guild,
                            text_state,
                        ):
                            self._schedule_text_sync_retry(guild)
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

    @settings_group.command(
        name="과거동기화",
        description="현재 SoD/EoD 채널의 과거 메시지를 동기화합니다.",
    )
    async def backfill_command(self, interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.backfill_current_channel(interaction.guild)
            content = (
                "과거 동기화 완료: "
                f"처리 {result.processed_count}개, 감지 {result.event_count}개"
            )
        except (sqlite3.Error, discord.DiscordException, ChannelChanged, ValueError):
            logger.exception("activity SoD/EoD backfill failed")
            content = (
                "과거 동기화에 실패했습니다. "
                "설정, 로그와 채널 권한을 확인해 주세요."
            )
        try:
            await self._complete_ephemeral(
                interaction,
                content,
            )
        except discord.DiscordException:
            logger.exception("activity SoD/EoD backfill response edit failed")
            raise

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
