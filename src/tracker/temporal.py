"""Timezone-aware due periods and recurrence without database side effects."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from dateutil.rrule import rrulestr

DEFAULT_TIMEZONE = "America/Chicago"
UTC = timezone.utc
PERIOD_PRECISIONS = {"year", "month", "week", "day"}
FREQUENCIES = {"YEARLY": 0, "MONTHLY": 1, "WEEKLY": 2, "DAILY": 3, "HOURLY": 4, "MINUTELY": 5}
PRECISION_RANK = {
    "year": 0,
    "month": 1,
    "week": 2,
    "day": 3,
    "hour": 4,
    "minute": 5,
    "second": 6,
    "microsecond": 7,
}
UNIT_RANK = {"years": 0, "months": 1, "weeks": 2, "days": 3, "hours": 4, "minutes": 5}
WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
INTEGER_PARTS = {
    "BYMONTH": (1, 12),
    "BYMONTHDAY": (-31, 31),
    "BYYEARDAY": (-366, 366),
    "BYWEEKNO": (-53, 53),
    "BYHOUR": (0, 23),
    "BYMINUTE": (0, 59),
    "BYSETPOS": (-366, 366),
}
RULE_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST", "BYDAY", *INTEGER_PARTS}
DATETIME_PATTERN = re.compile(
    r"(?P<day>\d{4}-\d{2}-\d{2})[Tt ](?P<hour>\d{2})"
    r"(?::(?P<minute>\d{2})(?::(?P<second>\d{2})(?:[.,](?P<fraction>\d{1,6}))?)?)?"
    r"(?P<offset>[Zz]|[+-]\d{2}(?::?\d{2})?)?"
)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _zone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("timezone must be an IANA timezone name")
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc


def _valid_local_times(value: datetime, zone: ZoneInfo) -> list[datetime]:
    found = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != value:
            continue
        if not found or candidate.utcoffset() != found[0].utcoffset():
            found.append(candidate)
    return found


def _localize(value: datetime, zone: ZoneInfo, *, strict: bool = True) -> datetime:
    candidates = _valid_local_times(value, zone)
    if not candidates:
        raise ValueError(f"Local time {value.isoformat()} does not exist in {zone.key} (DST gap)")
    if strict and len(candidates) > 1:
        raise ValueError(
            f"Local time {value.isoformat()} is ambiguous in {zone.key}; supply an explicit UTC offset"
        )
    return candidates[0]


def _aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("after must be an ISO datetime with a UTC offset") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("after must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _format_datetime(value: datetime, precision: str) -> str:
    if precision == "microsecond" or value.microsecond:
        return value.isoformat(timespec="microseconds")
    if precision == "second" or value.second:
        return value.isoformat(timespec="seconds")
    if precision == "minute" or value.minute:
        return value.isoformat(timespec="minutes")
    return value.isoformat(timespec="hours")


def parse_due(due: Mapping[str, Any]) -> dict[str, str]:
    """Normalize an ISO due value, defaulting its IANA timezone to Chicago.

    ``parse_due({"value": "2026-10-22"})`` represents the whole local day:
    starts_at is 2026-10-22T05:00:00.000000Z and due_at is the next midnight.
    Year, month, ISO week and date values expire at the next period boundary;
    hour, minute, second and fractional datetimes expire at the given instant.
    Offset-free repeated local times require an explicit offset. Every UTC
    timestamp uses six fractional digits so SQL text ordering is reliable.
    """
    if not isinstance(due, Mapping):
        raise ValueError("due must be an object containing value and optional timezone")
    raw = due.get("value")
    if not isinstance(raw, str) or not raw or len(raw) > 80:
        raise ValueError("due.value must be a nonempty ISO date or datetime")
    value = raw.strip()
    zone = _zone(due.get("timezone", DEFAULT_TIMEZONE))
    try:
        if re.fullmatch(r"\d{4}", value):
            start = date(int(value), 1, 1)
            end = date(start.year + 1, 1, 1)
            precision = "year"
        elif re.fullmatch(r"\d{4}-\d{2}", value):
            start = date.fromisoformat(value + "-01")
            end = start + relativedelta(months=1)
            precision = "month"
        elif re.fullmatch(r"\d{4}-W\d{2}", value):
            start = date.fromisocalendar(int(value[:4]), int(value[6:]), 1)
            end = start + timedelta(days=7)
            precision = "week"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            start = date.fromisoformat(value)
            end = start + timedelta(days=1)
            precision = "day"
        else:
            match = DATETIME_PATTERN.fullmatch(value)
            if match is None:
                raise ValueError(
                    "Use an ISO year, month, week, date, or datetime (up to six fractional digits)"
                )
            groups = match.groupdict()
            offset = groups["offset"]
            if offset and offset not in {"Z", "z"}:
                digits = offset[1:].replace(":", "")
                if int(digits[:2]) > 23 or (len(digits) == 4 and int(digits[2:]) > 59):
                    raise ValueError("UTC offset hours or minutes are out of range")
            precision = next(
                name
                for name, key in (
                    ("microsecond", "fraction"),
                    ("second", "second"),
                    ("minute", "minute"),
                    ("hour", "hour"),
                )
                if groups[key] is not None
            )
            parsed = datetime.fromisoformat(
                value.replace("t", "T")
                .replace("z", "+00:00")
                .replace("Z", "+00:00")
                .replace(",", ".")
            )
            if parsed.tzinfo is None:
                parsed = _localize(parsed, zone)
            else:
                parsed.astimezone(zone)
            timestamp = _utc(parsed)
            return {
                "value": _format_datetime(parsed, precision),
                "timezone": zone.key,
                "precision": precision,
                "starts_at": timestamp,
                "due_at": timestamp,
            }
        start_at = _localize(datetime.combine(start, time.min), zone, strict=False)
        due_at = _localize(datetime.combine(end, time.min), zone, strict=False)
        return {
            "value": value,
            "timezone": zone.key,
            "precision": precision,
            "starts_at": _utc(start_at),
            "due_at": _utc(due_at),
        }
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"Invalid due value {value!r}: {exc}") from exc


def _parts(rule: str) -> dict[str, str]:
    parts = {}
    for field in rule.split(";"):
        if field.count("=") != 1:
            raise ValueError(
                "Calendar rule must contain semicolon-separated RRULE key=value fields"
            )
        key, value = field.split("=")
        if key not in RULE_PARTS:
            raise ValueError(
                f"Unsupported RRULE field: {key}; SECONDLY, BYSECOND and extensions are not supported"
            )
        if key in parts or not value:
            raise ValueError(f"Duplicate or empty RRULE field: {key}")
        parts[key] = value
    return parts


def _until(value: str, precision: str) -> date | datetime:
    try:
        if re.fullmatch(r"\d{8}", value) and precision in PERIOD_PRECISIONS:
            return datetime.strptime(value, "%Y%m%d").date()
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid RRULE UNTIL: {value}") from exc
    raise ValueError(
        "UNTIL must be a UTC YYYYMMDDTHHMMSSZ timestamp (or YYYYMMDD for a calendar-period due date)"
    )


def _within_until(candidate: datetime, end: date | datetime | None) -> bool:
    if end is None:
        return True
    if isinstance(end, datetime):
        return candidate.astimezone(UTC) <= end
    return candidate.date() <= end


def _calendar_rule(parts: Mapping[str, str], anchor: datetime):
    text = ";".join(
        f"{key}={value}" for key, value in parts.items() if key not in {"COUNT", "UNTIL"}
    )
    return rrulestr(text, dtstart=anchor.replace(tzinfo=None, microsecond=0))


def _calendar_time(candidate: datetime, anchor: datetime, zone: ZoneInfo) -> datetime | None:
    candidate = candidate.replace(microsecond=anchor.microsecond)
    if candidate == anchor.replace(tzinfo=None):
        return anchor
    candidates = _valid_local_times(candidate, zone)
    return candidates[0] if candidates else None


def normalize_recurrence(config: Mapping[str, Any], due: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a calendar RRULE or an interval measured after completion.

    Calendar example: {"mode": "calendar", "rule": "FREQ=WEEKLY;BYDAY=MO"}.
    Completion example: {"mode": "after_completion", "every": 3, "unit": "days"}.
    The due date must match a calendar rule's first occurrence. Frequencies
    finer than the due date's precision are rejected instead of losing time.
    """
    if not isinstance(config, Mapping):
        raise ValueError("recurrence must be an object")
    normalized_due = (
        due
        if isinstance(due, Mapping) and "starts_at" in due and "precision" in due
        else parse_due(due)
    )
    precision = normalized_due["precision"]
    if precision not in PRECISION_RANK:
        raise ValueError("Invalid due precision")
    mode = config.get("mode")
    if mode == "after_completion":
        if set(config) != {"mode", "every", "unit"}:
            raise ValueError("after_completion recurrence requires only mode, every and unit")
        every = config["every"]
        unit = config["unit"]
        if not isinstance(every, int) or isinstance(every, bool) or not 1 <= every <= 10000:
            raise ValueError("recurrence.every must be an integer between 1 and 10000")
        if not isinstance(unit, str) or unit not in UNIT_RANK:
            raise ValueError("recurrence.unit must be minutes, hours, days, weeks, months or years")
        if UNIT_RANK[unit] > PRECISION_RANK[precision]:
            raise ValueError("Recurrence interval cannot be finer than the due date's precision")
        return {"mode": mode, "every": every, "unit": unit}
    if mode != "calendar" or set(config) != {"mode", "rule"}:
        raise ValueError(
            "Calendar recurrence requires mode='calendar' and rule; completion recurrence requires mode='after_completion', every and unit"
        )
    raw_rule = config["rule"]
    if (
        not isinstance(raw_rule, str)
        or not raw_rule
        or len(raw_rule) > 4096
        or any(char.isspace() for char in raw_rule)
    ):
        raise ValueError(
            "Calendar rule must be a single RRULE value of at most 4096 characters without whitespace"
        )
    parts = _parts(raw_rule.upper())
    frequency = parts.get("FREQ")
    if frequency not in FREQUENCIES:
        raise ValueError("RRULE FREQ must be MINUTELY, HOURLY, DAILY, WEEKLY, MONTHLY or YEARLY")
    if FREQUENCIES[frequency] > PRECISION_RANK[precision]:
        raise ValueError("Calendar frequency cannot be finer than the due date's precision")
    if precision == "week" and frequency != "WEEKLY":
        raise ValueError("ISO-week due dates require a WEEKLY calendar frequency")
    if "COUNT" in parts and "UNTIL" in parts:
        raise ValueError("RRULE must use either COUNT or UNTIL, not both")
    for key, limit in (("INTERVAL", 10000), ("COUNT", 1000000)):
        if key in parts:
            if not re.fullmatch(r"\d+", parts[key]) or not 1 <= int(parts[key]) <= limit:
                raise ValueError(f"RRULE {key} must be an integer between 1 and {limit}")
            parts[key] = str(int(parts[key]))
    if "WKST" in parts and parts["WKST"] not in WEEKDAYS:
        raise ValueError("RRULE WKST must be a weekday abbreviation")
    for key, (minimum, maximum) in INTEGER_PARTS.items():
        if key not in parts:
            continue
        numbers = parts[key].split(",")
        if any(not re.fullmatch(r"[+-]?\d+", item) for item in numbers):
            raise ValueError(f"Invalid RRULE {key} values")
        values = [int(item) for item in numbers]
        if any(
            number < minimum or number > maximum or (minimum < 0 and number == 0)
            for number in values
        ):
            raise ValueError(f"RRULE {key} is out of range")
        parts[key] = ",".join(str(number) for number in dict.fromkeys(values))
    if "BYDAY" in parts:
        for value in parts["BYDAY"].split(","):
            match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", value)
            if match is None:
                raise ValueError("Invalid RRULE BYDAY value")
            if match[1] is not None:
                ordinal = int(match[1])
                if (
                    ordinal == 0
                    or abs(ordinal) > 53
                    or frequency not in {"MONTHLY", "YEARLY"}
                    or "BYWEEKNO" in parts
                ):
                    raise ValueError(
                        "Ordinal BYDAY requires MONTHLY or YEARLY without BYWEEKNO, and an ordinal between -53 and 53 excluding zero"
                    )
    if "BYWEEKNO" in parts and frequency != "YEARLY":
        raise ValueError("BYWEEKNO requires YEARLY frequency")
    if "BYYEARDAY" in parts and frequency in {"DAILY", "WEEKLY", "MONTHLY"}:
        raise ValueError("BYYEARDAY is not valid with DAILY, WEEKLY or MONTHLY frequency")
    if "BYMONTHDAY" in parts and frequency == "WEEKLY":
        raise ValueError("BYMONTHDAY is not valid with WEEKLY frequency")
    if "BYSETPOS" in parts and not any(key.startswith("BY") and key != "BYSETPOS" for key in parts):
        raise ValueError("BYSETPOS requires another BY selector")
    forbidden = {
        "year": {key for key in parts if key.startswith("BY")},
        "month": {"BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYHOUR", "BYMINUTE"},
        "week": {"BYDAY", "BYHOUR", "BYMINUTE"},
        "day": {"BYHOUR", "BYMINUTE"},
        "hour": {"BYMINUTE"},
    }.get(precision, set())
    if forbidden.intersection(parts):
        raise ValueError(
            f"RRULE selectors {', '.join(sorted(forbidden.intersection(parts)))} are finer than {precision} due precision"
        )
    end = _until(parts["UNTIL"], precision) if "UNTIL" in parts else None
    zone = _zone(normalized_due["timezone"])
    anchor = _aware(normalized_due["starts_at"]).astimezone(zone)
    try:
        first = next(iter(_calendar_rule(parts, anchor)), None)
    except (ValueError, OverflowError, KeyError) as exc:
        raise ValueError(f"Invalid calendar rule: {exc}") from exc
    if (
        first is None
        or first != anchor.replace(tzinfo=None, microsecond=0)
        or not _within_until(anchor, end)
    ):
        raise ValueError(
            "The initial due date must match the calendar rule and fall on or before UNTIL"
        )
    return {"mode": "calendar", "rule": ";".join(f"{key}={value}" for key, value in parts.items())}


