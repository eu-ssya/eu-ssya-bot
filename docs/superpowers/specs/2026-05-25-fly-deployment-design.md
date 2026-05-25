# Fly.io 배포 디자인 — eu-ssya-bot

**작성일**: 2026-05-25
**상태**: Design (브레인스토밍 완료, 사용자 승인 대기)
**대상**: `D:\dev\eu-ssya-bot` (브랜치 `feat/deploy-render`)

## 1. Summary

eu-ssya-bot을 **Fly.io 무료 tier**에 배포하여 24/7 운영. 기존 JSON 파일 저장소를 그대로 사용하고 데이터는 Fly Volume(영구 disk)에 마운트한다. Render Free + MongoDB + UptimeRobot 같은 hack-driven 조합 대신 Discord 봇 운영의 표준 best practice인 Fly.io를 선택. **코드 변경은 단 1줄** (`DATA_FILE = os.getenv("DATA_FILE", "rss_data.json")`), 신규 파일 3개(`Dockerfile`, `fly.toml`, `.dockerignore`). `wallet_cog.py`는 한 줄도 안 바뀌고 RSS 로직, 모든 verify 스크립트, manual checklist 모두 그대로 유효. 신용카드 등록은 필요하지만 Spend Limit $0 설정으로 자동 청구 차단.

## 2. 결정 사항

| 항목 | 선택 | 근거 |
|------|------|------|
| 호스팅 | **Fly.io 무료 tier** (shared-cpu-1x, 256MB RAM) | Discord 봇 호스팅 표준, sleep 없음, 영구 무료 |
| 리전 | `nrt` (Narita, Tokyo) | 한국 사용자 latency ~30ms, Free tier 포함. Seoul `icn`은 paid only |
| 스토리지 | **Fly Volume** (`bot_data`, 1GB) → `/data` 마운트 | 영구 disk, 재시작/재배포 시 데이터 유지 |
| DB | **없음** (JSON 파일 + Volume) | 1만건 미만 데이터엔 SQL/Mongo 오버엔지니어링 |
| 데이터 파일 경로 | `/data/rss_data.json` (Fly), `rss_data.json` (로컬) | 환경변수 `DATA_FILE`로 자동 분리 |
| Keep-alive | **불필요** | Fly micro VM은 sleep 안 함 |
| HTTP 서버 | **불필요** | Discord 봇은 outbound WebSocket만 사용 |
| Migration | **빈 시작** | 라이브 테스트 데이터 손실 OK, prod 클린 출발 |
| 청구 방지 | **Spend Limit $0** | 카드는 등록되지만 자동 청구 차단 |

## 3. 비목표 (out of scope)

- 멀티 인스턴스/이중화 — Fly Free는 1 VM (충분)
- 데이터 백업 자동화 — Fly Volume snapshot은 paid tier
- 모니터링/알람 — Fly 무료 tier는 기본 로그만
- CI/CD — `fly deploy` 수동 실행 (GitHub Actions 자동화는 v2)
- MongoDB/Postgres 마이그레이션 — 데이터 1만건 넘으면 그때
- HTTP keep-alive endpoint, UptimeRobot 핑 — Fly에선 불필요

## 4. Architecture

```
   ┌─────────────────────────────────────┐
   │  Fly.io shared-cpu-1x (256MB RAM)   │
   │  region: nrt (Narita, 한국 가까움)    │
   │  ┌─────────────────────────────────┐ │
   │  │  Python 3.13-slim Docker image  │ │
   │  │  bot.py + wallet_cog.py         │ │
   │  │   ├─ /ping, /rss, /모임통장      │ │
   │  │   ├─ rss_loop (5분 폴링)         │ │
   │  │   ├─ wallet_cog                  │ │
   │  │   │  └─ rename_worker (60s)     │ │
   │  │   └─ DATA_FILE=/data/rss_data.json│
   │  └─────────────────────────────────┘ │
   │                  │                    │
   │                  ▼                    │
   │     ┌─────────────────────────────┐   │
   │     │ Volume "bot_data" (1GB)     │   │
   │     │  /data/rss_data.json (영구) │   │
   │     └─────────────────────────────┘   │
   └─────────────────────────────────────┘
                   │
                   ▼
            Discord Gateway
```

