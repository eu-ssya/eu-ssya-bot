# 활동 현황 PC 고정폭 표 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/활동현황`을 활동일 내림차순 랭킹으로 바꾸고, PC Discord에서 읽기 쉬운 8열 고정폭 표와 같은 형식의 전체 TXT를 관리자에게 제공한다.

**Architecture:** SQLite 수집·집계 모델과 `ReportRow` 필드는 그대로 두고 `ActivityStore.build_report()`의 최종 정렬 키만 교체한다. 표시 계층인 `activity_cog.py`에는 표 셀 폭, 시간, 경고를 변환하는 순수 함수를 두고 페이지와 TXT가 하나의 행 렌더러를 공유하게 한다. 권한은 기존 Discord 등록 권한과 런타임 관리자·보고서 소유자 검사를 그대로 유지하며 회귀 테스트로 고정한다.

**Tech Stack:** Python 3.11+, 표준 라이브러리 `datetime`·`re`·`unicodedata`, `discord.py>=2.4,<3.0`, SQLite, `unittest`/`IsolatedAsyncioTestCase`

## Global Constraints

- SQLite 테이블·컬럼·인덱스·마이그레이션과 `ReportRow` 필드는 변경하지 않는다.
- 대상 멤버 선정, 음성 세션 겹침 계산, SoD/EoD 날짜 집계, coverage 산정 쿼리의 의미는 변경하지 않는다.
- 활동일은 `combined_days`, 즉 SoD 또는 EoD 중 하나 이상을 작성한 KST 날짜 수이며 음성 이용일을 포함하지 않는다.
- 정렬 키는 `활동일 ↓` → `독서실+스터디 시간 ↓` → `최근 활동 ↓(없음은 마지막)` → `display_name.casefold() ↑` → `user_id ↑` 순서다.
- 페이지와 TXT의 열 순서는 `순위`, `이름`, `최근 활동`, `독서실`, `스터디`, `SoD`, `EoD`, `활동일`로 고정한다.
- 이름 셀은 terminal-cell 14칸이며 한글·이모지는 2칸, ASCII는 1칸, 결합 문자·variation selector는 0칸으로 계산한다. 넘치면 `...`으로 자른다.
- 최근 활동은 KST `YYYY-MM-DD HH:MM`, 미기록은 정확히 `없음`으로 출력한다.
- 음성 시간은 초를 버린 `0분`, `N분`, `N시간 M분`만 출력하고 접속 횟수는 출력하지 않는다.
- 페이지는 최대 15명이고 제목·설명·코드블록·경고를 모두 포함해 Discord 2,000자 이하여야 한다. 표 15행과 헤더는 보존하고 경고를 먼저 축약한다.
- 전체 TXT는 페이지 제한 없이 동일한 열·시간·경고 형식을 사용하며 Discord 사용자 ID와 음성 접속 횟수를 포함하지 않는다.
- `/활동설정`, `/활동현황`은 등록 시 `default_permissions(administrator=True)`를 유지하고 callback과 버튼에서 런타임 관리자 권한을 다시 검사한다. 보고서 버튼은 최초 실행자만 사용할 수 있다.
- 새 외부 패키지를 추가하지 않는다.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `activity_store.py` | Modify | 집계가 끝난 `ReportRow`를 승인된 랭킹 키로 안정 정렬한다. 스키마와 집계 SQL은 유지한다. |
| `tests/test_activity_store.py` | Modify | 활동일·합산 음성·최근 활동·이름·ID tie-break와 음성 전용 멤버의 활동일 0을 검증한다. |
| `activity_cog.py` | Modify | terminal-cell 계산, 최근 활동·duration·warning KST 변환, 페이지·TXT 공용 표 렌더링을 담당한다. |
| `tests/test_activity_cog.py` | Modify | PC 표 정렬, 15명/2,000자, ID·접속 횟수 제거, TXT 동일 형식, 관리자 이중 권한을 검증한다. |

---

### Task 1: 활동일 중심 랭킹 정렬

**Files:**
- Modify: `activity_store.py:826-972`
- Test: `tests/test_activity_store.py:1223-1527`

**Interfaces:**
- Consumes: 기존 `ReportRow(combined_days, reading_seconds, study_seconds, last_activity_epoch, display_name, user_id)` 필드와 기존 집계 결과
- Produces: `ActivityStore.build_report(...) -> ActivityReport`가 승인된 다섯 단계 키로 정렬한 `ActivityReport.rows`

- [ ] **Step 1: 음성 전용 멤버가 활동일을 얻지 않고 음성 합계로만 동률을 해소하는 실패 테스트로 기존 정렬 테스트를 갱신한다**

`tests/test_activity_store.py`의 `ReportStoreTests.test_zero_member_sort_and_positive_overlap_count`를 다음 기대값으로 바꾼다. 현재 구현은 활동 기록이 없는 멤버를 먼저 놓으므로 `[2, 1, 3]`을 반환해 실패해야 한다.

```python
def test_voice_only_member_ranks_by_voice_but_keeps_activity_days_zero(self):
    members = [
        activity_store.ReportMember(1, "Zulu"),
        activity_store.ReportMember(2, "Alpha"),
        activity_store.ReportMember(3, "Bravo"),
    ]
    self.store.reconcile_session(1, 3, "study", 100)
    self.store.reconcile_session(1, 3, None, 160, close_reason="normal")

    report = self.store.build_report(
        guild_id=1,
        members=members,
        start_epoch=0,
        end_epoch=200,
        as_of_epoch=200,
    )

    self.assertEqual([row.user_id for row in report.rows], [3, 2, 1])
    voice_only = report.rows[0]
    self.assertEqual(
        (
            voice_only.study_seconds,
            voice_only.study_session_count,
            voice_only.combined_days,
        ),
        (60, 1, 0),
    )
```

