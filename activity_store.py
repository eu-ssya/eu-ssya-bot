import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from contextlib import closing
from pathlib import Path


KST = timezone(timedelta(hours=9), "KST")
_UNSET = object()


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
CREATE TABLE IF NOT EXISTS voice_sessions (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,activity_kind TEXT NOT NULL CHECK(activity_kind IN ('reading_room','study')),started_epoch INTEGER NOT NULL,last_checkpoint_epoch INTEGER NOT NULL,ended_epoch INTEGER,closed_reason TEXT CHECK(closed_reason IN ('normal','category_change','role_removed','config_changed','reconciled','gateway_disconnect','restart_checkpoint')),CHECK(last_checkpoint_epoch >= started_epoch),CHECK((ended_epoch IS NULL AND closed_reason IS NULL) OR (ended_epoch IS NOT NULL AND closed_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch));
CREATE TABLE IF NOT EXISTS voice_collection_runs (id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,started_epoch INTEGER NOT NULL,last_checkpoint_epoch INTEGER NOT NULL,ended_epoch INTEGER,ended_reason TEXT CHECK(ended_reason IN ('config_changed','config_invalid','graceful_shutdown','gateway_disconnect','restart_checkpoint')),CHECK(last_checkpoint_epoch >= started_epoch),CHECK((ended_epoch IS NULL AND ended_reason IS NULL) OR (ended_epoch IS NOT NULL AND ended_reason IS NOT NULL)),CHECK(ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch));
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
        conn.execute(
            """
            UPDATE voice_sessions
            SET ended_epoch=?, closed_reason=?
            WHERE guild_id=? AND ended_epoch IS NULL
            """,
            (effective_at_epoch, reason, guild_id),
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
    def _transition_sod_period_in_tx(
        conn: sqlite3.Connection,
        guild_id: int,
        old_channel_id: int | None,
        new_channel_id: int | None,
        effective_at_epoch: int,
    ) -> None:
        if old_channel_id is not None:
            conn.execute(
                """
                UPDATE sod_eod_channel_periods
                SET ended_epoch=?, ended_reason='channel_changed'
                WHERE guild_id=? AND ended_epoch IS NULL
                """,
                (effective_at_epoch, guild_id),
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
