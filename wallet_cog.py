"""모임통장(Wallet) Cog — 스터디 커뮤니티 운영비 관리."""
from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Dict, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

# bot.py의 공유 락/저장소
from bot import _data_lock, load_data, save_data

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
) -> Optional[Tuple[str, dict]]:
    """이 guild에 이미 등록된 wallet이 있으면 (channel_id_str, wallet_dict) 반환, 없으면 None."""
    for ch_id_str, wallet in wallets.items():
        if str(wallet.get("guild_id", "")) == str(guild_id):
            return (ch_id_str, wallet)
    return None


def _build_registered_response(initial_balance: int) -> str:
    """등록 성공 응답 문자열."""
    return (
        f"모임통장을 시작합니다. 초기 잔액: {format_krw(initial_balance)}\n"
        f"채널 이름이 곧 업데이트됩니다."
    )


def _format_transaction_message(
    kind: str, amount: int, memo: str, date_str: str, new_balance: int
) -> str:
    """채널에 송신할 거래 한 줄 메시지.

    예: '📥 +50,000원 · 지각벌금 홍길동 · 2026-11-28 · 잔액: 285,000원'
    메모가 빈 문자열이면 메모 부분 생략.
    """
    if kind == "income":
        emoji = "📥"
        sign = "+"
    elif kind == "expense":
        emoji = "📤"
        sign = "-"
    else:
        raise ValueError(f"unknown kind: {kind!r}")

    parts = [f"{emoji} {sign}{amount:,}원"]
    if memo:
        parts.append(memo)
    parts.append(date_str)
    parts.append(f"잔액: {new_balance:,}원")
    return " · ".join(parts)