def _due_from_local(value: datetime, precision: str, zone: ZoneInfo) -> dict[str, str]:
    if precision == "year":
        text = f"{value.year:04d}"
    elif precision == "month":
        text = f"{value.year:04d}-{value.month:02d}"
    elif precision == "week":
        year, week, _ = value.isocalendar()
        text = f"{year:04d}-W{week:02d}"
    elif precision == "day":
        text = value.date().isoformat()
    else:
        text = _format_datetime(value, precision)
    due = parse_due({"value": text, "timezone": zone.key})
    if precision not in PERIOD_PRECISIONS:
        due["precision"] = precision
    return due


def _after_completion(
    due: Mapping[str, Any], recurrence: Mapping[str, Any], after: datetime | str | None
) -> dict[str, str] | None:
    if after is None:
        raise ValueError("after_completion recurrence requires the completion timestamp as after")
    boundary = _aware(after)
    zone = _zone(due["timezone"])
    every, unit = recurrence["every"], recurrence["unit"]
    try:
        if unit in {"minutes", "hours"}:
            candidate = (boundary + timedelta(**{unit: every})).astimezone(zone)
        else:
            local = boundary.astimezone(zone).replace(tzinfo=None)
            shifted = local + relativedelta(**{unit: every})
            candidates = _valid_local_times(shifted, zone)
            if candidates:
                candidate = candidates[0]
            else:
                candidate = shifted.replace(tzinfo=zone).astimezone(UTC).astimezone(zone)
        return _due_from_local(candidate, due["precision"], zone)
    except (OverflowError, ValueError) as exc:
        if "out of range" in str(exc) or isinstance(exc, OverflowError):
            return None
        raise


