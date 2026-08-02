# 활동 현황 보고서 설계 — eu-ssya-bot

**작성일**: 2026-08-02  
**상태**: 승인된 구현 설계  
**대상**: `D:\dev\eu-ssya-bot`

## 1. 배경, 목표, 비목표

이 기능은 운영자가 장기 미활동(유령) 멤버를 판단할 때 참고하는 **원시 활동 현황 보고서**다. 경쟁 랭킹·자동 활동 판정·점수·등급·자동 경고·강퇴는 만들지 않는다. 운영자가 수치를 보고 직접 판단한다.

현재 `으쌰으쌰` 역할을 가진 사람 멤버를 전원 표시하며, 기록 0명도 포함한다. 수집 대상은 수집/스캔 시점에 봇이 아니고 대상 역할을 가진 계정뿐이다. 역할 없는 계정(부스트만 하고 대상 역할이 없는 계정 포함), 봇, 일반 Discord 채팅, 카카오톡, 오프라인 활동은 저장·보고하지 않는다. 역할 제거 뒤 기존 기록은 보존하되 현재 보고서에서는 제외한다.

수집하는 것은 지정 독서실·스터디 음성 카테고리 체류 시간과 지정 텍스트 채널의 SoD/EoD 참여 흔적뿐이다. 메시지 본문, 음성 내용, 실제 발화 시간, 녹음, 화면 공유·카메라 상태는 저장하거나 판단하지 않는다.

## 2. 사용자 흐름

1. Administrator가 `/활동설정 대상역할`, `/활동설정 독서실`, `/활동설정 스터디`, `/활동설정 sod_eod`를 어떤 순서로든 설정한다.
2. 대상 역할·독서실 카테고리·스터디 카테고리가 모두 유효해지는 순간 음성 수집이 활성화된다. 이미 음성에 있는 대상 멤버도 reconcile로 즉시 반영한다.
3. 관리자는 `/활동설정 과거동기화`로 현재 SoD/EoD 채널의 접근 가능한 과거 메시지를 동기화한다.
4. 관리자는 `/활동현황 최근 일수:N` 또는 `/활동현황 기간 시작일:YYYY-MM-DD 종료일:YYYY-MM-DD`로 관리자 전용 ephemeral 보고서를 열고, 15명씩 이전/다음 페이지 또는 전체 TXT를 받는다.
5. 보고서는 데이터 가용·중단 구간 경고를 함께 보여 준다. 수치는 판단 보조 자료일 뿐 자동 조치로 이어지지 않는다.

## 3. 구성요소와 격리

| 구성요소 | 책임 |
|---|---|
| `bot.py` | `members=True`, `message_content=True`, `voice_states=True` Intent로 봇을 만들고 기존 RSS·모임통장 setup hook을 유지한다. |
| `activity_cog.py` | 명령·View·Discord 이벤트, guild single-flight, reconcile, recovery gate, checkpoint, 보고서/TXT, backfill을 맡는다. |
| `activity_store.py` | 표준 라이브러리 `sqlite3`만으로 schema·트랜잭션·조회 API를 제공한다. Discord 객체는 받지 않는다. |
| `/data/activity.db` | 기존 Fly 단일 Machine의 volume에 두는 활동 전용 SQLite DB다. 로컬은 `ACTIVITY_DB_PATH`로 경로를 바꿀 수 있다. |

현재 `bot.py`의 `setup_hook`에서 activity extension load와 초기 DB/schema 준비를 별도 `try/except` 경계로 감싼다. 이 단계가 실패하면 `logger.exception`을 남기고 활동 extension과 명령은 등록하지 않으며, wallet extension load, RSS loop 시작, Discord 로그인은 계속 진행한다. extension이 정상 로드된 뒤의 개별 DB·설정·Discord API 오류는 해당 활동 명령 또는 이벤트만 실패시키고, 명령은 administrator에게 ephemeral 오류를 반환한다. 어느 경우에도 RSS 또는 모임통장을 죽이지 않는다.

## 4. 권한과 명령 계약

Discord Developer Portal에서 **Server Members Intent**, **Message Content Intent**를 활성화한다. `voice_states` Intent도 코드에서 활성화한다. 봇 권한은 View Channels, Read Message History, Send Messages, Attach Files, Use Application Commands다. 봇은 음성 채널에 접속하지 않으므로 Connect 권한은 필요 없다.

