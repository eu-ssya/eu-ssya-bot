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
        VolumeId = [string]$volume.id
        VolumeName = [string]$volume.name
        AttachedMachineId = [string]$volume.attached_machine_id
    }
}
$topology = Get-SoleFlyStorage $App
$machineId = $topology.MachineId
$topology | Format-List
```

- [ ] JSON 검사 결과 실제 Machine ID가 정확히 하나다.
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
- [ ] `/활동설정 과거동기화`가 성공했다.
- [ ] `/활동현황 최근 일수:<N>` 보고서를 열었다.
- [ ] `/활동현황 기간 시작일:<YYYY-MM-DD> 종료일:<YYYY-MM-DD>` 보고서를 열었다.
- [ ] 관리자 본인이 이전/다음/TXT 버튼을 사용할 수 있고 TXT가 ephemeral 첨부로 온다.
- [ ] 비관리자의 활동 설정/보고서 명령 접근은 데이터 없이 ephemeral denial만 반환한다.
- [ ] 비관리자 또는 최초 실행자가 아닌 사용자의 이전/다음/TXT 클릭은 데이터·파일 없이 ephemeral denial만 반환한다.
- [ ] 보고서를 연 뒤 10분 동안 사용하지 않으면 View의 모든 버튼이 disabled 된다.
- [ ] 음성 기록은 배포와 유효한 세 가지 음성 설정 이후부터만 시작하며 과거 음성은 소급되지 않음을 확인했다.
- [ ] 삭제되었거나 권한/API 때문에 접근 불가능한 SoD/EoD 이력은 backfill되지 않는다는 운영 한계를 확인했다.

## 6. restart 전 SQLite snapshot — 외부, 미실행

`sqlite3` CLI 설치를 가정하지 않는다. 컨테이너의 Python 표준 라이브러리를 사용하고, SSH 대상은 반드시 검증한 `$machineId`로 고정한다.

```powershell
$snapshotCode = 'import os,sqlite3,hashlib,json; p="/data/activity.db"; assert os.path.isfile(p), "activity.db missing"; c=sqlite3.connect("file:"+p+"?mode=ro",uri=True); ts=("activity_config","voice_sessions","voice_collection_runs","sod_eod_events","sod_eod_daily","activity_sync_state","sod_eod_channel_periods"); rows=[(t,c.execute("select * from "+t+" order by rowid").fetchall()) for t in ts]; print(json.dumps({"exists":1,"counts":{t:len(r) for t,r in rows},"checksum":hashlib.sha256(repr(rows).encode()).hexdigest()},sort_keys=True))'
$snapshotBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($snapshotCode))
$snapshotCommand = "python -c `"import base64;exec(base64.b64decode('$snapshotBase64'))`""
$beforeLine = fly ssh console -a $App --machine $machineId -C $snapshotCommand | Select-Object -Last 1
$before = $beforeLine | ConvertFrom-Json
$before | ConvertTo-Json -Depth 4
if ([int]$before.exists -ne 1) { throw "Pre-restart activity.db does not exist" }
```

- [ ] `$before.exists`가 `1`이다.
- [ ] 모든 테이블 count와 checksum을 변경 불가능한 작업 기록에 저장했다.
- [ ] WAL/SHM 파일은 checkpoint 뒤 없어질 수 있으므로 그 존재 자체를 영속성 판정 기준으로 사용하지 않았다.

## 7. 명시적 결정 뒤 정확한 ID 하나만 restart — 외부, 미실행

restart는 서비스 중단을 일으키는 운영 작업이다. 테스트 트래픽을 멈추고 담당자가 명시적으로 승인한 경우에만 실행한다. destroy, force, 전체/복수 Machine 대상 명령은 사용하지 않는다.

```powershell
$decision = Read-Host "Traffic is paused. Type RESTART to restart only $machineId"
if ($decision -cne "RESTART") { throw "Restart not approved" }
fly machine restart $machineId -a $App
```

- [ ] 담당자가 트래픽 중단과 restart를 명시적으로 승인했다.
- [ ] JSON으로 검증한 정확한 `$machineId` 하나만 restart했다.
- [ ] restart 완료와 Machine 정상 실행을 확인했다.

## 8. restart 후 snapshot과 판정 — 외부, 미실행

```powershell
$afterLine = fly ssh console -a $App --machine $machineId -C $snapshotCommand | Select-Object -Last 1
$after = $afterLine | ConvertFrom-Json
$after | ConvertTo-Json -Depth 4
if ([int]$after.exists -ne 1) { throw "Post-restart activity.db does not exist" }
```

트래픽을 멈춘 기본 판정은 모든 테이블 count와 논리 데이터 checksum의 정확한 일치다.

```powershell
$tables = @("activity_config", "voice_sessions", "voice_collection_runs", "sod_eod_events", "sod_eod_daily", "activity_sync_state", "sod_eod_channel_periods")
$countMismatch = @()
foreach ($table in $tables) {
    $beforeCount = [int64]$before.counts.PSObject.Properties[$table].Value
    $afterCount = [int64]$after.counts.PSObject.Properties[$table].Value
    if ($afterCount -ne $beforeCount) {
        $countMismatch += "$table ($beforeCount -> $afterCount)"
    }
}
if ($countMismatch.Count -ne 0 -or $before.checksum -ne $after.checksum) {
    throw "SQLite persistence mismatch: counts=$($countMismatch -join ', ') checksum=$($before.checksum)->$($after.checksum)"
}
```

- [ ] 트래픽을 멈춘 경우 before/after의 모든 count와 checksum이 정확히 같다.

트래픽을 멈출 수 없었다면 정확한 checksum 일치 대신 모든 테이블 count가 감소하지 않았는지 확인한다. 이 fallback은 테스트 중 발생한 정상 이벤트와 변경된 checksum을 함께 기록해야 하며, 행 수정·삭제의 완전한 무결성 증명은 아니라는 한계를 남긴다.

```powershell
$decreased = @()
foreach ($table in $tables) {
    $beforeCount = [int64]$before.counts.PSObject.Properties[$table].Value
    $afterCount = [int64]$after.counts.PSObject.Properties[$table].Value
    if ($afterCount -lt $beforeCount) {
        $decreased += "$table ($beforeCount -> $afterCount)"
    }
}
if ($decreased.Count -ne 0) {
    throw "SQLite count decreased: $($decreased -join ', ')"
}
Write-Warning "Active-traffic fallback used; record expected events and checksums: before=$($before.checksum) after=$($after.checksum)"
```

- [ ] 트래픽 활성 fallback을 쓴 경우 모든 after count가 before 이상이고, 그 사이 예상한 이벤트와 달라진 checksum을 기록했다.
- [ ] 아래 JSON 재검증으로 restart 뒤에도 정확히 한 Machine과 그 ID에 연결된 `bot_data` 하나를 재확인했다.

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
