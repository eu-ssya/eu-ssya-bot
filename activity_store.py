import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from contextlib import closing
from pathlib import Path


KST = timezone(timedelta(hours=9), "KST")
_UNSET = object()


def _require_integer_epochs(**epochs: int) -> None:
    for name, value in epochs.items():
        if type(value) is not int:
            raise TypeError(f"{name} must be integer epoch seconds")


def kst_day_for_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, KST).date().isoformat()


def kst_range_to_epoch(start: date, end: date) -> tuple[int, int]:
    start_epoch = int(datetime.combine(start, time.min, KST).timestamp())
    end_epoch = int(datetime.combine(end + timedelta(days=1), time.min, KST).timestamp())
    return start_epoch, end_epoch


def session_overlap_seconds(
    started_epoch: int,
    ended_epoch: int | None,
    range_start: int,
    range_end: int,
) -> int:
    effective_end = range_end if ended_epoch is None else ended_epoch
    return max(
        0,
        min(effective_end, range_end) - max(started_epoch, range_start),
    )


@dataclass(frozen=True)
class ActivityConfig:
    guild_id: int
    target_role_id: int | None
    reading_category_id: int | None
    study_category_id: int | None
    sod_eod_channel_id: int | None
    voice_collection_started_epoch: int | None

    @property
    def voice_is_complete(self) -> bool:
        return all((self.target_role_id, self.reading_category_id, self.study_category_id))


@dataclass(frozen=True)
class ReportMember:
    user_id: int
    display_name: str


@dataclass(frozen=True)
class ReportRow:
    user_id: int
    display_name: str
    last_activity_epoch: int | None
    reading_seconds: int
    study_seconds: int
    reading_session_count: int
    study_session_count: int
    sod_days: int
    eod_days: int
    combined_days: int


@dataclass(frozen=True)
class CoverageWarning:
    code: str
    text: str


@dataclass(frozen=True)
class ActivityReport:
    rows: list[ReportRow]
    warnings: list[CoverageWarning]
    start_date: date
    end_date: date
    start_epoch: int
    end_epoch: int
    generated_epoch: int
    period_label: str
    txt_filename: str
    page_count: int


@dataclass(frozen=True)
class CoverageSummary:
    covered: list[tuple[int, int]]
    gaps: list[tuple[int, int]]


@dataclass(frozen=True)
class ActivitySyncState:
    guild_id: int
    channel_id: int
    newest_processed_message_id: int | None
    newest_processed_message_created_epoch: int | None
    history_from_epoch: int | None
    completed_epoch: int | None
    updated_epoch: int


