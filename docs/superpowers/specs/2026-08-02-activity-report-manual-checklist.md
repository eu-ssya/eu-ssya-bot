# 활동 현황 보고서 배포·영속성 수동 수용 체크리스트

이 문서는 `eu-ssya-bot`의 활동 현황 기능을 실제 Fly 앱에서 수용하기 위한 운영 체크리스트다. **Task 12 구현 세션에서는 Fly 조회, deploy, restart, SSH를 포함한 외부 smoke를 실행하지 않았다.** 아래 Fly 항목은 모두 실제 운영자가 확인할 때까지 의도적으로 미체크 상태다.

공식 참고 자료:

- [Discord Gateway Intents](https://discord.com/developers/docs/events/gateway#gateway-intents)
- [Discord Permissions](https://discord.com/developers/docs/topics/permissions)
- [Fly Machines](https://fly.io/docs/machines/)
- [Fly Volumes](https://fly.io/docs/volumes/)
- [`fly volumes list` JSON output](https://fly.io/docs/flyctl/volumes-list/)
- [Fly volume snapshots](https://fly.io/docs/volumes/snapshots/)
- [Fly Secrets](https://fly.io/docs/apps/secrets/)

## 1. 로컬 수용

- [x] `Dockerfile`이 `bot.py`, `wallet_cog.py`, `activity_cog.py`, `activity_store.py`를 모두 복사한다.
- [x] Docker 이미지의 기본값과 `fly.toml`에 `DATA_FILE=/data/rss_data.json`, `ACTIVITY_DB_PATH=/data/activity.db`가 있다.
- [x] `fly.toml`의 기존 `[[mounts]] source="bot_data" destination="/data"`, VM 크기, deploy 전략은 유지되고 토큰과 `release_command`가 없다.
- [x] `.gitignore`와 `.dockerignore`가 `activity.db`, `activity.db-wal`, `activity.db-shm`, 루트 `/data/` 로컬 산출물을 제외한다.
- [x] Python compile, 전체 unittest, `verify_wallet.py`, `verify_load_data.py`가 성공한다.
- [x] `verify_final.py`의 유일한 실패가 기존 baseline인 AC4 overdraft reject assertion인지 확인한다.
- [x] `requirements.txt` 변경, 추적 중인 DB 파일, conflict marker, 미완료 작업 주석이 없다.
- [ ] Docker가 이미 로컬에 있으면 임시 태그로 이미지를 build하고 non-deploy import check를 실행한다. Docker가 없거나 daemon이 꺼져 있으면 선택 검사를 생략하고 이유를 기록한다.

Task 12 세션의 선택 Docker 검사는 daemon `27.4.0`까지 확인했지만 `python:3.13-slim` base image가 로컬에 없었다. 선택 검사만을 위해 pull하지 않는다는 원칙에 따라 build/import를 생략했다.

로컬 명령:

```powershell
& "venv\Scripts\python.exe" -m py_compile bot.py activity_cog.py activity_store.py wallet_cog.py
& "venv\Scripts\python.exe" -m unittest discover -s tests -v
& "venv\Scripts\python.exe" scripts\verify_wallet.py
& "venv\Scripts\python.exe" scripts\verify_load_data.py
& "venv\Scripts\python.exe" scripts\verify_final.py
git diff --check
git status --short
rg -n "[T]ODO|[F]IXME|<{7}|={7}|>{7}" activity_cog.py activity_store.py tests README.md docs\superpowers\specs\2026-08-02-activity-report-manual-checklist.md
git diff -- requirements.txt
git ls-files | Select-String -Pattern '(^|/)(activity\.db|activity\.db-wal|activity\.db-shm)$'
```

## 2. 배포 전 Discord·운영 확인 — 외부, 미실행

- [ ] Developer Portal에서 **Server Members Intent**와 **Message Content Intent**를 먼저 활성화했다. 이 작업은 deploy보다 먼저 한다.
- [ ] 코드는 표준 `guilds`, `voice_states`, `guild_messages` intent를 사용한다.
- [ ] 대상 guild/channel에서 View Channels, Read Message History, Send Messages, Attach Files, Use Application Commands를 부여했다.
- [ ] 모임통장을 사용하면 별도로 Manage Channels를 부여했다.
- [ ] 활동 기능에는 Connect와 Speak 권한이 필요하지 않음을 확인했다.
- [ ] `DISCORD_BOT_TOKEN`은 `fly.toml`이 아니라 Fly secret에만 있다.
- [ ] 테스트 시간대의 관리자 활동을 멈추거나, 멈출 수 없으면 아래 monotonic fallback을 사용할 운영 결정을 기록했다.
- [ ] SQLite volume은 자동 복제되지 않는 단일 장애 지점이므로 snapshot/백업과 복구 절차를 확인했다.

## 3. 실제 Machine·volume 사전 점검 — 외부, 미실행

아래 명령은 Machine과 volume을 변경하지 않는 조회다. JSON에서 **정확히 하나의 실제 Machine과 하나의 `bot_data` volume** 및 일치하는 attachment를 얻지 못하면 이후 deploy/restart/SSH를 진행하지 않는다. 이후 절차는 같은 PowerShell 세션에서 실행하고, 새 세션을 열었다면 이 블록부터 다시 실행한다.

```powershell
$App = "eu-ssya-bot"
function Get-SoleFlyStorage([string]$AppName) {
    $machines = @(fly machine list -a $AppName --json | ConvertFrom-Json)
    $machineIds = @($machines | ForEach-Object { [string]$_.id } | Where-Object { $_ })
    if ($machineIds.Count -ne 1) {
        throw "Expected exactly one Fly Machine, found $($machineIds.Count)"
    }
    if ([string]$machines[0].state -cne "started") {
        throw "Expected the sole Fly Machine to be running (state=started), found $($machines[0].state)"
    }

    $volumes = @(fly volumes list -a $AppName --json | ConvertFrom-Json)
    if ($volumes.Count -ne 1) {
        throw "Expected exactly one Fly Volume, found $($volumes.Count)"
    }
    $volume = $volumes[0]
    if ([string]$volume.name -cne "bot_data") {
        throw "Expected volume name bot_data, found $($volume.name)"
    }
    if ([string]$volume.attached_machine_id -cne $machineIds[0]) {
        throw "Volume is not attached to the sole Machine: volume=$($volume.attached_machine_id) machine=$($machineIds[0])"
    }

    [pscustomobject]@{
        MachineId = $machineIds[0]
        MachineState = [string]$machines[0].state
        VolumeId = [string]$volume.id
        VolumeName = [string]$volume.name
        AttachedMachineId = [string]$volume.attached_machine_id
    }
}
$topology = Get-SoleFlyStorage $App
$machineId = $topology.MachineId
$topology | Format-List
```

- [ ] JSON 검사 결과 실제 Machine ID가 정확히 하나이고 `state=started`(실행 중)다.
- [ ] JSON 검사 결과 volume이 정확히 하나이고 이름은 `bot_data`다.
- [ ] JSON의 `attached_machine_id`가 `$machineId`와 정확히 같고 mount destination은 배포 구성의 `/data`다.
- [ ] Machine 하나와 volume 하나 제약을 유지한다. scale-out, volume 추가/복제, destroy/force 명령을 사용하지 않는다.

Fly volume은 한 번에 한 Machine에만 붙는 로컬 영속 스토리지이며 자동 복제되지 않는다. 이 앱은 SQLite 때문에 정확히 한 Machine과 한 volume을 유지한다.

## 4. 구성 적용 deploy — 외부, 미실행

deploy는 Dockerfile과 `fly.toml` 구성을 적용하는 별도 단계다. restart만으로는 변경된 구성이 적용되지 않는다.

```powershell
fly deploy -a $App
```

- [ ] Portal intent와 권한 확인 뒤 `fly deploy -a $App`를 별도로 실행했다.
- [ ] deploy 성공 뒤 다음 조회를 다시 실행해 Machine이 여전히 정확히 하나인지 확인하고 `$machineId`를 최신 실제 ID로 갱신했다.

```powershell
$topology = Get-SoleFlyStorage $App
$machineId = $topology.MachineId
$topology | Format-List
```

- [ ] deploy 후에도 `bot_data`가 최신 `$machineId`에 연결되어 있다.

## 5. Discord 기능 smoke — 외부, 미실행

- [ ] Administrator가 다음 네 설정을 각각 실행했다. 네 설정은 독립적이며 순서는 무관하다.
  - [ ] `/활동설정 대상역할 역할:<Role>`
  - [ ] `/활동설정 독서실 카테고리:<Category>`
  - [ ] `/활동설정 스터디 카테고리:<Category>`
  - [ ] `/활동설정 sod_eod 채널:<TextChannel>`
- [ ] `/활동설정 상태`에서 네 설정과 수집/동기화 상태를 확인했다.
- [ ] 최초 과거동기화 전 재시작에서는 채널 전체 history가 자동 조회되지 않고 동기화 미완료 경고가 유지된다.
- [ ] `/활동설정 과거동기화`가 성공했다.
- [ ] 메시지가 없는 채널도 최초 과거동기화의 history 조회 시작 직전 snowflake 경계가 exclusive cursor로 생기며, 조회 종료~완료 기록 사이 및 같은 millisecond에 도착한 메시지가 이후 delta에서 회수된다.
- [ ] 구형 DB의 실제 메시지 cursor 없는 완료 상태는 미완료로 전환되고, history 전체 자동 조회 없이 명시적 과거동기화를 기다린다.
- [ ] 최초 동기화 뒤 Gateway/guild unavailable 복귀 시 누락 메시지가 자동 delta로 보충되고, 준비 DB 쓰기나 권한/API 실패 직후부터 완료 상태 대신 부분 데이터 경고와 중복 없는 지연 재시도가 유지된다.
- [ ] `/활동현황 최근 일수:<N>` 보고서를 열었다.
- [ ] `/활동현황 기간 시작일:<YYYY-MM-DD> 종료일:<YYYY-MM-DD>` 보고서를 열었다.
- [ ] 관리자 본인이 이전/다음/TXT 버튼을 사용할 수 있고 TXT가 ephemeral 첨부로 온다.
- [ ] 비관리자의 활동 설정/보고서 명령 접근은 데이터 없이 ephemeral denial만 반환한다.
- [ ] 비관리자 또는 최초 실행자가 아닌 사용자의 이전/다음/TXT 클릭은 데이터·파일 없이 ephemeral denial만 반환한다.
- [ ] 보고서를 연 뒤 10분 동안 사용하지 않으면 View의 모든 버튼이 disabled 된다.
- [ ] 음성 기록은 배포와 유효한 세 가지 음성 설정 이후부터만 시작하며 과거 음성은 소급되지 않음을 확인했다.
- [ ] 삭제되었거나 권한/API 때문에 접근 불가능한 SoD/EoD 이력은 backfill되지 않는다는 운영 한계를 확인했다.

## 6. restart 전 SQLite snapshot — 외부, 미실행

`sqlite3` CLI 설치를 가정하지 않는다. 컨테이너의 Python 표준 라이브러리를 사용하고, SSH 대상은 반드시 검증한 `$machineId`로 고정한다. restart는 열린 `voice_sessions`와 `voice_collection_runs`를 정상 종료하고 현재 Discord 상태를 새 열린 행으로 reconcile하므로 두 음성 테이블은 전체 checksum 동일성을 요구하면 안 된다.

먼저 운영 모드를 선택한다.

- `PAUSED`(권장): 설정 변경, backfill, SoD/EoD 메시지, 음성 입·이동·퇴장을 멈춘다. 지정 테스트 멤버 한 명은 감시 음성 채널에 그대로 연결해 둔다.
- `ACTIVE`: 일반 사용자 트래픽을 멈출 수 없는 경우다. 설정 명령과 backfill만은 중단하고, 발생한 SoD/EoD·음성 이벤트를 별도 기록한다. 이 모드는 더 약한 append-only/voice 전이 증명임을 결과에 남긴다.

```powershell
$trafficMode = (Read-Host "Type PAUSED (recommended) or ACTIVE").ToUpperInvariant()
if ($trafficMode -notin @("PAUSED", "ACTIVE")) { throw "Invalid traffic mode" }

$snapshotCode = @'
import hashlib
import json
import os
import sqlite3
import time

p = "/data/activity.db"
assert os.path.isfile(p), "activity.db missing"
c = sqlite3.connect("file:" + p + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row

def rows(table):
    return [dict(row) for row in c.execute("select * from " + table + " order by rowid")]

def digest(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()

stable_names = ("activity_config", "sod_eod_channel_periods")
append_names = ("sod_eod_events", "sod_eod_daily")
stable = {name: rows(name) for name in stable_names}
append_only = {name: rows(name) for name in append_names}
result = {
    "exists": 1,
    "captured_epoch": int(time.time()),
    "stable": stable,
    "stable_counts": {name: len(value) for name, value in stable.items()},
    "stable_checksum": digest(stable),
    "append_only": append_only,
    "append_counts": {name: len(value) for name, value in append_only.items()},
    "append_checksum": digest(append_only),
    "sync_state": rows("activity_sync_state"),
    "voice_sessions": rows("voice_sessions"),
    "voice_collection_runs": rows("voice_collection_runs"),
}
print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
'@
$snapshotBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($snapshotCode))
$snapshotCommand = "python -c `"import base64;exec(base64.b64decode('$snapshotBase64'))`""
$beforeLine = fly ssh console -a $App --machine $machineId -C $snapshotCommand | Select-Object -Last 1
$before = $beforeLine | ConvertFrom-Json
$before | ConvertTo-Json -Depth 12
if ([int]$before.exists -ne 1) { throw "Pre-restart activity.db does not exist" }
```

- [ ] `$before.exists`가 `1`이고 snapshot JSON 전체를 변경 불가능한 작업 기록에 저장했다.
- [ ] `PAUSED` 모드라면 `$before.voice_sessions`에 지정 테스트 멤버의 열린 행이, `$before.voice_collection_runs`에 열린 run이 최소 하나 있다.
- [ ] WAL/SHM 파일은 checkpoint 뒤 없어질 수 있으므로 그 존재 자체를 영속성 판정 기준으로 사용하지 않았다.

## 7. 명시적 결정 뒤 정확한 ID 하나만 restart — 외부, 미실행

restart는 서비스 중단을 일으키는 운영 작업이다. 담당자가 선택한 traffic mode와 중단 영향을 확인한 경우에만 실행한다. destroy, force, 전체/복수 Machine 대상 명령은 사용하지 않는다.

```powershell
$decision = Read-Host "Mode=$trafficMode. Type RESTART to restart only $machineId"
if ($decision -cne "RESTART") { throw "Restart not approved" }
fly machine restart $machineId -a $App
```

- [ ] 담당자가 traffic mode, 서비스 중단과 restart를 명시적으로 승인했다.
- [ ] JSON으로 검증한 정확한 `$machineId` 하나만 restart했다.
- [ ] restart 완료 뒤 topology 검사에서 Machine이 여전히 정확히 하나이고 `state=started`다.

## 8. restart 후 snapshot과 lifecycle 판정 — 외부, 미실행

```powershell
$afterLine = fly ssh console -a $App --machine $machineId -C $snapshotCommand | Select-Object -Last 1
$after = $afterLine | ConvertFrom-Json
$after | ConvertTo-Json -Depth 12
if ([int]$after.exists -ne 1) { throw "Post-restart activity.db does not exist" }
```

설정과 channel period는 restart가 바꾸지 않으므로 traffic mode와 무관하게 count와 checksum이 정확히 같아야 한다. sync state는 restart 직후 incomplete로 전환된 뒤 cursor delta 성공 시 completed/updated가 바뀌므로 아래에서 별도 검증한다.

```powershell
$stableTables = @("activity_config", "sod_eod_channel_periods")
foreach ($table in $stableTables) {
    $beforeCount = [int64]$before.stable_counts.PSObject.Properties[$table].Value
    $afterCount = [int64]$after.stable_counts.PSObject.Properties[$table].Value
    if ($afterCount -ne $beforeCount) { throw "Stable table count changed: $table" }
}
if ($before.stable_checksum -cne $after.stable_checksum) {
    throw "Stable table checksum changed"
}
```

sync state는 기존 채널 행과 initialization/history 의미를 보존하고 cursor가 후퇴하지 않아야 한다. 초기화된 채널은 자동 delta 성공 뒤 completed가 복구되어야 하며, 초기화 전 채널은 completed가 null인 채 history 전체 자동 조회 없이 명시적 과거동기화를 기다린다.

```powershell
$beforeSync = @($before.sync_state)
$afterSync = @($after.sync_state)
foreach ($row in $beforeSync) {
    $matches = @($afterSync | Where-Object {
        [int64]$_.guild_id -eq [int64]$row.guild_id -and
        [int64]$_.channel_id -eq [int64]$row.channel_id
    })
    if ($matches.Count -ne 1) { throw "Sync row missing or duplicated: guild=$($row.guild_id) channel=$($row.channel_id)" }
    $next = $matches[0]
    foreach ($field in @("initialized_epoch", "history_from_epoch")) {
        if ([string]$next.$field -cne [string]$row.$field) { throw "Sync meaning changed: channel=$($row.channel_id) field=$field" }
    }
    if ($null -ne $row.newest_processed_message_created_epoch -and
        [int64]$next.newest_processed_message_created_epoch -lt [int64]$row.newest_processed_message_created_epoch) {
        throw "Sync cursor epoch regressed: channel=$($row.channel_id)"
    }
    if ($null -ne $row.initialized_epoch -and $null -eq $next.completed_epoch) {
        throw "Initialized sync did not complete automatic delta: channel=$($row.channel_id)"
    }
    if ($null -eq $row.initialized_epoch -and $null -ne $next.completed_epoch) {
        throw "Uninitialized sync was incorrectly auto-completed: channel=$($row.channel_id)"
    }
}
```

SoD/EoD 테이블은 `PAUSED`에서는 exact count/checksum을 요구한다. `ACTIVE`에서는 기존 row가 같은 rowid 순서의 prefix로 모두 보존되고 새 row만 뒤에 추가되는지 확인한다. 이는 restart에 의한 정상 음성 행 변경과 사용자 메시지로 인한 append를 구분한다.

```powershell
$appendTables = @("sod_eod_events", "sod_eod_daily")
if ($trafficMode -ceq "PAUSED") {
    foreach ($table in $appendTables) {
        $beforeCount = [int64]$before.append_counts.PSObject.Properties[$table].Value
        $afterCount = [int64]$after.append_counts.PSObject.Properties[$table].Value
        if ($afterCount -ne $beforeCount) { throw "Paused append-only count changed: $table" }
    }
    if ($before.append_checksum -cne $after.append_checksum) {
        throw "Paused append-only checksum changed"
    }
} else {
    foreach ($table in $appendTables) {
        $beforeRows = @($before.append_only.PSObject.Properties[$table].Value)
        $afterRows = @($after.append_only.PSObject.Properties[$table].Value)
        if ($afterRows.Count -lt $beforeRows.Count) { throw "Append-only table shrank: $table" }
        for ($i = 0; $i -lt $beforeRows.Count; $i++) {
            $beforeJson = $beforeRows[$i] | ConvertTo-Json -Compress -Depth 5
            $afterJson = $afterRows[$i] | ConvertTo-Json -Compress -Depth 5
            if ($beforeJson -cne $afterJson) { throw "Existing append-only row changed: $table index=$i" }
        }
    }
    Write-Warning "ACTIVE fallback used; record expected SoD/EoD events and append checksums"
}
```

음성 테이블은 restart lifecycle을 별도로 검증한다. 기존 closed row는 그대로 보존되어야 한다. restart 전 open row는 같은 ID로 보존되어 정상 종료되어야 하며, hard-recovery면 `restart_checkpoint`와 checkpoint 시각, graceful/disconnect 경로면 허용된 사유와 checkpoint 이후 종료 시각을 가져야 한다. 유효 설정의 guild에는 새 열린 run이 생긴다. `PAUSED`에서 감시 채널에 그대로 둔 테스트 멤버는 같은 guild/user/kind의 새 열린 session이 생긴다.

```powershell
function Row-Json($row) { $row | ConvertTo-Json -Compress -Depth 5 }
function Find-ById($rows, [int64]$id) {
    $found = @($rows | Where-Object { [int64]$_.id -eq $id })
    if ($found.Count -ne 1) { throw "Expected one preserved row id=$id, found $($found.Count)" }
    $found[0]
}
function Assert-ClosedTransition($beforeRow, $afterRow, [string]$reasonField, [string[]]$allowedReasons) {
    foreach ($field in @("id", "guild_id", "started_epoch")) {
        if ([string]$beforeRow.$field -cne [string]$afterRow.$field) { throw "Voice row field changed: id=$($beforeRow.id) field=$field" }
    }
    if ($beforeRow.PSObject.Properties["user_id"]) {
        foreach ($field in @("user_id", "activity_kind")) {
            if ([string]$beforeRow.$field -cne [string]$afterRow.$field) { throw "Voice session identity changed: id=$($beforeRow.id) field=$field" }
        }
    }
    $reason = [string]$afterRow.$reasonField
    if ($reason -notin $allowedReasons) { throw "Unexpected close reason: id=$($beforeRow.id) reason=$reason" }
    if ($null -eq $afterRow.ended_epoch) { throw "Prior open row is still open: id=$($beforeRow.id)" }
    if ([int64]$afterRow.last_checkpoint_epoch -lt [int64]$beforeRow.last_checkpoint_epoch) {
        throw "Checkpoint regressed: id=$($beforeRow.id)"
    }
    if ($reason -ceq "restart_checkpoint" -and [int64]$afterRow.ended_epoch -ne [int64]$afterRow.last_checkpoint_epoch) {
        throw "restart_checkpoint must close at checkpoint: id=$($beforeRow.id)"
    }
    if ($reason -cne "restart_checkpoint" -and [int64]$afterRow.ended_epoch -lt [int64]$afterRow.last_checkpoint_epoch) {
        throw "Close precedes checkpoint: id=$($beforeRow.id)"
    }
}
function Assert-NewOpen($row, [string]$reasonField) {
    if ($null -ne $row.ended_epoch -or $null -ne $row.$reasonField) { throw "Replacement row is not open: id=$($row.id)" }
    if ([int64]$row.started_epoch -lt [int64]$before.captured_epoch -or [int64]$row.started_epoch -gt [int64]$after.captured_epoch) {
        throw "Replacement row start is outside restart window: id=$($row.id)"
    }
    if ([int64]$row.last_checkpoint_epoch -lt [int64]$row.started_epoch) { throw "Replacement checkpoint precedes start: id=$($row.id)" }
}

$beforeSessions = @($before.voice_sessions)
$afterSessions = @($after.voice_sessions)
$beforeRuns = @($before.voice_collection_runs)
$afterRuns = @($after.voice_collection_runs)
$beforeSessionIds = @($beforeSessions | ForEach-Object { [int64]$_.id })
$beforeRunIds = @($beforeRuns | ForEach-Object { [int64]$_.id })
$beforeOpenSessions = @($beforeSessions | Where-Object { $null -eq $_.ended_epoch })
$beforeOpenRuns = @($beforeRuns | Where-Object { $null -eq $_.ended_epoch })

foreach ($row in @($beforeSessions | Where-Object { $null -ne $_.ended_epoch })) {
    if ((Row-Json $row) -cne (Row-Json (Find-ById $afterSessions ([int64]$row.id)))) { throw "Closed session changed: id=$($row.id)" }
}
foreach ($row in @($beforeRuns | Where-Object { $null -ne $_.ended_epoch })) {
    if ((Row-Json $row) -cne (Row-Json (Find-ById $afterRuns ([int64]$row.id)))) { throw "Closed run changed: id=$($row.id)" }
}
$sessionCloseReasons = @("restart_checkpoint", "reconciled", "gateway_disconnect")
if ($trafficMode -ceq "ACTIVE") {
    $sessionCloseReasons += @("normal", "category_change", "role_removed")
}
foreach ($row in $beforeOpenSessions) {
    Assert-ClosedTransition $row (Find-ById $afterSessions ([int64]$row.id)) "closed_reason" $sessionCloseReasons
}
foreach ($row in $beforeOpenRuns) {
    Assert-ClosedTransition $row (Find-ById $afterRuns ([int64]$row.id)) "ended_reason" @("restart_checkpoint", "graceful_shutdown", "gateway_disconnect")
}

$newOpenRuns = @($afterRuns | Where-Object { $null -eq $_.ended_epoch -and ([int64]$_.id -notin $beforeRunIds) })
foreach ($prior in $beforeOpenRuns) {
    $replacement = @($newOpenRuns | Where-Object { [int64]$_.guild_id -eq [int64]$prior.guild_id })
    if ($replacement.Count -ne 1) { throw "Expected one new open run for guild $($prior.guild_id)" }
    Assert-NewOpen $replacement[0] "ended_reason"
}

if ($trafficMode -ceq "PAUSED") {
    if ($beforeOpenRuns.Count -eq 0 -or $beforeOpenSessions.Count -eq 0) { throw "PAUSED smoke requires a pre-restart open run and test session" }
    $newOpenSessions = @($afterSessions | Where-Object { $null -eq $_.ended_epoch -and ([int64]$_.id -notin $beforeSessionIds) })
    foreach ($prior in $beforeOpenSessions) {
        $replacement = @($newOpenSessions | Where-Object {
            [int64]$_.guild_id -eq [int64]$prior.guild_id -and
            [int64]$_.user_id -eq [int64]$prior.user_id -and
            [string]$_.activity_kind -ceq [string]$prior.activity_kind
        })
        if ($replacement.Count -ne 1) { throw "Expected one replacement open session for user $($prior.user_id)" }
        Assert-NewOpen $replacement[0] "closed_reason"
    }
} else {
    Write-Warning "ACTIVE fallback: session replacements follow live Discord state; record observed moves/leaves and resulting new open sessions"
}
```

- [ ] stable 설정/period 테이블은 exact count/checksum, sync는 initialization/history 보존과 cursor 비후퇴, SoD/EoD는 선택한 mode의 exact 또는 append-only 보존 규칙을 통과했다.
- [ ] 기존 closed 음성 행은 불변이고 기존 open 음성 행은 정상 사유/시각으로 종료되었다.
- [ ] 유효 guild의 새 open run과 `PAUSED` 테스트 멤버의 새 open session을 확인했다. `ACTIVE`면 실제 음성 이벤트와 결과를 기록했다.
- [ ] 아래 JSON 재검증으로 restart 뒤에도 정확히 한 실행 중 Machine과 그 ID에 연결된 `bot_data` 하나를 재확인했다.

```powershell
$topology = Get-SoleFlyStorage $App
if ($topology.MachineId -cne $machineId) {
    throw "Machine ID changed during restart smoke: before=$machineId after=$($topology.MachineId)"
}
$topology | Format-List
```

## 9. 최종 운영 확인 — 외부, 미실행

- [ ] `/data/activity.db`만 아니라 RSS/모임통장 기존 기능도 정상이다.
- [ ] 보고서의 음성 coverage와 SoD/EoD history/sync 경고가 실제 수집 범위와 일치한다.
- [ ] 메시지 본문이나 음성/오디오 내용은 저장되지 않고 이벤트 메타데이터와 세션 시각만 저장됨을 확인했다.
- [ ] 자동 점수·경고·역할 변경·강퇴 등 멤버 조치가 발생하지 않는다.
- [ ] 외부 smoke의 명령, 시간, 담당자, before/after JSON, 선택한 판정 방식을 운영 기록에 남겼다.
