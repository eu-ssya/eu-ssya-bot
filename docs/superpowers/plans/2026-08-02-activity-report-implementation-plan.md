# 활동 현황 보고서 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 현재 으쌰으쌰 역할의 사람 멤버 활동을 운영자 전용 보고서와 TXT로 제공한다.

**Architecture:** activity_store.py는 SQLite 저장·집계, activity_cog.py는 Discord 명령·이벤트·View, bot.py는 intent·확장 로드를 맡는다.

**Tech Stack:** Python 3.13, discord.py 2.4 이상 3 미만, sqlite3, unittest, asyncio, Fly Machines/Volumes.

## Global Constraints

- requirements.txt는 변경하지 않으며 새 패키지를 설치하지 않는다.
- DB는 ACTIVITY_DB_PATH 또는 /data/activity.db를 쓰며 timestamp는 UTC epoch INTEGER, KST 날짜는 YYYY-MM-DD TEXT다.
- SQLite connection은 asyncio.to_thread 호출 안에서 열고 WAL/busy_timeout을 설정한 뒤 같은 호출에서 닫는다. 테스트는 :memory: 대신 TemporaryDirectory 실파일을 사용한다.
- 메시지 본문·음성 내용·발화·녹음·화면 공유·카메라 상태는 저장하거나 출력하지 않는다.
- 현재 대상 역할 non-bot 멤버 전원을 기록 0명까지 보고한다.
- 자동 점수·판정·경고·강퇴와 최소 체류시간은 만들지 않는다.
- Group은 guild-only/Administrator이며 button은 클릭 시점 Administrator와 최초 실행자 ID를 재확인하고 ephemeral로만 응답한다.
- Server Members Intent와 Message Content Intent는 배포 전에 Portal에서 켜고 코드에는 members, message_content, voice_states intent를 모두 켠다.
- baseline: verify_wallet.py PASS, verify_load_data.py PASS, verify_final.py AC4 overdraft assertion FAIL. wallet 코드와 baseline은 수정하지 않는다.
- Fly 지속성은 실제 machine/volume 결과와 restart 뒤 activity.db 재조회로 판정한다.

---

## File structure and responsibilities

| Path | Change | Responsibility |
| --- | --- | --- |
| activity_store.py | Create | schema, config, sessions, events, aggregation |
| activity_cog.py | Create | commands, gateway events, backfill, pagination |
| tests/test_activity_store.py | Create | TemporaryDirectory SQLite tests |
| tests/test_activity_cog.py | Create | IsolatedAsyncioTestCase Cog tests |
| tests/activity_fixtures.py | Create | Fake Discord object, FakeBot, interaction, configured Cog fixture |
| bot.py | Modify | intents and activity load isolation |
| Dockerfile | Modify | activity modules copy |
| fly.toml | Modify | ACTIVITY_DB_PATH |
| .gitignore | Modify | activity.db artifacts exclude |
| .dockerignore | Create/Modify | DB artifacts exclude |
| README.md | Modify | authority, privacy, operation docs |
| docs/superpowers/specs/2026-08-02-activity-report-manual-checklist.md | Create | Discord/Fly manual smoke |


---

### Task 1: SQLite schema and KST time primitives

**Review gate:** schema initialization is idempotent, all seven approved tables and indexes exist, and the store has no discord import.

**Files:**
- Create: activity_store.py
- Create: tests/__init__.py
- Create: tests/test_activity_store.py

**Interfaces:**
- Produces: ActivityStore.initialize(), kst_day_for_epoch(epoch: int) -> str, kst_range_to_epoch(start: date, end: date) -> tuple[int, int], ActivityConfig, ReportMember, CoverageSummary.

- [ ] **Step 1: Write the failing schema/time test**

~~~python
class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ActivityStore(str(Path(self.tmp.name) / "activity.db"))
    def tearDown(self):
        self.tmp.cleanup()
    def test_schema_is_idempotent(self):
        self.store.initialize(); self.store.initialize()
        self.assertEqual(set(self.store.table_names()), {
            "activity_config", "voice_sessions", "voice_collection_runs",
            "sod_eod_events", "sod_eod_daily", "activity_sync_state",
            "sod_eod_channel_periods"})
    def test_kst_day_range_includes_end_date(self):
        start, end = kst_range_to_epoch(date(2026, 8, 2), date(2026, 8, 2))
        self.assertEqual(end - start, 86400)
        self.assertEqual(kst_day_for_epoch(start), "2026-08-02")
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store.SchemaTests -v

Expected: FAIL with ModuleNotFoundError for activity_store.

- [ ] **Step 3: Implement connection lifecycle and schema**

