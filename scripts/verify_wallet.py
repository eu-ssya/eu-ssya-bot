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
for bad in ["2026/11/28", "2026-13-32", "20261128", "yesterday", "", "2026-11", "2026-2-3"]:
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
