import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from contextlib import closing
from pathlib import Path


KST = timezone(timedelta(hours=9), "KST")


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