## 5. Code Changes

### 5.1 `bot.py` — 1줄 변경

```python
# Before (line ~30)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATA_FILE = "rss_data.json"

# After
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATA_FILE = os.getenv("DATA_FILE", "rss_data.json")
```

이게 전부. `wallet_cog.py`는 0줄 변경.

### 5.2 `wallet_cog.py` — 0줄

`from bot import _data_lock, load_data, save_data` 그대로. JSON 파일 경로 변경은 `DATA_FILE` 환경변수 한 곳만 영향.

### 5.3 `requirements.txt` — 0줄

새 의존성 없음. 기존:
```
discord.py>=2.4,<3.0
feedparser==6.0.11
python-dotenv==1.0.1
```

## 6. 신규 파일

### 6.1 `Dockerfile`

```dockerfile
FROM python:3.13-slim

WORKDIR /app

# 의존성 캐시 활용 — requirements.txt만 먼저 복사
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 봇 코드 복사
COPY bot.py wallet_cog.py ./

# Volume mount 경로 미리 생성
RUN mkdir -p /data

# 환경 변수 기본값 (fly.toml에서 override)
ENV DATA_FILE=/data/rss_data.json

# 봇 실행 — -u 플래그로 stdout 즉시 flush
CMD ["python", "-u", "bot.py"]
```

핵심:
- **`python:3.13-slim`** — 로컬 환경 일치
- **`requirements.txt` 먼저 복사** — 의존성 캐시 레이어 활용 (코드만 바뀌면 재설치 안 함)
- **`-u`** — Python stdout/stderr unbuffered (Fly logs에 실시간 보임)
- **`mkdir -p /data`** — volume mount point 디렉토리

### 6.2 `fly.toml`

```toml
app = "eu-ssya-bot"
primary_region = "nrt"

[build]
  dockerfile = "Dockerfile"

[env]
  DATA_FILE = "/data/rss_data.json"
  LOG_LEVEL = "INFO"
  TZ = "Asia/Seoul"

[[mounts]]
  source = "bot_data"
  destination = "/data"
  initial_size = "1gb"

[[vm]]
  size = "shared-cpu-1x"
  memory = "256mb"

[deploy]
  strategy = "immediate"

[experimental]
  auto_rollback = true
```

핵심:
- **`[http_service]` 섹션 없음** — Fly가 worker로 인식, sleep 안 시킴
- **`primary_region = "nrt"`** — Tokyo, 한국 가까움
- **`[[mounts]]`** — `/data` 영구 disk
- **`TZ = "Asia/Seoul"`** — KST 타임존, _now_kst_iso 같은 함수 일관성
- **`auto_rollback`** — 배포 실패 시 이전 버전 자동 복귀

### 6.3 `.dockerignore`

```
venv/
__pycache__/
*.pyc
*.pyo
*.pyd
.env
.env.*
rss_data.json
.git/
.github/
.omc/
.superpowers/
docs/
scripts/
README.md
LICENSE
*.md
.vscode/
.idea/
*.log
.DS_Store
```

이미지 크기 ↓, 빌드 속도 ↑. 특히 `.env`와 `rss_data.json`은 절대 이미지에 들어가면 안 됨 (보안 + 데이터 분리).

## 7. Migration & Local Dev 호환성

### 7.1 Migration: 빈 시작

라이브 테스트 데이터 (모임통장 등록, 입출금 거래) → **운영 시작 시 새로 등록**.

근거:
- 라이브 테스트 데이터는 어차피 테스트
- prod에 테스트 데이터 섞이는 것 방지
- 빈 시작이 가장 깔끔