`/활동설정`과 `/활동현황`은 guild-only 부모 `app_commands.Group`이며, 부모 Group에 `default_permissions(administrator=True)`를 적용한다. subcommand마다 중복 적용하지 않는다. 모든 callback, pagination 버튼, TXT 버튼은 클릭 순간 `guild_permissions.administrator`를 재검증하고 최초 명령 실행자 ID와도 일치해야 한다. 불일치하면 데이터나 파일을 보내지 않는다.

| 명령 | 계약 |
|---|---|
| `/활동설정 대상역할 역할` | guild role을 검증해 대상 역할 ID를 저장한다. |
| `/활동설정 독서실 카테고리` | 음성 카테고리 ID를 `reading_category_id`로 저장한다. |
| `/활동설정 스터디 카테고리` | 음성 카테고리 ID를 `study_category_id`로 저장한다. 독서실과 같은 ID는 거부한다. |
| `/활동설정 sod_eod 채널` | guild text channel ID를 저장하고 채널별 동기화 상태를 원자적으로 전환한다. |
| `/활동설정 상태` | 설정 ID, 음성 coverage run, SoD/EoD channel period별 sync/history/gap, 진행 세션, checkpoint를 ephemeral로 표시한다. |
| `/활동설정 과거동기화` | 현재 채널을 backfill한다. |
| `/활동현황 최근 일수:N` | KST 기준 오늘 포함 최근 N일. N은 1 이상 정수다. |
| `/활동현황 기간 시작일 종료일` | 양 끝 포함 KST 기간. 시작일은 종료일보다 늦을 수 없다. |

설정 명령은 새 값의 guild 소속·유형·독서실/스터디 ID 상이성을 먼저 검증하고, 그 다음 `BEGIN IMMEDIATE` 트랜잭션으로 저장한다. ID는 모두 nullable이며 설정 순서는 무관하다. `target_role_id`, `reading_category_id`, `study_category_id` 중 하나라도 실제로 바뀌면 같은 트랜잭션에서 기존 열린 세션과 coverage run을 변경 시각에 닫는다. commit 뒤 구성 완성/미완성 전환 여부와 무관하게 같은 guild single-flight 안에서 항상 full voice reconcile한다. 새 구성이 완성·유효하면 collection gate/run을 열고 현재 대상 멤버 세션을 시작하며, 미완성·무효면 gate를 닫고 새 세션을 시작하지 않는다. `sod_eod_channel_id` 단독 변경은 voice reconcile을 수행하지 않고 아래 channel period와 sync state만 원자적으로 전환한다. invalid 입력·DB 오류는 rollback하여 기존 설정과 열린 세션/run/period를 유지한다.

backfill, 보고서 생성, TXT 생성은 interaction 수신 뒤 3초 안에 `defer(ephemeral=True)`하고 `edit_original_response` 또는 ephemeral `followup`으로 결과를 보낸다.

## 5. SQLite·시간·스키마

`sqlite3` connection은 각 `asyncio.to_thread` 호출 **내부**에서 열고, connection별 `journal_mode=WAL`, `busy_timeout`을 설정한 뒤 사용·close한다. connection을 다른 thread에 넘기지 않는다. 활동 Cog의 쓰기는 `asyncio.Lock`으로 직렬화한다. 실제 foreign key는 사용하지 않으므로 foreign key pragma는 설정하지 않는다.

모든 timestamp는 UTC Unix epoch seconds `INTEGER`이고 컬럼 이름은 `*_epoch`으로 통일한다. KST 날짜만 `YYYY-MM-DD TEXT`다.