- [ ] **Step 2: 활동일·음성·최근활동·이름·ID의 모든 tie-break를 한 번에 고정하는 실패 테스트를 작성한다**

`ReportStoreTests`에 아래 테스트를 추가한다. 모든 이벤트는 기존 public 저장 API를 사용하며 SQLite를 직접 조작하지 않는다.

```python
def test_report_ranking_applies_every_tie_break_in_order(self):
    previous_start, _ = kst_range_to_epoch(date(2026, 7, 31), date(2026, 7, 31))
    start_epoch, end_epoch = kst_range_to_epoch(
        date(2026, 8, 1), date(2026, 8, 2)
    )
    self.store.apply_config_change(
        1, sod_eod_channel_id=10, effective_at_epoch=previous_start
    )

    message_id = 1

    def record(user_id, created_epoch):
        nonlocal message_id
        self.store.record_live_message(
            guild_id=1,
            channel_id=10,
            message_id=message_id,
            user_id=user_id,
            message_created_epoch=created_epoch,
            event_types={"sod"},
            updated_epoch=created_epoch,
            expected_current_channel_id=10,
        )
        message_id += 1

    # user 1: 활동일 2일. 다른 모든 tie-break보다 먼저 온다.
    record(1, start_epoch + 10)
    record(1, start_epoch + 86400 + 10)

    # users 2~6: 활동일 1일. 이후 키를 독립적으로 비교한다.
    for user_id in (2, 3, 4, 5, 6):
        record(user_id, start_epoch + 10)

    # user 2: 합산 음성 120초로 users 3~6보다 먼저 온다.
    self.store.reconcile_session(1, 2, "study", start_epoch + 100)
    self.store.reconcile_session(
        1, 2, None, start_epoch + 220, close_reason="normal"
    )

    # user 3: 합산 음성은 60초로 같지만 최근 활동이 users 4~6보다 늦다.
    self.store.reconcile_session(1, 3, "reading_room", start_epoch + 200)
    self.store.reconcile_session(
        1, 3, None, start_epoch + 260, close_reason="normal"
    )

    # users 4~6: 활동일·음성·최근 활동까지 같아 이름과 ID로 정렬한다.
    for user_id in (4, 5, 6):
        self.store.reconcile_session(1, user_id, "study", start_epoch + 100)
        self.store.reconcile_session(
            1, user_id, None, start_epoch + 160, close_reason="normal"
        )

    # user 7은 조회 범위 밖 보존 활동이 있고, user 8은 활동이 전혀 없다.
    record(7, previous_start + 10)

    report = self.store.build_report(
        guild_id=1,
        members=[
            activity_store.ReportMember(8, "No activity"),
            activity_store.ReportMember(5, "Beta"),
            activity_store.ReportMember(6, "alpha"),
            activity_store.ReportMember(4, "Alpha"),
            activity_store.ReportMember(3, "Recent"),
            activity_store.ReportMember(2, "Voice"),
            activity_store.ReportMember(1, "Days"),
            activity_store.ReportMember(7, "Old record"),
        ],
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        as_of_epoch=end_epoch,
    )

    self.assertEqual(
        [row.user_id for row in report.rows],
        [1, 2, 3, 4, 6, 5, 7, 8],
    )
    self.assertEqual(report.rows[-2].combined_days, 0)
    self.assertIsNotNone(report.rows[-2].last_activity_epoch)
    self.assertIsNone(report.rows[-1].last_activity_epoch)
```

- [ ] **Step 3: 정렬 테스트가 현재 구현에서 Red인지 확인한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest `
  tests.test_activity_store.ReportStoreTests.test_voice_only_member_ranks_by_voice_but_keeps_activity_days_zero `
  tests.test_activity_store.ReportStoreTests.test_report_ranking_applies_every_tie_break_in_order -v
```

Expected: 두 테스트 모두 FAIL. 첫 테스트는 실제 순서 `[2, 1, 3]`, 두 번째 테스트는 `last_activity_epoch is None` 행이 앞에 있는 현재 순서를 보여야 한다.

- [ ] **Step 4: `build_report()`의 최종 정렬 키만 승인된 순서로 교체한다**

`activity_store.py:946-953`을 다음으로 바꾼다. `ReportRow`, SQL, 스키마에는 손대지 않는다.

```python
rows.sort(
    key=lambda row: (
        -row.combined_days,
        -(row.reading_seconds + row.study_seconds),
        row.last_activity_epoch is None,
        -(row.last_activity_epoch or 0),
        row.display_name.casefold(),
        row.user_id,
    )
)
```

- [ ] **Step 5: 새 정렬 테스트와 전체 store 테스트가 Green인지 확인한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest tests.test_activity_store.ReportStoreTests -v
& "venv\Scripts\python.exe" -m unittest tests.test_activity_store -v
```

Expected: 모두 PASS. 기존 `test_all_55_current_members_are_returned_in_deterministic_order`, `test_daily_counts_are_distinct_and_combined_days_are_a_union`, coverage 경고 테스트도 유지되어야 한다.

- [ ] **Step 6: 정렬 변경을 커밋한다**

