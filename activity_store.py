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
class CoverageSummary:
    covered: list[tuple[int, int]]
    gaps: list[tuple[int, int]]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activity_config (guild_id INTEGER PRIMARY KEY,target_role_id INTEGER,reading_category_id INTEGER,study_category_id INTEGER,sod_eod_channel_id INTEGER,voice_collection_started_epoch INTEGER,created_epoch INTEGER NOT NULL,updated_epoch INTEGER NOT NULL,CHECK(reading_category_id IS NULL OR study_category_id IS NULL OR reading_category_id <> study_category_id));
CREATE TABLE IF NOT EXISTS voice_sessions (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,activity_kind TEXT NOT NULL CHECK(activity_kind IN ('reading_room','study')),started_epoch INTEGER NOT NULL,last_checkpoint_epoch INTEGER NOT NULL,ended_epoch INTEGER,closed_reason TEXT CHECK(closed_reason IN ('normal','category_change','role_removed','config_changed','reconciled','gateway_disconnect','restart_checkpoint')),CHECK(typeof(started_epoch)='integer'),CHECK(typeof(last_checkpoint_epoch)='integer'),CHECK(ended_epoch IS NULL OR typeof(ended_epoch)='integer'),CHECK(last_checkpoint_epoch >= started_epoch),CHECK((ended_epoch IS NULL AND closed_reason IS NULL) OR (ended_epoch IS NOT NULL AND closed_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch));
CREATE TABLE IF NOT EXISTS voice_collection_runs (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,started_epoch INTEGER NOT NULL,last_checkpoint_epoch INTEGER NOT NULL,ended_epoch INTEGER,ended_reason TEXT CHECK(ended_reason IN ('config_changed','config_invalid','graceful_shutdown','gateway_disconnect','restart_checkpoint')),CHECK(typeof(started_epoch)='integer'),CHECK(typeof(last_checkpoint_epoch)='integer'),CHECK(ended_epoch IS NULL OR typeof(ended_epoch)='integer'),CHECK(last_checkpoint_epoch >= started_epoch),CHECK((ended_epoch IS NULL AND ended_reason IS NULL) OR (ended_epoch IS NOT NULL AND ended_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch));
CREATE TABLE IF NOT EXISTS sod_eod_events (message_id INTEGER NOT NULL,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,event_date_kst TEXT NOT NULL,event_type TEXT NOT NULL CHECK(event_type IN ('sod','eod')),message_created_epoch INTEGER NOT NULL,channel_id INTEGER NOT NULL,PRIMARY KEY(message_id,event_type));
CREATE TABLE IF NOT EXISTS sod_eod_daily (guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,event_date_kst TEXT NOT NULL,event_type TEXT NOT NULL CHECK(event_type IN ('sod','eod')),PRIMARY KEY(guild_id,user_id,event_date_kst,event_type));
CREATE TABLE IF NOT EXISTS activity_sync_state (guild_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,newest_processed_message_id INTEGER,newest_processed_message_created_epoch INTEGER,history_from_epoch INTEGER,completed_epoch INTEGER,updated_epoch INTEGER NOT NULL,PRIMARY KEY(guild_id,channel_id));
CREATE TABLE IF NOT EXISTS sod_eod_channel_periods (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,started_epoch INTEGER NOT NULL,ended_epoch INTEGER,ended_reason TEXT CHECK(ended_reason IN ('channel_changed','config_invalid')),CHECK((ended_epoch IS NULL AND ended_reason IS NULL) OR (ended_epoch IS NOT NULL AND ended_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= started_epoch));
CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_sessions_one_open_per_member ON voice_sessions(guild_id,user_id) WHERE ended_epoch IS NULL;
CREATE INDEX IF NOT EXISTS idx_voice_sessions_report ON voice_sessions(guild_id,user_id,activity_kind,started_epoch,ended_epoch);
CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_collection_runs_one_open ON voice_collection_runs(guild_id) WHERE ended_epoch IS NULL;
CREATE INDEX IF NOT EXISTS idx_voice_collection_runs_coverage ON voice_collection_runs(guild_id,started_epoch,ended_epoch);
CREATE INDEX IF NOT EXISTS idx_sod_eod_events_report ON sod_eod_events(guild_id,channel_id,user_id,event_date_kst,event_type);
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