class ChannelChanged(RuntimeError):
    pass


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activity_config (guild_id INTEGER PRIMARY KEY,target_role_id INTEGER,reading_category_id INTEGER,study_category_id INTEGER,sod_eod_channel_id INTEGER,voice_collection_started_epoch INTEGER,created_epoch INTEGER NOT NULL,updated_epoch INTEGER NOT NULL,CHECK(reading_category_id IS NULL OR study_category_id IS NULL OR reading_category_id <> study_category_id));
CREATE TABLE IF NOT EXISTS voice_sessions (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,activity_kind TEXT NOT NULL CHECK(activity_kind IN ('reading_room','study')),started_epoch INTEGER NOT NULL,last_checkpoint_epoch INTEGER NOT NULL,ended_epoch INTEGER,closed_reason TEXT CHECK(closed_reason IN ('normal','category_change','role_removed','config_changed','reconciled','gateway_disconnect','restart_checkpoint')),CHECK(typeof(started_epoch)='integer'),CHECK(typeof(last_checkpoint_epoch)='integer'),CHECK(ended_epoch IS NULL OR typeof(ended_epoch)='integer'),CHECK(last_checkpoint_epoch >= started_epoch),CHECK((ended_epoch IS NULL AND closed_reason IS NULL) OR (ended_epoch IS NOT NULL AND closed_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch));
CREATE TABLE IF NOT EXISTS voice_collection_runs (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,started_epoch INTEGER NOT NULL,last_checkpoint_epoch INTEGER NOT NULL,ended_epoch INTEGER,ended_reason TEXT CHECK(ended_reason IN ('config_changed','config_invalid','graceful_shutdown','gateway_disconnect','restart_checkpoint')),CHECK(typeof(started_epoch)='integer'),CHECK(typeof(last_checkpoint_epoch)='integer'),CHECK(ended_epoch IS NULL OR typeof(ended_epoch)='integer'),CHECK(last_checkpoint_epoch >= started_epoch),CHECK((ended_epoch IS NULL AND ended_reason IS NULL) OR (ended_epoch IS NOT NULL AND ended_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch));
CREATE TABLE IF NOT EXISTS sod_eod_events (message_id INTEGER NOT NULL,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,event_date_kst TEXT NOT NULL,event_type TEXT NOT NULL CHECK(event_type IN ('sod','eod')),message_created_epoch INTEGER NOT NULL CHECK(typeof(message_created_epoch)='integer'),channel_id INTEGER NOT NULL,PRIMARY KEY(message_id,event_type));
CREATE TABLE IF NOT EXISTS sod_eod_daily (guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,event_date_kst TEXT NOT NULL,event_type TEXT NOT NULL CHECK(event_type IN ('sod','eod')),PRIMARY KEY(guild_id,user_id,event_date_kst,event_type));
CREATE TABLE IF NOT EXISTS activity_sync_state (guild_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,newest_processed_message_id INTEGER,newest_processed_message_created_epoch INTEGER CHECK(newest_processed_message_created_epoch IS NULL OR typeof(newest_processed_message_created_epoch)='integer'),history_from_epoch INTEGER CHECK(history_from_epoch IS NULL OR typeof(history_from_epoch)='integer'),completed_epoch INTEGER CHECK(completed_epoch IS NULL OR typeof(completed_epoch)='integer'),updated_epoch INTEGER NOT NULL CHECK(typeof(updated_epoch)='integer'),PRIMARY KEY(guild_id,channel_id));
CREATE TABLE IF NOT EXISTS sod_eod_channel_periods (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,started_epoch INTEGER NOT NULL,ended_epoch INTEGER,ended_reason TEXT CHECK(ended_reason IN ('channel_changed','config_invalid')),CHECK((ended_epoch IS NULL AND ended_reason IS NULL) OR (ended_epoch IS NOT NULL AND ended_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= started_epoch));
CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_sessions_one_open_per_member ON voice_sessions(guild_id,user_id) WHERE ended_epoch IS NULL;
CREATE INDEX IF NOT EXISTS idx_voice_sessions_report ON voice_sessions(guild_id,user_id,activity_kind,started_epoch,ended_epoch);
CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_collection_runs_one_open ON voice_collection_runs(guild_id) WHERE ended_epoch IS NULL;
CREATE INDEX IF NOT EXISTS idx_voice_collection_runs_coverage ON voice_collection_runs(guild_id,started_epoch,ended_epoch);
CREATE INDEX IF NOT EXISTS idx_sod_eod_events_report ON sod_eod_events(guild_id,channel_id,user_id,event_date_kst,event_type);
CREATE INDEX IF NOT EXISTS idx_sod_eod_events_last_activity ON sod_eod_events(guild_id,user_id,message_created_epoch);
CREATE INDEX IF NOT EXISTS idx_sod_eod_daily_report ON sod_eod_daily(guild_id,event_date_kst,user_id,event_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sod_eod_channel_periods_one_open ON sod_eod_channel_periods(guild_id) WHERE ended_epoch IS NULL;
CREATE INDEX IF NOT EXISTS idx_sod_eod_channel_periods_coverage ON sod_eod_channel_periods(guild_id,channel_id,started_epoch,ended_epoch);
"""


class ActivityStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self):
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def table_names(self) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            return [row[0] for row in rows]

    def get_config(self, guild_id: int) -> ActivityConfig:
        with closing(self._connect()) as conn:
            return self._get_config_in_tx(conn, guild_id)

    def apply_config_change(
        self,
        guild_id: int,
        *,
        effective_at_epoch: int,
        target_role_id: int | None | object = _UNSET,
        reading_category_id: int | None | object = _UNSET,
        study_category_id: int | None | object = _UNSET,
        sod_eod_channel_id: int | None | object = _UNSET,
    ) -> ActivityConfig:
        _require_integer_epochs(effective_at_epoch=effective_at_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._get_config_in_tx(conn, guild_id)
                new = self._replace_unset(
                    old,
                    target_role_id,
                    reading_category_id,
                    study_category_id,
                    sod_eod_channel_id,
                )
                if (
                    new.reading_category_id is not None
                    and new.reading_category_id == new.study_category_id
                ):
                    raise ValueError("독서실과 스터디 카테고리는 서로 달라야 합니다.")
                if old.voice_collection_started_epoch is None and new.voice_is_complete:
                    new = dataclasses.replace(
                        new, voice_collection_started_epoch=effective_at_epoch
                    )
                if self._voice_core_changed(old, new):
                    self._close_open_rows_in_tx(
                        conn, guild_id, effective_at_epoch, "config_changed"
                    )
                if old.sod_eod_channel_id != new.sod_eod_channel_id:
                    self._transition_sod_period_in_tx(
                        conn,
                        guild_id,
                        old.sod_eod_channel_id,
                        new.sod_eod_channel_id,
                        effective_at_epoch,
                    )
                self._upsert_config_in_tx(conn, new, effective_at_epoch)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return new

    def list_channel_periods(self, guild_id: int) -> list[tuple[int, int, int | None]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT channel_id, started_epoch, ended_epoch
                FROM sod_eod_channel_periods
                WHERE guild_id=?
                ORDER BY started_epoch, id
                """,
                (guild_id,),
            )
            return [tuple(row) for row in rows]

    def reconcile_session(
        self,
        guild_id: int,
        user_id: int,
        desired_kind: str | None,
        effective_at_epoch: int,
        close_reason: str = "reconciled",
    ) -> None:
        _require_integer_epochs(effective_at_epoch=effective_at_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_open_session_in_tx(conn, guild_id, user_id)
                if row and (desired_kind is None or row[1] != desired_kind):
                    reason = "category_change" if desired_kind else close_reason
                    conn.execute(
                        """
                        UPDATE voice_sessions
                        SET ended_epoch=?, closed_reason=?
                        WHERE id=?
                        """,
                        (effective_at_epoch, reason, row[0]),
                    )
                if desired_kind and (row is None or row[1] != desired_kind):
                    try:
                        self._insert_open_session_in_tx(
                            conn,
                            guild_id,
                            user_id,
                            desired_kind,
                            effective_at_epoch,
                        )
                    except sqlite3.IntegrityError as exc:
                        if exc.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                            raise
                        retry = self._get_open_session_in_tx(
                            conn, guild_id, user_id
                        )
                        if retry is None:
                            raise
                        if retry[1] == desired_kind:
                            conn.commit()
                            return
                        conn.execute(
                            """
                            UPDATE voice_sessions
                            SET ended_epoch=?, closed_reason='category_change'
                            WHERE id=?
                            """,
                            (effective_at_epoch, retry[0]),
                        )
                        self._insert_open_session_in_tx(
                            conn,
                            guild_id,
                            user_id,
                            desired_kind,
                            effective_at_epoch,
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def open_collection_run(self, guild_id: int, started_epoch: int) -> None:
        _require_integer_epochs(started_epoch=started_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT id
                    FROM voice_collection_runs
                    WHERE guild_id=? AND ended_epoch IS NULL
                    """,
                    (guild_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO voice_collection_runs(
                            guild_id, started_epoch, last_checkpoint_epoch
                        ) VALUES (?, ?, ?)
                        """,
                        (guild_id, started_epoch, started_epoch),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close_open_rows(
        self, guild_id: int, effective_at_epoch: int, reason: str
    ) -> None:
        _require_integer_epochs(effective_at_epoch=effective_at_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._close_open_rows_in_tx(
                    conn, guild_id, effective_at_epoch, reason
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def checkpoint_open_rows(self, guild_id: int, checkpoint_epoch: int) -> None:
        _require_integer_epochs(checkpoint_epoch=checkpoint_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE voice_sessions
                    SET last_checkpoint_epoch=?
                    WHERE guild_id=? AND ended_epoch IS NULL
                    """,
                    (checkpoint_epoch, guild_id),
                )
                conn.execute(
                    """
                    UPDATE voice_collection_runs
                    SET last_checkpoint_epoch=?
                    WHERE guild_id=? AND ended_epoch IS NULL
                    """,
                    (checkpoint_epoch, guild_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def voice_seconds_for_range(
        self,
        guild_id: int,
        user_id: int,
        activity_kind: str,
        range_start: int,
        range_end: int,
    ) -> int:
        _require_integer_epochs(range_start=range_start, range_end=range_end)
        if range_end <= range_start:
            return 0
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(
                    MAX(
                        0,
                        MIN(COALESCE(ended_epoch, ?), ?) - MAX(started_epoch, ?)
                    )
                ), 0)
                FROM voice_sessions
                WHERE guild_id=?
                  AND user_id=?
                  AND activity_kind=?
                  AND started_epoch < ?
                  AND COALESCE(ended_epoch, ?) > ?
                """,
                (
                    range_end,
                    range_end,
                    range_start,
                    guild_id,
                    user_id,
                    activity_kind,
                    range_end,
                    range_end,
                    range_start,
                ),
            ).fetchone()
        return int(row[0])

    def voice_session_count_for_range(
        self,
        guild_id: int,
        user_id: int,
        activity_kind: str,
        range_start: int,
        range_end: int,
    ) -> int:
        _require_integer_epochs(range_start=range_start, range_end=range_end)
        if range_end <= range_start:
            return 0
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM voice_sessions
                WHERE guild_id=?
                  AND user_id=?
                  AND activity_kind=?
                  AND MIN(COALESCE(ended_epoch, ?), ?)
                      - MAX(started_epoch, ?) > 0
                """,
                (
                    guild_id,
                    user_id,
                    activity_kind,
                    range_end,
                    range_end,
                    range_start,
                ),
            ).fetchone()
        return int(row[0])

    def voice_coverage_for_range(
        self, guild_id: int, range_start: int, range_end: int
    ) -> CoverageSummary:
        _require_integer_epochs(range_start=range_start, range_end=range_end)
        if range_end <= range_start:
            return CoverageSummary([], [])
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT started_epoch, ended_epoch
                FROM voice_collection_runs
                WHERE guild_id=?
                  AND started_epoch < ?
                  AND COALESCE(ended_epoch, ?) > ?
                ORDER BY started_epoch, id
                """,
                (guild_id, range_end, range_end, range_start),
            ).fetchall()

        covered: list[tuple[int, int]] = []
        for started_epoch, ended_epoch in rows:
            start = max(started_epoch, range_start)
            end = min(
                range_end if ended_epoch is None else ended_epoch,
                range_end,
            )
            if start >= end:
                continue
            if covered and start <= covered[-1][1]:
                covered[-1] = (covered[-1][0], max(covered[-1][1], end))
            else:
                covered.append((start, end))

        gaps: list[tuple[int, int]] = []
        cursor = range_start
        for start, end in covered:
            if cursor < start:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < range_end:
            gaps.append((cursor, range_end))
        return CoverageSummary(covered, gaps)

    def build_report(
        self,
        *,
        guild_id: int,
        members: list[ReportMember],
        start_epoch: int,
        end_epoch: int,
        as_of_epoch: int,
    ) -> ActivityReport:
        _require_integer_epochs(
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            as_of_epoch=as_of_epoch,
        )
        start_date = datetime.fromtimestamp(start_epoch, KST).date()
        end_date = datetime.fromtimestamp(end_epoch - 1, KST).date()
        member_ids = list(dict.fromkeys(member.user_id for member in members))
        voice_totals = {
            user_id: {
                "reading_room": [0, 0],
                "study": [0, 0],
            }
            for user_id in member_ids
        }
        last_activity_by_user: dict[int, int] = {}
        daily_by_user: dict[int, tuple[int, int, int]] = {}
        if member_ids:
            placeholders = ",".join("?" for _ in member_ids)
            selected = (guild_id, *member_ids)
            with closing(self._connect()) as conn:
                sessions = conn.execute(
                    f"""
                    SELECT user_id, activity_kind, started_epoch, ended_epoch
                    FROM voice_sessions
                    WHERE guild_id=? AND user_id IN ({placeholders})
                    ORDER BY user_id, started_epoch, id
                    """,
                    selected,
                ).fetchall()
                for user_id, activity_kind, started, ended in sessions:
                    overlap = session_overlap_seconds(
                        started,
                        ended,
                        start_epoch,
                        end_epoch,
                    )
                    voice_totals[user_id][activity_kind][0] += overlap
                    if overlap > 0:
                        voice_totals[user_id][activity_kind][1] += 1
                    activity_epoch = as_of_epoch if ended is None else ended
                    previous = last_activity_by_user.get(user_id)
                    if previous is None or activity_epoch > previous:
                        last_activity_by_user[user_id] = activity_epoch

                daily_rows = conn.execute(
                    f"""
                    SELECT
                        user_id,
                        COUNT(DISTINCT CASE
                            WHEN event_type='sod' THEN event_date_kst
                        END),
                        COUNT(DISTINCT CASE
                            WHEN event_type='eod' THEN event_date_kst
                        END),
                        COUNT(DISTINCT event_date_kst)
                    FROM sod_eod_daily
                    WHERE guild_id=? AND user_id IN ({placeholders})
                      AND event_date_kst BETWEEN ? AND ?
                    GROUP BY user_id
                    """,
                    (
                        *selected,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    ),
                ).fetchall()
                daily_by_user = {
                    user_id: (int(sod_days), int(eod_days), int(combined_days))
                    for user_id, sod_days, eod_days, combined_days in daily_rows
                }

                latest_messages = conn.execute(
                    f"""
                    SELECT user_id, MAX(message_created_epoch)
                    FROM sod_eod_events
                    WHERE guild_id=? AND user_id IN ({placeholders})
                    GROUP BY user_id
                    """,
                    selected,
                ).fetchall()
                for user_id, latest_message in latest_messages:
                    previous = last_activity_by_user.get(user_id)
                    if previous is None or latest_message > previous:
                        last_activity_by_user[user_id] = latest_message

        rows = []
        for member in members:
            reading = voice_totals[member.user_id]["reading_room"]
            study = voice_totals[member.user_id]["study"]
            sod_days, eod_days, combined_days = daily_by_user.get(
                member.user_id, (0, 0, 0)
            )
            rows.append(
                ReportRow(
                    user_id=member.user_id,
                    display_name=member.display_name,
                    last_activity_epoch=last_activity_by_user.get(member.user_id),
                    reading_seconds=reading[0],
                    study_seconds=study[0],
                    reading_session_count=reading[1],
                    study_session_count=study[1],
                    sod_days=sod_days,
                    eod_days=eod_days,
                    combined_days=combined_days,
                )
            )

        rows.sort(
            key=lambda row: (
                row.last_activity_epoch is not None,
                row.last_activity_epoch or 0,
                row.display_name.casefold(),
                row.user_id,
            )
        )
        warnings = self._build_report_warnings(
            guild_id=guild_id,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
        )
        return ActivityReport(
            rows=rows,
            warnings=warnings,
            start_date=start_date,
            end_date=end_date,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            generated_epoch=as_of_epoch,
            period_label=f"조회 기간: {start_date} ~ {end_date}",
            txt_filename=(
                f"activity-report-{start_date:%Y%m%d}-{end_date:%Y%m%d}-kst.txt"
            ),
            page_count=max(1, (len(rows) + 14) // 15),
        )

    def _build_report_warnings(
        self,
        *,
        guild_id: int,
        start_epoch: int,
        end_epoch: int,
    ) -> list[CoverageWarning]:
        warnings = [
            CoverageWarning(
                code="voice_gap",
                text=(
                    f"음성 수집 누락 구간: {gap_start}~{gap_end} UTC epoch. "
                    "이 구간의 값은 부분 데이터입니다."
                ),
            )
            for gap_start, gap_end in self.voice_coverage_for_range(
                guild_id, start_epoch, end_epoch
            ).gaps
        ]

        with closing(self._connect()) as conn:
            raw_periods = conn.execute(
                """
                SELECT channel_id, started_epoch, ended_epoch
                FROM sod_eod_channel_periods
                WHERE guild_id=?
                  AND started_epoch < ?
                  AND COALESCE(ended_epoch, ?) > ?
                ORDER BY started_epoch, id
                """,
                (guild_id, end_epoch, end_epoch, start_epoch),
            ).fetchall()
            periods = [
                (
                    channel_id,
                    max(started, start_epoch),
                    min(end_epoch if ended is None else ended, end_epoch),
                )
                for channel_id, started, ended in raw_periods
            ]
            periods = [period for period in periods if period[1] < period[2]]

            for gap_start, gap_end in self._coverage_gaps(
                [(started, ended) for _, started, ended in periods],
                start_epoch,
                end_epoch,
            ):
                warnings.append(
                    CoverageWarning(
                        code="sod_channel_gap",
                        text=(
                            "SoD/EoD 채널 미설정 구간: "
                            f"{gap_start}~{gap_end} UTC epoch. "
                            "이 구간의 값은 부분 데이터입니다."
                        ),
                    )
                )

            if not periods:
                warnings.append(
                    CoverageWarning(
                        code="sod_history_unavailable",
                        text=(
                            f"조회 구간 {start_epoch}~{end_epoch} UTC epoch에 "
                            "SoD/EoD 채널 이력 시작점을 확인할 수 없습니다. "
                            "이 조회의 값은 부분 데이터입니다."
                        ),
                    )
                )
                return warnings

            channel_ids = list(dict.fromkeys(period[0] for period in periods))
            placeholders = ",".join("?" for _ in channel_ids)
            states = {
                channel_id: (history_from_epoch, completed_epoch)
                for channel_id, history_from_epoch, completed_epoch in conn.execute(
                    f"""
                    SELECT channel_id, history_from_epoch, completed_epoch
                    FROM activity_sync_state
                    WHERE guild_id=? AND channel_id IN ({placeholders})
                    """,
                    (guild_id, *channel_ids),
                )
            }
            for channel_id, period_start, period_end in periods:
                state = states.get(channel_id)
                history_from_epoch = None if state is None else state[0]
                completed_epoch = None if state is None else state[1]
                if history_from_epoch is None:
                    warnings.append(
                        CoverageWarning(
                            code="sod_history_unavailable",
                            text=(
                                f"SoD/EoD 채널 {channel_id}의 접근 가능한 과거 이력 "
                                "시작점을 확인할 수 없습니다; 활성 구간 "
                                f"{period_start}~{period_end} UTC epoch의 값은 "
                                "부분 데이터입니다."
                            ),
                        )
                    )
                elif history_from_epoch > period_start:
                    unavailable_end = min(history_from_epoch, period_end)
                    warnings.append(
                        CoverageWarning(
                            code="sod_history_partial",
                            text=(
                                f"SoD/EoD 채널 {channel_id}의 접근 가능한 이력은 "
                                f"{history_from_epoch} UTC epoch부터입니다; 활성 구간 "
                                f"{period_start}~{unavailable_end} UTC epoch의 값은 "
                                "부분 데이터입니다."
                            ),
                        )
                    )
                if completed_epoch is None:
                    warnings.append(
                        CoverageWarning(
                            code="sod_backfill_incomplete",
                            text=(
                                f"SoD/EoD 채널 {channel_id}의 과거 동기화가 "
                                "완료되지 않았습니다; 활성 구간 "
                                f"{period_start}~{period_end} UTC epoch의 값은 "
                                "부분 데이터입니다."
                            ),
                        )
                    )
        return warnings

    @staticmethod
    def _coverage_gaps(
        covered_ranges: list[tuple[int, int]],
        range_start: int,
        range_end: int,
    ) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(covered_ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        gaps: list[tuple[int, int]] = []
        cursor = range_start
        for start, end in merged:
            if cursor < start:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < range_end:
            gaps.append((cursor, range_end))
        return gaps

    def list_sessions(
        self, guild_id: int, user_id: int
    ) -> list[tuple[str, int, int | None, str | None]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT activity_kind, started_epoch, ended_epoch, closed_reason
                FROM voice_sessions
                WHERE guild_id=? AND user_id=?
                ORDER BY started_epoch, id
                """,
                (guild_id, user_id),
            )
            return [tuple(row) for row in rows]

    def list_runs(
        self, guild_id: int
    ) -> list[tuple[int, int, int | None, str | None]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT started_epoch, last_checkpoint_epoch, ended_epoch, ended_reason
                FROM voice_collection_runs
                WHERE guild_id=?
                ORDER BY started_epoch, id
                """,
                (guild_id,),
            )
            return [tuple(row) for row in rows]

    def open_session_count(self, guild_id: int, user_id: int) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM voice_sessions
                WHERE guild_id=? AND user_id=? AND ended_epoch IS NULL
                """,
                (guild_id, user_id),
            ).fetchone()
        return int(row[0])

    def invalidate_sod_eod_channel(
        self, guild_id: int, *, effective_at_epoch: int
    ) -> ActivityConfig:
        _require_integer_epochs(effective_at_epoch=effective_at_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._get_config_in_tx(conn, guild_id)
                if old.sod_eod_channel_id is None:
                    conn.commit()
                    return old
                new = dataclasses.replace(old, sod_eod_channel_id=None)
                self._transition_sod_period_in_tx(
                    conn,
                    guild_id,
                    old.sod_eod_channel_id,
                    None,
                    effective_at_epoch,
                    close_reason="config_invalid",
                )
                self._upsert_config_in_tx(conn, new, effective_at_epoch)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return new

    def invalidate_voice_config(
        self,
        guild_id: int,
        *,
        field: str,
        effective_at_epoch: int,
    ) -> ActivityConfig:
        _require_integer_epochs(effective_at_epoch=effective_at_epoch)
        if field not in {
            "target_role_id",
            "reading_category_id",
            "study_category_id",
        }:
            raise ValueError("알 수 없는 음성 설정 필드입니다.")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._get_config_in_tx(conn, guild_id)
                if getattr(old, field) is None:
                    conn.commit()
                    return old
                new = dataclasses.replace(old, **{field: None})
                conn.execute(
                    """
                    UPDATE voice_sessions
                    SET ended_epoch=?, closed_reason='config_changed'
                    WHERE guild_id=? AND ended_epoch IS NULL
                    """,
                    (effective_at_epoch, guild_id),
                )
                conn.execute(
                    """
                    UPDATE voice_collection_runs
                    SET ended_epoch=?, ended_reason='config_invalid'
                    WHERE guild_id=? AND ended_epoch IS NULL
                    """,
                    (effective_at_epoch, guild_id),
                )
                self._upsert_config_in_tx(conn, new, effective_at_epoch)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return new

    def count_open_sessions(self, guild_id: int) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM voice_sessions
                WHERE guild_id=? AND ended_epoch IS NULL
                """,
                (guild_id,),
            ).fetchone()
        return int(row[0])

    def abort_full_reconcile(
        self, guild_id: int, *, effective_at_epoch: int
    ) -> None:
        _require_integer_epochs(effective_at_epoch=effective_at_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE voice_sessions
                    SET ended_epoch=?, closed_reason='config_changed'
                    WHERE guild_id=? AND ended_epoch IS NULL
                    """,
                    (effective_at_epoch, guild_id),
                )
                conn.execute(
                    """
                    UPDATE voice_collection_runs
                    SET ended_epoch=?, ended_reason='config_invalid'
                    WHERE guild_id=? AND ended_epoch IS NULL
                    """,
                    (effective_at_epoch, guild_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def record_live_message(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        message_created_epoch: int,
        event_types: set[str],
        updated_epoch: int,
        expected_current_channel_id: int,
    ) -> None:
        _require_integer_epochs(
            message_created_epoch=message_created_epoch,
            updated_epoch=updated_epoch,
        )
        kinds = self._validated_event_types(event_types)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_channel_in_tx(
                    conn,
                    guild_id,
                    channel_id,
                    expected_current_channel_id,
                )
                self._insert_event_and_daily_in_tx(
                    conn,
                    guild_id,
                    channel_id,
                    message_id,
                    user_id,
                    message_created_epoch,
                    kinds,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def record_backfill_message_and_advance_cursor(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        message_created_epoch: int,
        event_types: set[str],
        newest_processed_message_created_epoch: int,
        updated_epoch: int,
        expected_current_channel_id: int,
    ) -> None:
        _require_integer_epochs(
            message_created_epoch=message_created_epoch,
            newest_processed_message_created_epoch=(
                newest_processed_message_created_epoch
            ),
            updated_epoch=updated_epoch,
        )
        kinds = self._validated_event_types(event_types)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_current_channel_in_tx(
                    conn,
                    guild_id,
                    channel_id,
                    expected_current_channel_id,
                )
                self._insert_event_and_daily_in_tx(
                    conn,
                    guild_id,
                    channel_id,
                    message_id,
                    user_id,
                    message_created_epoch,
                    kinds,
                )
                self._advance_cursor_in_tx(
                    conn,
                    guild_id,
                    channel_id,
                    message_id,
                    newest_processed_message_created_epoch,
                    message_created_epoch,
                    updated_epoch,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_sync_state(
        self, guild_id: int, channel_id: int
    ) -> ActivitySyncState | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT guild_id, channel_id, newest_processed_message_id,
                       newest_processed_message_created_epoch, history_from_epoch,
                       completed_epoch, updated_epoch
                FROM activity_sync_state
                WHERE guild_id=? AND channel_id=?
                """,
                (guild_id, channel_id),
            ).fetchone()
        return None if row is None else ActivitySyncState(*row)

    def mark_backfill_started(
        self, guild_id: int, channel_id: int, updated_epoch: int
    ) -> None:
        _require_integer_epochs(updated_epoch=updated_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO activity_sync_state(
                        guild_id, channel_id, completed_epoch, updated_epoch
                    ) VALUES (?, ?, NULL, ?)
                    ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                        completed_epoch=NULL,
                        updated_epoch=excluded.updated_epoch
                    """,
                    (guild_id, channel_id, updated_epoch),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def mark_backfill_completed(
        self, guild_id: int, channel_id: int, completed_epoch: int
    ) -> None:
        _require_integer_epochs(completed_epoch=completed_epoch)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO activity_sync_state(
                        guild_id, channel_id, completed_epoch, updated_epoch
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                        completed_epoch=excluded.completed_epoch,
                        updated_epoch=excluded.updated_epoch
                    """,
                    (guild_id, channel_id, completed_epoch, completed_epoch),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def daily_types(
        self, guild_id: int, user_id: int, event_date_kst: str
    ) -> set[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT event_type
                FROM sod_eod_daily
                WHERE guild_id=? AND user_id=? AND event_date_kst=?
                """,
                (guild_id, user_id, event_date_kst),
            )
            return {row[0] for row in rows}

    @staticmethod
    def _replace_unset(
        old: ActivityConfig,
        target_role_id: int | None | object,
        reading_category_id: int | None | object,
        study_category_id: int | None | object,
        sod_eod_channel_id: int | None | object,
    ) -> ActivityConfig:
        return dataclasses.replace(
            old,
            target_role_id=(
                old.target_role_id if target_role_id is _UNSET else target_role_id
            ),
            reading_category_id=(
                old.reading_category_id
                if reading_category_id is _UNSET
                else reading_category_id
            ),
            study_category_id=(
                old.study_category_id if study_category_id is _UNSET else study_category_id
            ),
            sod_eod_channel_id=(
                old.sod_eod_channel_id if sod_eod_channel_id is _UNSET else sod_eod_channel_id
            ),
        )

    @staticmethod
    def _voice_core_changed(old: ActivityConfig, new: ActivityConfig) -> bool:
        return (
            old.target_role_id,
            old.reading_category_id,
            old.study_category_id,
        ) != (
            new.target_role_id,
            new.reading_category_id,
            new.study_category_id,
        )

    @staticmethod
    def _get_config_in_tx(conn: sqlite3.Connection, guild_id: int) -> ActivityConfig:
        row = conn.execute(
            """
            SELECT guild_id, target_role_id, reading_category_id, study_category_id,
                   sod_eod_channel_id, voice_collection_started_epoch
            FROM activity_config
            WHERE guild_id=?
            """,
            (guild_id,),
        ).fetchone()
        if row is None:
            return ActivityConfig(guild_id, None, None, None, None, None)
        return ActivityConfig(*row)

    @staticmethod
    def _close_open_rows_in_tx(
        conn: sqlite3.Connection, guild_id: int, effective_at_epoch: int, reason: str
    ) -> None:
        session_reason = "reconciled" if reason == "graceful_shutdown" else reason
        conn.execute(
            """
            UPDATE voice_sessions
            SET ended_epoch=?, closed_reason=?
            WHERE guild_id=? AND ended_epoch IS NULL
            """,
            (effective_at_epoch, session_reason, guild_id),
        )
        conn.execute(
            """
            UPDATE voice_collection_runs
            SET ended_epoch=?, ended_reason=?
            WHERE guild_id=? AND ended_epoch IS NULL
            """,
            (effective_at_epoch, reason, guild_id),
        )

    @staticmethod
    def _get_open_session_in_tx(
        conn: sqlite3.Connection, guild_id: int, user_id: int
    ) -> tuple[int, str] | None:
        return conn.execute(
            """
            SELECT id, activity_kind
            FROM voice_sessions
            WHERE guild_id=? AND user_id=? AND ended_epoch IS NULL
            """,
            (guild_id, user_id),
        ).fetchone()

    @staticmethod
    def _insert_open_session_in_tx(
        conn: sqlite3.Connection,
        guild_id: int,
        user_id: int,
        activity_kind: str,
        started_epoch: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO voice_sessions(
                guild_id, user_id, activity_kind, started_epoch,
                last_checkpoint_epoch
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, activity_kind, started_epoch, started_epoch),
        )

    @staticmethod
    def _validated_event_types(event_types: set[str]) -> frozenset[str]:
        kinds = frozenset(event_types)
        if not kinds.issubset({"sod", "eod"}):
            raise ValueError("알 수 없는 SoD/EoD 유형입니다.")
        return kinds

    @staticmethod
    def _assert_current_channel_in_tx(
        conn: sqlite3.Connection,
        guild_id: int,
        channel_id: int,
        expected_current_channel_id: int,
    ) -> None:
        row = conn.execute(
            "SELECT sod_eod_channel_id FROM activity_config WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        if (
            row is None
            or row[0] != expected_current_channel_id
            or channel_id != expected_current_channel_id
        ):
            raise ChannelChanged(channel_id)

    @staticmethod
    def _insert_event_and_daily_in_tx(
        conn: sqlite3.Connection,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
        message_created_epoch: int,
        event_types: frozenset[str],
    ) -> None:
        event_date_kst = kst_day_for_epoch(message_created_epoch)
        for event_type in sorted(event_types):
            conn.execute(
                """
                INSERT OR IGNORE INTO sod_eod_events(
                    message_id, guild_id, user_id, event_date_kst, event_type,
                    message_created_epoch, channel_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    guild_id,
                    user_id,
                    event_date_kst,
                    event_type,
                    message_created_epoch,
                    channel_id,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sod_eod_daily(
                    guild_id, user_id, event_date_kst, event_type
                ) VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, event_date_kst, event_type),
            )

    @staticmethod
    def _advance_cursor_in_tx(
        conn: sqlite3.Connection,
        guild_id: int,
        channel_id: int,
        message_id: int,
        newest_processed_message_created_epoch: int,
        message_created_epoch: int,
        updated_epoch: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO activity_sync_state(
                guild_id, channel_id, newest_processed_message_id,
                newest_processed_message_created_epoch, history_from_epoch,
                updated_epoch
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                newest_processed_message_id=CASE
                    WHEN activity_sync_state.newest_processed_message_created_epoch
                             IS NULL
                      OR excluded.newest_processed_message_created_epoch
                             > activity_sync_state.newest_processed_message_created_epoch
                      OR (
                          excluded.newest_processed_message_created_epoch
                              = activity_sync_state.newest_processed_message_created_epoch
                          AND excluded.newest_processed_message_id
                              > activity_sync_state.newest_processed_message_id
                      )
                    THEN excluded.newest_processed_message_id
                    ELSE activity_sync_state.newest_processed_message_id
                END,
                newest_processed_message_created_epoch=CASE
                    WHEN activity_sync_state.newest_processed_message_created_epoch
                             IS NULL
                      OR excluded.newest_processed_message_created_epoch
                             > activity_sync_state.newest_processed_message_created_epoch
                      OR (
                          excluded.newest_processed_message_created_epoch
                              = activity_sync_state.newest_processed_message_created_epoch
                          AND excluded.newest_processed_message_id
                              > activity_sync_state.newest_processed_message_id
                      )
                    THEN excluded.newest_processed_message_created_epoch
                    ELSE activity_sync_state.newest_processed_message_created_epoch
                END,
                history_from_epoch=CASE
                    WHEN activity_sync_state.history_from_epoch IS NULL
                    THEN excluded.history_from_epoch
                    ELSE MIN(
                        activity_sync_state.history_from_epoch,
                        excluded.history_from_epoch
                    )
                END,
                updated_epoch=excluded.updated_epoch
            """,
            (
                guild_id,
                channel_id,
                message_id,
                newest_processed_message_created_epoch,
                message_created_epoch,
                updated_epoch,
            ),
        )

    @staticmethod
    def _transition_sod_period_in_tx(
        conn: sqlite3.Connection,
        guild_id: int,
        old_channel_id: int | None,
        new_channel_id: int | None,
        effective_at_epoch: int,
        close_reason: str = "channel_changed",
    ) -> None:
        if old_channel_id is not None:
            conn.execute(
                """
                UPDATE sod_eod_channel_periods
                SET ended_epoch=?, ended_reason=?
                WHERE guild_id=? AND ended_epoch IS NULL
                """,
                (effective_at_epoch, close_reason, guild_id),
            )
        if new_channel_id is not None:
            conn.execute(
                """
                INSERT INTO sod_eod_channel_periods(guild_id, channel_id, started_epoch)
                VALUES (?, ?, ?)
                """,
                (guild_id, new_channel_id, effective_at_epoch),
            )
            conn.execute(
                """
                INSERT INTO activity_sync_state(guild_id, channel_id, updated_epoch)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, channel_id) DO NOTHING
                """,
                (guild_id, new_channel_id, effective_at_epoch),
            )

    @staticmethod
    def _upsert_config_in_tx(
        conn: sqlite3.Connection, config: ActivityConfig, effective_at_epoch: int
    ) -> None:
        conn.execute(
            """
            INSERT INTO activity_config(
                guild_id, target_role_id, reading_category_id, study_category_id,
                sod_eod_channel_id, voice_collection_started_epoch, created_epoch, updated_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                target_role_id=excluded.target_role_id,
                reading_category_id=excluded.reading_category_id,
                study_category_id=excluded.study_category_id,
                sod_eod_channel_id=excluded.sod_eod_channel_id,
                voice_collection_started_epoch=excluded.voice_collection_started_epoch,
                updated_epoch=excluded.updated_epoch
            """,
            (
                config.guild_id,
                config.target_role_id,
                config.reading_category_id,
                config.study_category_id,
                config.sod_eod_channel_id,
                config.voice_collection_started_epoch,
                effective_at_epoch,
                effective_at_epoch,
            ),
        )
