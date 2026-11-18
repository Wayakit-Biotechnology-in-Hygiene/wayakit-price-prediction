"""Property tests of `infra/instance/pi_boot_guard.py` (the scheduled-boot window).

Rules checked over generated instants, months and time zones:
- equivalence with an obvious reference (calendar.monthrange): True exactly on the last Thursday
  of the local month, local time in [21:40, 23:00);
- True implies: local Thursday, local day >= 22, next Thursday in another month, time in window;
- exactly 80 minutes are True around each month's last Thursday, as one contiguous block that
  starts at 21:40 local, whatever the DST state of the zone that day;
- the result depends only on the instant, not on the offset used to write it;
- t - 7 days and t + 7 days are never True when t is;
- months that end on a Thursday (and years that end on one) open the window on their last day,
  and the next day (Friday, first of the month) is closed;
- DST: for zones with daylight saving time (and Mexico City before 2022) the window follows the
  local wall clock, so its UTC position moves with the offset;
- `main` only returns 0 when the instant is inside; any `--now` text gives 0, 1 or 255.
- `--now` with a trailing `Z` (or `z`) gives the same result as with `+00:00` (Python 3.9/3.10
  `fromisoformat` does not accept `Z`; the instance's system Python is 3.9).

No AWS and no clock: every instant is generated.
"""
from __future__ import annotations

import calendar
import io
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from infra import config
from infra.instance import pi_boot_guard as guard

pytestmark = pytest.mark.property

UTC = timezone.utc
TZ = guard.DEFAULT_TZ
# Zones with DST in the window's months (north and south) plus Mexico City (DST until 2022).
DST_ZONES = ("America/Mexico_City", "America/New_York", "America/Chicago", "Europe/Madrid",
             "Europe/London", "Australia/Sydney", "America/Santiago")
ZONES = DST_ZONES + ("Asia/Riyadh", "UTC", "Asia/Kolkata", "Pacific/Kiritimati")
WINDOW_MINUTES = 80


def last_thursday(year: int, month: int) -> date:
    """Reference: the last Thursday of the month, via calendar.monthrange."""
    days = calendar.monthrange(year, month)[1]
    last = date(year, month, days)
    return last - timedelta(days=(last.weekday() - 3) % 7)


def reference(now_utc: datetime, tz: str = TZ) -> bool:
    local = now_utc.astimezone(ZoneInfo(tz))
    return (local.date() == last_thursday(local.year, local.month)
            and time(21, 40) <= local.time() < time(23, 0))


years = st.integers(1990, 2099)
months = st.integers(1, 12)
utc_instants = st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2099, 12, 31),
                            timezones=st.just(UTC))


