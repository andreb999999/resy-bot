"""Send a few test availability requests to Resy and print what comes back. Never books.

The Actions log is public, so this never prints the token, request headers, or account data.
"""
import datetime as dt
import os
import sys

import requests

sys.path.insert(0, os.getcwd())
from resy import API, API_KEY, UA, Resy  # noqa: E402

token = "".join(os.environ["RESY_TOKEN"].split())
venue, party = 65452, 2  # Tatiana
today = dt.date.today()
day = (today + dt.timedelta(days=(4 - today.weekday()) % 7 + 7)).isoformat()  # a Friday next week
end = (today + dt.timedelta(days=30)).isoformat()
find = {"lat": 0, "long": 0, "day": day, "party_size": party, "venue_id": venue}


def show(label, r, body=True):
    print(f"\n=== {label}\n{r.request.method} {r.url.split('?')[0]}  ->  HTTP {r.status_code}")
    print("response headers:", {k: v for k, v in r.headers.items()
                                if k.lower() in ("server", "content-type", "content-length", "via")
                                or k.lower().startswith(("x-", "cf-"))})
    if not body:
        return
    text = r.text
    try:
        d = r.json()
        if isinstance(d, dict) and "results" in d:
            vs = d["results"].get("venues", [])
            text = f"venues={len(vs)} slots per venue={[len(v.get('slots', [])) for v in vs]}"
        elif isinstance(d, dict) and "scheduled" in d:
            text = f"calendar days={len(d['scheduled'])}; first two: {d['scheduled'][:2]}"
    except ValueError:
        pass
    print("body:", text[:500] or "(empty)")


def run(label, fn, body=True):
    try:
        show(label, fn(), body)
    except requests.RequestException as e:
        print(f"\n=== {label}\nnetwork error: {e}")


bot = Resy(token).s
plain = requests.Session()
plain.headers.update({"Authorization": f'ResyAPI api_key="{API_KEY}"', "User-Agent": UA})
plain_auth = requests.Session()
plain_auth.headers.update(plain.headers)
plain_auth.headers.update({"X-Resy-Auth-Token": token, "X-Resy-Universal-Auth": token})
no_token = requests.Session()
no_token.headers.update({k: v for k, v in bot.headers.items() if not k.lower().startswith("x-resy")})

print(f"Testing venue {venue} (Tatiana), party {party}, day {day}")
run("login token still valid (/2/user, body hidden)", lambda: bot.get(API + "/2/user", timeout=15), body=False)
run("find - exactly as the bot does it", lambda: bot.get(API + "/4/find", params=find, timeout=15))
run("find - New York coordinates instead of 0,0",
    lambda: bot.get(API + "/4/find", params={**find, "lat": 40.7128, "long": -74.006}, timeout=15))
run("find - without the login token (API key only)", lambda: no_token.get(API + "/4/find", params=find, timeout=15))
run("find - minimal headers, no login token", lambda: plain.get(API + "/4/find", params=find, timeout=15))
run("find - minimal headers plus login token", lambda: plain_auth.get(API + "/4/find", params=find, timeout=15))
run("find - as POST with a JSON body", lambda: bot.post(API + "/4/find", json=find, timeout=15))
run("calendar - exactly as the bot does it",
    lambda: bot.get(API + "/4/venue/calendar", timeout=15,
                    params={"venue_id": venue, "num_seats": party, "start_date": today.isoformat(), "end_date": end}))
