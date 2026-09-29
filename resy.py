#!/usr/bin/env python3
"""
resy.py - a small command-line client for Resy's (unofficial) web API.

Commands
  login          save your auth token (pasted from resy.com), or --email for password login
  whoami         check the saved token still works
  search         find a restaurant by name  -> venue id
  venue          look up a venue by its resy.com URL or slug -> venue id
  find           list open slots for a venue / day / party size
  book           book one slot (asks before booking unless --yes)
  reservations   list your upcoming reservations
  cancel         cancel a reservation by its resy_token
  snipe          wait for the release time, poll hard, book the best slot
  scan           check a range of dates against your weekly hours (optionally
                 book the first fit, or keep re-checking with --every)
  watch          every 30s, check all your restaurants and log openings to
                 resy_openings.csv - never books

Restaurants: list them in the config as [[restaurants]] (id + name). find / scan /
snipe work on all of them when you don't name one; config order = priority.

No double booking: automatic bookings (scan --book, snipe) claim a lock file,
resy_booked.json, *before* booking. Once one booking succeeds everything stops,
and later runs refuse to book until you delete that file or cancel the reservation
with `python resy.py cancel <resy_token>`. They also refuse to start if you
already have an upcoming reservation at one of your config restaurants.

Rules for every booking (config): the table must start at least min_hours_ahead
(default 24) hours from now, and its prepayment / cancellation fee must be within
max_prepay / max_cancel_fee. Tables that break a rule are skipped, not booked.

Weekly hours live in resy_config.toml next to this script (or --config PATH):
which hours work on each day of the week, preferred time, party size, seating.
find / snipe / scan all use it; --window / --prefer / --party on the command
line override it for that run.

Examples
  python resy.py login
  python resy.py search "via carota"
  python resy.py find 12345 --day 2026-10-02
  python resy.py book 12345 --day 2026-10-02 --time 19:00
  python resy.py reservations
  python resy.py cancel "<resy_token from reservations>"
  python resy.py scan --days 14                   # all my restaurants, next 2 weeks, my hours
  python resy.py scan --days 30 --book --every 60  # watch for cancellations, book ONE, then stop
  python resy.py snipe Lilia --day 2026-10-24 --at 10:00:00
  python resy.py watch                             # log openings every 30s, no booking
  python resy.py find Lilia --day 2026-10-24 --fees  # show prepay / cancel fee per table

Only dependency: requests  (pip install requests)
The auth token is saved to ~/.resy_auth.json (your password is never saved).
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import random
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests

API = "https://api.resy.com"
# Public key embedded in resy.com's own web app (same for every user).
API_KEY = os.environ.get("RESY_API_KEY", "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5")
AUTH_FILE = Path(os.environ.get("RESY_AUTH_FILE", Path.home() / ".resy_auth.json"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
NYC = (40.7128, -74.0060)
JITTER = 20            # seconds added/removed at random to each --every / watch wait
COOLDOWN_EVERY = 15    # every this many checks, take a longer breather instead...
COOLDOWN_SECONDS = 300 # ...of this many seconds (5 full minutes)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_CONFIG = Path(__file__).resolve().with_name("resy_config.toml")


class ResyError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


# --------------------------------------------------------------------------- client
class Resy:
    def __init__(self, token: str | None = None, payment_method_id: int | None = None):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f'ResyAPI api_key="{API_KEY}"',
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "X-Origin": "https://resy.com",
            "Cache-Control": "no-cache",
        })
        self.token = None
        self.payment_method_id = payment_method_id
        if token:
            self.set_token(token)

    # -- plumbing
    def set_token(self, token: str):
        self.token = token
        self.s.headers["X-Resy-Auth-Token"] = token
        self.s.headers["X-Resy-Universal-Auth"] = token

    def _req(self, method, path, timeout=10, **kw):
        r = self.s.request(method, API + path, timeout=timeout, **kw)
        if r.status_code >= 400:
            raise ResyError(r.status_code, r.text[:400])
        return r.json() if r.content else {}

    # -- auth
    def login(self, email: str, password: str) -> dict:
        data = self._req("POST", "/3/auth/password",
                         data={"email": email, "password": password})
        self.set_token(data["token"])
        self.payment_method_id = _pick_payment_method(data)
        return data

    def user(self) -> dict:
        data = self._req("GET", "/2/user")
        pm = _pick_payment_method(data)
        if pm:
            self.payment_method_id = pm
        return data

    # -- venues
    def search(self, query: str, lat=NYC[0], lng=NYC[1], limit=8) -> list[dict]:
        body = {"query": query, "geo": {"latitude": lat, "longitude": lng},
                "types": ["venue"], "per_page": limit, "page": 1}
        data = self._req("POST", "/3/venuesearch/search", json=body)
        out = []
        for h in data.get("search", {}).get("hits", []):
            vid = h.get("id", {})
            out.append({
                "id": vid.get("resy") if isinstance(vid, dict) else vid,
                "name": h.get("name"),
                "neighborhood": h.get("neighborhood"),
                "city": (h.get("location") or {}).get("name"),
                "slug": h.get("url_slug"),
            })
        return out

    def venue(self, slug_or_url: str, location: str = "ny") -> dict:
        m = re.search(r"cities/([^/]+)/(?:venues/)?([^/?#]+)", slug_or_url)
        slug, locs = (m.group(2), [m.group(1), location]) if m else (slug_or_url, [location])
        last = None
        for loc in dict.fromkeys(locs + ["ny", "new-york-ny"]):
            try:
                data = self._req("GET", "/3/venue", params={"url_slug": slug, "location": loc})
                return {"id": data["id"]["resy"], "name": data.get("name"), "slug": slug}
            except ResyError as e:
                last = e
        raise last

    # -- availability / booking
    def find(self, venue_id: int, day: str, party: int, timeout=10) -> list[dict]:
        params = {"lat": 0, "long": 0, "day": day, "party_size": party, "venue_id": venue_id}
        data = self._req("GET", "/4/find", params=params, timeout=timeout)
        venues = data.get("results", {}).get("venues", [])
        slots = []
        for v in venues:
            for sl in v.get("slots", []):
                cfg, date = sl.get("config", {}), sl.get("date", {})
                slots.append({
                    "start": date.get("start"),            # "2026-10-02 19:00:00"
                    "time": (date.get("start") or "")[11:16],
                    "type": cfg.get("type"),                # e.g. "Dining Room", "Bar"
                    "token": cfg.get("token"),              # config_id used to book
                })
        slots.sort(key=lambda x: x["start"] or "")
        return slots

    def calendar(self, venue_id: int, party: int, start: str, end: str) -> set[str] | None:
        """Dates with open inventory between start and end (YYYY-MM-DD), or None if unknown."""
        try:
            data = self._req("GET", "/4/venue/calendar",
                             params={"venue_id": venue_id, "num_seats": party,
                                     "start_date": start, "end_date": end})
        except Exception:
            return None
        return {d["date"] for d in data.get("scheduled", [])
                if (d.get("inventory") or {}).get("reservation") == "available"}

    def details(self, config_token: str, day: str, party: int) -> dict:
        """The step the website runs when you click a time: returns the book_token plus the
        payment / cancellation terms for that table."""
        try:
            return self._req("GET", "/3/details",
                             params={"config_id": config_token, "day": day, "party_size": party})
        except ResyError:
            return self._req("POST", "/3/details",
                             json={"commit": 1, "config_id": config_token,
                                   "day": day, "party_size": party})

    def book(self, book_token: str) -> dict:
        form = {"book_token": book_token, "source_id": "resy.com-venue-details"}
        if self.payment_method_id:
            form["struct_payment_method"] = json.dumps({"id": self.payment_method_id})
        return self._req("POST", "/3/book", data=form)

    def book_slot(self, slot: dict, day: str, party: int) -> dict:
        return self.book(self.details(slot["token"], day, party)["book_token"]["value"])

    def reservations(self, kind="upcoming") -> list[dict]:
        data = self._req("GET", "/3/user/reservations",
                         params={"limit": 25, "offset": 1, "type": kind})
        venues = data.get("venues", {})
        out = []
        for r in data.get("reservations", []):
            vid = str((r.get("venue") or {}).get("id", ""))
            out.append({
                "venue": (venues.get(vid) or {}).get("name") or vid,
                "venue_id": int(vid) if vid.isdigit() else None,
                "day": r.get("day"),
                "time": (r.get("time_slot") or "")[:5],
                "party": r.get("num_seats"),
                "resy_token": r.get("resy_token"),
            })
        return out

    def cancel(self, resy_token: str) -> dict:
        return self._req("POST", "/3/cancel", data={"resy_token": resy_token})


def _pick_payment_method(data: dict):
    pms = data.get("payment_methods") or []
    for pm in pms:
        if pm.get("is_default"):
            return pm.get("id")
    if pms:
        return pms[0].get("id")
    return data.get("payment_method_id")


# --------------------------------------------------------------------------- helpers
def load_client() -> Resy:
    if not AUTH_FILE.exists():
        sys.exit("Not logged in. Run:  python resy.py login")
    saved = json.loads(AUTH_FILE.read_text())
    return Resy(saved["token"], saved.get("payment_method_id"))


def save_auth(client: Resy, email: str | None):
    AUTH_FILE.write_text(json.dumps({"token": client.token,
                                     "payment_method_id": client.payment_method_id,
                                     "email": email,
                                     "saved_at": dt.datetime.now().isoformat(timespec="seconds")},
                                    indent=2))
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


def resolve_venue(client: Resy, v: str) -> int:
    if v.isdigit():
        return int(v)
    return int(client.venue(v)["id"])


def targets(client: Resy, cfg: dict, venue_arg: str | None) -> list[dict]:
    """Restaurants to work on: the one on the command line (id, slug, URL, or a name
    from the config), else every restaurant in the config, in priority order."""
    rs = cfg.get("restaurants", [])
    if venue_arg:
        for r in rs:
            if venue_arg == str(r["id"]) or venue_arg.lower() == r["name"].lower():
                return [r]
        vid = resolve_venue(client, venue_arg)
        return [{"id": vid, "name": str(venue_arg) if not venue_arg.isdigit() else f"venue {vid}"}]
    if not rs:
        sys.exit("No restaurant given and none in the config. Pass a venue id, or add "
                 "[[restaurants]] entries to resy_config.toml.")
    return rs


# --------------------------------------------------------------------------- booking lock
# One booking per run of the watcher. The lock file is claimed atomically *before* the
# booking request, so even two scripts running at once can't both book. It stays after a
# successful booking, so later runs won't book again until you delete it (or cancel that
# reservation with `python resy.py cancel`, which removes it).
LOCK_FILE = Path(os.environ.get("RESY_LOCK_FILE", Path(__file__).resolve().with_name("resy_booked.json")))
# reservation_id / resy_token for the current booking, kept out of LOCK_FILE: the
# GitHub Actions workflow commits LOCK_FILE back to the (public) repo, and a specific
# reservation's identifiers don't belong in public git history.
PRIVATE_LOCK_FILE = Path(__file__).resolve().with_name("resy_booked_private.json")


class AlreadyBooked(Exception):
    pass


class SkipSlot(Exception):
    """This table breaks one of your rules (prepay / cancellation fee); try another."""


# --------------------------------------------------------------------------- money rules
LAST_DETAILS_FILE = Path(__file__).resolve().with_name("resy_last_details.json")


def _num(x) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.replace("$", "").replace(",", ""))
        except ValueError:
            return None
    if isinstance(x, dict):                      # e.g. {"amount": 25.0, ...}
        return _num(x.get("amount"))
    return None


def _walk(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, path + (str(k).lower(),))
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, path)
    else:
        yield path, obj


def payment_terms(d: dict) -> dict:
    """What a table costs, read from the /3/details response.
    Deliberately cautious: if the response says a deposit / prepayment exists but we can't
    find the amount, `known` is False and the rules treat it as over the limit."""
    pay = d.get("payment") or {}
    canc = d.get("cancellation") or {}
    amounts = pay.get("amounts") or {}

    prepay = 0.0
    for k in ("total", "reservation_charge", "subtotal", "deposit", "prepay", "prepayment"):
        v = _num(amounts.get(k))
        if v:
            prepay = max(prepay, v)
    for path, v in _walk(pay):                   # any deposit/prepay amount elsewhere
        if any(w in p for p in path for w in ("deposit", "prepa")):
            n = _num(v)
            if n and not any("percent" in p or "rate" in p for p in path):
                prepay = max(prepay, n)
    ptype = str((pay.get("config") or {}).get("type") or "").lower() or None

    fee = _num((canc.get("fee") or {}).get("amount")) if isinstance(canc.get("fee"), dict) \
        else _num(canc.get("fee"))
    if fee is None:
        for path, v in _walk(canc.get("fee") or {}):
            n = _num(v)
            if n and path and path[-1] in ("amount", "value", "fee"):
                fee = max(fee or 0, n)
    fee_applies = (canc.get("fee") or {}).get("applies", True) if isinstance(canc.get("fee"), dict) else True

    policy = []
    for path, v in _walk(canc.get("display") or {}):
        if isinstance(v, str) and len(v) > 15:
            policy.append(v.strip())

    known = bool(pay or canc)
    if ptype and ptype not in ("free", "none", "no_charge") and prepay == 0 and \
            any(w in ptype for w in ("deposit", "prepa", "ticket", "charge")):
        known = False                            # says it charges, but no amount found
    return {
        "prepay": round(prepay, 2),
        "cancel_fee": round(fee, 2) if (fee and fee_applies) else 0.0,
        "payment_type": ptype,
        "policy": " | ".join(dict.fromkeys(policy))[:300],
        "known": known,
    }


def terms_text(t: dict) -> str:
    if not t.get("known"):
        return "payment terms unclear"
    bits = [f"prepay ${t['prepay']:.0f}" if t["prepay"] else "no prepay",
            f"cancel fee ${t['cancel_fee']:.0f}" if t["cancel_fee"] else "no cancel fee"]
    return ", ".join(bits)


def terms_problem(t: dict, cfg: dict) -> str | None:
    """Why this table breaks your money rules, or None if it's fine."""
    mp, mc = cfg.get("max_prepay"), cfg.get("max_cancel_fee")
    if mp is None and mc is None:
        return None
    if not t.get("known"):
        return f"couldn't read the payment terms (saved to {LAST_DETAILS_FILE.name})"
    if mp is not None and t["prepay"] > mp:
        return f"prepay ${t['prepay']:.0f} > max_prepay ${mp}"
    if mc is not None and t["cancel_fee"] > mc:
        return f"cancellation fee ${t['cancel_fee']:.0f} > max_cancel_fee ${mc}"
    return None


