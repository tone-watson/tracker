from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from tracker.temporal import next_due, normalize_recurrence, parse_due


@pytest.mark.parametrize(
    "value,precision,start,end",
    [
        ("2024", "year", "2024-01-01T06:00:00.000000Z", "2025-01-01T06:00:00.000000Z"),
        ("2024-02", "month", "2024-02-01T06:00:00.000000Z", "2024-03-01T06:00:00.000000Z"),
        ("2026-W01", "week", "2025-12-29T06:00:00.000000Z", "2026-01-05T06:00:00.000000Z"),
        ("2026-03-08", "day", "2026-03-08T06:00:00.000000Z", "2026-03-09T05:00:00.000000Z"),
        ("2026-11-01", "day", "2026-11-01T05:00:00.000000Z", "2026-11-02T06:00:00.000000Z"),
        ("2026-10-22T09", "hour", "2026-10-22T14:00:00.000000Z", "2026-10-22T14:00:00.000000Z"),
        (
            "2026-10-22T09:12",
            "minute",
            "2026-10-22T14:12:00.000000Z",
            "2026-10-22T14:12:00.000000Z",
        ),
        (
            "2026-10-22T09:12:34",
            "second",
            "2026-10-22T14:12:34.000000Z",
            "2026-10-22T14:12:34.000000Z",
        ),
        (
            "2026-10-22T09:12:34.123456",
            "microsecond",
            "2026-10-22T14:12:34.123456Z",
            "2026-10-22T14:12:34.123456Z",
        ),
    ],
)
def test_iso_precision_and_period_deadline(value, precision, start, end):
    due = parse_due({"value": value})
    assert due["precision"] == precision
    assert due["starts_at"] == start
    assert due["due_at"] == end
    assert due["timezone"] == "America/Chicago"


@pytest.mark.parametrize(
    "value",
    [
        "2023-02-29",
        "2026-13",
        "2026-W54",
        "0000",
        "9999",
        "2026-10-22T25",
        "2026-10-22T09:15:00.1234567",
        "2026-10-22T09:60",
        "2026-10-22T09:00+01:60",
        "2026-10-22T09:00+24:00",
        "tomorrow",
        "",
        None,
    ],
)
def test_invalid_due_is_rejected(value):
    with pytest.raises(ValueError):
        parse_due({"value": value})


def test_invalid_timezone_and_naive_dst_times_are_rejected():
    with pytest.raises(ValueError, match="timezone"):
        parse_due({"value": "2026-10-22", "timezone": "Made/Up"})
    with pytest.raises(ValueError, match="DST gap"):
        parse_due({"value": "2026-03-08T02:30"})
    with pytest.raises(ValueError, match="ambiguous"):
        parse_due({"value": "2026-11-01T01:30"})


def test_explicit_offsets_disambiguate_repeated_hour():
    early = parse_due({"value": "2026-11-01T01:30-05:00"})
    late = parse_due({"value": "2026-11-01T01:30-06:00"})
    assert early["due_at"] == "2026-11-01T06:30:00.000000Z"
    assert late["due_at"] == "2026-11-01T07:30:00.000000Z"


def test_calendar_weekdays_preserve_every_occurrence_and_count():
    original = parse_due({"value": "2026-10-19"})
    recurrence = normalize_recurrence(
        {"mode": "calendar", "rule": "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4"}, original
    )
    current = original
    values = []
    while (current := next_due(current, recurrence, original)) is not None:
        values.append(current["value"])
    assert values == ["2026-10-21", "2026-10-26", "2026-10-28"]


def test_month_end_rrule_skips_invalid_dates_instead_of_clamping():
    original = parse_due({"value": "2026-01-31"})
    recurrence = {"mode": "calendar", "rule": "FREQ=MONTHLY;COUNT=3"}
    march = next_due(original, recurrence, original)
    may = next_due(march, recurrence, original)
    assert march["value"] == "2026-03-31"
    assert may["value"] == "2026-05-31"
    assert next_due(may, recurrence, original) is None


def test_yearly_leap_day_and_month_precision():
    leap = parse_due({"value": "2024-02-29"})
    assert next_due(leap, {"mode": "calendar", "rule": "FREQ=YEARLY"})["value"] == "2028-02-29"
    month = parse_due({"value": "2026-01"})
    following = next_due(month, {"mode": "calendar", "rule": "FREQ=MONTHLY"})
    assert following["value"] == "2026-02"
    assert following["precision"] == "month"