```powershell
git add activity_store.py tests/test_activity_store.py
git commit -m "fix: 활동 현황 순위 기준 수정"
```

---

### Task 2: terminal-cell·시간·경고 표시 순수 함수

**Files:**
- Modify: `activity_cog.py:1-190`
- Test: `tests/test_activity_cog.py:3618-3761`

**Interfaces:**
- Consumes: `ReportRow`의 초 단위 음성 시간과 epoch, `CoverageWarning(code: str, text: str)`
- Produces: `_terminal_cell_width(value: str) -> int`, `_fit_terminal_cell(value: str, width: int, *, align: str = "left") -> str`, `_format_recent_activity(epoch: int | None) -> str`, `_format_duration(seconds: int) -> str`, `_humanize_warning(warning: CoverageWarning) -> str`

- [ ] **Step 1: `unicodedata` 기반 cell 폭과 14칸 이름 절단의 실패 테스트를 작성한다**

`tests/test_activity_cog.py`의 `ActivityReportViewTests` 앞에 순수 함수 전용 클래스를 추가한다.

```python
class ActivityReportFormattingTests(unittest.TestCase):
    def test_terminal_cell_width_handles_korean_ascii_emoji_and_combining_text(self):
        from activity_cog import _fit_terminal_cell, _terminal_cell_width

        for value in ("Alice", "홍길동", "👩🏽‍💻", "e\u0301"):
            with self.subTest(value=value):
                fitted = _fit_terminal_cell(value, 14)
                self.assertEqual(_terminal_cell_width(fitted), 14)

        truncated = _fit_terminal_cell("가나다라마바사아", 14)
        self.assertEqual(_terminal_cell_width(truncated), 14)
        self.assertTrue(truncated.rstrip().endswith("..."))
        self.assertNotIn("아", truncated)

    def test_terminal_cell_alignment_uses_cell_width_not_python_length(self):
        from activity_cog import _fit_terminal_cell

        self.assertEqual(_fit_terminal_cell("한글", 6), "한글  ")
        self.assertEqual(_fit_terminal_cell("12", 4, align="right"), "  12")
```

- [ ] **Step 2: 최근 활동 KST와 duration 분 버림 규칙의 실패 테스트를 작성한다**

같은 클래스에 다음 테스트를 추가한다.

```python
def test_recent_activity_and_duration_are_human_readable(self):
    from activity_cog import _format_duration, _format_recent_activity

    epoch = int(datetime.datetime(2026, 8, 3, 23, 24, tzinfo=KST).timestamp())
    self.assertEqual(_format_recent_activity(epoch), "2026-08-03 23:24")
    self.assertEqual(_format_recent_activity(None), "없음")

    cases = {
        0: "0분",
        59: "0분",
        60: "1분",
        45 * 60 + 59: "45분",
        65 * 60 + 59: "1시간 5분",
        12 * 3600 + 30 * 60 + 59: "12시간 30분",
    }
    for seconds, expected in cases.items():
        with self.subTest(seconds=seconds):
            self.assertEqual(_format_duration(seconds), expected)
```

- [ ] **Step 3: warning의 range·단일 epoch가 KST 문장으로 바뀌는 실패 테스트를 작성한다**

```python
def test_warning_epochs_are_replaced_with_kst_text(self):
    from activity_cog import _humanize_warning

    gap_start = int(
        datetime.datetime(2026, 8, 1, 3, 0, tzinfo=KST).timestamp()
    )
    gap_end = int(
        datetime.datetime(2026, 8, 1, 5, 10, tzinfo=KST).timestamp()
    )
    warning = CoverageWarning(
        code="voice_gap",
        text=(
            f"음성 수집 누락 구간: {gap_start}~{gap_end} UTC epoch. "
            "이 구간의 값은 부분 데이터입니다."
        ),
    )

    rendered = _humanize_warning(warning)

    self.assertEqual(
        rendered,
        "음성 수집 공백: 2026-08-01 03:00 KST ~ "
        "2026-08-01 05:10 KST. 이 구간의 값은 부분 데이터입니다.",
    )
    self.assertNotIn(str(gap_start), rendered)
    self.assertNotIn("UTC epoch", rendered)

    single = _humanize_warning(
        CoverageWarning(
            code="sod_history_partial",
            text=f"접근 가능한 이력은 {gap_start} UTC epoch부터입니다.",
        )
    )
    self.assertEqual(
        single,
        "접근 가능한 이력은 2026-08-01 03:00 KST부터입니다.",
    )
```

- [ ] **Step 4: 새 순수 함수 테스트가 정의 누락으로 Red인지 확인한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.ActivityReportFormattingTests -v
```

Expected: FAIL 또는 ERROR. `_fit_terminal_cell`, `_terminal_cell_width`, `_format_duration`, `_format_recent_activity`, `_humanize_warning`가 아직 없다.

- [ ] **Step 5: grapheme-like cluster와 cell 정렬 순수 함수를 최소 구현한다**

`activity_cog.py`에 `unicodedata`를 import하고 아래 순수 helper를 기존 formatter와 나란히 추가한다. 이 Task에서는 기존 `_clean_display_name`, `_base36`, `_page_identifier`, `_page_number`, `_page_row_line`을 제거하거나 호출부를 바꾸지 않는다. Task 3에서 새 고정폭 표로 전환할 때에만 더 이상 쓰이지 않는 기존 formatter를 제거한다. ZWJ 다음 문자와 피부색 modifier를 같은 이모지 cluster에 붙여 `👩🏽‍💻` 전체를 2칸으로 계산한다.

```python
import unicodedata