# --------------------------------------------------------------------------- time rule
def min_start(cfg: dict) -> dt.datetime:
    return dt.datetime.now() + dt.timedelta(hours=float(cfg.get("min_hours_ahead", 24)))


def far_enough(slots: list[dict], cfg: dict) -> list[dict]:
    """Drop tables that start less than min_hours_ahead from now (venue local time = NY)."""
    cutoff = min_start(cfg)
    out = []
    for s in slots:
        try:
            if dt.datetime.fromisoformat(s["start"]) >= cutoff:
                out.append(s)
        except (TypeError, ValueError):
            pass
    return out


def lock_info() -> dict | None:
    if not LOCK_FILE.exists():
        return None
    try:
        return json.loads(LOCK_FILE.read_text() or "{}")
    except Exception:
        return {"status": "unreadable"}


def lock_msg(info: dict) -> str:
    if info.get("status") == "booked":
        return (f"Already booked {info.get('venue')} on {info.get('day')} at {info.get('time')} "
                f"(lock file {LOCK_FILE.name}). Not booking anything else.\n"
                f"To allow a new booking: cancel it with `python resy.py cancel <resy_token>`, "
                f"or delete {LOCK_FILE}.")
    return (f"{LOCK_FILE.name} exists (status: {info.get('status')}) - another run may be "
            f"booking right now. If not, delete {LOCK_FILE}.")