~~~python
def _connect(self):
    Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(self.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def initialize(self):
    with self._connect() as conn:
        conn.executescript(SCHEMA_SQL)
~~~

~~~python
import dataclasses
from dataclasses import dataclass

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
~~~

Define these three dataclasses in activity_store.py directly after the timezone helpers and before ActivityStore. Use this complete SCHEMA_SQL value:

~~~sql
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
~~~

- [ ] **Step 4: Run green test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store.SchemaTests -v; & "venv\Scripts\python.exe" -m py_compile activity_store.py

Expected: 2 tests PASS and compile exits 0.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_store.py tests\__init__.py tests\test_activity_store.py
git commit -m "feat: 활동 SQLite 스키마와 시간 유틸 추가"
~~~

---

### Task 2: Atomic nullable configuration and SoD/EoD periods

**Review gate:** partial settings accept any order, invalid category equality rolls back, and A→B→A retains three historical channel periods.

**Files:**
- Modify: activity_store.py
- Modify: tests/test_activity_store.py

**Interfaces:**
- Consumes: Task 1 schema.
- Produces: ActivityConfig, get_config(), apply_config_change(), list_channel_periods().

- [ ] **Step 1: Write failing configuration tests**

~~~python
def test_partial_config_and_invalid_rollback(self):
    self.store.apply_config_change(1, target_role_id=10, effective_at_epoch=100)
    with self.assertRaises(ValueError):
        self.store.apply_config_change(1, reading_category_id=20,
                                       study_category_id=20, effective_at_epoch=110)
    self.assertEqual(self.store.get_config(1).target_role_id, 10)
    self.assertIsNone(self.store.get_config(1).reading_category_id)

def test_channel_period_a_b_a(self):
    self.store.apply_config_change(1, sod_eod_channel_id=40, effective_at_epoch=100)
    self.store.apply_config_change(1, sod_eod_channel_id=41, effective_at_epoch=200)
    self.store.apply_config_change(1, sod_eod_channel_id=40, effective_at_epoch=300)
    self.assertEqual(self.store.list_channel_periods(1),
                     [(40, 100, 200), (41, 200, 300), (40, 300, None)])

def test_collection_started_epoch_is_written_once(self):
    self.store.apply_config_change(1, target_role_id=10, effective_at_epoch=100)
    self.store.apply_config_change(1, reading_category_id=20, effective_at_epoch=110)
    first = self.store.apply_config_change(1, study_category_id=30, effective_at_epoch=120)
    later = self.store.apply_config_change(1, reading_category_id=21, effective_at_epoch=130)
    self.assertEqual(first.voice_collection_started_epoch, 120)
    self.assertEqual(later.voice_collection_started_epoch, 120)
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: FAIL because configuration APIs do not exist.

- [ ] **Step 3: Implement BEGIN IMMEDIATE transition**

~~~python
_UNSET = object()

def apply_config_change(self, guild_id, *, effective_at_epoch, target_role_id=_UNSET,
                        reading_category_id=_UNSET, study_category_id=_UNSET,
                        sod_eod_channel_id=_UNSET):
    with self._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = self._get_config_in_tx(conn, guild_id)
        new = self._replace_unset(old, target_role_id, reading_category_id,
                                  study_category_id, sod_eod_channel_id)
        if new.reading_category_id is not None and new.reading_category_id == new.study_category_id:
            raise ValueError("독서실과 스터디 카테고리는 서로 달라야 합니다.")
        if old.voice_collection_started_epoch is None and new.voice_is_complete:
            new = dataclasses.replace(new, voice_collection_started_epoch=effective_at_epoch)
        if self._voice_core_changed(old, new):
            self._close_open_rows_in_tx(conn, guild_id, effective_at_epoch, "config_changed")
        if old.sod_eod_channel_id != new.sod_eod_channel_id:
            self._transition_sod_period_in_tx(conn, guild_id, old.sod_eod_channel_id,
                                               new.sod_eod_channel_id, effective_at_epoch)
        self._upsert_config_in_tx(conn, new, effective_at_epoch)
        return new
~~~

Voice-core means target role, reading category, study category. When the new config first becomes voice_is_complete and old.voice_collection_started_epoch is null, write effective_at_epoch as voice_collection_started_epoch and preserve it on all later changes. For first valid SoD/EoD channel open a period and create channel sync state. A change closes only the old period with channel_changed and opens the new one. A deleted/inaccessible channel uses config_invalid. A SoD/EoD-only change must not close a voice session or collection run.

- [ ] **Step 4: Run green configuration test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: all Task 1–2 tests PASS.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_store.py tests\test_activity_store.py
git commit -m "feat: 활동 설정과 채널 기간 원자 저장 추가"
~~~

---

### Task 3: Voice session store, clipping, and collection coverage

**Review gate:** one member has at most one open session, same-kind movement is continuous, kind transitions split at one instant, and unknown time becomes a gap.

**Files:**
- Modify: activity_store.py
- Modify: tests/test_activity_store.py

**Interfaces:**
- Produces: reconcile_session(), open_collection_run(), close_open_rows(), checkpoint_open_rows(), voice_seconds_for_range(), voice_coverage_for_range().

- [ ] **Step 1: Write failing session tests**

~~~python
def test_kind_transition_and_clip(self):
    self.store.reconcile_session(1, 2, "reading_room", 100)
    self.store.reconcile_session(1, 2, "reading_room", 130)
    self.store.reconcile_session(1, 2, "study", 160)
    self.store.reconcile_session(1, 2, None, 220, close_reason="normal")
    self.assertEqual(self.store.list_sessions(1, 2), [
        ("reading_room", 100, 160, "category_change"),
        ("study", 160, 220, "normal")])
    self.assertEqual(self.store.voice_seconds_for_range(1, 2, "study", 170, 200), 30)

def test_disconnect_creates_exact_gap(self):
    self.store.open_collection_run(1, 100)
    self.store.close_open_rows(1, 160, "gateway_disconnect")
    self.store.open_collection_run(1, 200)
    self.assertEqual(self.store.voice_coverage_for_range(1, 100, 240).gaps, [(160, 200)])

def test_open_session_unique_race_rechecks_after_injected_duplicate(self):
    with self.store._connect() as conn:
        conn.execute("INSERT INTO voice_sessions(guild_id,user_id,activity_kind,started_epoch,last_checkpoint_epoch) VALUES(1,2,'study',90,90)")
    original_get = self.store._get_open_session_in_tx
    unique = sqlite3.IntegrityError("UNIQUE constraint failed")
    unique.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
    unique.sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"
    calls = iter([None])
    def stale_then_actual(conn, guild_id, user_id):
        try: return next(calls)
        except StopIteration: return original_get(conn, guild_id, user_id)
    with mock.patch.object(self.store, "_get_open_session_in_tx",
        side_effect=stale_then_actual), mock.patch.object(
            self.store, "_insert_open_session_in_tx", side_effect=unique):
        self.store.reconcile_session(1, 2, "study", 100)
    self.assertEqual(self.store.open_session_count(1, 2), 1)

def test_unique_race_different_kind_transitions_and_check_error_reraises(self):
    with self.store._connect() as conn:
        conn.execute("INSERT INTO voice_sessions(guild_id,user_id,activity_kind,started_epoch,last_checkpoint_epoch) VALUES(1,2,'study',90,90)")
    original_get = self.store._get_open_session_in_tx
    original_insert = self.store._insert_open_session_in_tx
    unique = sqlite3.IntegrityError("UNIQUE constraint failed")
    unique.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
    unique.sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"
    reads = iter([None])
    inserts = iter([unique, None])
    def stale_then_actual(conn, guild_id, user_id):
        try: return next(reads)
        except StopIteration: return original_get(conn, guild_id, user_id)
    def unique_then_real(*args):
        outcome = next(inserts)
        if outcome is not None: raise outcome
        return original_insert(*args)
    with mock.patch.object(self.store, "_get_open_session_in_tx", side_effect=stale_then_actual), \
         mock.patch.object(self.store, "_insert_open_session_in_tx",
             side_effect=unique_then_real):
        self.store.reconcile_session(1, 2, "reading_room", 100)
    self.assertEqual(self.store.list_sessions(1, 2), [
        ("study", 90, 100, "category_change"),
        ("reading_room", 100, None, None)])
    bad = sqlite3.IntegrityError("CHECK constraint failed")
    bad.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_CHECK
    with mock.patch.object(self.store, "_insert_open_session_in_tx", side_effect=bad):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reconcile_session(1, 3, "study", 100)

def test_graceful_shutdown_uses_session_and_run_reason_sets(self):
    self.store.reconcile_session(1, 2, "study", 100)
    self.store.open_collection_run(1, 100)
    self.store.close_open_rows(1, 120, "graceful_shutdown")
    self.assertEqual(self.store.list_sessions(1, 2)[0][3], "reconciled")
    self.assertEqual(self.store.list_runs(1)[0][3], "graceful_shutdown")
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: FAIL because voice APIs do not exist.

- [ ] **Step 3: Implement atomic voice state operation**

~~~python
def reconcile_session(self, guild_id, user_id, desired_kind, effective_at_epoch,
                      close_reason="reconciled"):
    with self._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = self._get_open_session_in_tx(conn, guild_id, user_id)
        if row and (desired_kind is None or row[1] != desired_kind):
            reason = "category_change" if desired_kind else close_reason
            conn.execute("UPDATE voice_sessions SET ended_epoch=?, closed_reason=? WHERE id=?",
                         (effective_at_epoch, reason, row[0]))
        if desired_kind and (row is None or row[1] != desired_kind):
            try:
                self._insert_open_session_in_tx(conn, guild_id, user_id, desired_kind, effective_at_epoch)
            except sqlite3.IntegrityError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                    raise
                retry = self._get_open_session_in_tx(conn, guild_id, user_id)
                if retry is None:
                    raise
                if retry[1] == desired_kind:
                    return
                conn.execute("UPDATE voice_sessions SET ended_epoch=?,closed_reason='category_change' WHERE id=?",
                             (effective_at_epoch, retry[0]))
                self._insert_open_session_in_tx(conn, guild_id, user_id, desired_kind, effective_at_epoch)
~~~

Catch only the partial-unique open-session IntegrityError; re-query open row, same kind is no-op, different kind closes/reopens, and every other IntegrityError re-raises. Use overlap as MAX(0, MIN(COALESCE(ended_epoch, range_end), range_end) - MAX(started_epoch, range_start)). Count a session only when that result is positive. Merge collection runs before calculating complement gaps. Current open session uses query end as temporary end; checkpoint does not become a report start. close_open_rows maps graceful_shutdown to session closed_reason reconciled and run ended_reason graceful_shutdown because graceful_shutdown is not a valid session reason.

Define seams _get_open_session_in_tx(conn,guild_id,user_id) and _insert_open_session_in_tx(conn,...). In the race test patch first _get_open_session_in_tx to return None, patch _insert_open_session_in_tx to raise sqlite3.IntegrityError with sqlite_errorcode SQLITE_CONSTRAINT_UNIQUE and sqlite_errorname SQLITE_CONSTRAINT_UNIQUE, then patch the second get to return the competing open session; assert convergence. A second test injects SQLITE_CONSTRAINT_CHECK and asserts it is re-raised.

- [ ] **Step 4: Run green voice test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: PASS; gap is exactly 160–200 and zero-length overlap is 0.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_store.py tests\test_activity_store.py
git commit -m "feat: 활동 음성 세션과 수집 구간 저장 추가"
~~~

---

### Task 4: SoD/EoD event, daily, and channel cursor store

**Review gate:** a message can contain both types, daily type dedupes, and event/daily/cursor all roll back together.

**Files:**
- Modify: activity_store.py
- Modify: tests/test_activity_store.py

**Interfaces:**
- Produces: record_live_message(), record_backfill_message_and_advance_cursor(), get_sync_state(), mark_backfill_completed(), daily_types(), ChannelChanged.

- [ ] **Step 1: Write failing transaction test**

~~~python
def test_event_daily_cursor_are_atomic(self):
    self.store.apply_config_change(1, sod_eod_channel_id=2, effective_at_epoch=99)
    with self.assertRaises(ValueError):
        self.store.record_backfill_message_and_advance_cursor(
            guild_id=1, channel_id=2, message_id=3, user_id=4,
            message_created_epoch=100, event_types={"bad"},
            newest_processed_message_created_epoch=100, updated_epoch=101,
            expected_current_channel_id=2)
    self.assertIsNone(self.store.get_sync_state(1, 2).newest_processed_message_id)
    self.store.record_backfill_message_and_advance_cursor(
        guild_id=1, channel_id=2, message_id=3, user_id=4,
        message_created_epoch=100, event_types={"sod", "eod"},
        newest_processed_message_created_epoch=100, updated_epoch=101,
        expected_current_channel_id=2)
    self.assertEqual(self.store.daily_types(1, 4, "1970-01-01"), {"sod", "eod"})
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: FAIL because event APIs do not exist.

- [ ] **Step 3: Implement one message transaction**

~~~python
def record_live_message(self, *, guild_id, channel_id, message_id, user_id,
                        message_created_epoch, event_types, updated_epoch,
                        expected_current_channel_id):
    with self._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT sod_eod_channel_id FROM activity_config WHERE guild_id=?",
                               (guild_id,)).fetchone()
        if current is None or current[0] != expected_current_channel_id:
            raise ChannelChanged(channel_id)
        self._insert_event_and_daily_in_tx(conn, guild_id, channel_id, message_id,
                                           user_id, message_created_epoch, event_types)