만약 필요하면 사후에 SFTP로 업로드 가능:
```powershell
fly ssh sftp shell
> put rss_data.json /data/rss_data.json
```

### 7.2 Local Dev 분리

`DATA_FILE` 환경변수 패턴 덕분:

| 환경 | `DATA_FILE` env | 사용되는 경로 |
|------|-----------------|---------------|
| 로컬 (Windows) | (없음, fallback) | `D:\dev\eu-ssya-bot\rss_data.json` |
| Fly.io 운영 | `/data/rss_data.json` (Dockerfile + fly.toml) | `/data/rss_data.json` (volume) |

→ 데이터 완전 분리, 로컬 테스트가 prod에 영향 0.

### 7.3 Secrets 관리

`.env` 파일은 로컬 전용. `.dockerignore`로 이미지에 안 들어감.

Fly에선:
```powershell
fly secrets set DISCORD_BOT_TOKEN=실제_토큰
```
→ Fly의 보안 vault에 저장, 일반 env와 격리, 로그 안 찍힘.

`TEST_GUILD_ID`는 prod에 설정 안 함 — 전 서버에 글로벌 sync.

## 8. 배포 절차

### 8.1 사용자 1회 작업 (수동)

1. **Fly.io 가입**
   - https://fly.io/app/sign-up
   - GitHub OAuth로 가입 (이메일만)
   - **신용카드 등록 요구됨**

2. **Spend Limit $0 설정 (필수!)**
   - https://fly.io/dashboard/personal/billing → **Spend Management** → **Spend Limit** → `$0/month`
   - 무료 tier 초과 시 자동 정지, 청구 0

3. **flyctl CLI 설치 (Windows PowerShell)**
   ```powershell
   iwr https://fly.io/install.ps1 -useb | iex
   fly auth login
   ```

4. **앱 생성**
   ```powershell
   cd D:\dev\eu-ssya-bot
   fly launch --no-deploy --name eu-ssya-bot --region nrt --copy-config
   ```

5. **Volume 생성**
   ```powershell
   fly volumes create bot_data --region nrt --size 1
   ```

6. **Secrets 등록**
   ```powershell
   fly secrets set DISCORD_BOT_TOKEN=실제_토큰_여기
   ```

7. **첫 배포**
   ```powershell
   fly deploy
   ```

8. **로그 확인**
   ```powershell
   fly logs
   ```
   기대 출력:
   ```
   ... INFO eu_ssya_bot: wallet rename worker loop ready
   ... INFO eu_ssya_bot: RSS loop ready
   ... INFO eu_ssya_bot: Synced 3 slash command(s) globally
   ... INFO eu_ssya_bot: Logged in as 으쌰봇#9660
   ```

### 8.2 반복 작업 (코드 수정 후)

```powershell
git add ... && git commit ...
fly deploy
```
→ Fly가 Dockerfile rebuild → 이미지 push → VM 무중단 교체 (~1-2분)

## 9. Verification

### 9.1 Local 검증 (배포 전, 자동)

```powershell
# 기존 verify 스크립트 회귀 없음
& "venv\Scripts\python.exe" scripts\verify_wallet.py
& "venv\Scripts\python.exe" scripts\verify_load_data.py
& "venv\Scripts\python.exe" scripts\verify_final.py

# DATA_FILE 환경변수 동작
$env:DATA_FILE = "test_data.json"
& "venv\Scripts\python.exe" -c "import bot; assert bot.DATA_FILE == 'test_data.json'; print('env OK')"
Remove-Item Env:DATA_FILE
& "venv\Scripts\python.exe" -c "import bot; assert bot.DATA_FILE == 'rss_data.json'; print('fallback OK')"
```

### 9.2 Docker 빌드 검증 (배포 전, 선택)

