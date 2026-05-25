"""모임통장(Wallet) Cog — 스터디 커뮤니티 운영비 관리."""
from __future__ import annotations

import datetime
import logging
from typing import Dict, Optional

import discord
from discord.ext import commands

logger = logging.getLogger("eu_ssya_bot")

# ---------------- 모듈 상태 ----------------
# 채널 ID -> 적용 대기 중인 최신 잔액. rename 워커가 소비한다.
_pending_balance: Dict[int, int] = {}
# 채널 ID -> 마지막 rename 성공 시각 (asyncio loop monotonic seconds).
_last_rename: Dict[int, float] = {}

# Discord 채널명 rate-limit: 10분당 2회 → 5분 floor (안전 마진).
RENAME_COOLDOWN_SECONDS = 300
# rename 워커 주기.
RENAME_WORKER_INTERVAL_SECONDS = 60
# 기본 채널명 포맷 — `{잔액}` 위치 자동 치환.
DEFAULT_WALLET_NAME_PREFIX = "💰-"
# 메모 최대 길이.
MAX_MEMO_LEN = 200
# View timeout (초).
VIEW_TIMEOUT_SECONDS = 600
# KST 타임존.
_KST = datetime.timezone(datetime.timedelta(hours=9))


# ---------------- 순수 유틸 함수 (Discord 무관, 테스트 가능) ----------------
def format_krw(amount: int) -> str:
    """정수 원화를 천 단위 구분 + '원' 접미사로 포맷. 음수는 '-' 접두사."""
    return f"{amount:,}원"


def _format_channel_name(balance: int) -> str:
    """잔액을 채널명 포맷 '💰-285,000원' 형태로 변환."""
    return f"{DEFAULT_WALLET_NAME_PREFIX}{balance:,}원"


def _now_kst_iso() -> str:
    """현재 시각을 KST ISO-8601(초 단위)로 반환."""
    return datetime.datetime.now(tz=_KST).isoformat(timespec="seconds")


def _today_kst_iso() -> str:
    """오늘 날짜를 KST 기준 'YYYY-MM-DD'로 반환."""
    return datetime.datetime.now(tz=_KST).date().isoformat()


def _parse_date(s: str) -> datetime.date:
    """ISO YYYY-MM-DD 엄격 파싱. 유효하지 않으면 ValueError.

    `datetime.date.fromisoformat`이 '2026-2-3' 같은 비표준도 허용하므로,
    엄격하게 'YYYY-MM-DD' 정확히 10자 + 두 자리 month/day + 모든 자리 숫자인지 확인한다.
    """
    if (
        not isinstance(s, str)
        or len(s) != 10
        or s[4] != "-"
        or s[7] != "-"
        or not s[0:4].isdigit()
        or not s[5:7].isdigit()
        or not s[8:10].isdigit()
    ):
        raise ValueError(f"날짜 형식이 잘못되었습니다: {s!r} (YYYY-MM-DD 형식이어야 합니다)")
    return datetime.date.fromisoformat(s)


def compute_new_balance(old_balance: int, kind: str, amount: int) -> int:
    """잔액 적용. kind는 'income' 또는 'expense'. amount는 양수."""
    if kind == "income":
        return old_balance + amount
    if kind == "expense":
        return old_balance - amount
    raise ValueError(f"unknown kind: {kind!r}")


def validate_amount(amount: int) -> Optional[str]:
    """유효하면 None, 아니면 한국어 에러 메시지."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return "금액은 1원 이상의 정수여야 합니다."
    return None


def validate_memo(memo: str) -> Optional[str]:
    """유효하면 None, 아니면 한국어 에러 메시지."""
    if not isinstance(memo, str):
        return "메모는 문자열이어야 합니다."
    if len(memo) > MAX_MEMO_LEN:
        return f"메모는 {MAX_MEMO_LEN}자 이하로 입력해주세요."
    return None


# ---------------- Discord 의존 가드 함수 ----------------
def is_admin(interaction: discord.Interaction) -> bool:
    """interaction을 호출한 사용자가 Discord Administrator 권한이 있는지."""
    if interaction.guild is None or interaction.user is None:
        return False
    # GuildMember인 경우 guild_permissions 사용 가능
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return perms.administrator


def _require_text_channel(interaction: discord.Interaction) -> Optional[str]:
    """텍스트 채널 + guild 가드. 통과하면 None, 실패하면 한국어 에러 메시지."""
    if interaction.guild is None:
        return "이 명령은 서버 채널에서만 사용할 수 있습니다."
    if not isinstance(interaction.channel, discord.TextChannel):
        return "일반 텍스트 채널에서만 사용할 수 있습니다."
    return None


def _get_existing_guild_wallet(
    wallets: Dict[str, dict], guild_id: str
) -> Optional[tuple]:
    """이 guild에 이미 등록된 wallet이 있으면 (channel_id_str, wallet_dict) 반환, 없으면 None."""
    for ch_id_str, wallet in wallets.items():
        if str(wallet.get("guild_id", "")) == str(guild_id):
            return (ch_id_str, wallet)
    return None


# ---------------- Cog ----------------
class WalletCog(commands.Cog):
    """모임통장 명령 + UI + rename worker."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WalletCog(bot))