class _CalendarCursor:
    """Bounded-memory optimization for sequential series expansion."""

    def __init__(self, rule: str, anchor_timestamp: str, zone_key: str, precision: str):
        self.zone = _zone(zone_key)
        self.anchor = _aware(anchor_timestamp).astimezone(self.zone)
        self.parts = _parts(rule)
        self.rule = _calendar_rule(self.parts, self.anchor)
        self.end = _until(self.parts["UNTIL"], precision) if "UNTIL" in self.parts else None
        self.count = int(self.parts["COUNT"]) if "COUNT" in self.parts else None
        self.boundary = None
        self.iterator = None
        self.valid_count = 0
        self.pending = None
        self.exhausted = False

    def next(self, boundary: datetime) -> datetime | None:
        if self.boundary is None or boundary < self.boundary:
            if self.count is None:
                local = boundary.astimezone(self.zone).replace(tzinfo=None, microsecond=0)
                self.iterator = self.rule.xafter(local, inc=True)
            else:
                self.iterator = iter(self.rule)
            self.valid_count = 0
            self.pending = None
            self.exhausted = False
        self.boundary = boundary
        if self.pending is not None:
            if self.pending.astimezone(UTC) > boundary:
                return self.pending
            self.pending = None
        if self.exhausted:
            return None
        for local in self.iterator:
            candidate = _calendar_time(local, self.anchor, self.zone)
            if candidate is None:
                continue
            if not _within_until(candidate, self.end):
                self.exhausted = True
                return None
            self.valid_count += 1
            if self.count is not None and self.valid_count > self.count:
                self.exhausted = True
                return None
            if candidate.astimezone(UTC) > boundary:
                self.pending = candidate
                return candidate
        self.exhausted = True
        return None


