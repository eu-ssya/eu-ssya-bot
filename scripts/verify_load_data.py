"""bot.load_data가 wallets 키를 자동 초기화하는지 검증."""
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")
import bot

# 새 임시 파일 (없는 파일)
tmp1 = os.path.join(tempfile.gettempdir(), "_test_load_data_missing.json")
if os.path.exists(tmp1):
    os.remove(tmp1)
bot.DATA_FILE = tmp1
d = bot.load_data()
assert d == {"feeds": [], "wallets": {}}, f"missing file: {d!r}"

# feeds만 있고 wallets 없는 파일
tmp2 = os.path.join(tempfile.gettempdir(), "_test_load_data_feeds_only.json")
with open(tmp2, "w", encoding="utf-8") as f:
    json.dump({"feeds": [{"url": "x", "channel_id": 1}]}, f)
bot.DATA_FILE = tmp2
d = bot.load_data()
assert d["feeds"][0]["url"] == "x"
assert d.get("wallets") == {}, f"wallets key missing: {d!r}"
os.remove(tmp2)

# wallets가 dict가 아닌 malformed 파일 — 빈 dict로 강제
tmp3 = os.path.join(tempfile.gettempdir(), "_test_load_data_malformed.json")
with open(tmp3, "w", encoding="utf-8") as f:
    json.dump({"feeds": [], "wallets": "not a dict"}, f)
bot.DATA_FILE = tmp3
d = bot.load_data()
assert d["wallets"] == {}, f"malformed wallets not normalized: {d!r}"
os.remove(tmp3)

# 정상 wallets 존재 — 그대로 유지
tmp4 = os.path.join(tempfile.gettempdir(), "_test_load_data_with_wallets.json")
with open(tmp4, "w", encoding="utf-8") as f:
    json.dump({"feeds": [], "wallets": {"42": {"balance": 100}}}, f)
bot.DATA_FILE = tmp4
d = bot.load_data()
assert d["wallets"]["42"]["balance"] == 100
os.remove(tmp4)

print("verify_load_data: all assertions PASS")