def _is_zero_width_extension(character: str) -> bool:
    codepoint = ord(character)
    return bool(
        character == "\u200d"
        or unicodedata.combining(character)
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
    )


def _terminal_clusters(value: str) -> list[str]:
    clusters: list[str] = []
    for character in value:
        if (
            not clusters
            or (
                not _is_zero_width_extension(character)
                and not clusters[-1].endswith("\u200d")
            )
        ):
            clusters.append(character)
        else:
            clusters[-1] += character
    return clusters


def _cluster_cell_width(cluster: str) -> int:
    for character in cluster:
        if _is_zero_width_extension(character):
            continue
        codepoint = ord(character)
        if (
            unicodedata.east_asian_width(character) in {"W", "F"}
            or 0x2600 <= codepoint <= 0x27BF
            or 0x1F000 <= codepoint <= 0x1FAFF
        ):
            return 2
        return 1
    return 0


def _terminal_cell_width(value: str) -> int:
    return sum(_cluster_cell_width(cluster) for cluster in _terminal_clusters(value))


def _fit_terminal_cell(
    value: str,
    width: int,
    *,
    align: str = "left",
) -> str:
    if align not in {"left", "right"}:
        raise ValueError("align must be 'left' or 'right'")
    if width < 3:
        raise ValueError("terminal cell width must be at least 3")

    normalized = " ".join(str(value).split())
    clusters = _terminal_clusters(normalized)
    rendered = normalized
    rendered_width = _terminal_cell_width(rendered)
    if rendered_width > width:
        kept: list[str] = []
        kept_width = 0
        for cluster in clusters:
            cluster_width = _cluster_cell_width(cluster)
            if kept_width + cluster_width + 3 > width:
                break
            kept.append(cluster)
            kept_width += cluster_width
        rendered = "".join(kept) + "..."
        rendered_width = kept_width + 3

    padding = " " * (width - rendered_width)
    return padding + rendered if align == "right" else rendered + padding
```

- [ ] **Step 6: 최근 활동, duration, warning KST 변환을 최소 구현한다**

warning은 range를 먼저 치환한 뒤 단일 epoch를 치환해야 range의 양 끝이 중복 처리되지 않는다. 저장된 `CoverageWarning.text`는 바꾸지 않고 사용자 출력 직전에만 변환한다.

```python
_WARNING_EPOCH_RANGE = re.compile(r"(?P<start>\d+)~(?P<end>\d+) UTC epoch")
_WARNING_EPOCH_POINT = re.compile(r"(?P<epoch>\d+) UTC epoch")


def _format_kst_minute(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, KST).strftime("%Y-%m-%d %H:%M KST")


def _format_recent_activity(epoch: int | None) -> str:
    if epoch is None:
        return "없음"
    try:
        return datetime.fromtimestamp(epoch, KST).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "표시 불가"


def _format_duration(seconds: int) -> str:
    total_minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}분"
    return f"{hours}시간 {minutes}분"


def _humanize_warning(warning: CoverageWarning) -> str:
    text = " ".join(str(warning.text).split())
    text = _WARNING_EPOCH_RANGE.sub(
        lambda match: (
            f"{_format_kst_minute(int(match.group('start')))} ~ "
            f"{_format_kst_minute(int(match.group('end')))}"
        ),
        text,
    )
    text = _WARNING_EPOCH_POINT.sub(
        lambda match: _format_kst_minute(int(match.group("epoch"))),
        text,
    )
    if warning.code == "voice_gap":
        text = text.replace("음성 수집 누락 구간", "음성 수집 공백")
    return text
```

- [ ] **Step 7: 순수 함수 테스트와 기존 Cog 테스트를 실행한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.ActivityReportFormattingTests -v
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog -v
```

Expected: 새 순수 함수 테스트와 기존 `tests.test_activity_cog` 전체가 모두 PASS. 이 Task는 기존 formatter의 호출부를 변경하지 않으므로 기존 페이지 출력 계약도 유지된다.

- [ ] **Step 8: 표시 순수 함수 변경을 커밋한다**

```powershell
git add activity_cog.py tests/test_activity_cog.py
git commit -m "feat: 활동 현황 표시 형식 유틸리티 추가"
```

---

### Task 3: 15명 PC 고정폭 페이지 표

**Files:**
- Modify: `activity_cog.py:33-36,136-218`
- Test: `tests/test_activity_cog.py:3618-3761`

**Interfaces:**
- Consumes: Task 2의 `_fit_terminal_cell`, `_format_recent_activity`, `_format_duration`, `_humanize_warning`; 이미 정렬된 `ActivityReport.rows`
- Produces: `_report_table_lines(rows: list[ReportRow], *, start_rank: int) -> list[str]`, `format_report_page(report: ActivityReport, page_index: int) -> str`

- [ ] **Step 1: 기존 ID 기반 페이지 도달 테스트를 전역 순위 기반 테스트로 바꾼다**

`ActivityReportViewTests.test_exact_page_counts_and_all_rows_are_reachable`을 다음으로 교체한다. 16번째 멤버가 2페이지에서 순위 16으로 유지되는 것도 검증한다.