```sql
CREATE TABLE activity_config (
  guild_id INTEGER PRIMARY KEY,
  target_role_id INTEGER,
  reading_category_id INTEGER,
  study_category_id INTEGER,
  sod_eod_channel_id INTEGER,
  voice_collection_started_epoch INTEGER,
  created_epoch INTEGER NOT NULL,
  updated_epoch INTEGER NOT NULL,
  CHECK (reading_category_id IS NULL OR study_category_id IS NULL
         OR reading_category_id <> study_category_id)
);

CREATE TABLE voice_sessions (
  id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  activity_kind TEXT NOT NULL CHECK(activity_kind IN ('reading_room', 'study')),
  started_epoch INTEGER NOT NULL,
  last_checkpoint_epoch INTEGER NOT NULL,
  ended_epoch INTEGER,
  closed_reason TEXT CHECK(closed_reason IN
    ('normal', 'category_change', 'role_removed', 'config_changed',
     'reconciled', 'gateway_disconnect', 'restart_checkpoint')),
  CHECK (last_checkpoint_epoch >= started_epoch),
  CHECK ((ended_epoch IS NULL AND closed_reason IS NULL)
      OR (ended_epoch IS NOT NULL AND closed_reason IS NOT NULL)),
  CHECK (ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch)
);

CREATE TABLE voice_collection_runs (
  id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  started_epoch INTEGER NOT NULL,
  last_checkpoint_epoch INTEGER NOT NULL,
  ended_epoch INTEGER,
  ended_reason TEXT CHECK(ended_reason IN
    ('config_changed', 'config_invalid', 'graceful_shutdown',
     'gateway_disconnect', 'restart_checkpoint')),
  CHECK(last_checkpoint_epoch >= started_epoch),
  CHECK ((ended_epoch IS NULL AND ended_reason IS NULL)
      OR (ended_epoch IS NOT NULL AND ended_reason IS NOT NULL)),
  CHECK (ended_epoch IS NULL OR ended_epoch >= last_checkpoint_epoch)
);

CREATE TABLE sod_eod_events (
  message_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  event_date_kst TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('sod', 'eod')),
  message_created_epoch INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  PRIMARY KEY (message_id, event_type)
);

CREATE TABLE sod_eod_daily (
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  event_date_kst TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN ('sod', 'eod')),
  PRIMARY KEY (guild_id, user_id, event_date_kst, event_type)
);

CREATE TABLE activity_sync_state (
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  newest_processed_message_id INTEGER,
  newest_processed_message_created_epoch INTEGER,
  history_from_epoch INTEGER,
  completed_epoch INTEGER,
  updated_epoch INTEGER NOT NULL,
  PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE sod_eod_channel_periods (
  id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  started_epoch INTEGER NOT NULL,
  ended_epoch INTEGER,
  ended_reason TEXT CHECK(ended_reason IN ('channel_changed', 'config_invalid')),
  CHECK ((ended_epoch IS NULL AND ended_reason IS NULL)
      OR (ended_epoch IS NOT NULL AND ended_reason IS NOT NULL)),
  CHECK (ended_epoch IS NULL OR ended_epoch >= started_epoch)
);
```

```sql
CREATE UNIQUE INDEX idx_voice_sessions_one_open_per_member
  ON voice_sessions(guild_id, user_id) WHERE ended_epoch IS NULL;
CREATE INDEX idx_voice_sessions_report
  ON voice_sessions(guild_id, user_id, activity_kind, started_epoch, ended_epoch);
CREATE UNIQUE INDEX idx_voice_collection_runs_one_open
  ON voice_collection_runs(guild_id) WHERE ended_epoch IS NULL;
CREATE INDEX idx_voice_collection_runs_coverage
  ON voice_collection_runs(guild_id, started_epoch, ended_epoch);
CREATE INDEX idx_sod_eod_events_report
  ON sod_eod_events(guild_id, channel_id, user_id, event_date_kst, event_type);
CREATE INDEX idx_sod_eod_daily_report
  ON sod_eod_daily(guild_id, event_date_kst, user_id, event_type);
CREATE UNIQUE INDEX idx_sod_eod_channel_periods_one_open
  ON sod_eod_channel_periods(guild_id) WHERE ended_epoch IS NULL;
CREATE INDEX idx_sod_eod_channel_periods_coverage
  ON sod_eod_channel_periods(guild_id, channel_id, started_epoch, ended_epoch);
```

`(message_id, event_type)`은 한 메시지의 SoD/EoD 동시 저장과 각 유형의 멱등을 보장한다. daily PK는 사용자·KST 날짜·유형별 1회 집계다. `first_message_id`는 저장하지 않는다.

## 6. 음성 상태 reconcile·설정 변경·복구

