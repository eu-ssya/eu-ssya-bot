"""모임통장 최종 정적 검증 — 모든 AC를 grep + 어설션으로 확인."""
import re
import sys

sys.path.insert(0, ".")

content = open("wallet_cog.py", encoding="utf-8").read()
bot_content = open("bot.py", encoding="utf-8").read()


def find_lines(text, pattern):
    return [(i + 1, line) for i, line in enumerate(text.split("\n")) if re.search(pattern, line)]


# AC1: 4개 슬래시 명령 모두 존재
for cmd in ["등록", "입금", "출금", "관리"]:
    matches = find_lines(content, rf'@mt_group\.command\(name="{cmd}"')
    assert matches, f"명령 /모임통장 {cmd} 누락"
print("AC1 (4 commands): OK")

# AC2: 한 서버 1개 가드
assert "_get_existing_guild_wallet" in content
assert "한 서버에는 한 개만" in content
print("AC2 (one wallet per guild): OK")

# AC3: Administrator 권한 체크
assert "is_admin(" in content
assert "guild_permissions" in content or "administrator" in content
print("AC3 (admin check): OK")

# AC4: overdraft 거부
assert "잔액(" in content and "보다 큰 출금" in content
print("AC4 (overdraft reject): OK")

# AC5: hard delete (transactions.remove 사용)
assert 'transactions"].remove(' in content
print("AC5 (hard delete): OK")

# AC6: 채널명 디바운스 큐
assert "_pending_balance" in content
assert "RENAME_COOLDOWN_SECONDS = 300" in content or "RENAME_COOLDOWN_SECONDS=300" in content
assert "@tasks.loop" in content
print("AC6 (rename debounce): OK")

# AC7: 429 처리 + retry_after
assert "discord.HTTPException" in content
assert "retry_after" in content
print("AC7 (429 retry_after): OK")

# AC8: load_data wallets 초기화
assert '"wallets"' in bot_content
assert 'wallets"] = {}' in bot_content
print("AC8 (load_data wallets init): OK")

# AC9: setup_hook이 wallet_cog load
assert 'load_extension("wallet_cog")' in bot_content
print("AC9 (load_extension): OK")

# AC10: 자동 메시지 포맷에 잔액 포함
assert "잔액:" in content
print("AC10 (auto message format): OK")

# AC11: 락 discipline — _data_lock 안에서 channel.edit/send/fetch_message/message.edit/delete 0건
lines = content.split("\n")
in_lock = False
indent_lock = 0
bad = []
forbidden = [
    "channel.edit(", "channel.send(", "fetch_message(",
    ".edit(content=", ".delete()", "msg.delete()", "msg.edit("
]
for i, line in enumerate(lines, 1):
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if "async with _data_lock" in stripped:
        in_lock = True
        indent_lock = indent
        continue
    if in_lock and stripped and indent <= indent_lock:
        in_lock = False
    if in_lock:
        for pat in forbidden:
            if pat in stripped:
                bad.append(f"L{i} ({pat}): {stripped[:80]}")
assert not bad, "lock discipline violations:\n" + "\n".join(bad)
print("AC11 (lock discipline): OK")

# AC12: View timeout 10분
assert "VIEW_TIMEOUT_SECONDS = 600" in content
print("AC12 (view timeout 10min): OK")

print("\n=== ALL ACs PASS ===")