```python
def test_exact_page_counts_and_global_ranks_are_reachable(self):
    from activity_cog import format_report_page

    for count, expected_pages in ((1, 1), (15, 1), (16, 2), (55, 4)):
        with self.subTest(count=count):
            report = report_with_members(count)
            self.assertEqual(report.page_count, expected_pages)
            pages = [format_report_page(report, page) for page in range(expected_pages)]
            rendered_rows = []
            for page in pages:
                table = page.split("```text\n", 1)[1].split("\n```", 1)[0]
                rendered_rows.extend(table.splitlines()[1:])
            self.assertEqual(len(rendered_rows), count)
            self.assertTrue(rendered_rows[0].startswith("   1 "))
            self.assertTrue(rendered_rows[-1].startswith(f"{count:>4} "))
```

- [ ] **Step 2: 실제 8열, 최근 활동, duration, ID·접속 횟수 제거를 검증하는 실패 테스트를 작성한다**

```python
def test_page_renders_readable_fixed_width_table_without_ids_or_session_counts(self):
    from activity_cog import format_report_page

    recent_epoch = int(
        datetime.datetime(2026, 8, 3, 23, 24, tzinfo=KST).timestamp()
    )
    row = ReportRow(
        user_id=987654321012345678,
        display_name="한정수",
        last_activity_epoch=recent_epoch,
        reading_seconds=12 * 3600 + 30 * 60 + 59,
        study_seconds=20 * 60 + 35,
        reading_session_count=99,
        study_session_count=88,
        sod_days=28,
        eod_days=21,
        combined_days=29,
    )

    page = format_report_page(make_report([row]), 0)

    self.assertIn(
        "순위 이름           최근 활동          독서실           스터디           SoD EoD 활동일",
        page,
    )
    self.assertIn("2026-08-03 23:24", page)
    self.assertIn("12시간 30분", page)
    self.assertIn("20분", page)
    self.assertIn(" 28", page)
    self.assertIn(" 21", page)
    self.assertIn(" 29", page)
    self.assertNotIn(str(row.user_id), page)
    self.assertNotIn("독서초", page)
    self.assertNotIn("독서회", page)
    self.assertNotIn("스터디초", page)
    self.assertNotIn("스터디회", page)
```

- [ ] **Step 3: 15개 긴 Unicode 이름과 긴 경고에서도 표를 보존하고 2,000자를 넘지 않는 실패 테스트로 기존 worst-case 테스트를 교체한다**

기존의 비현실적인 `10**250` 필드 테스트는 승인된 출력 계약을 검증하는 아래 테스트로 바꾼다.

```python
def test_fifteen_long_unicode_rows_keep_table_and_shorten_warnings_under_2000(self):
    from activity_cog import _terminal_cell_width, format_report_page

    rows = [
        self.row(
            index,
            display_name=f"{index:02d}-" + ("👩🏽‍💻한글Alice" * 30),
            last_activity_epoch=int(
                datetime.datetime(2026, 8, 3, 23, 24, tzinfo=KST).timestamp()
            ),
        )
        for index in range(1, 16)
    ]
    warnings = [
        CoverageWarning(
            code=f"warning_{index:02d}",
            text=f"경고 {index:02d} " + ("매우 긴 경고 내용 " * 100),
        )
        for index in range(30)
    ]

    page = format_report_page(make_report(rows, warnings=warnings), 0)
    table = page.split("```text\n", 1)[1].split("\n```", 1)[0]
    lines = table.splitlines()

    self.assertLessEqual(len(page), 2000)
    page.encode("utf-8")
    self.assertEqual(len(lines), 16)  # header + 15명
    for rank, line in enumerate(lines[1:], 1):
        self.assertTrue(line.startswith(f"{rank:>4} "))
        # 순위 4칸 뒤 공백 다음의 이름 cell이 정확히 14 terminal cells다.
        name_and_rest = line[5:]
        name_cell = name_and_rest.split(" 2026-", 1)[0]
        self.assertEqual(_terminal_cell_width(name_cell), 14)
        self.assertIn("...", name_cell)
```

- [ ] **Step 4: 페이지 표 테스트가 현재 압축 로그 출력에서 Red인지 확인한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest `
  tests.test_activity_cog.ActivityReportViewTests.test_exact_page_counts_and_global_ranks_are_reachable `
  tests.test_activity_cog.ActivityReportViewTests.test_page_renders_readable_fixed_width_table_without_ids_or_session_counts `
  tests.test_activity_cog.ActivityReportViewTests.test_fifteen_long_unicode_rows_keep_table_and_shorten_warnings_under_2000 -v
```

Expected: FAIL. 현재 출력에는 코드블록·표 헤더가 없고 `[user_id]`, `독서초`, 접속 횟수가 포함된다.

- [ ] **Step 5: 공용 8열 표 행 렌더러를 구현한다**

`activity_cog.py`에서 기존 `_page_row_line`을 제거하고 다음 고정폭 열과 공용 렌더러를 추가한다. 이름은 왼쪽, 순위·시간·집계는 오른쪽 정렬한다.

