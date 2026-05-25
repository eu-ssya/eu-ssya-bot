"""모임통장 pure function 검증 — TOKEN 없이 실행 가능."""
import sys
from datetime import date

sys.path.insert(0, ".")
import wallet_cog as W

# format_krw
assert W.format_krw(50000) == "50,000원", W.format_krw(50000)
assert W.format_krw(-15000) == "-15,000원", W.format_krw(-15000)
assert W.format_krw(0) == "0원"
assert W.format_krw(1234567) == "1,234,567원"

# _format_channel_name
assert W._format_channel_name(285000) == "💰-285,000원"
assert W._format_channel_name(0) == "💰-0원"

# _parse_date 유효
assert W._parse_date("2026-11-28") == date(2026, 11, 28)
assert W._parse_date("2026-01-01") == date(2026, 1, 1)

# _parse_date 무효 — ValueError 발생해야
for bad in ["2026/11/28", "2026-13-32", "20261128", "yesterday", "", "2026-11", "2026-2-3", "abcd-ef-gh", "20a6-11-28"]:
    try:
        W._parse_date(bad)
        raise AssertionError(f"_parse_date('{bad}') should have raised")
    except ValueError:
        pass

# compute_new_balance
assert W.compute_new_balance(100000, "income", 50000) == 150000
assert W.compute_new_balance(100000, "expense", 30000) == 70000
assert W.compute_new_balance(0, "income", 1) == 1
try:
    W.compute_new_balance(0, "weird", 1)
    raise AssertionError("unknown kind should raise")
except ValueError:
    pass

# validate_amount
assert W.validate_amount(1) is None
assert W.validate_amount(50000) is None
assert W.validate_amount(0) is not None  # error message
assert W.validate_amount(-100) is not None
assert W.validate_amount(True) is not None  # bool은 int 아님
assert W.validate_amount(False) is not None

# validate_memo
assert W.validate_memo("") is None
assert W.validate_memo("a" * 200) is None
assert W.validate_memo("a" * 201) is not None

# 상수 확인
assert W.RENAME_COOLDOWN_SECONDS >= 300
assert W.RENAME_WORKER_INTERVAL_SECONDS > 0
assert isinstance(W._pending_balance, dict)
assert isinstance(W._last_rename, dict)

print("verify_wallet: all assertions PASS")

# ---- 가드 함수 존재 확인 ----
import inspect
assert callable(W.is_admin), "is_admin not defined"
assert callable(W._require_text_channel), "_require_text_channel not defined"
assert callable(W._get_existing_guild_wallet), "_get_existing_guild_wallet not defined"

# _get_existing_guild_wallet 순수 로직
sample_wallets = {
    "100": {"guild_id": "G1", "balance": 50},
    "200": {"guild_id": "G2", "balance": 80},
}
hit = W._get_existing_guild_wallet(sample_wallets, "G1")
assert hit is not None and hit[0] == "100"
miss = W._get_existing_guild_wallet(sample_wallets, "G999")
assert miss is None

print("guard helpers OK")

# ---- _format_transaction_message ----
msg = W._format_transaction_message("income", 50000, "지각벌금 홍길동", "2026-11-28", 285000)
assert msg == "📥 +50,000원 · 지각벌금 홍길동 · 2026-11-28 · 잔액: 285,000원", msg

msg2 = W._format_transaction_message("expense", 15000, "", "2026-11-26", 270000)
assert msg2 == "📤 -15,000원 · 2026-11-26 · 잔액: 270,000원", msg2  # 메모 생략

msg3 = W._format_transaction_message("income", 100, "테스트", "2026-01-01", 100)
assert msg3 == "📥 +100원 · 테스트 · 2026-01-01 · 잔액: 100원", msg3

print("format_transaction_message OK")

# ---- cooldown 계산 검증 ----
# 429 시 _last_rename 값: now + bump - COOLDOWN
# 다음 체크: cur_now - _last_rename < COOLDOWN ? skip : proceed

COOLDOWN = W.RENAME_COOLDOWN_SECONDS
t0 = 1000.0
# retry_after=600 인 경우 bump = max(COOLDOWN, 600) = 600
bump = max(COOLDOWN, 600.0)
last_rename = t0 + bump - COOLDOWN  # = 1000 + 600 - 300 = 1300
# 다음 워커 틱 t=1060: 1060 - 1300 = -240 < 300 → skip ✓
# 다음 워커 틱 t=1600: 1600 - 1300 = 300 ≥ 300 → proceed ✓
assert (1060 - last_rename) < COOLDOWN  # skip
assert (1600 - last_rename) >= COOLDOWN  # proceed

# retry_after 없는 일반 성공: _last_rename = now, 다음 시도는 now+COOLDOWN 후
t0 = 2000.0
last_rename_ok = t0
assert (t0 + COOLDOWN - 1 - last_rename_ok) < COOLDOWN
assert (t0 + COOLDOWN - last_rename_ok) >= COOLDOWN

print("cooldown math OK")