def record_backfill_message_and_advance_cursor(self, *, guild_id, channel_id, message_id,
                                               user_id, message_created_epoch, event_types,
                                               newest_processed_message_created_epoch,
                                               updated_epoch, expected_current_channel_id):
    if not event_types.issubset({"sod", "eod"}):
        raise ValueError("알 수 없는 SoD/EoD 유형입니다.")
    with self._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT sod_eod_channel_id FROM activity_config WHERE guild_id=?",
                               (guild_id,)).fetchone()
        if current is None or current[0] != expected_current_channel_id:
            raise ChannelChanged(channel_id)
        for kind in event_types:
            conn.execute("INSERT OR IGNORE INTO sod_eod_events VALUES(?,?,?,?,?,?,?)",
                         (message_id, guild_id, user_id, kst_day_for_epoch(message_created_epoch),
                          kind, message_created_epoch, channel_id))
            conn.execute("INSERT OR IGNORE INTO sod_eod_daily VALUES(?,?,?,?)",
                         (guild_id, user_id, kst_day_for_epoch(message_created_epoch), kind))
        self._advance_cursor_in_tx(conn, guild_id, channel_id, message_id,
                                   newest_processed_message_created_epoch, updated_epoch)
~~~

record_live_message is the only method on_message may call, so live events cannot move a backfill marker. It too compares expected_current_channel_id after BEGIN IMMEDIATE and before every event/daily write, so eligibility checked in A cannot write after A→B; mismatch raises ChannelChanged with zero writes. record_backfill_message_and_advance_cursor is the only method allowed to write activity_sync_state. Marker-less scan advances for no-match and ineligible messages too. In the same transaction update history_from_epoch to MIN(existing history_from_epoch, message_created_epoch) for every successfully processed first-pass message; mark completed_epoch only after normal iterator completion. The expected-channel comparison occurs after BEGIN IMMEDIATE and before every event/daily/cursor write, so A→B produces ChannelChanged and rolls back to zero writes.

Add tests for a partial first scan, resumed scan, and delta scan: history_from_epoch stays at the earliest committed message, newest marker advances only through committed messages, and completed_epoch remains null after interruption then becomes non-null only after a successful iterator end. Add mark_backfill_started(guild_id, channel_id, updated_epoch), implemented as BEGIN IMMEDIATE setting completed_epoch=NULL before each scan. Add a live eligibility→A→B→record_live_message race test proving no event/daily row is written.

- [ ] **Step 4: Run green event test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: PASS; duplicate live/backfill event insert does not duplicate days.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_store.py tests\test_activity_store.py
git commit -m "feat: SoD EoD 이벤트와 동기화 커서 저장 추가"
~~~


---

### Task 5: Report-store aggregation and source coverage warnings

**Review gate:** all current eligible members including zero records appear; no-record then oldest sorting is deterministic; output warns instead of estimating missing data.

**Files:**
- Modify: activity_store.py
- Modify: tests/test_activity_store.py

**Interfaces:**
- Consumes: Tasks 2–4 persisted rows.
- Produces: ReportMember, ReportRow, ActivityReport, build_report(..., as_of_epoch: int).

- [ ] **Step 1: Write failing aggregation test**

~~~python
def test_zero_member_sort_and_positive_overlap_count(self):
    members = [ReportMember(1, "Zulu"), ReportMember(2, "Alpha"), ReportMember(3, "Bravo")]
    self.store.reconcile_session(1, 3, "study", 100)
    self.store.reconcile_session(1, 3, None, 160, close_reason="normal")
    report = self.store.build_report(guild_id=1, members=members, start_epoch=0, end_epoch=200,
                                     as_of_epoch=200)
    self.assertEqual([row.user_id for row in report.rows], [2, 1, 3])
    self.assertEqual((report.rows[-1].study_seconds, report.rows[-1].study_session_count), (60, 1))

def test_last_activity_uses_latest_preserved_record_outside_query_range(self):
    self.store.reconcile_session(1, 3, "study", 500)
    self.store.reconcile_session(1, 3, None, 520, close_reason="normal")
    report = self.store.build_report(guild_id=1, members=[ReportMember(3, "Bravo")],
                                     start_epoch=0, end_epoch=200, as_of_epoch=600)
    self.assertEqual(report.rows[0].last_activity_epoch, 520)

def test_open_session_last_activity_uses_report_as_of(self):
    self.store.reconcile_session(1, 3, "study", 100)
    report = self.store.build_report(guild_id=1, members=[ReportMember(3, "Bravo")],
                                     start_epoch=0, end_epoch=120, as_of_epoch=700)
    self.assertEqual(report.rows[0].last_activity_epoch, 700)

def test_structured_warnings_cover_voice_and_channel_sources(self):
    report = self.store.build_report(guild_id=1, members=[], start_epoch=100, end_epoch=300,
                                     as_of_epoch=300)
    codes = {warning.code for warning in report.warnings}
    self.assertIn("voice_gap", codes)
    self.assertIn("sod_history_unavailable", codes)
    self.assertFalse(any("추정" in warning.text for warning in report.warnings))
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: FAIL because report types are absent.

- [ ] **Step 3: Implement report rows and warnings**

~~~python
@dataclass(frozen=True)
class ReportRow:
    user_id: int; display_name: str; last_activity_epoch: int | None
    reading_seconds: int; study_seconds: int
    reading_session_count: int; study_session_count: int
    sod_days: int; eod_days: int; combined_days: int

def session_overlap_seconds(started, ended, range_start, range_end):
    return max(0, min(ended if ended is not None else range_end, range_end)
               - max(started, range_start))
~~~

For each ReportMember query type-specific daily counts, the union count of daily dates, clipped session seconds and positive-overlap session counts. build_report receives as_of_epoch at report creation. Last activity is latest exact message-created epoch or closed-session ended_epoch across all preserved records, or as_of_epoch for an open session; it is independent of the selected query range and never a checkpoint. Sort by (last_activity_epoch is not None, last_activity_epoch or 0, display_name.casefold()); an open-only member test fixes this ordering. Build structured warning objects from precise voice run complement gaps and channel period history/completion state, then render those objects identically in page and TXT; they state partial data only and never estimate missing values.

- [ ] **Step 4: Run green report-store test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v

Expected: PASS; a 55-member fixture returns 55 rows.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_store.py tests\test_activity_store.py
git commit -m "feat: 활동 기간 집계와 수집 경고 추가"
~~~

---

### Task 6: Activity extension scaffold and failure isolation

**Review gate:** schema/load failure disables activity commands only; wallet load, RSS loop start, and login still occur.

**Files:**
- Create: activity_cog.py
- Create: tests/test_activity_cog.py
- Create: tests/activity_fixtures.py
- Modify: bot.py

**Interfaces:**
- Consumes: Task 1 ActivityStore and tests.activity_fixtures.
- Produces: setup(bot), ActivityCog, _store_call(), guild locks/gates, bot._load_extensions(bot, rss_start).

- [ ] **Step 1: Write failing load-isolation test**

~~~python
class LoadIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_failure_does_not_skip_wallet_or_rss(self):
        bot = FakeBot(fail_extension="activity_cog")
        await _load_extensions(bot, bot.start_rss)
        self.assertEqual(bot.calls, ["activity_cog", "wallet_cog", "rss_loop.start"])
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.LoadIsolationTests -v

Expected: FAIL because extension seam does not exist.

- [ ] **Step 3: Implement load boundary and intent**

~~~python
# activity_cog.py
async def setup(bot):
    store = ActivityStore(os.getenv("ACTIVITY_DB_PATH", "/data/activity.db"))
    await asyncio.to_thread(store.initialize)
    await bot.add_cog(ActivityCog(bot, store))

# bot.py
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

async def _load_extensions(bot_instance, rss_start):
    try:
        await bot_instance.load_extension("activity_cog")
    except Exception:
        logger.exception("activity extension unavailable; RSS and wallet continue")
    await bot_instance.load_extension("wallet_cog")
    rss_start()

@bot.event
async def setup_hook():
    await _load_extensions(bot, rss_loop.start)
~~~

ActivityCog has one asyncio.Lock around store writes and a defaultdict(asyncio.Lock) for each guild. Every store call is await asyncio.to_thread(method, args) while the store lock is held. Runtime command errors return a short ephemeral error and do not cancel RSS.

~~~python
async def _store_call(self, method, *args, **kwargs):
    async with self.store_lock:
        return await asyncio.to_thread(method, *args, **kwargs)
~~~

Add IsolatedAsyncioTestCase tests with a recording synchronous method for positional args, keyword args, and a method raising sqlite3.Error. Assert positional/keyword values arrive unchanged and the exception reaches the caller unchanged; command callbacks, not _store_call, convert it to ephemeral text.