```python
_TABLE_COLUMNS = (
    ("순위", 4, "right"),
    ("이름", 14, "left"),
    ("최근 활동", 16, "left"),
    ("독서실", 16, "right"),
    ("스터디", 16, "right"),
    ("SoD", 3, "right"),
    ("EoD", 3, "right"),
    ("활동일", 6, "right"),
)


def _table_line(values: tuple[str, ...]) -> str:
    return " ".join(
        _fit_terminal_cell(value, width, align=align)
        for value, (_label, width, align) in zip(values, _TABLE_COLUMNS)
    )


def _report_table_lines(
    rows: list[ReportRow],
    *,
    start_rank: int,
) -> list[str]:
    lines = [_table_line(tuple(label for label, _width, _align in _TABLE_COLUMNS))]
    for offset, row in enumerate(rows):
        name = " ".join(str(row.display_name).split()) or "이름 없음"
        lines.append(
            _table_line(
                (
                    str(start_rank + offset),
                    name,
                    _format_recent_activity(row.last_activity_epoch),
                    _format_duration(row.reading_seconds),
                    _format_duration(row.study_seconds),
                    str(row.sod_days),
                    str(row.eod_days),
                    str(row.combined_days),
                )
            )
        )
    return lines
```

- [ ] **Step 6: 표를 먼저 고정한 뒤 남은 길이 안에서 warning을 축약하도록 페이지 렌더러를 교체한다**

`_warning_summary_lines`는 반드시 `_humanize_warning(warning)`을 사용한다. `format_report_page`는 표를 먼저 만들고 warning에 남은 예산만 전달한다. 현재 `REPORT_CONTENT_LIMIT = 1900`은 이 Step에서 Discord 승인 상한에 맞춰 명시적으로 `REPORT_CONTENT_LIMIT = 2000`으로 변경한다.

```python
def _warning_summary_lines(
    report: ActivityReport,
    *,
    character_limit: int,
) -> list[str]:
    if character_limit <= 0:
        return []
    if not report.warnings:
        return ["경고: 없음"] if len("경고: 없음") <= character_limit else []
    warning_count = len(report.warnings)
    warning_kind_count = len({warning.code for warning in report.warnings})
    lines = [f"경고: 총 {warning_count}건/{warning_kind_count}종"]
    if len(lines[0]) > character_limit:
        compact = f"경고: 총 {warning_count}건 · 상세는 전체 TXT 참조."
        return [compact] if len(compact) <= character_limit else []
    used = len(lines[0])
    for index, warning in enumerate(report.warnings):
        line = f"- {_humanize_warning(warning)}"
        remaining = len(report.warnings) - index
        omitted = f"- 나머지 {remaining}건 상세는 전체 TXT 참조."
        reserve = 1 + len(omitted) if remaining else 0
        if used + 1 + len(line) + reserve > character_limit:
            if used + 1 + len(omitted) <= character_limit:
                lines.append(omitted)
                return lines
            compact = f"경고: 총 {warning_count}건 · 상세는 전체 TXT 참조."
            return [compact] if len(compact) <= character_limit else []
        lines.append(line)
        used += 1 + len(line)
    return lines


def format_report_page(report: ActivityReport, page_index: int) -> str:
    page_count = max(1, report.page_count)
    page_index = min(max(0, page_index), page_count - 1)
    start = page_index * REPORT_PAGE_SIZE
    rows = report.rows[start : start + REPORT_PAGE_SIZE]
    table_lines = _report_table_lines(rows, start_rank=start + 1)
    if not rows:
        table_lines.append("표시할 대상 멤버가 없습니다.")

    fixed_lines = [
        (
            f"활동 현황 · {report.start_date} ~ {report.end_date} "
            f"· {page_index + 1}/{page_count}"
        ),
        "정렬: 활동일 ↓ · 음성시간 ↓ · 최근활동 ↓",
        "```text",
        *table_lines,
        "```",
    ]
    fixed_content = "\n".join(fixed_lines)
    warning_budget = REPORT_CONTENT_LIMIT - len(fixed_content) - 1
    warning_lines = _warning_summary_lines(
        report,
        character_limit=min(REPORT_WARNING_SUMMARY_LIMIT, warning_budget),
    )
    content = "\n".join([*fixed_lines, *warning_lines])
    if len(content) > REPORT_CONTENT_LIMIT:
        raise ValueError("activity report page budget exceeded")
    return content
```

`REPORT_CONTENT_LIMIT`은 기존 `1900`에서 Discord 상한과 같은 `2000`으로 변경하고, `REPORT_PAGE_SIZE`는 `15`로 유지한다. `_warning_summary_lines`는 남은 예산에 따라 상세 경고, `경고: 총 N건 · 상세는 전체 TXT 참조.`, 경고 줄 생략 순으로 결정적으로 축약하므로 표 15행과 헤더를 절대 줄이지 않는다.

- [ ] **Step 7: 페이지 단위 테스트와 버튼 페이지 이동 테스트가 Green인지 확인한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.ActivityReportFormattingTests -v
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.ActivityReportViewTests -v
```

Expected: PASS. 55명은 4페이지이고, 2페이지의 첫 행은 순위 16이며, 페이지당 최대 15명·전체 2,000자 이하·사용자 ID 없음이 확인되어야 한다.

- [ ] **Step 8: PC 표 출력을 커밋한다**

```powershell
git add activity_cog.py tests/test_activity_cog.py
git commit -m "feat: 활동 현황 PC 표 출력 적용"
```

---

### Task 4: 동일 형식 전체 TXT와 관리자 이중 권한 회귀

**Files:**
- Modify: `activity_cog.py:221-339,356-369,392-510`
- Test: `tests/test_activity_cog.py:3763-3903,3906-4032`

**Interfaces:**
- Consumes: Task 3의 `_report_table_lines(..., start_rank=1)`와 Task 2의 `_humanize_warning`, 기존 `ActivityReportView.interaction_check`, `require_admin`
- Produces: `build_report_txt(report: ActivityReport) -> str`가 페이지 제한 없이 동일한 8열 전체 표를 반환한다. 기존 `ActivityReportView.full_txt_button`은 UTF-8 `BytesIO` 파일로 이를 전송한다.

