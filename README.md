# eu-ssya-bot

Discord 스터디 커뮤니티를 위한 RSS 알림, 모임통장, 관리자용 활동 현황 보고서 봇입니다. Fly.io에 배포하면 개인 PC를 켜 둘 필요 없이 동작합니다.

## 주요 기능

- 채널별 RSS 피드 등록, 조회, 삭제 및 새 글 알림
- 입출금 기록과 채널 이름 잔액 표시를 제공하는 모임통장
- 지정 역할 멤버의 독서실·스터디 음성 카테고리 체류 시간 집계
- 지정 채널의 SoD/EoD 참여일 수집과 접근 가능한 과거 메시지 동기화
- 관리자 전용 15명 단위 활동 보고서와 전체 TXT 다운로드
- 수집 시작 전, Gateway 연결 중단, 채널 전환 등 데이터 가용 구간 경고

활동 보고서는 운영자의 판단을 돕는 원시 통계입니다. 점수, 등급, 경고, 역할 변경, 강퇴 같은 멤버 조치를 자동으로 수행하지 않습니다.

## 기술과 저장소

- Python 3.13 Docker 런타임
- discord.py 2.x, feedparser, python-dotenv
- RSS와 모임통장: `DATA_FILE`의 JSON 파일
- 활동 현황: Python 표준 라이브러리 `sqlite3`를 사용하는 `ACTIVITY_DB_PATH`의 SQLite 파일

SQLite는 무료이며 봇 프로세스 안에서 직접 실행되므로 별도 DB 서버나 라이선스가 필요하지 않습니다. Fly에 배포한 뒤에는 Fly Machine이 봇과 SQLite를 실행하므로 개인 PC를 계속 켜 둘 필요도 없습니다.

## Discord 사전 설정

