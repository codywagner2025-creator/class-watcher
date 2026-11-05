#!/usr/bin/env python3
"""Watches UCSB Math 6B (El-Hasan) for an open seat and sends an alert.

Read-only: it asks UCSB's public class-data service for seat counts.
It never logs in to GOLD and never enrols in anything.

  watch.py              run forever, checking every minute, day and night (the Mac job)
  watch.py --once       do one check now and print the result
  watch.py --ci         one check with retries, no Mac alerts (the GitHub backup job)
  watch.py --test-alert send a test alert through every alert channel
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ---- settings -------------------------------------------------------------
QUARTER = "20271"            # Winter 2027  (year + 1=Winter 2=Spring 3=Summer 4=Fall)
SUBJECT = "MATH"
COURSE = "6B"
INSTRUCTOR = "EL-HASAN"      # matched against the lecture's instructor name
CHECK_EVERY_SECONDS = 60
START_HOUR, END_HOUR = 0, 24  # hours of the day to check in, local time (0, 24 = all day)
REPEAT_ALERT_MINUTES = 5     # while a seat stays open, remind this often
TROUBLE_AFTER_FAILURES = 15  # send a warning if this many checks in a row fail
GOLD_URL = "https://my.sa.ucsb.edu/gold/"
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
KEY_FILE = Path.home() / ".ucsb_api_key"
NTFY_FILE = HERE / "ntfy_topic.txt"   # phone alerts go to this ntfy channel
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "watch.log"
CI = "--ci" in sys.argv      # running on GitHub: settings come from secrets, phone alerts only
ON_MAC = sys.platform == "darwin" and not CI

API = ("https://api.ucsb.edu/academics/curriculums/v3/classes/search"
       f"?quarter={QUARTER}&subjectCode={SUBJECT}"
       "&pageNumber=1&pageSize=200&includeClassSections=true")


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    if CI:
        return
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch():
    key = os.environ.get("UCSB_API_KEY", "").strip() or KEY_FILE.read_text().strip()
    req = urllib.request.Request(API, headers={
        "ucsb-api-key": key,
        "ucsb-api-version": "3.0",
        "accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def find_sections(data):
    """Return (lecture, [discussion sections]) for the watched instructor.

    A lecture is numbered like 0200 and its discussion sections 0201, 0202...
    To enrol you need a seat in one of the discussion sections.
    """
    for c in data.get("classes", []):
        parts = (c.get("courseId") or "").split()
        if parts[:2] != [SUBJECT, COURSE]:
            continue
        secs = c.get("classSections", [])
        for lec in secs:
            names = " ".join((i.get("instructor") or "") for i in lec.get("instructors") or [])
            if INSTRUCTOR in names.upper() and (lec.get("section") or "").endswith("00"):
                prefix = lec["section"][:2]
                subs = [s for s in secs
                        if (s.get("section") or "").startswith(prefix) and s is not lec]
                return lec, subs
    return None, []


def seats(sec):
    """Open seats in a section (0 if full, closed or cancelled)."""
    if sec.get("classClosed") == "Y" or sec.get("courseCancelled"):
        return 0
    cap = sec.get("maxEnroll") or 0
    return max(0, cap - (sec.get("enrolledTotal") or 0))


def describe(sec):
    tl = (sec.get("timeLocations") or [{}])[0]
    days = "".join((tl.get("days") or "").split())
    when = f"{days} {tl.get('beginTime', '?')}-{tl.get('endTime', '?')}"
    return f"section {sec.get('section')} ({when}), enroll code {sec.get('enrollCode')}"


def notify(title, body, urgent=True):
    """Mac notification + sound, and a phone push if an ntfy channel is set."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        if ON_MAC:
            subprocess.run(["/usr/bin/osascript", "-e",
                            f'display notification "{esc(body)}" with title "{esc(title)}" sound name "Glass"'],
                           timeout=15)
            if urgent:
                for _ in range(3):
                    subprocess.run(["/usr/bin/afplay", "/System/Library/Sounds/Glass.aiff"], timeout=15)
    except Exception as e:  # noqa: BLE001 - an alert channel failing must not stop the watcher
        log(f"mac alert failed: {e}")
    try:
        topic = os.environ.get("NTFY_TOPIC", "").strip() or (
            NTFY_FILE.read_text().strip() if NTFY_FILE.exists() else "")
        if topic:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}", data=body.encode(), method="POST",
                headers={"Title": title, "Priority": "urgent" if urgent else "default",
                         "Tags": "rotating_light" if urgent else "warning", "Click": GOLD_URL})
            urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001
        log(f"phone alert failed: {e}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except OSError:
        pass


def check(state):
    """One check. Returns the list of open discussion sections."""
    data = fetch()
    lec, subs = find_sections(data)
    if lec is None:
        raise RuntimeError(f"could not find {SUBJECT} {COURSE} with {INSTRUCTOR} in {QUARTER}")

    counts = ", ".join(f"{s.get('section')} {s.get('enrolledTotal')}/{s.get('maxEnroll')}"
                       for s in [lec] + subs)
    # With discussion sections, a seat must be open in one of them AND in the lecture.
    lec_open = seats(lec)
    open_secs = [s for s in subs if seats(s) > 0] if subs else ([lec] if lec_open else [])
    if subs and not lec_open:
        open_secs = []
    # Also log the other lectures of this course. They are still filling, so their
    # numbers show how often UCSB's data really refreshes.
    others = ", ".join(
        f"{s.get('section')} {s.get('enrolledTotal')}/{s.get('maxEnroll')}"
        for c in data.get("classes", []) if (c.get("courseId") or "").split()[:2] == [SUBJECT, COURSE]
        for s in c.get("classSections", [])
        if (s.get("section") or "").endswith("00") and s is not lec)
    log(f"{'OPEN' if open_secs else 'full'}  {counts}" + (f"  | other lectures: {others}" if others else ""))

    now = time.time()
    if open_secs:
        was_open = state.get("open", False)
        due = now - state.get("last_alert", 0) >= REPEAT_ALERT_MINUTES * 60
        if not was_open or due:
            body = "; ".join(f"{seats(s)} open in {describe(s)}" for s in open_secs)
            notify(f"{SUBJECT} {COURSE} {INSTRUCTOR}: SEAT OPEN - switch in GOLD now (don't drop first)", body)
            state["last_alert"] = now
            if not was_open and ON_MAC:
                subprocess.run(["/usr/bin/open", GOLD_URL])  # bring up the GOLD login page
        state["open"] = True
    else:
        state["open"] = False
    return open_secs


def main():
    if "--test-alert" in sys.argv:
        notify("Class watcher test", "If you can see this, alerts are working.")
        return
    if CI:
        # Stateless: every run that finds a seat alerts, so reminders come each run.
        for attempt in (1, 2, 3):
            try:
                check({})
                return
            except Exception as e:  # noqa: BLE001
                detail = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)
                log(f"check failed (attempt {attempt} of 3): {detail}")
                time.sleep(10)
        sys.exit(1)  # a failed run makes GitHub email the repo owner
    state = load_state()
    if "--once" in sys.argv:
        check(state)
        return

    log(f"watcher started: {SUBJECT} {COURSE} {INSTRUCTOR}, quarter {QUARTER}, "
        f"every {CHECK_EVERY_SECONDS}s, hours {START_HOUR}-{END_HOUR}")
    failures = 0
    while True:
        started = time.time()
        if START_HOUR <= datetime.now().hour < END_HOUR:
            try:
                check(state)
                if failures >= TROUBLE_AFTER_FAILURES:
                    notify("Class watcher is working again", "Checks are succeeding.", urgent=False)
                failures = 0
            except Exception as e:  # noqa: BLE001 - keep running through network blips
                failures += 1
                detail = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)
                log(f"check failed ({failures} in a row): {detail}")
                if failures == TROUBLE_AFTER_FAILURES:
                    notify("Class watcher is having trouble",
                           f"{failures} checks in a row failed ({detail}). It will keep trying.",
                           urgent=False)
            save_state(state)
        time.sleep(max(1, CHECK_EVERY_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