- [ ] **Step 1: TXT가 페이지와 같은 행을 쓰며 ID·초·접속 횟수를 제거하는 실패 테스트로 기존 TXT 테스트를 교체한다**

`test_txt_defers_then_sends_one_use_bytesio_file_with_all_fields`를 다음 계약으로 바꾼다.

```python
async def test_txt_uses_same_table_without_ids_seconds_or_session_counts(self):
    from activity_cog import build_report_txt, format_report_page

    recent_epoch = int(
        datetime.datetime(2026, 8, 3, 23, 24, tzinfo=KST).timestamp()
    )
    row = self.row(
        index=987654321012345678,
        display_name="보고 대상",
        last_activity_epoch=recent_epoch,
    )
    report = make_report([row])
    view, _original = self.make_view(report)
    interaction = fake_interaction(1, True, FakeGuild(1))

    await press(view, view.full_txt_button, interaction)

    self.assertTrue(interaction.response.deferred)
    self.assertFalse(interaction.response.defer_kwargs["thinking"])
    self.assertEqual(len(interaction.followup.sent), 1)
    _content, kwargs = interaction.followup.sent[0]
    self.assertTrue(kwargs["ephemeral"])
    attachment = kwargs["file"]
    self.assertIsInstance(attachment, discord.File)
    self.assertEqual(attachment.filename, report.txt_filename)
    self.assertIsInstance(attachment.fp, __import__("io").BytesIO)
    payload = attachment.fp.getvalue().decode("utf-8")
    self.assertEqual(payload, build_report_txt(report))

    page_table = format_report_page(report, 0).split("```text\n", 1)[1].split(
        "\n```", 1
    )[0]
    self.assertIn(page_table, payload)
    self.assertIn("2026-08-03 23:24", payload)
    self.assertIn("1분", payload)
    for forbidden in (
        str(row.user_id),
        "user_id=",
        "reading_seconds=",
        "study_seconds=",
        "reading_session_count=",
        "study_session_count=",
        "독서회",
        "스터디회",
    ):
        self.assertNotIn(forbidden, payload)
```

- [ ] **Step 2: TXT가 15명 제한 없이 모든 행과 사람이 읽는 전체 warning을 포함하는 실패 테스트를 추가한다**

```python
def test_txt_contains_all_rows_and_full_humanized_warnings(self):
    from activity_cog import build_report_txt

    gap_start = int(
        datetime.datetime(2026, 8, 1, 3, 0, tzinfo=KST).timestamp()
    )
    gap_end = int(
        datetime.datetime(2026, 8, 1, 5, 10, tzinfo=KST).timestamp()
    )
    report = make_report(
        [self.row(index, display_name=f"Member {index:02d}") for index in range(1, 56)],
        warnings=[
            CoverageWarning(
                code="voice_gap",
                text=(
                    f"음성 수집 누락 구간: {gap_start}~{gap_end} UTC epoch. "
                    "이 구간의 값은 부분 데이터입니다."
                ),
            )
        ],
    )

    payload = build_report_txt(report)
    table_lines = [line for line in payload.splitlines() if line[:4].strip().isdigit()]

    self.assertEqual(len(table_lines), 55)
    self.assertTrue(table_lines[0].startswith("   1 "))
    self.assertTrue(table_lines[-1].startswith("  55 "))
    self.assertIn(
        "음성 수집 공백: 2026-08-01 03:00 KST ~ 2026-08-01 05:10 KST",
        payload,
    )
    self.assertNotIn("UTC epoch", payload)
```

- [ ] **Step 3: 기존 warning 공유 테스트를 원시 epoch 비노출 계약으로 강화한다**

`test_page_and_txt_share_warning_text`를 다음으로 바꾼다.

```python
def test_page_and_txt_share_humanized_warning_text(self):
    from activity_cog import build_report_txt, format_report_page

    warning = CoverageWarning(
        code="gateway_disconnect",
        text="음성 수집 공백: 1785607200~1785610800 UTC epoch",
    )
    report = make_report([self.row()], warnings=[warning])
    page = format_report_page(report, 0)
    text = build_report_txt(report)

    self.assertIn("KST", page)
    self.assertIn("KST", text)
    self.assertNotIn("1785607200", page)
    self.assertNotIn("1785607200", text)
    self.assertNotIn("UTC epoch", page)
    self.assertNotIn("UTC epoch", text)