@lru_cache(maxsize=128)
def _calendar_cursor(
    rule: str, anchor_timestamp: str, zone_key: str, precision: str
) -> _CalendarCursor:
    return _CalendarCursor(rule, anchor_timestamp, zone_key, precision)


_CURSOR_LOCK = RLock()


def next_due(
    due: Mapping[str, Any],
    recurrence: Mapping[str, Any],
    anchor_due: Mapping[str, Any] | None = None,
    after: datetime | str | None = None,
) -> dict[str, str] | None:
    """Return the next occurrence strictly after the supplied boundary.

    Keep ``anchor_due`` equal to the series' original due date. Calendar mode
    defaults its boundary to ``due.starts_at`` and never skips missed cycles.
    COUNT includes the initial occurrence, while invalid dates and DST gaps
    do not count. Ambiguous generated times use the first clock occurrence.
    Completion mode requires ``after`` and adds elapsed minutes/hours or local
    calendar days/weeks/months/years, clamping month ends and advancing gaps.
    Returns None when COUNT, UNTIL or the representable calendar is exhausted.
    """
    current = due if isinstance(due, Mapping) and "starts_at" in due else parse_due(due)
    anchor_input = anchor_due if anchor_due is not None else current
    anchor_due = (
        anchor_input
        if isinstance(anchor_input, Mapping) and "starts_at" in anchor_input
        else parse_due(anchor_input)
    )
    recurrence = normalize_recurrence(recurrence, anchor_due)
    if recurrence["mode"] == "after_completion":
        return _after_completion(current, recurrence, after)
    boundary = _aware(after if after is not None else current["starts_at"])
    zone = _zone(anchor_due["timezone"])
    try:
        with _CURSOR_LOCK:
            cursor = _calendar_cursor(
                recurrence["rule"], anchor_due["starts_at"], zone.key, anchor_due["precision"]
            )
            candidate = cursor.next(boundary)
        return (
            _due_from_local(candidate, anchor_due["precision"], zone)
            if candidate is not None
            else None
        )
    except (OverflowError, ValueError) as exc:
        if "out of range" in str(exc) or isinstance(exc, OverflowError):
            return None
        raise