배포하기 **전에** [Discord Developer Portal](https://discord.com/developers/applications)의 Bot 화면에서 다음 Privileged Gateway Intents를 활성화해야 합니다. 활성화하지 않으면 Gateway 로그인에 실패할 수 있습니다.

- Server Members Intent
- Message Content Intent

코드는 표준 `guilds`, `voice_states`, `guild_messages` intent도 사용합니다. 자세한 구분은 [Discord Gateway Intents 공식 문서](https://discord.com/developers/docs/events/gateway#gateway-intents)를 참고하세요.

봇 초대 시 `bot`, `applications.commands` scope와 다음 권한을 부여합니다.

- View Channels
- Read Message History — SoD/EoD 과거 동기화에 필요
- Send Messages
- Attach Files — 전체 TXT 응답에 필요
- Use Application Commands
- Manage Channels — 모임통장 채널 이름의 잔액 표시에 필요

활동 현황 수집은 음성 채널에 봇이 접속하지 않고 Gateway 상태 이벤트만 사용합니다. 따라서 Connect와 Speak 권한은 필요하지 않습니다. 권한의 의미는 [Discord Permissions](https://discord.com/developers/docs/topics/permissions), 명령 등록 방식은 [Application Commands](https://discord.com/developers/docs/interactions/application-commands)에서 확인할 수 있습니다.

## 로컬 설치와 실행

```powershell
git clone https://github.com/grow-together-study/eu-ssya-bot.git
cd eu-ssya-bot
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

프로젝트 루트의 `.env`에 로컬 경로와 토큰을 설정합니다.

```dotenv
DISCORD_BOT_TOKEN=여기에_봇_토큰_입력
DATA_FILE=rss_data.json
ACTIVITY_DB_PATH=activity.db
LOG_LEVEL=INFO
```

`.env`는 Git과 Docker build context에서 제외됩니다. 토큰을 `fly.toml`이나 소스에 기록하지 마세요.

```powershell
venv\Scripts\python.exe bot.py
```

로컬 활동 DB는 위 예시의 `activity.db`이며, SQLite가 필요에 따라 `activity.db-wal`, `activity.db-shm`을 함께 만듭니다. 이 파일들은 Git과 Docker build context에서 제외됩니다.

## Fly 배포와 영속성

컨테이너 기본 경로와 `fly.toml`은 다음 파일을 `/data` volume에 둡니다.

- `DATA_FILE=/data/rss_data.json`
- `ACTIVITY_DB_PATH=/data/activity.db`
- SQLite 운영 보조 파일 `/data/activity.db-wal`, `/data/activity.db-shm`

Discord 토큰은 설정 파일이 아니라 [Fly Secrets](https://fly.io/docs/apps/secrets/)로만 등록합니다.

```powershell
fly secrets set DISCORD_BOT_TOKEN="실제_토큰" -a eu-ssya-bot
```

`fly deploy`는 Dockerfile과 `fly.toml` 구성을 적용합니다. Machine restart만으로는 변경된 구성이 적용되지 않습니다. 이 저장 방식은 `/data`가 붙은 **정확히 한 대의 Machine과 하나의 `bot_data` volume**을 전제로 합니다. Fly volume은 한 번에 한 Machine에만 연결되는 로컬 영속 스토리지이며 자동 복제되지 않습니다. Machine 수를 늘리지 말고 [Fly Volumes](https://fly.io/docs/volumes/)와 [Volume snapshots](https://fly.io/docs/volumes/snapshots/)를 참고해 별도 백업·복구 절차를 운영하세요. volume이 연결되지 않는 `release_command`에서 SQLite 작업을 실행하지 않습니다.

실제 배포 확인과 restart 지속성 검증은 [수동 수용 체크리스트](docs/superpowers/specs/2026-08-02-activity-report-manual-checklist.md)를 따릅니다. 체크리스트는 실제 Machine ID가 하나인지 먼저 검증한 뒤 그 ID만 사용합니다.

## 명령어

### 상태와 RSS

| 명령 | 설명 |
| --- | --- |
| `/ping` | 봇 응답 확인 |
| `/rss add url:<RSS_URL>` | 현재 채널에 RSS 피드 등록 |
| `/rss list` | 현재 채널의 RSS 목록 조회 |
| `/rss remove url:<RSS_URL>` | 현재 채널에서 RSS 피드 삭제 |

### 모임통장

| 명령 | 설명 |
| --- | --- |
| `/모임통장 등록` | 현재 채널을 서버의 모임통장으로 등록 |
| `/모임통장 입금 금액:<int> [메모] [날짜]` | 입금 기록 |
| `/모임통장 출금 금액:<int> [메모] [날짜]` | 잔액을 넘지 않는 출금 기록 |
| `/모임통장 관리` | 거래 수정·삭제 UI 열기 |

날짜는 `YYYY-MM-DD` 또는 `YYYYMMDD` 형식이며 생략하면 KST 오늘을 사용합니다. 모임통장 명령은 Discord Administrator만 실행할 수 있습니다.

### 활동 설정과 보고서

활동 명령과 버튼/TXT 접근은 Discord Administrator만 사용할 수 있으며 응답은 ephemeral입니다.

| 명령 | 설명 |
| --- | --- |
| `/활동설정 대상역할 역할:<Role>` | 보고·수집 대상 역할 설정 |
| `/활동설정 독서실 카테고리:<Category>` | 독서실 음성 카테고리 설정 |
| `/활동설정 스터디 카테고리:<Category>` | 스터디 음성 카테고리 설정 |
| `/활동설정 sod_eod 채널:<TextChannel>` | SoD/EoD 텍스트 채널 설정 |
| `/활동설정 상태` | 설정, 수집 구간, 동기화 상태 확인 |
| `/활동설정 과거동기화` | 현재 SoD/EoD 채널의 접근 가능한 과거 메시지 동기화 |
| `/활동현황 최근 일수:<N>` | KST 오늘을 포함한 최근 N일 보고서 |
| `/활동현황 기간 시작일:<YYYY-MM-DD> 종료일:<YYYY-MM-DD>` | 양 끝 날짜를 포함하는 KST 기간 보고서 |

## 활동 기능 운영 순서

1. 배포 전에 Developer Portal의 Server Members Intent와 Message Content Intent를 활성화합니다.
2. 대상 채널에서 봇의 View Channels와 Read Message History를 포함한 위 권한을 확인합니다.
3. Administrator가 네 설정 명령 `대상역할`, `독서실`, `스터디`, `sod_eod`를 실행합니다. 네 설정은 서로 독립적이며 어떤 순서로 설정해도 됩니다. 독서실과 스터디는 서로 다른 카테고리여야 합니다.
4. `/활동설정 상태`에서 설정과 데이터 가용 경고를 확인합니다.
5. `/활동설정 과거동기화`를 실행해 현재 SoD/EoD 채널의 읽을 수 있는 이력을 반영합니다.
6. `/활동현황 최근` 또는 `/활동현황 기간`으로 보고서를 열고 필요하면 전체 TXT를 받습니다.

최초 `/활동설정 과거동기화`는 관리자가 명시적으로 실행해야 합니다. 실행 전에는 startup이나 재연결이 채널 전체 이력을 자동으로 읽지 않으며 보고서에 동기화 미완료 경고가 남습니다. 최초 동기화가 한 번 정상 완료된 뒤에는 봇 재시작, Gateway 재연결, guild unavailable 복귀 때 저장된 메시지 cursor 이후만 자동으로 보충합니다. 빈 채널은 이력 조회를 시작하기 직전의 Discord snowflake 경계를 exclusive cursor로 저장하므로 조회 종료와 완료 기록 사이에 도착한 메시지도 다음 자동 보충 대상에 포함되고, 과거 전체 스캔으로 되돌아가지 않습니다. 구형 DB에서 실제 메시지 cursor 없이 완료로만 표시된 채널은 안전한 경계를 증명할 수 없어 미완료 상태로 전환되며 `/활동설정 과거동기화`를 다시 실행해야 합니다. 자동 보충 준비나 조회가 DB·권한·API 오류로 실패하면 완료로 표시하지 않고 지연 재시도와 부분 데이터 경고를 유지합니다.

음성 체류 기록은 봇이 배포되어 있고 세 가지 음성 설정(대상 역할, 독서실, 스터디)이 모두 유효해진 시점부터만 시작합니다. 배포 전 음성 활동은 소급할 수 없습니다. SoD/EoD 과거 동기화도 봇이 접근 가능한 현재 메시지만 읽으므로 삭제된 메시지, 권한 때문에 볼 수 없는 메시지, Discord API가 제공하지 않는 이력은 복원하지 못합니다. 채널 변경과 봇 중단 구간은 보고서의 coverage 경고를 확인하세요.

## 개인정보와 한계

활동 DB에는 guild/user/role/category/channel/message ID, 이벤트 유형, 날짜, 세션 시각 같은 이벤트 메타데이터와 세션 타이밍만 저장합니다. 메시지 본문, 음성·오디오 내용, 실제 발화, 녹음, 화면 공유나 카메라 상태는 저장하지 않습니다. 보고서는 현재 대상 역할을 가진 non-bot 멤버를 보여 주며, 기록이 없는 멤버도 포함합니다.

SQLite 파일과 Fly volume은 운영 데이터입니다. Git 또는 이미지에 포함하지 말고, 단일 Machine·단일 volume 제약과 자동 복제 부재를 고려해 정기적으로 백업하고 복구 절차를 점검하세요.