```

- [ ] **Step 4: 관리자 등록 권한·callback·버튼 소유자 검사의 회귀 테스트 범위를 확인하고 `/활동현황 기간` 비관리자 사례를 추가한다**

기존 테스트는 삭제하지 않는다.

- `ActivitySettingsCommandTests.test_settings_group_is_guild_only_with_admin_registration_hint`: `/활동설정`의 `default_permissions.administrator` 검증
- `ActivitySettingsCommandTests.test_non_admin_and_dm_guards_run_before_every_settings_mutation`: 모든 설정 callback의 `require_admin` 검증
- `ActivityReportCommandTests.test_report_group_is_guild_only_with_admin_registration_hint_and_range`: `/활동현황`의 `default_permissions.administrator` 검증
- `ActivityReportViewTests.test_owner_admin_and_same_guild_are_rechecked_before_any_mutation`: 다른 관리자, 권한을 잃은 실행자, 다른 길드가 `이전`·`다음`·`전체 TXT`를 쓰지 못함을 검증

`ActivityReportCommandTests.test_non_admin_is_rejected_before_defer_and_store`를 다음처럼 최근·기간 callback 모두 확인하도록 확장한다.

```python
async def test_non_admin_is_rejected_before_defer_and_store(self):
    cog, guild, _member = self.make_fixture()
    cases = (
        (cog.recent_report, (1,)),
        (cog.period_report, ("2026-08-01", "2026-08-03")),
    )
    for command, arguments in cases:
        with self.subTest(command=command.name):
            interaction = fake_interaction(2, False, guild)
            with mock.patch.object(
                cog, "_store_call", new=mock.AsyncMock()
            ) as store_call:
                await command.callback(cog, interaction, *arguments)
            store_call.assert_not_awaited()
            self.assertFalse(interaction.response.deferred)
            self.assertTrue(interaction.response.sent[0][1]["ephemeral"])
```

- [ ] **Step 5: TXT·권한 테스트가 현재 상세 덤프 형식에서 Red인지 확인한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest `
  tests.test_activity_cog.ActivityReportViewTests.test_txt_uses_same_table_without_ids_seconds_or_session_counts `
  tests.test_activity_cog.ActivityReportViewTests.test_txt_contains_all_rows_and_full_humanized_warnings `
  tests.test_activity_cog.ActivityReportViewTests.test_page_and_txt_share_humanized_warning_text `
  tests.test_activity_cog.ActivityReportCommandTests.test_non_admin_is_rejected_before_defer_and_store -v
```

Expected: TXT 관련 테스트는 `user_id`, raw seconds, raw epoch가 남아 FAIL. 관리자 callback 테스트는 기존 보호 동작으로 PASS하며 이후 구현에서도 계속 PASS해야 한다.

- [ ] **Step 6: 페이지와 같은 공용 표와 KST warning으로 전체 TXT를 교체한다**

`activity_cog.py:221-262`의 상세 key/value 덤프를 다음으로 바꾼다. 페이지와 똑같이 `_report_table_lines`를 호출하므로 열 순서와 시간 표현이 갈라지지 않는다.

```python
def build_report_txt(report: ActivityReport) -> str:
    generated = _format_recent_activity(report.generated_epoch)
    lines = [
        "활동 현황 보고서",
        report.period_label,
        f"생성: {generated} KST",
        "정렬: 활동일 ↓ · 음성시간 ↓ · 최근활동 ↓",
        "",
        *_report_table_lines(report.rows, start_rank=1),
    ]
    if not report.rows:
        lines.append("표시할 대상 멤버가 없습니다.")

    lines.extend(["", "전체 coverage 경고:"])
    if report.warnings:
        lines.extend(
            f"- [{warning.code}] {_humanize_warning(warning)}"
            for warning in report.warnings
        )
    else:
        lines.append("- 없음")
    return "\n".join(lines) + "\n"
```

`ActivityReportView.full_txt_button`의 `BytesIO`, 파일명, ephemeral 전송은 변경하지 않는다. `ActivityReportView.interaction_check`, `require_admin`, `settings_group.default_permissions`, `report_group.default_permissions`도 변경하지 않는다.

- [ ] **Step 7: 활동 보고서·권한 집중 테스트를 Green으로 만든다**

Run:

```powershell
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.ActivityReportFormattingTests -v
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.ActivityReportViewTests -v
& "venv\Scripts\python.exe" -m unittest tests.test_activity_cog.ActivityReportCommandTests -v
& "venv\Scripts\python.exe" -m unittest `
  tests.test_activity_cog.ActivitySettingsCommandTests.test_settings_group_is_guild_only_with_admin_registration_hint `
  tests.test_activity_cog.ActivitySettingsCommandTests.test_non_admin_and_dm_guards_run_before_every_settings_mutation -v
```

Expected: 모두 PASS. 다른 관리자도 기존 보고서의 페이지·TXT 버튼을 사용할 수 없고, 비관리자는 두 명령 그룹의 callback에서 store 호출 전에 거부된다.

- [ ] **Step 8: Python 컴파일, 전체 204+ 테스트, diff 범위를 검증한다**

Run:

```powershell
& "venv\Scripts\python.exe" -m py_compile activity_store.py activity_cog.py
& "venv\Scripts\python.exe" -m unittest discover -s tests -v
git diff --check
git diff --stat HEAD
git diff -- activity_store.py activity_cog.py tests/test_activity_store.py tests/test_activity_cog.py
```

Expected:

- `py_compile` 종료 코드 0
- `unittest discover`가 기존 204개에 새 테스트를 더한 수를 실행하고 마지막 줄이 `OK`
- `git diff --check` 출력 없음
- 변경 파일은 `activity_store.py`, `activity_cog.py`, `tests/test_activity_store.py`, `tests/test_activity_cog.py`뿐이며 `activity_store.py`의 `SCHEMA`, `ReportRow`, SQL에는 diff가 없음
- 페이지 및 TXT에 `user_id=`, `독서회`, `스터디회`, `UTC epoch`가 없고 전체 55명 순위가 보존됨

- [ ] **Step 9: TXT와 관리자 회귀 변경을 커밋한다**

```powershell
git add activity_cog.py tests/test_activity_cog.py
git commit -m "feat: 활동 현황 TXT 표 형식 통일"
```