단일 계약 `reconcile_member(member, effective_at_epoch)`를 모든 음성 진입점에서 사용한다. desired state는 `(not member.bot, target role 보유, 현재 음성 채널의 configured category kind)`이며, 마지막 요소는 `reading_room`·`study`·없음 중 하나다. 함수는 DB의 열린 세션과 비교해 하나의 쓰기 트랜잭션에서 다음을 수행한다.

- desired 없음 + open 있음: `effective_at_epoch`에 `reconciled`로 닫는다. 역할 제거 이벤트 경로는 더 구체적인 `role_removed`를 사용한다.
- desired 있음 + open 없음: 새 세션을 연다.
- desired와 open kind가 같음: no-op이다.
- desired kind가 바뀜: 기존을 `category_change`로 닫고 같은 시각에 새 세션을 연다.

새 세션 insert는 partial unique index와 같은 트랜잭션에서 처리한다. 제약 위반이면 이미 열린 세션을 재조회해 desired가 같으면 no-op, 다르면 닫고 연다. 따라서 voice update race, restart, 중복 Gateway event에도 사용자당 열린 세션은 하나다.

`on_voice_state_update`, `on_member_update`의 역할 추가·제거, 설정 변경, ready 뒤 초기화, `on_guild_available`, `on_resumed`는 guild single-flight lock 아래 이 함수를 호출한다. 역할을 잃으면 열린 세션은 event 시각의 `role_removed`로 닫는다. 역할을 추가한 상태로 이미 감시 음성에 있으면 즉시 시작한다.

`target_role_id`, `reading_category_id`, `study_category_id` 변경은 설정 트랜잭션에서 열린 세션을 `config_changed`, 열린 coverage run을 `config_changed`로 닫은 뒤 새 설정으로 full reconcile한다. `sod_eod_channel_id` 단독 변경은 voice session/run을 닫거나 재계산하지 않는다. 대신 최초 지정이면 period를 열고, A→B면 A period를 `channel_changed`로 닫고 B period를 열며, B→A도 새 A period를 연다. 새 현재 채널의 `(guild_id, channel_id)` sync state는 같은 트랜잭션에서 생성/선택한다. 대상 역할·카테고리의 삭제 또는 접근 불가가 감지되면 해당 설정 ID를 null로 만들고 열린 세션은 `config_changed`, coverage run은 `config_invalid`로 닫는다. SoD/EoD 채널 삭제·무효화는 현재 period를 `config_invalid`로 닫고 channel ID를 null로 만든다. 어느 경우나 `/활동설정 상태`에 관리자 경고를 남긴다. 프로세스 재시작은 설정과 channel period를 닫지 않는다. 독서실과 스터디의 같은 category는 앱 검증과 schema CHECK 모두 거부한다.

음성 수집이 유효한 것은 target role·reading category·study category가 모두 유효하고 guild collection gate가 열린 동안이다. 최초 유효화 시각만 `voice_collection_started_epoch`에 기록하되, 보고서 가용성은 이 하나의 날짜로 판단하지 않고 `voice_collection_runs` 전 구간으로 판단한다. 유효화+ready 또는 disconnect 뒤 full reconcile에서 run을 열고 1분 heartbeat에 `last_checkpoint_epoch`를 갱신한다. 설정 변경/무효화, graceful shutdown, gateway disconnect, restart recovery에서 run을 닫는다.

Cog load 시에는 DB/schema만 준비하고 recovery를 시작하지 않는다. recovery task는 `bot.wait_until_ready()` 뒤 guild별 once/idempotent로 실행한다.

1. recovery 시작 전에 존재한 열린 `voice_sessions.id`와 열린 `voice_collection_runs.id`를 snapshot한다.
2. recovery gate(각 guild의 collection gate) 전 Gateway 이벤트는 DB에 쓰지 않고 dirty member/guild만 메모리에 표시한다.
3. snapshot에 있던 행만 `last_checkpoint_epoch`에 `restart_checkpoint`로 닫는다. recovery 중 새로 열린 세션/run은 snapshot에 없으므로 닫지 않는다.
4. 각 guild의 현재 멤버 상태를 full reconcile하고, 유효 구성이라면 coverage run을 연다. dirty 표시도 이 reconcile로 해소한다.
5. gate를 set한다. 이후 이벤트는 즉시 reconcile한다.

