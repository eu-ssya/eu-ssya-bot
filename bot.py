import os
import asyncio
import json
import logging
import tempfile
from typing import Dict, List

import discord
from discord.ext import commands, tasks
from discord import app_commands
import feedparser
from dotenv import load_dotenv

# ---------------- 로깅 설정 ----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("eu_ssya_bot")

# ---------------- 설정 ----------------
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATA_FILE = "rss_data.json"

intents = discord.Intents.default()
# Slash 명령어만 사용하므로 message_content 권한은 불필요.

bot = commands.Bot(command_prefix="!", intents=intents)

# 공유 JSON 상태에 대한 동시성 보호용 락. async 래퍼에서만 잡고
# 동기 load_data/save_data 헬퍼는 락을 건드리지 않는다.
_data_lock = asyncio.Lock()

# ---------------- 저장소 유틸 ----------------
def load_data() -> Dict:
    """
    rss_data.json을 읽어서 Dict로 반환.
    파일이 없거나 형식이 잘못되었으면 기본 구조로 초기화.
    """
    if not os.path.exists(DATA_FILE):
        return {"feeds": [], "wallets": {}}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        # 파일이 깨졌거나 비어있을 때
        return {"feeds": [], "wallets": {}}

    # feeds 키가 없거나 list가 아니면 초기화
    if "feeds" not in data or not isinstance(data["feeds"], list):
        data["feeds"] = []

    # wallets 키가 없거나 dict가 아니면 초기화
    if "wallets" not in data or not isinstance(data["wallets"], dict):
        data["wallets"] = {}

    return data

