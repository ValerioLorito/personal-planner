"""
calendar_probe — read Apple Calendar in a throwaway process.

Why a separate process: an Apple Event that is waiting for a TCC decision can
block its caller for as long as the dialog stays on screen. Running the read
here, under a hard timeout, means the web server's calendar snapshot is bounded
by `subprocess.run(timeout=...)` instead of by macOS.

Usage:
    python3 -m planner.calendar_probe 2026-10-26 2026-11-01
    python3 -m planner.calendar_probe 2026-10-26 2026-11-01 --busy
    python3 -m planner.calendar_probe 2026-10-26 2026-11-01 --names "Casa,Work"

Prints one JSON object on stdout and exits 0 even when the calendar cannot be
read (the payload then carries an "error").
"""
import json
import os
import sys
from datetime import date, datetime

from planner import calendar_mac


CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "calendar_cache.json")


def write_cache(start, payload):
    """Persist a successful read so the server can serve it without waiting."""
    if payload.get("error"):
        return
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"week": start.isoformat(),
                       "fetched_at": datetime.now().isoformat(timespec="seconds"),
                       "events": payload.get("events") or [],
                       "calendars": payload.get("calendars") or []},
                      fh, ensure_ascii=False)
    except OSError:
        pass


def fetch(start, end, names=None, busy=False):
    """One read, cached on success. Never raises."""
    try:
        if busy:
            payload = calendar_mac.week_busy(start, end, calendar_names=names)
        else:
            payload = calendar_mac.week_events(start, end, calendar_names=names)
    except Exception as exc:
        payload = {"error": "%s: %s" % (type(exc).__name__, exc)}
    write_cache(start, payload)
    return payload


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out_path = None
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 < len(argv):
            out_path = argv[idx + 1]
            argv = argv[:idx] + argv[idx + 2:]

    want_busy = "--busy" in argv
    names = None
    for i, arg in enumerate(argv):
        if arg == "--names" and i + 1 < len(argv):
            names = [n.strip() for n in argv[i + 1].split(",") if n.strip()]
    plain = [a for a in argv if not a.startswith("--")]
    if len(plain) < 2:
        print(json.dumps({"error": "usage: calendar_probe START END [--busy]"}))
        return 2
    try:
        start = date.fromisoformat(plain[0])
        end = date.fromisoformat(plain[1])
    except ValueError as exc:
        print(json.dumps({"error": "bad date: %s" % exc}))
        return 2

    payload = fetch(start, end, names=names, busy=want_busy)
    payload["probe"] = True
    blob = json.dumps(payload, ensure_ascii=False)
    if out_path:
        # Writing to a file (not a pipe) matters: an Apple Event waiting on a
        # TCC decision never returns when osascript's stdout is a pipe.
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(blob)
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