def test_calendar_dst_gap_does_not_consume_count():
    original = parse_due({"value": "2026-03-07T02:30"})
    recurrence = {"mode": "calendar", "rule": "FREQ=DAILY;COUNT=3"}
    monday = next_due(original, recurrence, original)
    tuesday = next_due(monday, recurrence, original)
    assert monday["value"] == "2026-03-09T02:30-05:00"
    assert tuesday["value"] == "2026-03-10T02:30-05:00"
    assert next_due(tuesday, recurrence, original) is None


def test_calendar_keeps_local_time_across_dst_and_first_fold():
    original = parse_due({"value": "2026-10-31T01:30"})
    recurrence = {"mode": "calendar", "rule": "FREQ=DAILY"}
    sunday = next_due(original, recurrence, original)
    monday = next_due(sunday, recurrence, original)
    assert sunday["value"] == "2026-11-01T01:30-05:00"
    assert monday["value"] == "2026-11-02T01:30-06:00"


def test_explicit_second_fold_anchor_is_retained():
    original = parse_due({"value": "2026-11-01T01:30-06:00"})
    assert (
        next_due(original, {"mode": "calendar", "rule": "FREQ=DAILY;COUNT=2"})["value"]
        == "2026-11-02T01:30-06:00"
    )


def test_fractional_seconds_are_not_lost_in_calendar_recurrence():
    original = parse_due({"value": "2026-10-22T09:12:34.123456"})
    following = next_due(original, {"mode": "calendar", "rule": "FREQ=MINUTELY;COUNT=2"})
    assert following["value"] == "2026-10-22T09:13:34.123456-05:00"
    assert following["precision"] == "microsecond"


def test_explicit_offset_can_differ_from_series_timezone_without_losing_minutes():
    original = parse_due({"value": "2026-10-22T09Z", "timezone": "Asia/Kolkata"})
    following = next_due(original, {"mode": "calendar", "rule": "FREQ=DAILY"})
    assert following["value"] == "2026-10-23T14:30+05:30"
    assert following["starts_at"] == "2026-10-23T09:00:00.000000Z"
    assert following["precision"] == "hour"


def test_until_is_inclusive_for_period_and_timed_rules():
    original = parse_due({"value": "2026-10-22"})
    recurrence = {"mode": "calendar", "rule": "FREQ=DAILY;UNTIL=20261023"}
    friday = next_due(original, recurrence, original)
    assert friday["value"] == "2026-10-23"
    assert next_due(friday, recurrence, original) is None
    timed = parse_due({"value": "2026-10-22T09:00"})
    recurrence = {"mode": "calendar", "rule": "FREQ=DAILY;UNTIL=20261023T140000Z"}
    friday = next_due(timed, recurrence, timed)
    assert friday["value"] == "2026-10-23T09:00-05:00"
    assert next_due(friday, recurrence, timed) is None


def test_explicit_boundary_still_respects_original_count_and_interval():
    original = parse_due({"value": "2026-10-19"})
    recurrence = {"mode": "calendar", "rule": "FREQ=DAILY;INTERVAL=2;COUNT=4"}
    following = next_due(
        original, recurrence, original, after=datetime(2026, 10, 22, tzinfo=timezone.utc)
    )
    assert following["value"] == "2026-10-23"
    assert next_due(original, recurrence, original, after="2026-10-26T00:00:00Z") is None


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=SECONDLY",
        "FREQ=DAILY;BYSECOND=0",
        "FREQ=DAILY;BYEASTER=1",
        "FREQ=DAILY;COUNT=2;UNTIL=20261025",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;COUNT=-1",
        "FREQ=DAILY;BYMONTH=13",
        "FREQ=WEEKLY;BYDAY=0MO",
        "FREQ=WEEKLY;BYDAY=1TH",
        "FREQ=DAILY;BYMONTHDAY=0",
        "FREQ=DAILY;BYMONTHDAY=abc",
        "FREQ=DAILY;BYSETPOS=1",
        "FREQ=MONTHLY;BYWEEKNO=1",
        "FREQ=DAILY;BYYEARDAY=1",
        "FREQ=DAILY;BYHOUR=9",
        "FREQ=HOURLY",
        "FREQ=DAILY;FREQ=WEEKLY",
        "RRULE:FREQ=DAILY",
        "FREQ=DAILY\nRDATE:20261024",
        "FREQ=WEEKLY;BYDAY=MO",
        "FREQ=DAILY;UNTIL=20261021",
        "FREQ=DAILY;UNTIL=20260230",
        "FREQ=DAILY;WKST=XX",
        "FREQ=DAILY;UNTIL=20261025T090000",
    ],
)
def test_invalid_or_conflicting_rrules_are_rejected(rule):
    with pytest.raises(ValueError):
        normalize_recurrence({"mode": "calendar", "rule": rule}, parse_due({"value": "2026-10-22"}))