- [ ] **Step 3a: Define every test fixture used by later tasks**

~~~python
# tests/activity_fixtures.py
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import Mock
import datetime
import inspect
import tempfile
import unittest.mock

import discord
from activity_store import ActivityStore

class FakeBot:
    def __init__(self, fail_extension=None):
        self.fail_extension, self.calls, self.guilds = fail_extension, [], []
    async def load_extension(self, name):
        self.calls.append(name)
        if name == self.fail_extension:
            raise RuntimeError(name)
    def start_rss(self):
        self.calls.append("rss_loop.start")
    async def wait_until_ready(self):
        return None

@dataclass
class FakeResponse:
    sent: list = field(default_factory=list)
    edits: list = field(default_factory=list)
    deferred: bool = False
    done: bool = False
    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append((content, kwargs))
    async def edit_message(self, **kwargs):
        self.done = True
        self.edits.append(kwargs)
    async def defer(self, **kwargs):
        self.deferred, self.done = True, True
    def is_done(self):
        return self.done

def fake_interaction(user_id, administrator, guild=None):
    permissions = SimpleNamespace(administrator=administrator)
    guild = guild if guild is not None else FakeGuild(1)
    followup = FakeResponse()
    return SimpleNamespace(user=SimpleNamespace(id=user_id, guild_permissions=permissions),
                           response=FakeResponse(), files=[], guild=guild, followup=followup)

def fake_deferred_interaction(user_id, administrator, guild=None):
    interaction = fake_interaction(user_id, administrator, guild)
    interaction.original_edits = []
    async def edit_original_response(**kwargs):
        interaction.original_edits.append(kwargs)
    interaction.edit_original_response = edit_original_response
    return interaction

def fake_button():
    return SimpleNamespace(disabled=False)

async def press(view, item, interaction):
    if await view.interaction_check(interaction):
        await item.callback(interaction)

class FakeOriginalResponse:
    def __init__(self): self.edits = []
    async def edit(self, **kwargs): self.edits.append(kwargs)

def fake_message():
    return FakeOriginalResponse()

def configured_fixture():
    return build_fake_configured_cog(target_role_id=10, reading_category_id=20,
                                     study_category_id=30, sod_eod_channel_id=40)

@dataclass
class FakeMember:
    id: int
    guild: object
    role_ids: set[int]
    category_id: int | None = None
    bot: bool = False
    @property
    def roles(self):
        return [SimpleNamespace(id=role_id) for role_id in self.role_ids]
    @property
    def voice(self):
        return SimpleNamespace(channel=SimpleNamespace(category_id=self.category_id))
    def in_category(self, category_id):
        return FakeMember(self.id, self.guild, set(self.role_ids), category_id, self.bot)
    def with_roles(self, role_ids):
        return FakeMember(self.id, self.guild, set(role_ids), self.category_id, self.bot)

class FakeGuild:
    def __init__(self, guild_id):
        self.id, self.members, self.roles, self.channels = guild_id, [], [], []
        self.me = SimpleNamespace(id=999)
    def get_member(self, user_id):
        return next((m for m in self.members if m.id == user_id), None)
    async def fetch_member(self, user_id):
        member = self.get_member(user_id)
        if member is None: raise discord.NotFound(SimpleNamespace(status=404), "member")
        return member
    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)
    def get_channel(self, channel_id):
        return next((c for c in self.channels if c.id == channel_id), None)

def fake_role(role_id, guild):
    value = Mock(spec=discord.Role); value.id, value.guild = role_id, guild
    return value
def fake_category(channel_id, guild, can_read=True):
    value = Mock(spec=discord.CategoryChannel)
    value.id, value.guild = channel_id, guild
    value.permissions_for.return_value = SimpleNamespace(view_channel=can_read, read_message_history=can_read)
    return value
def fake_text_channel(channel_id, guild, can_read=True):
    value = Mock(spec=discord.TextChannel)
    value.id, value.guild = channel_id, guild
    value.permissions_for.return_value = SimpleNamespace(view_channel=can_read, read_message_history=can_read)
    return value

def build_fake_configured_cog(target_role_id, reading_category_id, study_category_id, sod_eod_channel_id):
    tmp = tempfile.TemporaryDirectory()
    store = ActivityStore(str(Path(tmp.name) / "activity.db")); store.initialize()
    guild = FakeGuild(1)
    guild.roles = [fake_role(target_role_id, guild)]
    guild.channels = [fake_category(reading_category_id, guild), fake_category(study_category_id, guild),
                      fake_category(21, guild), fake_text_channel(sod_eod_channel_id, guild)]
    member = FakeMember(1, guild, {target_role_id}, reading_category_id)
    guild.members = [member]
    bot = FakeBot(); bot.guilds = [guild]
    cog = ActivityCog(bot, store); cog._test_tmp = tmp
    store.apply_config_change(1, target_role_id=target_role_id,
                              reading_category_id=reading_category_id,
                              study_category_id=study_category_id,
                              sod_eod_channel_id=sod_eod_channel_id,
                              effective_at_epoch=1)
    cog.collection_gates[1].set()
    store.open_collection_run(1, 1)
    return cog, guild, member

def sample_report():
    return make_report([])

def report_with_members(count):
    return make_report([ReportRow(user_id=i, display_name=str(i), last_activity_epoch=None,
        reading_seconds=0, study_seconds=0, reading_session_count=0, study_session_count=0,
        sod_days=0, eod_days=0, combined_days=0) for i in range(count)])

def report_with_warning(code, text):
    return make_report([], warnings=[CoverageWarning(code=code, text=text)])

