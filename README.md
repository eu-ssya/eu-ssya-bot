# 📰 eu-ssya-bot RSS Discord Bot
> 💡 개인이 원하는 블로그·RSS 피드를 등록해두면, 신규 글이 올라올 때 자동으로 디스코드 채널에 알림을 보내주는 봇입니다.  
> Windows + Python + VS Code 환경 기준으로 제작되었습니다.



## ✨ Features

- 특정 디스코드 채널에 RSS 피드 등록
- 주기적으로 RSS를 체크하고 새 글만 자동 전송
- RSS 추가 / 조회 / 삭제 명령어 제공
- 티스토리, Velog 등 RSS 제공 블로그 자동 지원
- JSON 기반의 간단한 로컬 스토리지 사용 (원하면 SQLite/Redis로 확장 가능)


## 📦 Tech Stack

- **Language:** Python 3.14+
- **Discord Library:** discord.py (Slash Commands)
- **RSS Parser:** feedparser
- **Env Loader:** python-dotenv
- **Editor:** VS Code
- **Storage:** JSON
- **OS:** Windows 기준 설명

## 🔧 Installation (Windows)

### 1. Clone repository
```bash
git clone https://github.com/grow-together-study/eu-ssya-bot.git
cd eu-ssya-bot
```
### 2. Create & activate virtual environment
```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. 환경 변수 설정 (.env)

프로젝트 루트에 .env 파일 생성:
```bash
DISCORD_BOT_TOKEN=여기에_봇_토큰_입력
```

`.env`는 봇 시작 시 `python-dotenv`로 자동 로드됩니다.

## 🪵 Logging

환경 변수 `LOG_LEVEL`로 로그 레벨을 조정할 수 있습니다 (기본값: `INFO`).
예: `LOG_LEVEL=DEBUG`로 설정하면 RSS 폴링 루프의 상세 로그가 출력됩니다.

## ▶️ Running the Bot
### VS Code에서 실행 (권장)
1. VS Code 열기
2. Python: Select Interpreter에서 ./venv 선택
3. F5 또는 Run → Start Debugging

### 커맨드 라인에서 실행
```bash
venv\Scripts\activate
python bot.py
```

## 💬 Bot Commands
| Command                 | 설명                |
| ----------------------- | ----------------- |
| `/ping`                 | 봇 응답 테스트          |
| `/rss add <RSS_URL>`    | 현재 채널에 RSS 등록     |
| `/rss list`             | 등록된 RSS 목록 조회     |
| `/rss remove <RSS_URL>` | 현재 채널에서 해당 RSS 삭제 |

### 모임통장 (스터디 커뮤니티 운영비)

| Command | 설명 |
| ------- | ---- |
| `/모임통장 등록` | 이 채널을 모임통장으로 등록 (한 서버 1개만, 초기 잔액 0원) |
| `/모임통장 입금 금액:<int> [메모] [날짜]` | 입금 기록 (잔액 증가) |
| `/모임통장 출금 금액:<int> [메모] [날짜]` | 출금 기록 (잔액 감소, 잔액 초과 거부) |
| `/모임통장 관리` | 임베드 UI로 거래 수정/삭제 (날짜→항목→버튼) |

모든 명령은 **서버 관리자(Discord Administrator) 권한**만 실행할 수 있습니다.
모든 멤버는 채널의 자동 메시지로 거래 내역을 조회할 수 있습니다.
잔액은 채널 이름에 자동 반영됩니다 (예: `💰-285,000원`).
봇에 **Manage Channels** 권한이 필요합니다 (채널명 변경용).

날짜 형식: `YYYY-MM-DD` (미입력 시 오늘 KST). 미래 날짜도 허용됩니다.

## 🔄 How It Works

1. 유저가 /rss add https://xxx.tistory.com/rss 입력
2. 봇이 해당 RSS 주소를 JSON에 저장
3. 백그라운드 루프에서 일정 간격으로 RSS 최신 글 확인
4. 새 글 발견 시 디스코드 채널에 자동 메시지 전송