@st.composite
def near_window(draw, tz=st.just(TZ)):
    """Instants around the last-Thursday window (inside, at the edges and just outside)."""
    zone = draw(tz)
    day = last_thursday(draw(years), draw(months))
    minutes = draw(st.one_of(st.integers(21 * 60 + 40, 22 * 60 + 59),       # inside
                             st.sampled_from([21 * 60 + 39, 23 * 60, 21 * 60 + 40, 22 * 60 + 59]),
                             st.integers(0, 24 * 60 - 1)))
    seconds = draw(st.integers(0, 59))
    local = datetime.combine(day, time(minutes // 60, minutes % 60, seconds), tzinfo=ZoneInfo(zone))
    return zone, local.astimezone(UTC)


instants = st.one_of(utc_instants, near_window().map(lambda zt: zt[1]))
offsets = st.integers(-14 * 60, 14 * 60).map(lambda m: timezone(timedelta(minutes=m)))


# --- Equivalence with the reference ------------------------------------------------------------
@given(instants)
@example(datetime(2026, 11, 27, 3, 50, tzinfo=UTC))           # the schedule itself
@example(datetime(2027, 1, 1, 3, 50, tzinfo=UTC))             # Thu 31 Dec 2026 local
@example(datetime(2021, 4, 30, 2, 45, tzinfo=UTC))            # CDT (Mexico City DST, 2021)
def test_equivalent_to_the_reference(now):
    assert guard.is_scheduled_window(now) is reference(now)


@given(st.sampled_from(ZONES).flatmap(lambda z: near_window(st.just(z))))
def test_equivalent_to_the_reference_in_any_zone(zone_and_instant):
    zone, now = zone_and_instant
    assert guard.is_scheduled_window(now, zone) is reference(now, zone)


@given(instants)
def test_true_implies_last_thursday_and_window(now):
    assume(guard.is_scheduled_window(now))
    local = now.astimezone(ZoneInfo(TZ))
    assert local.weekday() == 3
    assert local.day >= 22
    assert (local + timedelta(days=7)).month != local.month
    assert time(21, 40) <= local.time() < time(23, 0)


@given(st.dates(min_value=date(1990, 1, 1), max_value=date(2099, 12, 31)))
def test_is_last_thursday_matches_the_reference(day):
    assert guard.is_last_thursday(day) is (day == last_thursday(day.year, day.month))


@given(years, months)
def test_one_last_thursday_per_month(year, month):
    days = calendar.monthrange(year, month)[1]
    hits = [d for d in range(1, days + 1) if guard.is_last_thursday(date(year, month, d))]
    assert hits == [last_thursday(year, month).day]


# --- Exactly 80 minutes per month --------------------------------------------------------------
@given(years, months, st.sampled_from(ZONES))
@example(2026, 11, TZ)
@example(2026, 12, TZ)       # the window is on 31 Dec local and on 1 Jan in UTC
@example(2021, 4, TZ)        # Mexico City in CDT
@example(2026, 3, "America/New_York")   # a few weeks after the DST change
@example(2026, 10, "Europe/London")     # the Thursday before the change back
def test_exactly_80_contiguous_minutes_around_the_last_thursday(year, month, zone):
    """Every UTC minute from local midnight the day before to local midnight the day after."""
    tz = ZoneInfo(zone)
    day = last_thursday(year, month)
    start = datetime.combine(day - timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=2), time(0), tzinfo=tz).astimezone(UTC)
    inside = []
    t = start
    while t < end:
        if guard.is_scheduled_window(t, zone):
            inside.append(t)
        t += timedelta(minutes=1)
    assert len(inside) == WINDOW_MINUTES
    assert inside[-1] - inside[0] == timedelta(minutes=WINDOW_MINUTES - 1), "one contiguous block"
    first = inside[0].astimezone(tz)
    assert (first.date(), first.time()) == (day, time(21, 40))


# --- Offset independence and ±7 days -----------------------------------------------------------
@given(instants, offsets)
def test_depends_only_on_the_instant(now, offset):
    assert guard.is_scheduled_window(now.astimezone(offset)) is guard.is_scheduled_window(now)


@given(near_window())
def test_a_week_before_or_after_is_never_inside(zone_and_instant):
    zone, now = zone_and_instant
    assume(guard.is_scheduled_window(now, zone))
    for delta in (timedelta(days=7), timedelta(days=-7)):
        assert guard.is_scheduled_window(now + delta, zone) is False


# --- Months that end on a Thursday -------------------------------------------------------------
THURSDAY_ENDS = [(y, m) for y in range(1990, 2100) for m in range(1, 13)
                 if date(y, m, calendar.monthrange(y, m)[1]).weekday() == 3]


@given(st.sampled_from(THURSDAY_ENDS), st.integers(21 * 60 + 40, 22 * 60 + 59))
@example((2026, 12), 21 * 60 + 50)
@example((2024, 2), 21 * 60 + 50)
@example((2026, 4), 22 * 60 + 59)
def test_month_ending_on_thursday_opens_on_its_last_day(year_month, minutes):
    year, month = year_month
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    local = datetime.combine(last_day, time(minutes // 60, minutes % 60), tzinfo=ZoneInfo(TZ))
    assert guard.is_scheduled_window(local.astimezone(UTC)) is True
    # One day later is the first of the next month (a Friday): closed.
    assert guard.is_scheduled_window((local + timedelta(days=1)).astimezone(UTC)) is False


# --- DST: the window follows the local wall clock ---------------------------------------------
@given(st.sampled_from(DST_ZONES), years, months, st.integers(21 * 60 + 40, 22 * 60 + 59))
@example("America/New_York", 2026, 3, 21 * 60 + 40)      # EDT (UTC-4)
@example("America/New_York", 2026, 11, 21 * 60 + 40)     # EST (UTC-5)
@example("America/Mexico_City", 2021, 4, 21 * 60 + 40)   # CDT before 2022
@example("Australia/Sydney", 2026, 10, 22 * 60 + 59)     # southern-hemisphere DST
def test_window_follows_local_time_across_dst(zone, year, month, minutes):
    tz = ZoneInfo(zone)
    day = last_thursday(year, month)
    local = datetime.combine(day, time(minutes // 60, minutes % 60), tzinfo=tz)
    now = local.astimezone(UTC)
    assert guard.is_scheduled_window(now, zone) is True
    before = datetime.combine(day, time(21, 39, 59), tzinfo=tz).astimezone(UTC)
    after = datetime.combine(day, time(23, 0), tzinfo=tz).astimezone(UTC)
    assert guard.is_scheduled_window(before, zone) is False
    assert guard.is_scheduled_window(after, zone) is False
    # With the zone's offset of another season the same UTC instant is not always inside: the
    # guard must not use a fixed offset. Checked against the reference, which uses the zone.
    assert guard.is_scheduled_window(now, zone) is reference(now, zone)


# --- main ----------------------------------------------------------------------------------------
def _main(argv, now=None):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            return guard.main(argv, now=now)
        except SystemExit as exc:
            return exc.code


@given(instants)
def test_main_returns_0_only_inside(now):
    expected = guard.EXIT_INSIDE if guard.is_scheduled_window(now) else guard.EXIT_OUTSIDE
    assert _main([], now=now) == expected
    assert _main([f"--now={now.isoformat()}"]) == expected


@given(st.text(max_size=40))
@example("2026-11-27T03:50:00")             # no offset
@example("2026-11-27T03:50:00+00:00")
@example("")
@example("-1")
@example("2026-11-27T03:50:00Z")
@example("Z")
def test_any_now_text_gives_0_1_or_255_and_0_only_when_inside(text):
    rc = _main([f"--now={text}"])
    assert rc in (0, 1, 255)
    if rc == 0:
        iso = text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text
        parsed = datetime.fromisoformat(iso)
        assert parsed.tzinfo is not None and guard.is_scheduled_window(parsed)


@given(st.sampled_from(ZONES).flatmap(lambda z: near_window(st.just(z))), st.sampled_from(["Z", "z"]))
@example((TZ, datetime(2026, 11, 27, 3, 50, tzinfo=UTC)), "Z")     # the schedule's instant
@example((TZ, datetime(2026, 11, 27, 5, 0, tzinfo=UTC)), "z")      # window end (exclusive)
def test_trailing_z_equals_plus_zero_offset(zone_and_instant, suffix):
    zone, now = zone_and_instant
    stamp = now.astimezone(UTC).replace(tzinfo=None).isoformat()
    with_z = _main([f"--now={stamp}{suffix}", f"--tz={zone}"])
    with_offset = _main([f"--now={stamp}+00:00", f"--tz={zone}"])
    assert with_z == with_offset == (0 if reference(now.astimezone(UTC), zone) else 1)


@given(st.sampled_from(ZONES + ("Mars/Olympus_Mons", "", "../etc/passwd", "UTC+3")), instants)
def test_any_tz_gives_0_1_or_255(zone, now):
    rc = _main([f"--now={now.isoformat()}", f"--tz={zone}"])
    if zone in ZONES:
        assert rc == (0 if reference(now, zone) else 1)
    else:
        assert rc == 255


def test_guard_timezone_is_the_schedule_timezone():
    assert TZ == config.SCHEDULE_TIMEZONE