def safe_book(c: Resy, venue: dict, slot: dict, day: str, party: int, cfg: dict,
              confirm=None) -> tuple[dict, dict]:
    """Check the 24h + money rules, claim the lock, book, record the result.
    Raises SkipSlot if the table breaks a rule, AlreadyBooked if the lock is held."""
    if not far_enough([slot], cfg):
        raise SkipSlot(f"less than {cfg.get('min_hours_ahead', 24)}h away")
    if lock_info():
        raise AlreadyBooked(lock_msg(lock_info()))
    det = c.details(slot["token"], day, party)
    terms = payment_terms(det)
    problem = terms_problem(terms, cfg)
    if problem:
        _save_details(det)
        raise SkipSlot(problem)
    if confirm and not confirm(terms):
        raise SkipSlot("you said no")
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AlreadyBooked(lock_msg(lock_info() or {}))
    with os.fdopen(fd, "w") as f:
        json.dump({"status": "booking", "pid": os.getpid(), "venue": venue["name"],
                   "day": day, "time": slot["time"]}, f)
    try:
        res = c.book(det["book_token"]["value"])
    except BaseException:
        LOCK_FILE.unlink(missing_ok=True)      # booking failed -> release the claim
        raise
    LOCK_FILE.write_text(json.dumps({
        "status": "booked", "venue": venue["name"], "venue_id": venue["id"], "day": day,
        "time": slot["time"], "type": slot["type"], "party": party,
        "prepay": terms["prepay"], "cancel_fee": terms["cancel_fee"], "policy": terms["policy"],
        "booked_at": dt.datetime.now().isoformat(timespec="seconds")}, indent=2))
    PRIVATE_LOCK_FILE.write_text(json.dumps({
        "reservation_id": res.get("reservation_id"), "resy_token": res.get("resy_token")}, indent=2))
    try:
        os.chmod(PRIVATE_LOCK_FILE, 0o600)
    except OSError:
        pass
    _save_details(det)
    return res, terms


def _save_details(det: dict):
    try:
        LAST_DETAILS_FILE.write_text(json.dumps(det, indent=2)[:200_000])
    except Exception:
        pass


