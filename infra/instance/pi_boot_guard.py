#!/usr/bin/env python3
"""Boot guard of the Price Intelligence instance: run the pipeline only on the scheduled boot.

What it leaves configured: nothing. It is a check that `pi-pipeline.service` runs as its
`ExecCondition=` on every boot of `wayakit-app-server`. The EventBridge schedule
(`infra/schedule.py`) starts the instance on the last Thursday of the month at 21:50
America/Mexico_City; any other boot (maintenance over SSM, a manual start, the F4 deploy start)
must not launch a 30-hour scraping run and an Odoo upload.

Exit codes (systemd `ExecCondition=` semantics):
- 0: inside the window -> systemd runs `ExecStart` (`run_pipeline.sh`, no arguments).
- 1: outside the window -> systemd skips `ExecStart`; the unit is NOT failed and the instance
  stays on for whoever started it.
- 255: the guard itself failed (bad time zone, bad `--now`, unexpected error) -> the unit is
  marked failed. Any error MUST end here and never in 1-254, which systemd would read as a
  silent "skip".

The window is [21:40, 23:00) local time on the last Thursday of the month: the schedule fires at
21:50 and the boot takes a few minutes; 10 minutes before covers clock skew, the rest covers a
slow start. Mexico City has no daylight saving time since 2022, but the check works on local
time through `zoneinfo`, so it would follow DST if the time zone ever had it.

Standalone on purpose (stdlib only, Python >= 3.9, no repo imports): it runs with the system
`python3` of Amazon Linux 2023 before conda is involved. `DEFAULT_TZ` must stay equal to
`infra.config.SCHEDULE_TIMEZONE` (a unit test checks it).

Cost: none.

Usage:
    python3 infra/instance/pi_boot_guard.py                     # now; exit 0 inside the window
    python3 infra/instance/pi_boot_guard.py --now 2026-11-27T03:50:00+00:00   # or ...03:50:00Z
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Mexico_City"  # == infra.config.SCHEDULE_TIMEZONE
WINDOW_START = time(21, 40)  # inclusive
WINDOW_END = time(23, 0)  # exclusive
THURSDAY = 3  # date.weekday()

EXIT_INSIDE = 0
EXIT_OUTSIDE = 1
EXIT_ERROR = 255


def is_last_thursday(day: date) -> bool:
    """True if `day` is a Thursday and the next Thursday falls in another month."""
    return day.weekday() == THURSDAY and (day + timedelta(days=7)).month != day.month


def is_scheduled_window(now_utc: datetime, tz: str = DEFAULT_TZ) -> bool:
    """True only on the last Thursday of the month between 21:40 (inclusive) and 23:00
    (exclusive) local time in `tz`. `now_utc` must be timezone-aware (any offset; it is converted);
    a naive datetime raises ValueError instead of guessing its zone."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    local = now_utc.astimezone(ZoneInfo(tz))
    return is_last_thursday(local.date()) and WINDOW_START <= local.time() < WINDOW_END


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2, which ExecCondition would read as "skip": make it a failure.
        self.print_usage(sys.stderr)
        print(f"pi_boot_guard: {message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def _parse_now(text: str) -> datetime:
    # fromisoformat accepts a trailing "Z" only from Python 3.11; the instance may run 3.9/3.10.
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError("--now needs a UTC offset, e.g. 2026-11-27T03:50:00+00:00 or 2026-11-27T03:50:00Z")
    return value


def main(argv=None, now=None) -> int:
    """Return the exit code (see the module docstring). `now` lets tests inject the clock; pass
    `argv=[]` in tests (None reads sys.argv). Argument errors raise SystemExit(EXIT_ERROR)."""
    parser = _Parser(description="Exit 0 only inside the scheduled monthly boot window.")
    parser.add_argument("--now", type=str, default=None,
                        help="ISO 8601 time with offset to check instead of the current time")
    parser.add_argument("--tz", default=DEFAULT_TZ, help=f"schedule time zone (default {DEFAULT_TZ})")
    args = parser.parse_args(argv)
    try:
        if args.now is not None:
            now = _parse_now(args.now)
        elif now is None:
            now = datetime.now(timezone.utc)
        inside = is_scheduled_window(now, args.tz)
        local = now.astimezone(ZoneInfo(args.tz))
    except Exception as exc:  # noqa: BLE001 - every error must become EXIT_ERROR, not a skip
        print(f"pi_boot_guard: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    stamp = f"{local:%Y-%m-%d %H:%M} {args.tz}"
    if inside:
        print(f"pi_boot_guard: {stamp} is inside the scheduled window: running the pipeline.")
        return EXIT_INSIDE
    print(f"pi_boot_guard: {stamp} is outside the scheduled window "
          f"(last Thursday of the month, {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M}): "
          "not running the pipeline (maintenance or manual boot).")
    return EXIT_OUTSIDE


def _cli() -> int:
    try:
        return main()
    except SystemExit as exc:
        # Only "inside the window" may return 0: `--help` (argparse exits 0) means "do not run".
        if exc.code in (0, None):
            return EXIT_OUTSIDE
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    except BaseException as exc:  # noqa: BLE001 - see EXIT_ERROR
        print(f"pi_boot_guard: ERROR {type(exc).__name__}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(_cli())