Docker Desktop 설치되어 있으면:
```powershell
docker build -t eu-ssya-bot-test .
docker run --rm -e DISCORD_BOT_TOKEN=dummy_token eu-ssya-bot-test python -c "import bot; print('docker OK')"
```

미설치 시 건너뜀 — Fly 빌드 시 동일 Dockerfile 사용.

### 9.3 Fly 배포 후 검증

```powershell
fly status        # VM status: running
fly logs --tail   # 실시간 로그
fly ssh console   # SSH로 VM 접속 (디버깅 시)
```

Discord 라이브:
1. `/ping` → pong
2. `/모임통장 등록` → 성공, ~1분 후 채널명 변경
3. `/모임통장 입금 금액:1000 메모:테스트` → 자동 메시지, 5분 후 채널명 갱신
4. **데이터 영속성 확인** (가장 중요):
   ```powershell
   fly apps restart eu-ssya-bot
   ```
   봇 재시작 후 `/모임통장 관리` → 이전 거래 그대로 보임

### 9.4 Spend Limit 확인

배포 직후 https://fly.io/dashboard/personal/billing 에서 Spend Limit이 $0인지 재확인. 무료 tier 안에 있으면 청구 0.

## 10. Risk & Mitigation

| # | Risk | 가능성 | 영향 | 완화 |
|---|------|--------|------|------|
| 1 | Fly 무료 tier 한도 초과 → VM 정지 | L | M | Spend Limit $0로 자동 정지 (청구는 없음). 운영비 봇은 트래픽 적어 한도 여유 |
| 2 | Volume이 한 region에 고정 — region 변경 시 데이터 이동 필요 | L | L | nrt 영구 사용. 만약 region 변경 시 SFTP로 데이터 이동 |
| 3 | Fly 무료 tier 정책 변경 (확률 낮지만) | L | M | 정책 변경 알림 와도 1개월 유예. 그때 Railway나 Oracle Cloud로 이주 가능 |
| 4 | Volume 1GB 가득 참 | L | L | 운영비 봇은 1MB 미만 데이터. 1GB는 1000년 분량 |
| 5 | Docker 빌드 실패 | L | L | `auto_rollback = true`로 자동 이전 버전 복귀 |
| 6 | DISCORD_BOT_TOKEN 노출 (.env가 이미지에 들어감) | L | H | `.dockerignore`에 `.env` 포함 — 이미 들어감. Fly secrets로만 주입 |
| 7 | DNS/Network 일시적 문제 | L | L | Fly 자체 healthcheck로 자동 재시작 |
| 8 | 카드 등록은 했는데 청구되면 어쩌나 | L | H | Spend Limit $0 + 무료 tier 안에 있으면 0원. 약관에 명시 |
| 9 | 봇 재시작 시 다운타임 | L | L | `strategy = immediate`로 ~10초 다운. 그 사이 슬래시 명령 호출 시 디스코드가 retry |
| 10 | 한국에서 nrt(Tokyo) 지연 | L | L | ~30ms — 사용자 경험에 영향 없음 |

## 11. Open Questions

(현재 없음 — 모든 결정 합의됨)

## 12. Approval

- [ ] 사용자 승인 후 → 코드 변경 작업 (단 1줄 + 3 신규 파일)
- [ ] 그 다음 사용자가 Fly.io 가입 + Spend Limit 설정 + 배포

## 13. 다음 단계

1. **이 spec 사용자 리뷰** (지금)
2. **코드 변경** (executor 위임 또는 직접):
   - `bot.py` 한 줄 수정 (DATA_FILE 환경변수)
   - `Dockerfile` 생성
   - `fly.toml` 생성
   - `.dockerignore` 생성
3. **로컬 verify** (3개 verify 스크립트 + env var test)
4. **main으로 merge** (feat/deploy-render → main)
5. **사용자: Fly.io 가입 + Spend Limit 설정 + `fly launch` + `fly deploy`**
6. **라이브 검증** (manual checklist + 재시작 후 데이터 유지 테스트)