`on_disconnect`는 모든 guild collection gate를 즉시 닫아 새 Gateway event DB write와 heartbeat를 멈춘다. DB에 접근할 수 있으면 disconnect epoch에 열린 세션과 coverage run을 `gateway_disconnect`로 멱등 종료한다. 종료 DB write가 실패하거나 프로세스가 crash하면, 다음 `on_resumed` 또는 새 ready가 pre-resume 열린 row를 snapshot해 `last_checkpoint_epoch`에서 `gateway_disconnect`(disconnect가 확인된 경우) 또는 `restart_checkpoint`(crash만 확인된 경우)로 닫는다. 그 뒤 full current-state reconcile로 resume epoch부터 새 session/run을 열고 collection gate를 set한다. `on_resumed`는 이 pre-resume closure와 reconcile 외에 startup closure를 반복하지 않는다. disconnect interval, 그 동안의 leave/move, crash 뒤 checkpoint 이후와 downtime은 절대 체류로 집계하지 않는다. 현재 열려 있는 세션의 보고서 계산은 `started_epoch`부터 조회 종료까지 overlap하므로 checkpoint부터만 계산하지 않으며, 조회 자체는 DB write를 하지 않는다.

## 7. SoD/EoD 수집과 backfill

지정한 현재 텍스트 채널의 일반 사용자 메시지만 검사한다. 본문은 수신/조회 시점에만 `casefold()`와 ASCII whole-word 정규식으로 검사한다. `SoD`, `EOD!`는 인식하고 `sodastream`, `preEoDpost`는 인식하지 않는다. 한 메시지에 둘 다 있으면 `sod`, `eod` 이벤트를 각각 저장한다. 작성 epoch를 KST 날짜로 바꿔 daily를 유형별 1회 upsert하고, 둘 중 하나가 있으면 통합 활동일은 1일이다. 본문은 저장하지 않는다.

수집/스캔 당시 target role이 없는 계정의 이벤트는 저장하지 않는다. 이후 역할 제거 시 과거 이벤트는 보존하지만 현재 보고서 대상에서는 제외한다. 라이브 create 시점과 backfill 스냅샷만 기록하며 이후 `on_message_edit`·`on_message_delete`는 기존 이벤트/daily를 바꾸지 않는다.

sync state는 `(guild_id, channel_id)` PK로 채널 범위다. `sod_eod_channel_periods`는 설정된 채널의 실제 활성 기간을 보존한다. guild single-flight lock은 backfill과 `sod_eod` 채널 변경을 직렬화한다. 현재 채널 변경은 period와 새 channel state를 `BEGIN IMMEDIATE`에서 원자 전환하며, 이전 채널 이벤트 metadata와 sync state는 보존한다. 보고서는 channel period와 채널별 history/sync를 조합해 설정 전환, 현재 채널 미동기화, 이전·새 채널의 coverage gap을 정확히 경고한다.

backfill은 state의 marker가 없으면 채널 전체를 `oldest_first`로, marker가 있으면 `after=newest_processed_message_id`부터 `oldest_first`로 읽는다. 따라서 중단은 최신 cursor 이후부터 재개하고, 최초 완료 뒤 재실행은 다운타임 중 새 메시지를 보충한다. 메시지 하나마다 0/1/2 event insert-ignore, daily upsert, 최신 cursor update를 **하나의 SQLite 트랜잭션**에 커밋한다. marker가 없거나 비대상인 메시지도 cursor는 같은 트랜잭션에서 전진한다. API/DB 오류면 rollback하고 cursor를 전진시키지 않은 채 scan을 중단한다. live와 backfill은 같은 insert-ignore 경로라 순서와 무관하게 멱등이다. 최초 전체 스캔 완료 시 접근한 메시지 중 가장 오래된 `message_created_epoch`를 해당 channel state의 `history_from_epoch`에 기록한다. 접근 가능한 메시지가 없으면 null이다.

## 8. 집계·보고서·TXT

기간은 KST `[시작일 00:00:00, 종료일 다음날 00:00:00)`을 UTC epoch 범위로 변환한다. 양 끝 날짜를 포함하며 기간 최대값은 두지 않는다. 약 55명과 필수 SQLite 인덱스 범위에서 긴 기간도 허용한다. 종료·진행 세션은 이 범위와 clip된 양의 길이만 합산한다. `session_count`는 **조회 범위와 양의 길이로 overlap한 세션 수**다.