def preflight(c: Resy, venues: list[dict], cfg: dict):
    """Before an automatic booking run: stop if we already hold a booking."""
    info = lock_info()
    if info:
        sys.exit(lock_msg(info))
    if cfg.get("stop_if_already_reserved", True):
        ids = {v["id"] for v in venues}
        try:
            held = [r for r in c.reservations() if r.get("venue_id") in ids]
        except Exception as e:
            print(f"(couldn't check your existing reservations: {e})")
            held = []
        if held:
            r = held[0]
            sys.exit(f"You already have an upcoming reservation at {r['venue']} on {r['day']} "
                     f"{r['time']} - not booking another. (Set stop_if_already_reserved = false "
                     f"in the config to allow it.)")


def mins(hhmm: str) -> int:
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def parse_window(w: str) -> tuple[int, int]:
    try:
        a, b = w.replace(" ", "").split("-")
        lo, hi = mins(a), mins(b)
    except Exception:
        raise ValueError(f"bad time window '{w}' - use HH:MM-HH:MM, e.g. 19:00-21:30")
    if hi < lo:
        raise ValueError(f"window '{w}' ends before it starts (past-midnight windows aren't supported)")
    return lo, hi


# --------------------------------------------------------------------------- config
def load_config(path: str | None) -> dict:
    """Load resy_config.toml. Missing default file = no config (any time works)."""
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        if path:
            sys.exit(f"Config file not found: {p}")
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            sys.exit("Reading the config needs Python 3.11+, or: pip install tomli")
    try:
        with open(p, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        sys.exit(f"Couldn't read {p}: {e}")

    def bad(msg):
        sys.exit(f"{p.name}: {msg}")

    hours = cfg.get("hours")
    if hours is not None:
        if not isinstance(hours, dict):
            bad("[hours] must be a table of day = [\"HH:MM-HH:MM\", ...]")
        for day, wins in list(hours.items()):
            if day not in DAYS:
                bad(f"[hours] unknown day '{day}' - use {', '.join(DAYS)}")
            if isinstance(wins, str):
                wins = hours[day] = [wins]
            for w in wins:
                try:
                    parse_window(w)
                except ValueError as e:
                    bad(f"[hours] {day}: {e}")
    prefer = cfg.get("prefer")
    for day, t in (prefer.items() if isinstance(prefer, dict) else [("all", prefer)]):
        if t is None:
            continue
        if day != "all" and day not in DAYS:
            bad(f"[prefer] unknown day '{day}'")
        try:
            mins(t)
        except Exception:
            bad(f"prefer time '{t}' should look like 19:30")
    for k in ("max_prepay", "max_cancel_fee", "min_hours_ahead", "watch_every", "watch_days"):
        if k in cfg and (isinstance(cfg[k], bool) or not isinstance(cfg[k], (int, float))
                         or cfg[k] < 0):
            bad(f"{k} should be a number >= 0 (got {cfg[k]!r})")
    rs = cfg.get("restaurants", [])
    if not isinstance(rs, list):
        bad("restaurants must be [[restaurants]] entries with id = ... and name = ...")
    for i, r in enumerate(rs, 1):
        if not isinstance(r, dict) or "id" not in r:
            bad(f"restaurant #{i} needs an id, e.g.  id = 12345")
        try:
            r["id"] = int(r["id"])
        except (TypeError, ValueError):
            bad(f"restaurant #{i}: id should be a number (use `python resy.py search` to find it)")
        r.setdefault("name", f"venue {r['id']}")
    cfg["_path"] = str(p)
    return cfg


def rules_for(cfg: dict, day: str, a) -> tuple[list | None, str | None, list | None]:
    """(windows, prefer, types) for a date. Command-line flags beat the config.
    windows: None = any time, [] = this day doesn't work."""
    wd = DAYS[dt.date.fromisoformat(day).weekday()]
    if getattr(a, "window", None):
        windows = [a.window]
    elif "hours" in cfg:
        windows = cfg["hours"].get(wd, [])      # a day left out of [hours] doesn't work
    else:
        windows = None
    prefer = getattr(a, "prefer", None)
    if not prefer:
        p = cfg.get("prefer")
        prefer = p.get(wd) if isinstance(p, dict) else p
    types = getattr(a, "type", None) or cfg.get("types") or None
    if isinstance(types, str):
        types = [types]
    return windows, prefer, types


def party_for(cfg: dict, a, venue: dict | None = None) -> int:
    """Command line > per-restaurant party in the config > global party > 2."""
    return a.party or int((venue or {}).get("party") or cfg.get("party", 2))


def describe(windows, prefer):
    w = "any time" if windows is None else (", ".join(windows) if windows else "doesn't work")
    return w + (f", prefer {prefer}" if prefer and windows != [] else "")


def rank_slots(slots, windows=None, prefer=None, types=None):
    """Keep slots inside any of the windows (None = any time) and of an accepted seating
    type, then order by closeness to the preferred time."""
    if isinstance(windows, str):
        windows = [windows]
    ranges = [parse_window(w) for w in windows] if windows is not None else [(0, 24 * 60)]
    types_l = [t.lower() for t in types] if types else None
    keep = [s for s in slots
            if s["time"] and any(lo <= mins(s["time"]) <= hi for lo, hi in ranges)
            and (not types_l or any(t in (s["type"] or "").lower() for t in types_l))]
    if prefer:
        p = mins(prefer)
        keep.sort(key=lambda s: (abs(mins(s["time"]) - p), s["time"]))
    return keep


def print_slots(slots):
    if not slots:
        print("  (no open slots)")
    for i, s in enumerate(slots):
        print(f"  [{i:2}] {s['time']}  {s['type']}")


def ts():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def send_alert_email(subject: str, body: str) -> None:
    """Best-effort email alert (e.g. for an expired token). Configured via env vars
    ALERT_SMTP_USER / ALERT_SMTP_PASSWORD / ALERT_EMAIL_TO; does nothing if unset."""
    user = os.environ.get("ALERT_SMTP_USER")
    password = os.environ.get("ALERT_SMTP_PASSWORD")
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    if not (user and password and to_addr):
        print(f"[{ts()}] (email alert skipped - ALERT_SMTP_USER/ALERT_SMTP_PASSWORD/"
              f"ALERT_EMAIL_TO not set)")
        return
    host = os.environ.get("ALERT_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("ALERT_SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            s.login(user, password)
            s.send_message(msg)
        print(f"[{ts()}] alert email sent to {to_addr}")
    except Exception as e:
        print(f"[{ts()}] couldn't send alert email: {e}")


# --------------------------------------------------------------------------- commands
def cmd_login(a):
    c = Resy()
    if not a.token and not a.email:
        a.token = input("Paste your Resy auth token (starts with eyJ): ").strip().strip('"\'')
    if a.token:
        c.set_token(a.token)
        u = c.user()
        email = u.get("em_address") or u.get("email")
    else:
        email = a.email or input("Resy email: ").strip()
        password = getpass.getpass("Resy password (hidden): ")
        try:
            c.login(email, password)
        except ResyError as e:
            sys.exit(f"Login failed: {e}\nIf this keeps failing, use the browser-token method:"
                     "  python resy.py login --token <token>")
        c.user()
    save_auth(c, email)
    print(f"Logged in as {email}. Token saved to {AUTH_FILE}")
    print(f"Payment method on file: {c.payment_method_id or 'none found'}")


def cmd_whoami(a):
    c = load_client()
    u = c.user()
    print(f"OK - {u.get('first_name', '')} {u.get('last_name', '')} "
          f"<{u.get('em_address') or u.get('email')}>, payment method {c.payment_method_id}")


def cmd_search(a):
    c = load_client()
    hits = c.search(a.query)
    if not hits:
        print("No matches.")
    for h in hits:
        print(f"  {h['id']:>7}  {h['name']}  ({h.get('neighborhood') or h.get('city') or ''})")


def cmd_venue(a):
    c = load_client()
    v = c.venue(a.slug_or_url, a.location)
    print(f"{v['id']}  {v['name']}")


def cmd_find(a):
    c = load_client()
    wd = DAYS[dt.date.fromisoformat(a.day).weekday()]
    if a.all:
        windows, prefer, types = None, None, None
    else:
        windows, prefer, types = rules_for(a.cfg, a.day, a)
    print(f"{a.day} ({wd}) - your hours: {describe(windows, prefer)}"
          + ("" if a.all else f"; only tables {a.cfg.get('min_hours_ahead', 24)}h+ from now"))
    for v in targets(c, a.cfg, a.venue):
        party = party_for(a.cfg, a, v)
        all_slots = c.find(v["id"], a.day, party)
        slots = rank_slots(all_slots, windows, prefer, types)
        if not a.all:
            slots = far_enough(slots, a.cfg)
        print(f"\n{v['name']} (id {v['id']}), party of {party}:")
        if a.fees:
            if not slots:
                print("  (no open slots)")
            for i, s in enumerate(slots):
                try:
                    det = c.details(s["token"], a.day, party)
                    t = payment_terms(det)
                    _save_details(det)
                    prob = terms_problem(t, a.cfg)
                    extra = terms_text(t) + (f"  <- SKIP: {prob}" if prob else "")
                except Exception as e:
                    extra = f"(couldn't get terms: {e})"
                print(f"  [{i:2}] {s['time']}  {s['type']}  -  {extra}")
        else:
            print_slots(slots)
        if not a.all and all_slots:
            print(f"  ({len(all_slots)} open slots in total; --all shows every one"
                  + ("" if a.fees else ", --fees shows prepay / cancellation fees") + ")")


def cmd_book(a):
    """Manual booking of one table. Shows the terms and asks first (it doesn't use the lock
    file, so test bookings don't block the automatic runs)."""
    c = load_client()
    v = targets(c, a.cfg, a.venue)[0]
    party = party_for(a.cfg, a, v)
    if not c.payment_method_id:
        c.user()
    slots = c.find(v["id"], a.day, party)
    cands = [s for s in slots if s["time"] == a.time and
             (not a.type or a.type.lower() in (s["type"] or "").lower())]
    if not cands:
        print(f"No {a.time} slot. Open slots:")
        print_slots(slots)
        sys.exit(1)
    s = cands[0]
    if not far_enough([s], a.cfg):
        sys.exit(f"{a.day} {s['time']} is less than {a.cfg.get('min_hours_ahead', 24)}h away "
                 f"(min_hours_ahead in the config). Not booking.")
    det = c.details(s["token"], a.day, party)
    _save_details(det)
    t = payment_terms(det)
    prob = terms_problem(t, a.cfg)
    print(f"{v['name']} {a.day} {s['time']} ({s['type']}), party of {party}: {terms_text(t)}")
    if t["policy"]:
        print(f"  policy: {t['policy']}")
    if prob:
        print(f"  WARNING: {prob}")
    if a.yes and prob:
        sys.exit("Not booked (breaks your config rules; run without --yes to decide yourself).")
    if not a.yes:
        ok = input("Book it? [y/N] ")
        if ok.strip().lower() != "y":
            sys.exit("Not booked.")
    res = c.book(det["book_token"]["value"])
    print("BOOKED:", json.dumps({k: res.get(k) for k in ("reservation_id", "resy_token")}, indent=2))


def cmd_reservations(a):
    c = load_client()
    rs = c.reservations()
    if not rs:
        print("No upcoming reservations.")
    for r in rs:
        print(f"  {r['day']} {r['time']}  {r['venue']}  (party {r['party']})\n"
              f"      resy_token: {r['resy_token']}")


def cmd_cancel(a):
    c = load_client()
    print(json.dumps(c.cancel(a.resy_token), indent=2)[:500])
    private = {}
    if PRIVATE_LOCK_FILE.exists():
        try:
            private = json.loads(PRIVATE_LOCK_FILE.read_text() or "{}")
        except Exception:
            pass
    if lock_info() and private.get("resy_token") == a.resy_token:
        LOCK_FILE.unlink(missing_ok=True)
        PRIVATE_LOCK_FILE.unlink(missing_ok=True)
        print(f"Removed {LOCK_FILE.name} - the watcher is free to book again.")


def _print_matches(matches, terms=None):
    cur = None
    for m in matches:
        v, d, s, party = m
        head = (v["id"], d)
        if head != cur:
            cur = head
            wd = DAYS[dt.date.fromisoformat(d).weekday()]
            print(f"  {v['name']}  -  {d} ({wd}), party of {party}")
        extra = f"  -  {terms_text(terms[_key(m)])}" if terms and _key(m) in terms else ""
        print(f"      {s['time']}  {s['type']}{extra}")


def _key(m):
    v, d, s, _ = m
    return (v["id"], d, s["time"], s["type"])


OPENINGS_FIELDS = ["event", "logged_at", "restaurant", "venue_id", "date", "weekday", "time",
                   "seating", "party", "hours_away", "prepay", "cancel_fee", "payment_type",
                   "within_your_rules", "why_not", "policy", "open_minutes"]


def _log_rows(path: Path, rows: list[dict]):
    import csv
    new_file = not path.exists()
    for attempt in range(3):                     # Excel may have the file open
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=OPENINGS_FIELDS)
                if new_file:
                    w.writeheader()
                w.writerows(rows)
            return
        except PermissionError:
            time.sleep(1)
    print(f"[{ts()}] couldn't write {path.name} (open in Excel?) - {len(rows)} row(s) lost")


def _confirm_prompt(v, d, s, party):
    def ask(terms):
        ans = input(f"Book {v['name']} {d} {s['time']} ({s['type']}) for {party} - "
                    f"{terms_text(terms)}? [y = book / n = next option / q = quit] ")
        ans = ans.strip().lower()
        if ans == "q":
            sys.exit("Not booked.")
        return ans == "y"
    return ask


def cmd_scan(a):
    """Check a range of dates at one or all config restaurants.
    --book: book one table (then stop).  --every N: repeat.  --log FILE: record openings."""
    c = load_client()
    venues = targets(c, a.cfg, a.venue)
    if a.book:
        if not c.payment_method_id:
            c.user()
        preflight(c, venues, a.cfg)
    start = dt.date.fromisoformat(a.start) if a.start else dt.date.today()
    end = dt.date.fromisoformat(a.end) if a.end else start + dt.timedelta(days=a.days - 1)
    dates = []
    for i in range((end - start).days + 1):
        d = (start + dt.timedelta(days=i)).isoformat()
        rules = rules_for(a.cfg, d, a)
        if rules[0] != []:          # skip days your hours say don't work
            dates.append((d, rules))
    if not dates:
        sys.exit("None of those dates fall on a day your config says works.")
    order = a.cfg.get("order", "restaurant")
    log = Path(a.log) if a.log else None
    if log and not log.is_absolute():
        log = Path(__file__).resolve().with_name(log.name) if log.parent == Path(".") else log
    print(f"{start} to {end}: {len(dates)} day(s) fit your hours; "
          f"{a.cfg.get('min_hours_ahead', 24)}h+ ahead only; "
          f"restaurants: {', '.join(v['name'] for v in venues)}"
          + (f"  [priority: {order}]" if a.book and len(venues) > 1 else "")
          + (f"\nWatching every {max(1, a.every - JITTER):g}-{a.every + JITTER:g}s (random), NOT booking; openings go to {log}" if log and not a.book else "")
          + ("\nCtrl+C to stop." if a.every else ""))

    seen, skipped, rnd = set(), set(), 0
    open_since: dict = {}           # key -> (first seen, match)   for --log
    terms_cache: dict = {}
    while True:
        rnd += 1
        round_start, days_checked, errors = time.time(), 0, 0
        if a.book and lock_info():
            sys.exit(lock_msg(lock_info()))
        matches, ok_venues = [], set()          # (venue, day, slot, party)
        for v in venues:
            party = party_for(a.cfg, a, v)
            open_days = c.calendar(v["id"], party, start.isoformat(), end.isoformat())
            complete = True
            for d, (windows, prefer, types) in dates:
                if open_days is not None and d not in open_days:
                    continue
                days_checked += 1
                try:
                    slots = rank_slots(c.find(v["id"], d, party), windows, prefer, types)
                except ResyError as e:
                    complete = False
                    errors += 1
                    if e.status == 429:
                        print(f"[{ts()}] rate-limited; slowing down")
                        time.sleep(3)
                        continue
                    if e.status in (401, 419):
                        raise
                    print(f"[{ts()}] {v['name']} {d}: {e}")
                    continue
                except requests.RequestException as e:
                    complete = False
                    errors += 1
                    print(f"[{ts()}] network error: {e}")
                    continue
                matches += [(v, d, s, party) for s in far_enough(slots, a.cfg)]
                time.sleep(0.15)          # be gentle across many requests
            if complete:
                ok_venues.add(v["id"])
        if order == "date":               # earliest date first, restaurant order breaks ties
            matches.sort(key=lambda m: m[1])

        new = [m for m in matches if _key(m) not in seen]
        seen |= {_key(m) for m in matches}

        # ---- fetch prepay / cancellation terms for new openings (for the log)
        if log and new:
            for m in new[:15]:
                v, d, s, party = m
                try:
                    terms_cache[_key(m)] = payment_terms(c.details(s["token"], d, party))
                except Exception:
                    pass

        # ---- report
        if a.every is None:
            if matches:
                _print_matches(matches, terms_cache)
            else:
                print(f"[{ts()}] nothing that fits your hours.")
        elif new:
            print(f"\a[{ts()}] NEW openings:")
            _print_matches(new, terms_cache)

        # ---- log openings / closings
        if log:
            now = dt.datetime.now()
            rows = []
            for m in new:
                v, d, s, party = m
                t = terms_cache.get(_key(m))
                why = terms_problem(t, a.cfg) if t else "terms not fetched"
                rows.append({
                    "event": "opened", "logged_at": now.isoformat(timespec="seconds"),
                    "restaurant": v["name"], "venue_id": v["id"], "date": d,
                    "weekday": DAYS[dt.date.fromisoformat(d).weekday()], "time": s["time"],
                    "seating": s["type"], "party": party,
                    "hours_away": round((dt.datetime.fromisoformat(s["start"]) - now).total_seconds() / 3600, 1),
                    "prepay": t["prepay"] if t and t["known"] else "",
                    "cancel_fee": t["cancel_fee"] if t and t["known"] else "",
                    "payment_type": (t or {}).get("payment_type") or "",
                    "within_your_rules": "" if not t else ("no" if why else "yes"),
                    "why_not": why or "", "policy": (t or {}).get("policy", ""),
                    "open_minutes": ""})
                open_since[_key(m)] = (now, m)
            current = {_key(m) for m in matches}
            for k, (since, m) in list(open_since.items()):
                if k not in current and k[0] in ok_venues:
                    v, d, s, party = m
                    rows.append({
                        "event": "gone", "logged_at": now.isoformat(timespec="seconds"),
                        "restaurant": v["name"], "venue_id": v["id"], "date": d,
                        "weekday": DAYS[dt.date.fromisoformat(d).weekday()], "time": s["time"],
                        "seating": s["type"], "party": party,
                        "open_minutes": round((now - since).total_seconds() / 60, 1)})
                    del open_since[k]
                    seen.discard(k)           # if it comes back, log it again
            if rows:
                _log_rows(log, rows)

        # ---- book
        if a.book and matches:
            failures = 0
            for m in matches:
                v, d, s, party = m
                if _key(m) in skipped:
                    continue
                confirm = _confirm_prompt(v, d, s, party) if (a.every is None and not a.yes) else None
                try:
                    res, t = safe_book(c, v, s, d, party, a.cfg, confirm=confirm)
                except AlreadyBooked as e:
                    sys.exit(str(e))
                except SkipSlot as e:
                    skipped.add(_key(m))
                    print(f"[{ts()}] skipped {v['name']} {d} {s['time']}: {e}")
                    continue
                except Exception as e:
                    print(f"[{ts()}] {v['name']} {d} {s['time']} failed: {e}")
                    failures += 1
                    if failures >= a.max_attempts:
                        break
                    continue
                print(f"[{ts()}] BOOKED {v['name']} {d} {s['time']} ({s['type']}) for {party} - "
                      f"{terms_text(t)}. Stopping; no more bookings until you cancel it or "
                      f"delete {LOCK_FILE.name}. (resy_token saved to "
                      f"{PRIVATE_LOCK_FILE.name}, not committed to the repo)")
                return
        if a.every is None:
            return
        took = time.time() - round_start
        if rnd % COOLDOWN_EVERY == 0:
            wait = COOLDOWN_SECONDS         # every 15th check: a longer breather
        else:
            # random wait around the pivot: 30s -> anywhere from 10s to 50s
            wait = max(1.0, a.every + random.uniform(-JITTER, JITTER))
        print(f"[{ts()}] check #{rnd} done in {took:.1f}s - {len(venues)} restaurant(s), "
              f"{days_checked} day(s) with tables looked at, {len(matches)} fit your rules "
              f"({len(new)} new)" + (f", {errors} error(s)" if errors else "")
              + f". Next check in {wait:.0f}s.")
        time.sleep(wait)


def cmd_watch(a):
    """scan every N seconds across all config restaurants, log openings to a file, never book."""
    a.book, a.yes, a.max_attempts, a.start, a.end = False, False, 0, None, None
    a.every = a.every or float(a.cfg.get("watch_every", 30))
    a.days = a.days or int(a.cfg.get("watch_days", 30))
    a.log = a.log or a.cfg.get("openings_file", "resy_openings.csv")
    try:
        cmd_scan(a)
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_snipe(a):
    c = load_client()
    venues = targets(c, a.cfg, a.venue)
    windows, prefer, types = rules_for(a.cfg, a.day, a)
    last_ok = dt.datetime.combine(dt.date.fromisoformat(a.day), dt.time(23, 59))
    if last_ok < min_start(a.cfg):
        sys.exit(f"{a.day} is less than {a.cfg.get('min_hours_ahead', 24)}h away "
                 "(min_hours_ahead in the config) - nothing there could be booked.")
    if windows == []:
        sys.exit(f"Your config says {DAYS[dt.date.fromisoformat(a.day).weekday()]} doesn't work. "
                 "Add hours for that day or pass --window.")
    # Warm up: validate the token, check for existing bookings, open the TLS connection.
    c.user()
    if not a.dry_run:
        preflight(c, venues, a.cfg)
    parties = {v["id"]: party_for(a.cfg, a, v) for v in venues}
    print(f"[{ts()}] token OK, payment method {c.payment_method_id}")
    print(f"[{ts()}] {a.day}, hours: {describe(windows, prefer)}; "
          + ", ".join(f"{v['name']} (party {parties[v['id']]})" for v in venues))
    for v in venues:
        c.find(v["id"], a.day, parties[v["id"]])

    if a.at:
        today = dt.date.today()
        target = dt.datetime.combine(today, dt.time.fromisoformat(a.at))
        if target < dt.datetime.now() - dt.timedelta(seconds=a.duration):
            target += dt.timedelta(days=1)
        start = target - dt.timedelta(seconds=a.lead)
        print(f"[{ts()}] waiting until {start:%Y-%m-%d %H:%M:%S} "
              f"(release {target:%H:%M:%S}, starting {a.lead}s early)")
        last_warm = time.time()
        while (remaining := (start - dt.datetime.now()).total_seconds()) > 0:
            # keep the connection warm every ~30s while waiting
            if time.time() - last_warm > 30 and remaining > 5:
                try:
                    c.find(venues[0]["id"], a.day, parties[venues[0]["id"]], timeout=5)
                except Exception:
                    pass
                last_warm = time.time()
            time.sleep(min(remaining, 0.5 if remaining > 2 else 0.01))

    deadline = time.time() + a.duration
    tried = set()
    polls = 0
    print(f"[{ts()}] polling every {a.interval}s for up to {a.duration}s ...")
    while time.time() < deadline:
        polls += 1
        found_any = False
        for v in venues:                  # config order = priority
            party = parties[v["id"]]
            try:
                slots = c.find(v["id"], a.day, party, timeout=5)
            except ResyError as e:
                if e.status == 429:
                    print(f"[{ts()}] rate-limited, backing off 2s")
                    time.sleep(2)
                else:
                    print(f"[{ts()}] {v['name']} find error: {e}")
                continue
            except requests.RequestException as e:
                print(f"[{ts()}] network error: {e}")
                continue

            cands = [s for s in far_enough(rank_slots(slots, windows, prefer, types), a.cfg)
                     if s["token"] not in tried]
            if not cands:
                continue
            found_any = True
            print(f"[{ts()}] {v['name']}: {len(cands)} matching slots: "
                  + ", ".join(f"{s['time']} {s['type']}" for s in cands[:6]))
            if a.dry_run:
                print(f"Dry run - not booking. Best: {v['name']} {cands[0]['time']} {cands[0]['type']}")
                return
            failures = 0
            for s in cands:
                tried.add(s["token"])
                try:
                    res, t = safe_book(c, v, s, a.day, party, a.cfg)
                except AlreadyBooked as e:
                    sys.exit(str(e))
                except SkipSlot as e:
                    print(f"[{ts()}] skipped {v['name']} {s['time']} {s['type']}: {e}")
                    continue
                except Exception as e:
                    print(f"[{ts()}] {v['name']} {s['time']} {s['type']} failed: {e}")
                    failures += 1
                    if failures >= a.max_attempts:
                        break
                    continue
                print(f"[{ts()}] BOOKED {v['name']} {a.day} {s['time']} ({s['type']}) for {party} - "
                      f"{terms_text(t)}. Stopping; no more bookings until you cancel it or "
                      f"delete {LOCK_FILE.name}. (resy_token saved to "
                      f"{PRIVATE_LOCK_FILE.name}, not committed to the repo)")
                return
        if not found_any:
            if polls % 20 == 0:
                print(f"[{ts()}] poll {polls}: nothing matching yet")
            time.sleep(a.interval)
    print(f"[{ts()}] gave up after {polls} polls - nothing booked.")
    sys.exit(1)



# --------------------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description="Unofficial Resy client")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login"); s.set_defaults(f=cmd_login)
    s.add_argument("--email"); s.add_argument("--token", help="paste an auth token from the browser instead")

    s = sub.add_parser("whoami"); s.set_defaults(f=cmd_whoami)

    s = sub.add_parser("search"); s.set_defaults(f=cmd_search)
    s.add_argument("query")

    s = sub.add_parser("venue"); s.set_defaults(f=cmd_venue)
    s.add_argument("slug_or_url"); s.add_argument("--location", default="ny")

    def common(s, day=True, optional_venue=True):
        if optional_venue:
            s.add_argument("venue", nargs="?", help="venue id, slug, URL, or a name from your "
                                                    "config (default: every config restaurant)")
        else:
            s.add_argument("venue", help="venue id, slug, URL, or a name from your config")
        if day:
            s.add_argument("--day", required=True, help="YYYY-MM-DD")
        s.add_argument("--party", type=int, help="party size (default: config, else 2)")
        s.add_argument("--config", help=f"weekly hours file (default: {DEFAULT_CONFIG.name} next to this script)")

    def hours_flags(s):
        s.add_argument("--window", help="override your config hours, e.g. 18:30-21:00")
        s.add_argument("--prefer", help="ideal time, e.g. 19:30 (closest wins)")
        s.add_argument("--type", nargs="*", help="seating types to accept, e.g. 'dining room' bar")

    s = sub.add_parser("find"); s.set_defaults(f=cmd_find); common(s); hours_flags(s)
    s.add_argument("--all", action="store_true", help="ignore your hours, show every slot")
    s.add_argument("--fees", action="store_true", help="also show prepay / cancellation fee per table")

    s = sub.add_parser("scan"); s.set_defaults(f=cmd_scan); common(s, day=False); hours_flags(s)
    s.add_argument("--start", help="first date, YYYY-MM-DD (default today)")
    s.add_argument("--end", help="last date, YYYY-MM-DD")
    s.add_argument("--days", type=int, default=14, help="number of days to check if no --end (default 14)")
    s.add_argument("--book", action="store_true", help="book the best fit (earliest date first)")
    s.add_argument("--yes", action="store_true", help="with --book: skip the confirmation prompt")
    s.add_argument("--every", type=float, help="keep re-checking every N seconds (with --book: "
                                                 "books automatically on the first fit)")
    s.add_argument("--max-attempts", type=int, default=3, help="failed booking attempts before giving up a round")
    s.add_argument("--log", help="append openings (and when they disappear) to this CSV file")

    s = sub.add_parser("watch", help="check all restaurants every N s, log openings, never book")
    s.set_defaults(f=cmd_watch); common(s, day=False); hours_flags(s)
    s.add_argument("--every", type=float, help="seconds between checks (default: config watch_every, else 30)")
    s.add_argument("--days", type=int, help="days ahead to cover (default: config watch_days, else 30)")
    s.add_argument("--log", help="CSV file (default: config openings_file, else resy_openings.csv)")

    s = sub.add_parser("book"); s.set_defaults(f=cmd_book); common(s, optional_venue=False)
    s.add_argument("--time", required=True, help="HH:MM"); s.add_argument("--type")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    sub.add_parser("reservations").set_defaults(f=cmd_reservations)

    s = sub.add_parser("cancel"); s.set_defaults(f=cmd_cancel)
    s.add_argument("resy_token")

    s = sub.add_parser("snipe"); s.set_defaults(f=cmd_snipe); common(s); hours_flags(s)
    s.add_argument("--at", help="local release time HH:MM:SS; omit to start now")
    s.add_argument("--lead", type=float, default=2.0, help="start polling this many seconds early")
    s.add_argument("--interval", type=float, default=0.25, help="seconds between polls")
    s.add_argument("--duration", type=float, default=90, help="seconds to keep polling")
    s.add_argument("--max-attempts", type=int, default=3, help="slots to try per poll")
    s.add_argument("--dry-run", action="store_true", help="find but don't book")

    a = p.parse_args()
    a.cfg = load_config(a.config) if hasattr(a, "config") else {}
    try:
        a.f(a)
    except AlreadyBooked as e:
        sys.exit(str(e))
    except ResyError as e:
        if e.status in (401, 419):
            send_alert_email(
                "Resy bot: your auth token expired",
                f"resy.py got HTTP {e.status} calling the Resy API - your saved token "
                f"looks expired or invalid.\n\nRun `python resy.py login` (paste a fresh "
                f"token from resy.com) to fix it.\n\nDetails: {e}",
            )
            sys.exit(f"Auth problem ({e.status}) - token expired? Run: python resy.py login")
        sys.exit(str(e))


if __name__ == "__main__":
    main()