# ---------------- Cog ----------------
class WalletCog(commands.Cog):
    """모임통장 명령 + UI + rename worker."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    mt_group = app_commands.Group(
        name="모임통장",
        description="모임통장 (스터디 커뮤니티 운영비) 관리",
    )

    # ---------------- /모임통장 등록 ----------------
    @mt_group.command(name="등록", description="이 채널을 모임통장으로 등록합니다.")
    async def register(self, interaction: discord.Interaction) -> None:
        # 권한
        if not is_admin(interaction):
            await interaction.response.send_message(
                "이 명령은 서버 관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return

        # 컨텍스트
        err = _require_text_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        channel = interaction.channel
        guild = interaction.guild
        me = guild.me
        if me is None or not channel.permissions_for(me).manage_channels:
            await interaction.response.send_message(
                "봇에 '채널 관리' 권한이 필요합니다. 서버 설정에서 권한을 부여해주세요.",
                ephemeral=True,
            )
            return

        ch_key = str(channel.id)
        guild_id_str = str(guild.id)

        async with _data_lock:
            data = load_data()
            wallets = data["wallets"]

            # 이 채널 중복 등록
            if ch_key in wallets:
                await interaction.response.send_message(
                    "이 채널은 이미 모임통장으로 등록되어 있습니다.", ephemeral=True
                )
                return

            # 같은 서버 다른 채널 등록 충돌
            existing = _get_existing_guild_wallet(wallets, guild_id_str)
            if existing is not None:
                existing_ch_id, _ = existing
                await interaction.response.send_message(
                    f"이 서버에는 이미 <#{existing_ch_id}> 에 모임통장이 등록되어 있습니다. "
                    f"한 서버에는 한 개만 가능합니다.",
                    ephemeral=True,
                )
                return

            # 등록
            wallets[ch_key] = {
                "guild_id": guild_id_str,
                "balance": 0,
                "original_name": channel.name,
                "created_at": _now_kst_iso(),
                "transactions": [],
            }
            save_data(data)
            _pending_balance[channel.id] = 0

        logger.info(
            "wallet register: channel=%s guild=%s by=%s",
            channel.id, guild.id, interaction.user.id,
        )
        await interaction.response.send_message(_build_registered_response(0))

    # ---------------- 거래 공통 helper ----------------
    async def _record_transaction(
        self,
        interaction: discord.Interaction,
        kind: str,
        amount: int,
        memo: str,
        date_str: Optional[str],
    ) -> None:
        """입금/출금 공통 로직. kind는 'income' 또는 'expense'."""
        # 권한
        if not is_admin(interaction):
            await interaction.response.send_message(
                "이 명령은 서버 관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return

        # 컨텍스트
        err = _require_text_channel(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        # 금액 / 메모 검증
        amt_err = validate_amount(amount)
        if amt_err:
            await interaction.response.send_message(amt_err, ephemeral=True)
            return
        memo = memo or ""
        memo_err = validate_memo(memo)
        if memo_err:
            await interaction.response.send_message(memo_err, ephemeral=True)
            return

        # 날짜 — 미입력 시 오늘
        if not date_str:
            date_str = _today_kst_iso()
        else:
            try:
                _parse_date(date_str)
            except ValueError:
                await interaction.response.send_message(
                    f"날짜는 YYYY-MM-DD 형식으로 입력해주세요. 예: 2026-11-28",
                    ephemeral=True,
                )
                return

        channel = interaction.channel
        ch_key = str(channel.id)

        # 트랜잭션 저장 + 잔액 갱신 (락 안)
        new_balance: int
        tx_id: str
        async with _data_lock:
            data = load_data()
            wallet = data["wallets"].get(ch_key)
            if wallet is None:
                await interaction.response.send_message(
                    "이 채널은 모임통장으로 등록되어 있지 않습니다. "
                    "/모임통장 등록 을 먼저 실행하세요.",
                    ephemeral=True,
                )
                return

            old_balance = int(wallet["balance"])
            new_balance = compute_new_balance(old_balance, kind, amount)

            # Overdraft 거부 (출금)
            if kind == "expense" and new_balance < 0:
                await interaction.response.send_message(
                    f"잔액({format_krw(old_balance)})보다 큰 출금은 등록할 수 없습니다.",
                    ephemeral=True,
                )
                return

            tx_id = str(uuid.uuid4())
            tx = {
                "id": tx_id,
                "kind": kind,
                "amount": int(amount),
                "memo": memo,
                "date": date_str,
                "ts": _now_kst_iso(),
                "user_id": str(interaction.user.id),
                "channel_message_id": None,  # send 후 갱신
            }
            wallet["transactions"].append(tx)
            wallet["balance"] = new_balance
            save_data(data)
            _pending_balance[channel.id] = new_balance

        # 채널 자동 메시지 — 락 밖에서 송신
        message_id: Optional[int] = None
        try:
            msg = await channel.send(
                _format_transaction_message(kind, amount, memo, date_str, new_balance)
            )
            message_id = msg.id
        except discord.HTTPException as e:
            logger.warning(
                "wallet auto-message send failed: channel=%s err=%s",
                channel.id, e,
            )

        # 메시지 ID 사후 저장 (락 다시)
        if message_id is not None:
            async with _data_lock:
                data = load_data()
                wallet = data["wallets"].get(ch_key)
                if wallet is not None:
                    for t in wallet["transactions"]:
                        if t["id"] == tx_id:
                            t["channel_message_id"] = str(message_id)
                            break
                    save_data(data)
                else:
                    logger.warning(
                        "wallet deleted between tx record and message_id update: "
                        "channel=%s tx_id=%s message_id=%s",
                        ch_key, tx_id, message_id,
                    )

        verb = "입금" if kind == "income" else "출금"
        sign = "+" if kind == "income" else "-"
        logger.info(
            "wallet %s: channel=%s amount=%d new_balance=%d user=%s",
            kind, channel.id, amount, new_balance, interaction.user.id,
        )
        await interaction.response.send_message(
            f"{verb} {sign}{amount:,}원 기록 완료. 현재 잔액: {format_krw(new_balance)}",
            ephemeral=True,
        )

    # ---------------- /모임통장 입금 ----------------
    @mt_group.command(name="입금", description="입금을 기록합니다 (잔액 증가).")
    @app_commands.describe(
        금액="입금 금액 (정수, 원, 1 이상)",
        메모="거래 메모 (선택, 최대 200자)",
        날짜="YYYY-MM-DD 형식 (선택, 기본=오늘 KST)",
    )
    async def deposit(
        self,
        interaction: discord.Interaction,
        금액: int,
        메모: str = "",
        날짜: str = "",
    ) -> None:
        await self._record_transaction(interaction, "income", 금액, 메모, 날짜 or None)

    # ---------------- /모임통장 출금 ----------------
    @mt_group.command(name="출금", description="출금을 기록합니다 (잔액 감소).")
    @app_commands.describe(
        금액="출금 금액 (정수, 원, 1 이상, 잔액 이하)",
        메모="거래 메모 (선택, 최대 200자)",
        날짜="YYYY-MM-DD 형식 (선택, 기본=오늘 KST)",
    )
    async def withdraw(
        self,
        interaction: discord.Interaction,
        금액: int,
        메모: str = "",
        날짜: str = "",
    ) -> None:
        await self._record_transaction(interaction, "expense", 금액, 메모, 날짜 or None)

    # ---------------- 채널명 rename 워커 ----------------
    @tasks.loop(seconds=RENAME_WORKER_INTERVAL_SECONDS)
    async def rename_worker_loop(self) -> None:
        if not _pending_balance:
            return

        loop = asyncio.get_running_loop()
        now = loop.time()

        # 락 안에서 메타데이터 스냅샷만 — 네트워크 호출 없음
        to_process: list = []
        async with _data_lock:
            data = load_data()
            wallets = data.get("wallets", {})
            for ch_id, desired_balance in list(_pending_balance.items()):
                last = _last_rename.get(ch_id, 0.0)
                if now - last < RENAME_COOLDOWN_SECONDS:
                    continue
                wallet = wallets.get(str(ch_id))
                if wallet is None:
                    # 등록 해제됨 — 큐 정리
                    _pending_balance.pop(ch_id, None)
                    continue
                to_process.append((ch_id, int(desired_balance)))

        # 락 밖에서 실제 channel.edit (개별 try/except)
        for ch_id, desired_balance in to_process:
            channel = self.bot.get_channel(ch_id)
            if channel is None:
                logger.info("rename worker: channel %s not found", ch_id)
                _pending_balance.pop(ch_id, None)
                continue

            new_name = _format_channel_name(desired_balance)
            try:
                await channel.edit(name=new_name, reason="모임통장 잔액 업데이트")
            except discord.HTTPException as e:
                status = getattr(e, "status", None)
                retry_after = getattr(e, "retry_after", None)
                if status == 429:
                    logger.warning(
                        "rename rate-limited: channel=%s retry_after=%s",
                        ch_id, retry_after,
                    )
                    bump = max(RENAME_COOLDOWN_SECONDS, float(retry_after or 0))
                    _last_rename[ch_id] = (
                        asyncio.get_running_loop().time() + bump - RENAME_COOLDOWN_SECONDS
                    )
                    continue
                logger.warning(
                    "rename failed: channel=%s status=%s err=%s",
                    ch_id, status, e,
                )
                _pending_balance.pop(ch_id, None)
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("rename unexpected error: channel=%s err=%s", ch_id, e)
                _pending_balance.pop(ch_id, None)
                continue

            # 성공
            _last_rename[ch_id] = asyncio.get_running_loop().time()
            # 같은 desired_balance인 경우만 pop — 새 값이 들어왔으면 유지
            if _pending_balance.get(ch_id) == desired_balance:
                _pending_balance.pop(ch_id, None)
            logger.info(
                "rename committed: channel=%s new_name=%s",
                ch_id, new_name,
            )

    @rename_worker_loop.before_loop
    async def _before_rename_worker_loop(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("wallet rename worker loop ready")

    async def cog_load(self) -> None:
        self.rename_worker_loop.start()

    async def cog_unload(self) -> None:
        self.rename_worker_loop.cancel()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WalletCog(bot))