def make_report(rows, warnings=None):
    return ActivityReport(rows=rows, warnings=warnings or [], start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2), start_epoch=0, end_epoch=172800, generated_epoch=172800,
        period_label="조회 기간: 2026-08-01 ~ 2026-08-02",
        txt_filename="activity-report-20260801-20260802-kst.txt",
        page_count=max(1, (len(rows) + 14) // 15))

def fixture_channel_with_missing_author():
    author = SimpleNamespace(id=99, bot=False)
    message = SimpleNamespace(id=12, author=author,
                              created_at=datetime.datetime(2026, 8, 2, tzinfo=datetime.UTC),
                              content="SoD")
    class Channel:
        id = 40
        async def history(self, **kwargs):
            yield message
    return Channel()

def make_message(message_id, member, content):
    return SimpleNamespace(id=message_id, author=member, content=content,
        created_at=datetime.datetime(2026, 8, 2, tzinfo=datetime.UTC), type=discord.MessageType.default)

def make_history_channel(guild, channel_id, messages, before_first_yield=None):
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.guild = channel_id, guild
    async def history(**kwargs):
        if before_first_yield is not None:
            result = before_first_yield()
            if inspect.isawaitable(result):
                await result
        for message in messages:
            yield message
    channel.history.side_effect = history
    return channel

def make_controlled_history_channel(guild, channel_id, messages, entered, release):
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.guild = channel_id, guild
    async def history(**kwargs):
        entered.set()
        await release.wait()
        for message in messages:
            yield message
    channel.history.side_effect = history
    return channel

async def prepare_sync_marker(cog, channel_id, message_id):
    await cog._store_call(cog.store.record_backfill_message_and_advance_cursor,
        guild_id=1, channel_id=channel_id, message_id=message_id, user_id=1,
        message_created_epoch=99, event_types=set(),
        newest_processed_message_created_epoch=99, updated_epoch=100,
        expected_current_channel_id=channel_id)
~~~

Define test helper functions in the same fixture module and call these functions from tests instead of attaching undeclared methods to ActivityCog:

~~~python
async def session_rows(cog, user_id):
    return await cog._store_call(cog.store.list_sessions, 1, user_id)
async def voice_rows(cog):
    return await cog._store_call(cog.store.list_sessions_for_guild, 1)
async def open_fixture_session(cog, user_id, kind, started_epoch):
    await cog._store_call(cog.store.reconcile_session, 1, user_id, kind, started_epoch)
async def coverage_gaps(cog, start_epoch, end_epoch):
    coverage = await cog._store_call(cog.store.voice_coverage_for_range, 1, start_epoch, end_epoch)
    return coverage.gaps
async def event_count(cog):
    return await cog._store_call(cog.store.event_count, 1)
async def sync_state(cog, channel_id):
    return await cog._store_call(cog.store.get_sync_state, 1, channel_id)
async def open_session_count(cog, user_id):
    return await cog._store_call(cog.store.open_session_count, 1, user_id)
async def set_reading_for_test(cog, guild, category_id, now_epoch):
    await cog._change_voice_setting(guild, now_epoch, reading_category_id=category_id)
async def set_sod_channel_for_test(cog, guild, channel_id, now_epoch):
    await cog._change_sod_setting(guild, now_epoch, channel_id)
async def on_disconnect_for_test(cog, now_epoch):
    with unittest.mock.patch("activity_cog.utc_now_epoch", return_value=now_epoch):
        await cog.on_disconnect()
async def on_resumed_for_test(cog, now_epoch):
    with unittest.mock.patch("activity_cog.utc_now_epoch", return_value=now_epoch):
        await cog.on_resumed()
async def recover_after_ready_for_test(cog, now_epoch):
    with unittest.mock.patch("activity_cog.utc_now_epoch", return_value=now_epoch):
        await cog.recover_after_ready()
async def record_live_message_for_test(cog, message_id, content):
    config = await cog._store_call(cog.store.get_config, 1)
    await cog._store_call(cog.store.record_live_message, guild_id=1, channel_id=config.sod_eod_channel_id,
                          message_id=message_id, user_id=1, message_created_epoch=100,
                          event_types=detect_sod_eod(content), updated_epoch=101,
                          expected_current_channel_id=config.sod_eod_channel_id)
async def checkpoint_once_for_test(cog):
    await cog._checkpoint_open_rows_once()
async def run_checkpoint(cog, guild_id):
    return await cog._store_call(cog.store.get_open_run_checkpoint, guild_id)
~~~

The test class keeps direct calls to actual ActivityCog methods only for reconcile_member, recover_after_ready, on_disconnect, on_resumed, backfill_current_channel, _change_voice_setting, and _change_sod_setting. Import only the names shown in the tests from tests.activity_fixtures.py. No test may name a helper that is not defined in this module or the test class itself.

- [ ] **Step 4: Run green test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.LoadIsolationTests -v

Expected: PASS.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_cog.py bot.py tests\activity_fixtures.py tests\test_activity_cog.py
git commit -m "feat: 활동 확장 로드 격리 추가"
~~~

---

### Task 7: Settings commands and full reconcile contract

**Review gate:** independent setting order works; each Discord resource is type/guild validated before DB write; voice-core changes full-reconcile; SoD-only change does not touch voice rows.

**Files:**
- Modify: activity_cog.py
- Modify: tests/test_activity_cog.py

**Interfaces:**
- Consumes: Task 2 apply_config_change.
- Produces: /활동설정 대상역할, 독서실, 스터디, sod_eod, 상태.

- [ ] **Step 1: Write failing settings tests**

~~~python
async def test_sod_change_preserves_voice_rows(self):
    cog, guild, member = configured_fixture()
    await cog.reconcile_member(member.in_category(20), 100)
    before = await voice_rows(cog)
    await set_sod_channel_for_test(cog, guild, 41, 120)
    self.assertEqual(await voice_rows(cog), before)

async def test_voice_change_closes_then_reconciles(self):
    cog, guild, member = configured_fixture()
    await cog.reconcile_member(member.in_category(20), 100)
    await set_reading_for_test(cog, guild, 21, 150)
    self.assertEqual(await session_rows(cog, member.id),
                     [("reading_room", 100, 150, "config_changed")])

async def test_status_rejects_non_admin_and_invalid_resource(self):
    cog, guild, member = configured_fixture()
    interaction = fake_interaction(user_id=2, administrator=False, guild=guild)
    await cog.activity_status.callback(cog, interaction)
    self.assertEqual(interaction.response.sent[0][1]["ephemeral"], True)
    guild.roles = []
    admin = fake_interaction(user_id=1, administrator=True, guild=guild)
    await cog.activity_status.callback(cog, admin)
    self.assertIn("대상 역할을 찾을 수 없습니다", admin.response.sent[0][0])
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: FAIL because settings handlers are absent.

- [ ] **Step 3: Implement Group and setting paths**

~~~python
settings_group = app_commands.Group(
    name="활동설정", description="활동 현황 수집 설정", guild_only=True,
    default_permissions=discord.Permissions(administrator=True))

async def require_admin(interaction):
    permissions = getattr(interaction.user, "guild_permissions", None)
    if interaction.guild is None or not bool(permissions and permissions.administrator):
        if interaction.response.is_done():
            await interaction.followup.send("서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        else:
            await interaction.response.send_message("서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return False
    return True

async def _change_voice_setting(self, guild, now_epoch, **change):
    async with self.guild_locks[guild.id]:
        await self._store_call(self.store.apply_config_change, guild.id,
                               effective_at_epoch=now_epoch, **change)
        await self.full_reconcile_guild(guild, now_epoch)

async def _change_sod_setting(self, guild, now_epoch, channel_id):
    async with self.guild_locks[guild.id]:
        await self._store_call(self.store.apply_config_change, guild.id,
                               sod_eod_channel_id=channel_id, effective_at_epoch=now_epoch)
~~~

Every target-role, reading, study, sod_eod, status, and 과거동기화 callback begins with if not await require_admin(interaction): return. The four setters validate interaction.guild.id equals resource.guild.id and resource type before calling the store. Reject same reading/study category. The status callback also re-fetches configured guild.get_role, guild.get_channel category, and guild.get_channel text channel; if one is absent or has wrong type it calls _invalidate_configured_resource(guild, field, utc_now_epoch()) under the guild lock, which sets that ID to null and closes voice rows/config run or SoD period with config_invalid. Status prints only IDs, periods, sync history/completion, open session/run counts and warnings.

- [ ] **Step 4: Run green settings tests**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: PASS; SoD-only before/after voice rows match exactly.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_cog.py tests\test_activity_cog.py
git commit -m "feat: 활동 수집 설정 명령 추가"
~~~

---

### Task 8: Live voice listeners and desired-state reconcile

**Review gate:** voice updates, role adds/removals, guild availability, and valid configuration changes converge through reconcile_member; target-role non-bot filter holds.

**Files:**
- Modify: activity_cog.py
- Modify: tests/test_activity_cog.py

**Interfaces:**
- Consumes: Task 3 session APIs and Task 7 configuration changes.
- Produces: desired_kind_for_member(), reconcile_member(), full_reconcile_guild(), event listeners.

- [ ] **Step 1: Write failing listener test**

~~~python
async def test_role_and_kind_transitions(self):
    cog, guild, member = configured_fixture()
    await cog.reconcile_member(member.in_category(20).with_roles({10}), 100)
    await cog.reconcile_member(member.in_category(30).with_roles({10}), 150)
    await cog.reconcile_member(member.with_roles(set()), 200, "role_removed")
    self.assertEqual(await session_rows(cog, member.id), [
        ("reading_room", 100, 150, "category_change"),
        ("study", 150, 200, "role_removed")])

async def test_resource_deletions_and_valid_voice_change(self):
    cog, guild, member = configured_fixture()
    await cog.reconcile_member(member.in_category(20), 100, allow_closed_gate=True)
    await cog._change_voice_setting(guild, 120, reading_category_id=21)
    self.assertEqual(await session_rows(cog, member.id),
                     [("reading_room", 100, 120, "config_changed")])
    await cog.on_guild_role_delete(guild.get_role(10))
    await cog.on_guild_channel_delete(guild.get_channel(30))
    await cog.on_guild_channel_delete(guild.get_channel(40))
    await cog.on_member_remove(member)
    self.assertIsNone((await cog._store_call(cog.store.get_config, guild.id)).target_role_id)
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: FAIL because reconcile listener behavior is absent.

- [ ] **Step 3: Implement shared reconcile contract**

~~~python
def desired_kind_for_member(self, member, config):
    role_ids = {role.id for role in member.roles}
    if member.bot or config.target_role_id not in role_ids:
        return None
    category_id = getattr(getattr(member.voice, "channel", None), "category_id", None)
    if category_id == config.reading_category_id: return "reading_room"
    if category_id == config.study_category_id: return "study"
    return None

async def reconcile_member(self, member, effective_at_epoch, close_reason="reconciled",
                           allow_closed_gate=False):
    if not allow_closed_gate and not self.collection_gates[member.guild.id].is_set():
        self.dirty_guilds.add(member.guild.id); return
    config = await self._store_call(self.store.get_config, member.guild.id)
    desired = self.desired_kind_for_member(member, config) if config.voice_is_complete else None
    await self._store_call(self.store.reconcile_session, member.guild.id, member.id,
                           desired, effective_at_epoch, close_reason)

async def full_reconcile_guild(self, guild, effective_at_epoch, allow_closed_gate=False):
    gate = self.collection_gates[guild.id]
    gate.clear()
    config = await self._store_call(self.store.get_config, guild.id)
    valid = await self._validate_configured_resources(guild, config, effective_at_epoch)
    if not valid.voice_is_complete:
        await self._store_call(self.store.close_open_rows, guild.id, effective_at_epoch, "config_invalid")
        return
    await self._store_call(self.store.open_collection_run, guild.id, effective_at_epoch)
    for member in guild.members:
        await self.reconcile_member(member, effective_at_epoch, allow_closed_gate=True)
    gate.set()
~~~

full_reconcile_guild is the only function that owns collection gate lifecycle: clear first, validate, leave clear for incomplete/invalid config, otherwise open run, reconcile with allow_closed_gate=True, then set. Recovery and resume call it and never set gate separately. Add tests for incomplete→complete, complete→incomplete, and recovery/config-change race. on_voice_state_update, on_member_update role diff, and on_guild_available use this function or reconcile_member under guild lock. Add listeners on_guild_role_delete(role), on_guild_channel_delete(channel), and on_guild_channel_update(before, after). Add on_member_remove(member) to close any open session at event time with reconciled. If deleted or changed resource ID equals target_role_id, reading_category_id, study_category_id, or sod_eod_channel_id, call _invalidate_configured_resource. For role/category invalidation clear the matching ID, close open sessions with config_changed and collection run with config_invalid. For text channel invalidation clear sod_eod_channel_id and close only its open period with config_invalid. Channel permission/access validation is also performed by status and each command before it writes.

Add separate listener tests for role deletion, reading/study category deletion, category type change, SoD/EoD text deletion, text-channel access loss, and member removal. Add regression tests that SoD 40→41 leaves all voice sessions, runs, and voice session counts unchanged, while valid reading category A→B closes an active A session as config_changed then immediately creates B-kind state if the member is already in B.

- [ ] **Step 4: Run green listener test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: PASS; same-kind channel movement leaves exactly one open session.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_cog.py tests\test_activity_cog.py
git commit -m "feat: 활동 음성 이벤트 수집 추가"
~~~


---

### Task 9: Recovery snapshot, checkpoint, and Gateway disconnect protection

**Review gate:** recovery closes only pre-existing snapshot rows; disconnect interval cannot accrue time; resume current-state reconcile starts fresh coverage.

**Files:**
- Modify: activity_cog.py
- Modify: tests/test_activity_cog.py

**Interfaces:**
- Produces: recover_after_ready(), on_disconnect(), on_resumed(), 60-second checkpoint loop.

- [ ] **Step 1: Write failing recovery tests**

~~~python
async def test_recovery_closes_snapshot_not_new_row(self):
    cog, guild, member = configured_fixture()
    await open_fixture_session(cog, member.id, "study", 50)
    await recover_after_ready_for_test(cog, 100)
    self.assertEqual(await session_rows(cog, member.id), [
        ("study", 50, 50, "restart_checkpoint"),
        ("reading_room", 100, None, None)])

async def test_recovery_reconcile_writes_while_gate_is_closed(self):
    cog, guild, member = configured_fixture()
    cog.collection_gates[guild.id].clear()
    await recover_after_ready_for_test(cog, 100)
    self.assertEqual(await open_session_count(cog, member.id), 1)

async def test_disconnect_gap_ignores_event(self):
    cog, guild, member = configured_fixture()
    await recover_after_ready_for_test(cog, 100)
    await on_disconnect_for_test(cog, 160)
    await cog.reconcile_member(member.in_category(20), 170)
    await on_resumed_for_test(cog, 200)
    self.assertEqual(await coverage_gaps(cog, 100, 240), [(160, 200)])

async def test_closed_gate_does_not_checkpoint(self):
    cog, guild, _ = configured_fixture()
    cog.collection_gates[guild.id].clear()
    before = await run_checkpoint(cog, guild.id)
    await checkpoint_once_for_test(cog)
    self.assertEqual(await run_checkpoint(cog, guild.id), before)
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: FAIL because recovery APIs are absent.

- [ ] **Step 3: Implement snapshot/gate order**

~~~python
async def recover_after_ready(self):
    await self.bot.wait_until_ready()
    now_epoch = utc_now_epoch()
    for guild in self.bot.guilds:
        async with self.guild_locks[guild.id]:
            self.collection_gates[guild.id].clear()
            snapshot = await self._store_call(self.store.snapshot_open_row_ids, guild.id)
            await self._store_call(self.store.close_snapshot_rows_at_checkpoint, snapshot,
                                   "restart_checkpoint")
            await self.full_reconcile_guild(guild, now_epoch, allow_closed_gate=True)

@commands.Cog.listener()
async def on_disconnect(self):
    now = utc_now_epoch()
    for guild in self.bot.guilds:
        self.collection_gates[guild.id].clear()
    for guild in self.bot.guilds:
        try:
            async with self.guild_locks[guild.id]:
                await self._store_call(self.store.close_open_rows, guild.id, now, "gateway_disconnect")
        except sqlite3.Error:
            logger.exception("disconnect close failed for guild %s", guild.id)

def cog_load(self):
    self.recovery_task = self.bot.loop.create_task(self.recover_after_ready())
    self.checkpoint_task.start()
~~~

snapshot_open_row_ids returns tuples (table_name, row_id, last_checkpoint_epoch), and close_snapshot_rows_at_checkpoint updates each snapshot row using its own last_checkpoint_epoch rather than now_epoch. cog_load starts recovery and the 60-second checkpoint loop; recovery obtains now only after wait_until_ready. on_resumed snapshots remaining pre-resume rows, closes them with gateway_disconnect when a disconnect timestamp was observed or restart_checkpoint when only crash recovery is known, then calls full_reconcile_guild; it never sets a gate directly. Voice listeners acquire guild_locks[guild.id], then check gate and reconcile in that same critical section. Add an interleaving test where a listener already holds that lock when disconnect begins; after the listener exits and disconnect close runs, no open row remains. Checkpoint writes last_checkpoint_epoch only when gate is set; add a closed-gate heartbeat test. In cog_unload cancel checkpoint/recovery tasks and schedule best-effort close_open_rows(guild_id, utc_now_epoch(), graceful_shutdown) without blocking Discord shutdown; session reason is reconciled and run reason graceful_shutdown; failure is logged.

- [ ] **Step 4: Run green recovery test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: PASS; no reported seconds come from 160 through 200.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_cog.py tests\test_activity_cog.py
git commit -m "feat: 활동 수집 복구와 연결 중단 보호 추가"
~~~

---

### Task 10: Live SoD/EoD listener and streaming backfill

**Review gate:** only configured channel/eligible authors are scanned; history is streamed in 256MB VM; after cursor resumes; edit/delete never mutates stored activity.

**Files:**
- Modify: activity_cog.py
- Modify: tests/test_activity_cog.py

**Interfaces:**
- Consumes: Task 4 event/cursor store and Task 7 settings.
- Produces: detect_sod_eod(), on_message(), /활동설정 과거동기화, backfill_current_channel().

- [ ] **Step 1: Write failing parser/backfill test**

~~~python
def test_whole_word_casefold_parser(self):
    self.assertEqual(detect_sod_eod("SoD and EOD!"), {"sod", "eod"})
    self.assertEqual(detect_sod_eod("sodastream preEoDpost"), set())

async def test_backfill_uses_after_and_oldest_first(self):
    cog, guild, member = configured_fixture()
    await prepare_sync_marker(cog, channel_id=40, message_id=11)
    channel = make_history_channel(guild, 40, [make_message(12, member, "SoD")])
    await cog.backfill_current_channel(guild, channel=channel)
    channel.history.assert_called_once_with(limit=None, oldest_first=True, after=discord.Object(11))

async def test_backfill_unresolved_author_advances_cursor_without_event(self):
    cog, guild, _ = configured_fixture()
    channel = fixture_channel_with_missing_author()
    await cog.backfill_current_channel(guild, channel=channel)
    self.assertEqual(await event_count(cog), 0)
    self.assertEqual((await sync_state(cog, channel.id)).newest_processed_message_id, 12)

async def test_live_arrival_does_not_advance_cursor_and_backfill_resumes(self):
    cog, guild, member = configured_fixture()
    await prepare_sync_marker(cog, channel_id=40, message_id=11)
    await record_live_message_for_test(cog, message_id=13, content="SoD")
    self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 11)
    channel = make_history_channel(guild, 40, [make_message(13, member, "SoD")])
    await cog.backfill_current_channel(guild, channel=channel)
    self.assertEqual((await sync_state(cog, 40)).newest_processed_message_id, 13)

async def test_backfill_holds_guild_lock_before_sod_config_change(self):
    cog, guild, member = configured_fixture()
    entered, release, order = asyncio.Event(), asyncio.Event(), []
    channel = make_controlled_history_channel(guild, 40, [make_message(12, member, "SoD")],
        entered, release)
    backfill = asyncio.create_task(cog.backfill_current_channel(guild, channel=channel))
    await entered.wait()
    change = asyncio.create_task(set_sod_channel_for_test(cog, guild, 41, 101))
    await asyncio.sleep(0)
    self.assertFalse(change.done())
    release.set()
    await backfill; order.append("backfill")
    await change; order.append("change")
    self.assertEqual(order, ["backfill", "change"])
    self.assertEqual((await cog._store_call(cog.store.get_config, guild.id)).sod_eod_channel_id, 41)

async def test_backfill_callback_defers_before_history_then_edits_original(self):
    cog, guild, member = configured_fixture()
    interaction = fake_deferred_interaction(1, True, guild)
    channel = make_history_channel(guild, 40, [make_message(12, member, "SoD")])
    guild.channels = [item for item in guild.channels if item.id != 40] + [channel]
    await cog.backfill_command.callback(cog, interaction)
    self.assertTrue(interaction.response.deferred)
    self.assertTrue(interaction.original_edits)
    channel.history.assert_called_once()

async def test_backfill_callback_error_uses_ephemeral_followup(self):
    cog, guild, _ = configured_fixture()
    interaction = fake_deferred_interaction(1, True, guild)
    cog.backfill_current_channel = unittest.mock.AsyncMock(side_effect=sqlite3.Error("db"))
    await cog.backfill_command.callback(cog, interaction)
    self.assertTrue(interaction.response.deferred)
    self.assertEqual(interaction.followup.sent[0][1]["ephemeral"], True)
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: FAIL because parser/backfill does not exist.

- [ ] **Step 3: Implement streaming scan and live insertion**

~~~python
SOD_EOD_PATTERN = re.compile(r"(?i)(?<![a-z0-9])(sod|eod)(?![a-z0-9])")
def detect_sod_eod(content):
    return {m.group(1).casefold() for m in SOD_EOD_PATTERN.finditer(content.casefold())}

@settings_group.command(name="과거동기화", description="SoD/EoD 과거 메시지를 동기화합니다.")
async def backfill_command(self, interaction):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = await self.backfill_current_channel(interaction.guild)
        await interaction.edit_original_response(
            content=f"과거 동기화 완료: 처리 {result.processed_count}개, 기록 {result.event_count}개")
    except (sqlite3.Error, discord.DiscordException, ChannelChanged):
        logger.exception("activity backfill failed")
        await interaction.followup.send("과거 동기화에 실패했습니다. 로그와 채널 권한을 확인해주세요.", ephemeral=True)

async for message in channel.history(limit=None, oldest_first=True, after=after):
    kinds = set()
    member = guild.get_member(message.author.id)
    if member is None:
        try:
            member = await guild.fetch_member(message.author.id)
        except (discord.NotFound, discord.Forbidden):
            member = None
    if member is not None and not member.bot and self.member_has_target_role(member, config):
        kinds = detect_sod_eod(message.content)
    await self._store_call(self.store.record_backfill_message_and_advance_cursor,
        guild_id=guild.id, channel_id=channel.id, message_id=message.id,
        user_id=message.author.id, message_created_epoch=int(message.created_at.timestamp()),
        event_types=kinds, newest_processed_message_created_epoch=int(message.created_at.timestamp()),
        updated_epoch=utc_now_epoch(), expected_current_channel_id=channel.id)
~~~

The callback defers ephemerally before channel lookup, guild lock acquisition, or history iteration; this is the explicit 3-second acknowledgement contract. Backfill holds the existing guild_locks[guild.id] for one entire streaming history scan, so sod_eod configuration change is serialized behind scan completion; no separate backfill lock exists. Live on_message calls record_live_message only and never acquires or changes the cursor. expected_current_channel comparison remains defense in depth. Add a controlled-iterator test that starts backfill, confirms a config-change task is pending while the iterator holds guild lock, releases the iterator, then asserts backfill completes before SoD channel changes. Never create list(channel.history(...)). The shared eligibility function returns true only for guild message, configured current channel, MessageType.default, non-bot author, and target-role member; test DM/thread/other-channel/system/bot/roleless rejection. On history API/DB error stop scan at last committed cursor. Live and backfill use the same eligibility function but different persistence methods. Do not register edit/delete mutators.

- [ ] **Step 4: Run green backfill test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: PASS; both event types persist once and no full history is held in memory.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_cog.py tests\test_activity_cog.py
git commit -m "feat: SoD EoD 실시간 수집과 과거 동기화 추가"
~~~

---

### Task 11: Administrator report commands, pagination, and TXT

**Review gate:** recent and explicit inclusive KST periods work; every 15-row page is reachable; buttons reauthorize; TXT uses io.BytesIO and contains all required fields.

**Files:**
- Modify: activity_cog.py
- Modify: tests/test_activity_cog.py

**Interfaces:**
- Consumes: Task 5 ActivityReport.
- Produces: /활동현황 최근, /활동현황 기간, ActivityReportView, build_report_txt().

- [ ] **Step 1: Write failing View tests**

~~~python
async def test_other_admin_cannot_download_txt(self):
    view = ActivityReportView(1, sample_report(), fake_deferred_interaction(1, True).edit_original_response)
    interaction = fake_interaction(user_id=2, administrator=True)
    await press(view, view.full_txt_button, interaction)
    self.assertEqual(interaction.files, [])

async def test_owner_txt_defers_then_sends_followup_file(self):
    view = ActivityReportView(1, sample_report(), fake_deferred_interaction(1, True).edit_original_response)
    interaction = fake_interaction(user_id=1, administrator=True)
    await press(view, view.full_txt_button, interaction)
    self.assertTrue(interaction.response.deferred)
    self.assertIsInstance(interaction.followup.sent[0][1]["file"], discord.File)

async def test_timeout_disables_buttons(self):
    view = ActivityReportView(1, sample_report(), fake_deferred_interaction(1, True).edit_original_response)
    owner_interaction = fake_deferred_interaction(user_id=1, administrator=True)
    view.original_response_editor = owner_interaction.edit_original_response
    await view.on_timeout()
    self.assertTrue(all(child.disabled for child in view.children))
    self.assertEqual(owner_interaction.original_edits[-1]["view"], view)

async def test_page_boundaries_and_round_trip_for_55_members(self):
    view = ActivityReportView(1, report_with_members(55), fake_deferred_interaction(1, True).edit_original_response)
    interaction = fake_interaction(user_id=1, administrator=True)
    for _ in range(9): await press(view, view.next_page, interaction)
    self.assertEqual(view.page_index, 3)
    for _ in range(9): await press(view, view.previous_page, interaction)
    self.assertEqual(view.page_index, 0)

def test_page_layout_and_empty_page(self):
    report = report_with_members(55)
    self.assertEqual([len(report.rows[i:i + 15]) for i in range(0, 55, 15)], [15, 15, 15, 10])
    self.assertIn("표시할 대상 멤버가 없습니다", format_report_page(report_with_members(0), 0))

def test_page_and_txt_render_same_warning_summary(self):
    report = report_with_warning(code="gateway_disconnect", text="음성 수집 공백: 160~200")
    self.assertIn("음성 수집 공백: 160~200", format_report_page(report, 0))
    self.assertIn("음성 수집 공백: 160~200", build_report_txt(report))
~~~

- [ ] **Step 2: Run red test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: FAIL because report commands/View are absent.

- [ ] **Step 3: Implement Group, View, and TXT**

~~~python
report_group = app_commands.Group(
    name="활동현황", description="활동 현황을 조회합니다.", guild_only=True,
    default_permissions=discord.Permissions(administrator=True))

async def send_report(self, interaction, start_date, end_date):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        generated_epoch = utc_now_epoch()
        members = [ReportMember(m.id, m.display_name) for m in interaction.guild.members
                   if not m.bot and self.member_has_target_role(m, await self._config(interaction.guild.id))]
        start_epoch, end_epoch = kst_range_to_epoch(start_date, end_date)
        report = await self._store_call(self.store.build_report, guild_id=interaction.guild.id,
                                        members=members, start_epoch=start_epoch, end_epoch=end_epoch,
                                        as_of_epoch=generated_epoch)
        report = dataclasses.replace(report, generated_epoch=generated_epoch)
        view = ActivityReportView(interaction.user.id, report, interaction.edit_original_response)
        await interaction.edit_original_response(content=format_report_page(report, 0), view=view)
    except (sqlite3.Error, discord.DiscordException, ValueError):
        logger.exception("activity report failed")
        await interaction.followup.send("활동 현황을 만들지 못했습니다. 설정과 로그를 확인해주세요.", ephemeral=True)

class ActivityReportView(discord.ui.View):
    def __init__(self, owner_id, report, original_response_editor):
        super().__init__(timeout=600)
        self.owner_id, self.report, self.page_index = owner_id, report, 0
        self.original_response_editor = original_response_editor
        self._sync_page_buttons()
    async def interaction_check(self, interaction):
        admin = bool(getattr(interaction.user, "guild_permissions", None)
                     and interaction.user.guild_permissions.administrator)
        if interaction.user.id != self.owner_id or not admin:
            await interaction.response.send_message("이 보고서를 실행한 관리자만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True
    async def full_txt(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        data = io.BytesIO(build_report_txt(self.report).encode("utf-8"))
        await interaction.followup.send(
            file=discord.File(data, filename=self.report.txt_filename), ephemeral=True)
    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction, button):
        await self._move_page(interaction, -1)
    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        await self._move_page(interaction, 1)
    async def _move_page(self, interaction, delta):
        self.page_index = min(max(0, self.page_index + delta), self.report.page_count - 1)
        self._sync_page_buttons()
        await interaction.response.edit_message(content=format_report_page(self.report, self.page_index), view=self)
    @discord.ui.button(label="전체 TXT", style=discord.ButtonStyle.primary)
    async def full_txt_button(self, interaction, button):
        await self.full_txt(interaction, button)
    def _sync_page_buttons(self):
        self.previous_page.disabled = self.page_index == 0
        self.next_page.disabled = self.page_index == self.report.page_count - 1
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.original_response_editor(view=self)
        except discord.DiscordException:
            logger.warning("activity report timeout edit failed", exc_info=True)
~~~

Both report command callbacks call send_report, so runtime Administrator verification occurs before defer; immediately after defer send_report obtains generated_epoch exactly once, passes it as build_report as_of_epoch, and stores the same epoch in ActivityReport for page header and TXT generation timestamp. Add an actual callback test with an open session asserting last activity equals that one generated_epoch. Long work returns through edit_original_response and post-defer errors through ephemeral followup. Recent accepts Range[int, 1] and includes KST today. Explicit date strictly parses YYYY-MM-DD and rejects start after end. Page content has name, last activity, reading, study, SoD days, EoD days, combined days. TXT has each member name/user_id, UTC/KST last timestamp, two seconds/session_count columns, day counts, and coverage warnings. View timeout disables all controls by editing the original ephemeral response through the saved interaction editor; edit failure only logs.

- [ ] **Step 4: Run green report UI test**

Run: & "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v

Expected: PASS; 55 members produce four pages and unauthorized interaction receives no file.

- [ ] **Step 5: Commit**

~~~powershell
git add activity_cog.py tests\test_activity_cog.py
git commit -m "feat: 관리자 활동 현황 보고서와 TXT 추가"
~~~

---

### Task 12: Packaging, ignores, docs, manual acceptance, and final verification

**Review gate:** runtime image contains both modules, DB artifacts are excluded from Git/Docker, operational documentation is complete, and Fly persistence is proved against the actual deployment.

**Files:**
- Modify: Dockerfile
- Modify: fly.toml
- Modify: .gitignore
- Create or Modify: .dockerignore
- Modify: README.md
- Create: docs/superpowers/specs/2026-08-02-activity-report-manual-checklist.md

**Interfaces:**
- Consumes: completed Tasks 1–11.
- Produces: deployable, documented feature and release evidence.

- [ ] **Step 1: Make exact packaging/ignore changes**

~~~dockerfile
COPY bot.py wallet_cog.py activity_cog.py activity_store.py ./
ENV DATA_FILE=/data/rss_data.json
ENV ACTIVITY_DB_PATH=/data/activity.db
~~~

~~~toml
[env]
  DATA_FILE = "/data/rss_data.json"
  ACTIVITY_DB_PATH = "/data/activity.db"
  LOG_LEVEL = "INFO"
  TZ = "Asia/Seoul"
~~~

~~~gitignore
activity.db
activity.db-wal
activity.db-shm
~~~

~~~text
activity.db
activity.db-wal
activity.db-shm
__pycache__/
*.pyc
venv/
~~~

- [ ] **Step 2: Write README and manual acceptance checklist**

README must explain: Portal intents must be set before deployment to avoid Gateway login failure; View Channels, Read Message History, Send Messages, Attach Files, Use Application Commands are required; Connect is not required; configuration/backfill command flow; voice data begins at deployment; inaccessible/deleted history cannot be backfilled; no message content/voice content is stored; no automatic member action occurs; /data contains operational SQLite files.

~~~markdown
# Fly persistence smoke

- [ ] fly machine list -a eu-ssya-bot shows exactly one running Machine.
- [ ] fly volumes list -a eu-ssya-bot shows bot_data attached to that Machine.
- [ ] Administrator sets all four settings, executes backfill, and can open a report.
- [ ] Non-administrator command/button/TXT access receives only an ephemeral denial.
- [ ] A PowerShell command reads the sole actual Machine ID from Fly JSON, verifies exactly one ID, and restarts that ID successfully.
- [ ] fly ssh console -a eu-ssya-bot -C "python -c \"import sqlite3; print(sqlite3.connect('/data/activity.db').execute('select count(*) from activity_config').fetchone()[0])\"" returns the pre-restart count.
- [ ] Report buttons are disabled after 10 idle minutes.
~~~

- [ ] **Step 3: Run automated checks and record baseline**

~~~powershell
& "venv\Scripts\python.exe" -m py_compile bot.py activity_cog.py activity_store.py wallet_cog.py
& "venv\Scripts\python.exe" -m unittest discover -s tests -v
& "venv\Scripts\python.exe" scripts\verify_wallet.py
& "venv\Scripts\python.exe" scripts\verify_load_data.py
& "venv\Scripts\python.exe" scripts\verify_final.py
~~~

Expected: compile/new tests/verify_wallet/verify_load_data PASS. verify_final remains non-zero only at its existing AC4 overdraft reject assertion; any other new failure blocks release.

- [ ] **Step 4: Run actual Fly smoke after Portal intent setup**

~~~powershell
fly machine list -a eu-ssya-bot
fly volumes list -a eu-ssya-bot
fly deploy -a eu-ssya-bot
$before = fly ssh console -a eu-ssya-bot -C "python -c \"import sqlite3,hashlib,json; c=sqlite3.connect('/data/activity.db'); ts=['activity_config','voice_sessions','sod_eod_events','sod_eod_daily']; rows=[(t,c.execute('select * from '+t+' order by rowid').fetchall()) for t in ts]; print(json.dumps({'exists':1,'counts':[(t,len(r)) for t,r in rows],'checksum':hashlib.sha256(repr(rows).encode()).hexdigest()}))\""
$machines = fly machine list -a eu-ssya-bot --json | ConvertFrom-Json
$machineIds = @($machines | ForEach-Object { $_.id })
if ($machineIds.Count -ne 1) { throw "Expected exactly one Fly Machine, found $($machineIds.Count)" }
fly machine restart $machineIds[0] -a eu-ssya-bot
$after = fly ssh console -a eu-ssya-bot -C "python -c \"import sqlite3,hashlib,json; c=sqlite3.connect('/data/activity.db'); ts=['activity_config','voice_sessions','sod_eod_events','sod_eod_daily']; rows=[(t,c.execute('select * from '+t+' order by rowid').fetchall()) for t in ts]; print(json.dumps({'exists':1,'counts':[(t,len(r)) for t,r in rows],'checksum':hashlib.sha256(repr(rows).encode()).hexdigest()}))\""
if ($before.Trim() -ne $after.Trim()) { throw "SQLite persistence mismatch: before=$before after=$after" }
fly machine list -a eu-ssya-bot
fly volumes list -a eu-ssya-bot
~~~

Expected: pause test traffic before capturing $before, then require exact logical counts/checksum equality after restart; if traffic cannot be paused, parse the JSON and require every after count to be greater than or equal to before while recording the changed checksum. Existence must be 1 in both snapshots. WAL/SHM absence after checkpoint is acceptable.

- [ ] **Step 5: Run source hygiene checks**

~~~powershell
git diff --check
git status --short
rg -n "[T]ODO|[F]IXME|<{7}|={7}|>{7}" activity_cog.py activity_store.py tests README.md docs\superpowers\specs\2026-08-02-activity-report-manual-checklist.md
git diff -- requirements.txt
~~~

Expected: no whitespace/conflict markers, no tracked DB artifacts, and no requirements.txt diff.

- [ ] **Step 6: Commit**

~~~powershell
git add Dockerfile fly.toml .gitignore .dockerignore README.md docs\superpowers\specs\2026-08-02-activity-report-manual-checklist.md
git commit -m "docs: 활동 보고서 배포와 운영 안내 추가"
~~~

---

## Spec coverage review

| Approved acceptance area | Tasks |
| --- | --- |
| Admin guild-only commands and click-time reauthorization | 7, 11, 12 |
| Atomic partial settings and voice/SoD setting separation | 2, 7 |
| Role/non-bot scope and historical record policy | 5, 8, 10, 11 |
| Voice seconds, state transition, recovery/disconnect safety | 3, 8, 9 |
| Voice/channel source coverage warnings | 3, 5, 10, 11 |
| Whole-word SoD/EoD, both kinds, daily dedupe, immutable edit/delete | 4, 10 |
| Channel-scoped resumable atomic backfill and A→B→A periods | 2, 4, 10 |
| KST range, 15-page View, timeout, full TXT | 1, 5, 11 |
| Failure isolation and no package addition | 6, 12 |
| Existing test baseline and Fly Machine/volume persistence | 12 |

## Execution handoff

Plan complete and saved to docs/superpowers/plans/2026-08-02-activity-report-implementation-plan.md.

1. **Subagent-Driven (recommended):** dispatch one fresh agent per task and review each task before continuing.
2. **Inline Execution:** use superpowers:executing-plans to run tasks in batches with review checkpoints.

Choose one execution approach before code implementation begins.

## Official implementation references

- [Discord Gateway Intents](https://discord.com/developers/docs/events/gateway#gateway-intents)
- [discord.py interaction API](https://discordpy.readthedocs.io/en/stable/interactions/api.html)
- [discord.py TextChannel.history](https://discordpy.readthedocs.io/en/stable/api.html#discord.TextChannel.history)
- [Fly Volumes](https://fly.io/docs/volumes/)
