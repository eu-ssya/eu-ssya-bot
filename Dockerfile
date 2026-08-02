FROM python:3.13-slim

WORKDIR /app

# 의존성 캐시 활용 — requirements.txt만 먼저 복사
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 봇 코드 복사
COPY bot.py wallet_cog.py activity_cog.py activity_store.py ./

# Volume mount 경로 미리 생성
RUN mkdir -p /data

# 환경 변수 기본값 (fly.toml에서 override)
ENV DATA_FILE=/data/rss_data.json
ENV ACTIVITY_DB_PATH=/data/activity.db

# 봇 실행 — -u 플래그로 stdout 즉시 flush
CMD ["python", "-u", "bot.py"]