대상은 조회 시점의 현재 target role 보유 non-bot 멤버 전원이다. 정렬은 기록 없음 → 마지막 활동이 더 오래됨 → 이름 casefold다. 표 열은 이름, 마지막 활동, 독서실 시간, 스터디 시간, SoD일, EoD일, 통합일이다. 마지막 활동은 모든 보존된 활동 event/세션 중 가장 최근 정확한 UTC·KST timestamp이며, 없으면 `기록 없음`이다.

voice run coverage가 조회 범위와 겹치지 않거나 run 사이 공백(특히 gateway disconnect interval)이 있으면 그 시간의 음성 값은 0 또는 수집된 부분값일 수 있음을 경고한다. SoD/EoD는 channel period와 채널별 history/sync를 조합해 현재 채널 history가 null, 최초 history 이후에만 부분 수집, 설정 전환 gap, backfill 미완료 상태를 경고한다. 경고는 수치를 채우거나 추정하지 않는다.

응답은 15명/page다. View timeout은 10분(15분 미만)이며 timeout 때 원래 ephemeral message를 edit해 모든 버튼을 disabled로 만든다. message edit 실패는 로그만 남긴다. 버튼은 클릭 시점의 관리자·최초 실행자 재검증을 수행한다.

`전체 TXT`는 `activity-report-YYYYMMDD-YYYYMMDD-kst.txt`로 ephemeral 첨부한다. 헤더에 기간, 생성 UTC/KST, voice coverage run/gap(포함된 gateway disconnect interval), SoD/EoD channel period별 history/sync/gap 경고를 쓴다. 현재 대상 멤버를 한 줄씩 모두 쓰며 `이름`, `user_id`, 마지막 활동 UTC/KST, 독서실·스터디 초, 각각의 `session_count`, SoD일, EoD일, 통합일을 포함한다. 기록 없는 멤버는 `없음`/0으로 명시한다.

## 9. 보안·개인정보·한계

저장 식별자는 guild/user/role/category/channel/message ID, 활동 유형, UTC epoch, KST 날짜뿐이다. DB는 `/data/activity.db`와 WAL/SHM를 Fly volume에 두고 이미지·git에 포함하지 않는다. 보고서/TXT는 administrator에게만 ephemeral로 보이고, 로그에 메시지 본문이나 TXT 전체를 남기지 않는다.

배포 전 음성 활동은 소급할 수 없다. 봇 다운타임과 Gateway disconnect interval의 라이브 수집은 불가능하고 coverage gap으로 드러난다. 삭제되어 API에서 볼 수 없는 과거 메시지는 backfill할 수 없다. 단일 Fly volume은 장애 지점이므로 백업·복구를 운영에서 고려한다. 실제 발화 시간은 측정하지 않는다.

## 10. 테스트 전략과 baseline

새 기능은 표준 라이브러리만 추가하는 설계에 맞춰 `unittest`를 사용한다. `IsolatedAsyncioTestCase`, 임시 SQLite, Discord fixture로 자동화한다. 구현 전 architect 실행 baseline은 다음으로 고정한다.

| 스크립트 | baseline |
|---|---|
| `scripts/verify_wallet.py` | PASS |
| `scripts/verify_load_data.py` | PASS |
| `scripts/verify_final.py` | AC4 overdraft reject assertion FAIL |

구현 뒤 새 unittest는 모두 PASS해야 한다. 기존 스크립트는 baseline과 비교하여 새 실패가 없어야 하며, 위 `verify_final.py` 기존 불일치는 별도 보고한다. 활동 기능 범위에서 unrelated wallet 동작이나 테스트를 수정하지 않는다.

필수 테스트는 다음을 포함한다.

