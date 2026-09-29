"""Repeat the scan's requests in one session until Resy starts failing, then test what recovers.

Never books. The Actions log is public, so this never prints the token, request headers,
cookie values, or account data (cookie names only).
"""
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.getcwd())
from resy import API, Resy  # noqa: E402

token = "".join(os.environ["RESY_TOKEN"].split())
venues = {834: "4 Charles", 65452: "Tatiana", 60058: "Monkey bar"}
party = 2
today = dt.date.today()
end = (today + dt.timedelta(days=29)).isoformat()
day = (today + dt.timedelta(days=(4 - today.weekday()) % 7 + 7)).isoformat()
LIMIT = 420


def fresh():
    return Resy(token).s


def cal(s, v):
    return s.get(API + "/4/venue/calendar", timeout=15, params={
        "venue_id": v, "num_seats": party, "start_date": today.isoformat(), "end_date": end})


def find(s, v):
    return s.get(API + "/4/find", timeout=15, params={
        "lat": 0, "long": 0, "day": day, "party_size": party, "venue_id": v})


def info(r):
    return f"HTTP {r.status_code}  X-Iinfo={r.headers.get('X-Iinfo', '-')}  body={(r.text[:150] or '(empty)')!r}"


s = fresh()
r = s.get(API + "/3/user/reservations", timeout=15, params={"limit": 25, "offset": 1, "type": "upcoming"})
print(f"reservations check, like booking mode does first: HTTP {r.status_code} (body hidden)")

t0, rnd, failed = time.time(), 0, None
while time.time() - t0 < LIMIT and not failed:
    rnd += 1
    codes = []
    for v in venues:
        for req in (cal, find):
            r = req(s, v)
            codes.append(r.status_code)
            if r.status_code >= 400:
                failed = (req.__name__, v, r)
                break
            time.sleep(0.15)
        if failed:
            break
    print(f"[{time.time() - t0:5.0f}s] round {rnd}: {codes}  cookie names={sorted(s.cookies.keys())}")
    if not failed:
        time.sleep(30)

if not failed:
    print(f"\nNo errors in {LIMIT // 60} minutes with one session.")
    sys.exit(0)

name, v, r = failed
print(f"\nFirst failure after {time.time() - t0:.0f}s, round {rnd}: {name} for {venues[v]}")
print("  ", info(r))
print("\nWhat recovers? (same calendar request each time)")
print("1. same session, again:           ", info(cal(s, v)))
print("2. brand-new session:              ", info(cal(fresh(), v)))
s.cookies.clear()
print("3. same session, cookies cleared:  ", info(cal(s, v)))
time.sleep(60)
print("4. old session, cleared, 60s later:", info(cal(s, v)))