def test_bysetpos_last_weekday_of_month():
    original = parse_due({"value": "2026-01-30"})
    following = next_due(
        original, {"mode": "calendar", "rule": "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"}
    )
    assert following["value"] == "2026-02-27"


def test_after_completion_advances_from_completion_and_clamps_month_ends():
    original = parse_due({"value": "2026-01-10"})
    recurrence = {"mode": "after_completion", "every": 1, "unit": "months"}
    following = next_due(original, recurrence, after="2026-01-31T20:00:00Z")
    assert following["value"] == "2026-02-28"
    assert following["precision"] == "day"
    leap = parse_due({"value": "2024-02-29"})
    assert (
        next_due(
            leap,
            {"mode": "after_completion", "every": 1, "unit": "years"},
            after="2024-02-29T20:00:00Z",
        )["value"]
        == "2025-02-28"
    )


def test_after_completion_hours_are_elapsed_and_days_preserve_wall_clock():
    original = parse_due({"value": "2026-03-07T02:30"})
    elapsed = next_due(
        original,
        {"mode": "after_completion", "every": 24, "unit": "hours"},
        after="2026-03-07T08:30:00Z",
    )
    calendar = next_due(
        original,
        {"mode": "after_completion", "every": 1, "unit": "days"},
        after="2026-03-07T08:30:00Z",
    )
    assert elapsed["value"] == "2026-03-08T03:30-05:00"
    assert calendar["value"] == "2026-03-08T03:30-05:00"
    original = parse_due({"value": "2026-03-07T12:00"})
    elapsed = next_due(
        original,
        {"mode": "after_completion", "every": 24, "unit": "hours"},
        after="2026-03-07T18:00:00Z",
    )
    calendar = next_due(
        original,
        {"mode": "after_completion", "every": 1, "unit": "days"},
        after="2026-03-07T18:00:00Z",
    )
    assert elapsed["value"] == "2026-03-08T13:00-05:00"
    assert calendar["value"] == "2026-03-08T12:00-05:00"


def test_after_completion_retains_exact_completion_instant():
    original = parse_due({"value": "2026-10-22T09:00"})
    following = next_due(
        original,
        {"mode": "after_completion", "every": 3, "unit": "days"},
        after="2026-10-22T17:21:48.123456Z",
    )
    assert following["value"] == "2026-10-25T12:21:48.123456-05:00"


@pytest.mark.parametrize(
    "config",
    [
        {"mode": "after_completion", "every": 0, "unit": "days"},
        {"mode": "after_completion", "every": True, "unit": "days"},
        {"mode": "after_completion", "every": 1.5, "unit": "days"},
        {"mode": "after_completion", "every": 1, "unit": "fortnights"},
        {"mode": "after_completion", "every": 1, "unit": "hours"},
    ],
)
def test_invalid_after_completion_recurrence(config):
    with pytest.raises(ValueError):
        normalize_recurrence(config, parse_due({"value": "2026-10-22"}))


def test_after_completion_requires_aware_completion_timestamp():
    original = parse_due({"value": "2026-10-22"})
    recurrence = {"mode": "after_completion", "every": 1, "unit": "days"}
    with pytest.raises(ValueError, match="completion timestamp"):
        next_due(original, recurrence)
    with pytest.raises(ValueError, match="timezone-aware"):
        next_due(original, recurrence, after=datetime(2026, 10, 22))


@pytest.mark.parametrize("suffix", [";COUNT=200", ";UNTIL=20261022T190000Z", ""])
def test_interleaved_calendar_queries_are_independent_of_cursor_history(suffix):
    original = parse_due({"value": "2026-10-22T09:00"})
    recurrence = {"mode": "calendar", "rule": "FREQ=MINUTELY" + suffix}
    start = datetime(2026, 10, 22, 14, tzinfo=timezone.utc)
    offsets = [40, 1, 190, 20, 20, 0, 199, 8, 100, 2] * 4

    def lookup(offset):
        result = next_due(original, recurrence, original, after=start + timedelta(minutes=offset))
        return result["starts_at"] if result else None

    with ThreadPoolExecutor(max_workers=8) as executor:
        actual = list(executor.map(lookup, offsets))
    expected = [
        None
        if suffix == ";COUNT=200" and offset == 199
        else (start + timedelta(minutes=offset + 1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        for offset in offsets
    ]
    assert actual == expected