def save_data(data: Dict) -> None:
    """
    rss_data.json 저장 (원자적 쓰기).
    같은 디렉토리에 임시 파일을 만들고 os.replace로 교체하여,
    프로세스가 도중에 종료돼도 파일이 손상되지 않도록 한다.
    """
    dir_ = os.path.dirname(os.path.abspath(DATA_FILE)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".rss_data.", suffix=".tmp", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------- RSS 페치 유틸 ----------------
FEED_FETCH_TIMEOUT = 30  # seconds


async def fetch_feed(url: str):
    """
    동기 feedparser.parse를 워커 스레드에서 실행하고 타임아웃을 적용.
    참고: asyncio.wait_for는 await를 취소할 뿐 내부 소켓을 끊지는 못한다.
    그래도 이벤트 루프가 풀리는 것이 목적이므로 허용 가능한 trade-off.
    """
    return await asyncio.wait_for(
        asyncio.to_thread(feedparser.parse, url),
        timeout=FEED_FETCH_TIMEOUT,
    )

# ---------------- 기본 명령어 ----------------
@bot.event
async def on_ready():
    if not hasattr(bot, "synced"):
        synced = await bot.tree.sync()
        bot.synced = True
        logger.info("Synced %d slash command(s)", len(synced))
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("------")


@bot.tree.command(name="ping", description="eu-ssya-bot 상태 체크")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("pong from eu-ssya-bot")

# ---------------- Slash 명령어 그룹: /rss ... ----------------
rss_group = app_commands.Group(
    name="rss",
    description="RSS 구독 관리 명령어"
)

@rss_group.command(name="add", description="현재 채널에 RSS 피드를 등록합니다.")
@app_commands.describe(url="RSS 피드 URL (예: https://xxx.tistory.com/rss)")
async def rss_add_slash(interaction: discord.Interaction, url: str):
    """
    /rss add
    """
    async with _data_lock:
        data = load_data()

        # 중복 체크
        for feed in data["feeds"]:
            if feed["url"] == url and feed["channel_id"] == interaction.channel.id:
                await interaction.response.send_message(
                    "이 채널에는 이미 이 RSS 피드가 등록되어 있습니다.",
                    ephemeral=True,
                )
                return

    try:
        parsed = await fetch_feed(url)
    except Exception as e:
        await interaction.response.send_message(
            f"RSS 조회 실패: `{url}` ({type(e).__name__})",
            ephemeral=True,
        )
        return

    last_entry_id = None
    if parsed.entries:
        entry = parsed.entries[0]
        last_entry_id = getattr(entry, "id", None) or getattr(entry, "link", None)

    async with _data_lock:
        data = load_data()
        # 락을 잠깐 풀었던 사이 다른 핸들러가 같은 피드를 먼저 추가했을 수 있으므로 재확인.
        for feed in data["feeds"]:
            if feed["url"] == url and feed["channel_id"] == interaction.channel.id:
                await interaction.response.send_message(
                    "이 채널에는 이미 이 RSS 피드가 등록되어 있습니다.",
                    ephemeral=True,
                )
                return
        data["feeds"].append(
            {
                "url": url,
                "channel_id": interaction.channel.id,
                "last_entry_id": last_entry_id,
            }
        )
        save_data(data)

    await interaction.response.send_message(
        f"RSS 등록 완료:\n`{url}` → {interaction.channel.mention}"
    )

@rss_group.command(name="list", description="현재 채널에 등록된 RSS 목록을 보여줍니다.")
async def rss_list_slash(interaction: discord.Interaction):
    """
    /rss list
    """
    async with _data_lock:
        data = load_data()
    feeds = [f for f in data["feeds"] if f["channel_id"] == interaction.channel.id]

    if not feeds:
        await interaction.response.send_message("이 채널에 등록된 RSS가 없습니다.")
        return

    lines: List[str] = []
    for idx, feed in enumerate(feeds, start=1):
        lines.append(f"{idx}. {feed['url']}")

    msg = "등록된 RSS 목록:\n" + "\n".join(lines)
    await interaction.response.send_message(msg)


@rss_group.command(name="remove", description="현재 채널에서 특정 RSS를 제거합니다.")
@app_commands.describe(url="삭제할 RSS 피드 URL")
async def rss_remove_slash(interaction: discord.Interaction, url: str):
    """
    /rss remove
    """
    async with _data_lock:
        data = load_data()
        before = len(data["feeds"])

        data["feeds"] = [
            f for f in data["feeds"]
            if not (f["url"] == url and f["channel_id"] == interaction.channel.id)
        ]
        after = len(data["feeds"])

        save_data(data)

    if before == after:
        await interaction.response.send_message(
            "해당 URL은 이 채널에 등록되어 있지 않습니다."
        )
    else:
        await interaction.response.send_message(f"RSS 삭제 완료: `{url}`")

# 그룹을 트리에 등록
bot.tree.add_command(rss_group)

# ---------------- RSS 폴링 루프 ----------------
@tasks.loop(seconds=300)
async def rss_loop():
    """
    일정 간격으로 모든 등록된 RSS 피드를 확인하고,
    새 글이 있으면 디스코드 채널에 전송한다.
    """
    logger.debug("RSS loop tick")

    async with _data_lock:
        data = load_data()
    feeds: List[Dict] = data.get("feeds") or []   # ← 여기 방어

    changed = False

    for feed in feeds:
        url = feed["url"]
        channel_id = feed["channel_id"]
        last_entry_id = feed.get("last_entry_id")

        logger.debug("  checking feed: %s (channel=%s)", url, channel_id)

        try:
            parsed = await fetch_feed(url)
        except Exception as e:
            logger.warning("feed fetch failed: %s (%s)", url, e)
            continue

        entries = parsed.entries

        if not entries:
            logger.debug("    no entries")
            continue

        new_entries = []
        for entry in entries:
            entry_id = getattr(entry, "id", None) or getattr(entry, "link", None)
            if last_entry_id is not None and entry_id == last_entry_id:
                break
            new_entries.append(entry)

        if not new_entries:
            logger.debug("    no new entries")
            continue

        channel = bot.get_channel(channel_id)
        if channel is None:
            logger.info("channel %s not found or inaccessible", channel_id)
            continue

        logger.debug("    found %d new entries", len(new_entries))
        new_entries.reverse()

        sent_any = False
        send_failed = False
        for entry in new_entries:
            title = getattr(entry, "title", "제목 없음")
            link = getattr(entry, "link", "")
            try:
                await channel.send(f"[새 글] {title}\n{link}")
                sent_any = True
            except (discord.DiscordException, OSError) as e:
                logger.warning("send failed for %s: %s", url, e)
                send_failed = True
                # 전송 못 한 항목 너머로 last_entry_id를 앞당기지 않기 위해 즉시 중단.
                break

        if sent_any and not send_failed:
            latest = entries[0]
            feed["last_entry_id"] = (
                getattr(latest, "id", None) or getattr(latest, "link", None)
            )
            changed = True

    if changed:
        async with _data_lock:
            save_data(data)


@rss_loop.before_loop
async def _before_rss_loop():
    await bot.wait_until_ready()
    logger.info("RSS loop ready")


@bot.event
async def setup_hook():
    await bot.load_extension("wallet_cog")
    rss_loop.start()


# ---------------- 엔트리 포인트 ----------------
async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN 환경 변수가 설정되어 있지 않습니다.")

    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