- 부분 설정의 순서 무관성, invalid rollback/기존 설정 유지, 구성 완료와 이미 음성 중인 멤버의 즉시 수집, 유효 A→유효 B 음성 설정 변경 중 현재 접속자의 기존 세션 종료와 새 세션 시작
- 입장·같은 kind 이동·kind 변경·퇴장·역할 추가/제거·category/role 삭제, config change/invalid, `reconcile_member`의 네 desired/open 조합
- race/restart 중 열린 세션 하나 보장, recovery snapshot/gate에서 새 row 미종료, dirty event full reconcile, Gateway disconnect 중 gate/heartbeat 중단·leave/move 미집계·반복 disconnect/resume, on_resumed/새 ready full reconcile, coverage run/gap
- 짧은 세션, 초 단위 clipping, 진행 세션, checkpoint crash 상한과 downtime 제외
- SoD/EoD case/whole-word/both/daily dedupe, role filter, create 뒤 edit/delete 불변성
- 채널별 backfill state와 channel period, A→B→A 전환·삭제/무효화·재시작 period 유지, after+oldest_first 재개, 다운타임 보충, live 동시성, 메시지별 atomic event/daily/cursor rollback
- `sod_eod_channel_id` 단독 변경이 voice session/run 및 음성 `session_count`를 닫거나 바꾸지 않는 회귀
- 기록 0 대상 멤버, 55명 정렬, 15명 pagination, TXT 모든 멤버·ID·timestamp·양의 overlap session_count·coverage 경고
- callback/button 재인증과 최초 실행자 일치, defer 응답, 10분 timeout disabled edit 실패 로그
- extension DB init/load 실패 시 활동 명령이 등록되지 않고 RSS·wallet 로그인/loop가 지속되는 격리, 정상 load 뒤 개별 DB/config 오류의 활동 명령 ephemeral 응답
- Dockerfile copy, SQLite `/data` open, Fly Machine/volume 지속성 smoke

Fly smoke는 실제 `fly machine list`로 Machine이 1개인지, `fly volumes list`와 machine 정보를 통해 volume이 1:1 attach인지, `/data` mount인지 확인한다. deploy·restart 뒤 `/data/activity.db`, `activity.db-wal`, `activity.db-shm`와 활동 데이터가 지속되는지 확인한다. `fly.toml`만으로 단일 Machine을 주장하지 않는다.

## 11. 수용 기준

1. Administrator parent Group 권한·guild-only와 모든 callback/button의 클릭 시점 관리자·최초 실행자 재검증이 동작한다.
2. 부분 설정은 순서 무관하고 invalid 입력은 rollback한다. 세 핵심 음성 설정 중 하나가 실제로 바뀌면 유효 A→유효 B를 포함해 항상 full voice reconcile하며, 새 구성이 유효하면 현재 접속자의 새 세션을 시작하고 미완성·무효면 gate를 닫는다. `sod_eod_channel_id` 단독 변경은 voice reconcile을 하지 않는다.
3. 수집/스캔 당시 target role 없는 계정은 저장하지 않고, 이후 역할 제거의 과거 기록은 보존하되 현재 보고서에서는 제외한다.
4. 음성 세션은 초 단위이며 같은 kind 이동은 연속, kind 변경은 분할, 사용자당 열린 세션은 하나다. recovery gate는 startup snapshot만 닫고 새 세션은 닫지 않는다. Gateway disconnect는 gate를 닫고 열린 row를 멱등 종료하며, resume/ready full reconcile 뒤에만 새 row를 연다.
5. coverage run과 SoD/EoD channel period별 sync/history로 모든 조회 범위의 가용·중단 구간(포함된 gateway disconnect와 설정 전환)을 경고하며 누락 시간을 추정하지 않는다.
6. 지정 채널에서만 whole-word·case-insensitive SoD/EoD를 저장하고, 한 메시지의 둘 다·유형별 daily dedupe·edit/delete 불변 정책을 지킨다.
7. backfill은 channel period와 채널 scoped state, after+oldest_first, 메시지별 atomic cursor, live 멱등성, channel change 직렬화를 만족한다. SoD/EoD 채널 단독 변경은 음성 session/run 또는 session_count를 바꾸지 않는다.
8. KST 양 끝 포함 조회는 기간 상한 없이 overlap을 clip하고, 15명 페이지·10분 timeout·전체 TXT에 정의된 수치와 경고를 제공한다.
9. 활동 extension load/초기 schema 실패 시 활동 명령은 등록되지 않지만 RSS·wallet·로그인은 지속한다. 정상 load 뒤 개별 DB/config 오류는 해당 활동 명령의 ephemeral 오류로 한정되고 RSS·wallet을 중단하지 않는다.
10. 새 unittest 전부 PASS, 기존 검증은 확정 baseline 대비 새 실패 없음, Fly 실물 Machine/volume persistence smoke가 확인된다.
