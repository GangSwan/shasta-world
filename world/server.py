"""shasta-world server: PROTOCOL.md v1 over HTTP, with all state in sqlite. Stdlib only.

    python -m world.server --config config.json      # the real world
    python -m world.server --test [--port 8642]      # test mode: fixed roster, /v1/_test, no weather or events

Every request runs in one sqlite transaction under one lock, and the reply is sent only after the
commit. A crash or a kill at any moment leaves the world as it was before the request or after it,
never halfway, and a creature that never got its reply resends the same action id.
"""
import argparse
import contextlib
import hmac
import json
import random
import re
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from world.town import PLACES, RULES, WEATHER_KINDS, background_event, fetch_weather, pacific, part_of_day, season

TEST_ROSTER = {
    "bolt": {"name": "Bolt", "home": "nest", "token": "test-bolt"},
    "spike": {"name": "Spike", "home": "fir", "token": "test-spike"},
    "wren": {"name": "Wren", "home": "lake", "token": "test-wren"},
}

MAX_BODY = 8192
QUIET_AFTER = 600
BURST = 30
BREAK_AFTER, BREAK_FOR = 8, 1800
SAY_PER_DAY, GIVES_PER_DAY, HIDES_PER_DAY = 60, 3, 10
DEEDS, DEED_WINDOW = 6, 300
FLOOD, FLOOD_WINDOW = 30, 300
DOING_FOR = 900
HEARD_KEEP, IDS_KEEP = 86400, 30 * 86400
MAP_TALK, MAP_EVENTS = 12, 30       # what the map page shows: the newest talk lines and events in town
MAP_PAGE = Path(__file__).with_name("map.html")
EVENTS_KEPT = 20
SUMMARY_CHARS, SUMMARY_LINES = 700, 8
EVENT_EVERY = (600, 1200)          # a background event at an occupied place every 10-20 minutes
WEATHER_EVERY = 1800
BACKUPS_KEPT = 14
DEFAULT_WEATHER = {"kind": "clear", "temp_f": 55, "wind_mph": 3}

ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,64}$")
DEED_ACTIONS = {"go", "sound", "doing", "give", "take", "drop"}
AWAKE_ONLY = DEED_ACTIONS | {"say"}
TEXT, NAME, WHO, BOOL = "text", "name", "who", "bool"
ACTIONS = {   # action: {field: (kind, required, max length)}
    "go": {"to": (NAME, True, 64)},
    "say": {"text": (TEXT, True, 300), "to": (WHO, False, 32)},
    "sound": {"text": (TEXT, True, 60)},
    "doing": {"text": (TEXT, True, 60)},
    "give": {"to": (WHO, True, 32), "item": (TEXT, True, 60)},
    "take": {"item": (NAME, True, 64)},
    "drop": {"item": (TEXT, True, 60)},
    "hide": {"place": (NAME, True, 64), "item": (TEXT, True, 60), "by": (TEXT, True, 32), "for": (WHO, False, 32)},
    "presence": {"awake": (BOOL, True, None)},
    "human_spoke": {"by": (TEXT, True, 32)},
}
ROUTES = {("GET", "/v1/town"), ("POST", "/v1/hello"), ("GET", "/v1/perceive"), ("POST", "/v1/act"),
          ("GET", "/v1/inbox"), ("POST", "/v1/ack")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS creatures (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (n INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, place TEXT NOT NULL,
                                  by TEXT NOT NULL, for_ TEXT);
CREATE TABLE IF NOT EXISTS deliveries (n INTEGER PRIMARY KEY AUTOINCREMENT, creature TEXT NOT NULL, item TEXT NOT NULL,
                                       frm TEXT NOT NULL, how TEXT NOT NULL, t REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (n INTEGER PRIMARY KEY AUTOINCREMENT, place TEXT NOT NULL, t REAL NOT NULL,
                                   text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS heard (seq INTEGER PRIMARY KEY AUTOINCREMENT, listener TEXT NOT NULL, frm TEXT NOT NULL,
                                  name TEXT NOT NULL, to_ TEXT, text TEXT NOT NULL, place TEXT NOT NULL,
                                  at TEXT NOT NULL, t REAL NOT NULL);
CREATE TABLE IF NOT EXISTS actions (creature TEXT NOT NULL, id TEXT NOT NULL, canon TEXT NOT NULL, reply TEXT NOT NULL,
                                    t REAL NOT NULL, PRIMARY KEY (creature, id));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_place ON items(place);
CREATE INDEX IF NOT EXISTS deliveries_creature ON deliveries(creature);
CREATE INDEX IF NOT EXISTS events_place ON events(place, n);
CREATE INDEX IF NOT EXISTS heard_listener ON heard(listener, seq);
CREATE INDEX IF NOT EXISTS heard_t ON heard(t);
CREATE INDEX IF NOT EXISTS actions_t ON actions(t);
"""


def log(msg):
    print(time.strftime("[%m-%d %H:%M:%S] ") + msg, flush=True)


class BadRequest(Exception):
    pass


def clean(text):
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()


def a_or_an(thing):
    return ("an " if thing[:1].lower() in "aeiou" else "a ") + thing


def iso(ts):
    return pacific(ts).isoformat(timespec="seconds")


def new_creature(cid, name, home):
    return {"id": cid, "name": name, "home": home, "place": home, "awake": True, "seen": None,
            "doing": None, "doing_until": 0, "day": None, "said": 0, "gifts": 0, "hides": 0,
            "streak": 0, "break_until": 0, "last_say": None, "deeds": [],
            "flood": {"presence": [], "human_spoke": []}}


class World:
    def __init__(self, db_path, roster, test=False, rng=None):
        self.roster, self.test = roster, test
        self.rng = rng or random.Random()
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.C = {}
        with self.lock, self._tx():
            self.offset = float(self._meta("offset", "0")) if test else 0.0
            for cid, r in roster.items():
                row = self.db.execute("SELECT data FROM creatures WHERE id=?", (cid,)).fetchone()
                c = json.loads(row[0]) if row else new_creature(cid, r["name"], r["home"])
                c.update(name=r["name"], home=r["home"])
                self.db.execute("INSERT OR REPLACE INTO creatures VALUES (?, ?)", (cid, json.dumps(c)))

    def close(self):
        with self.lock:
            self.db.close()

    # ---------- plumbing
    @contextlib.contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.C = {cid: json.loads(d) for cid, d in
                      self.db.execute("SELECT id, data FROM creatures") if cid in self.roster}
            yield
            for cid, c in self.C.items():
                self.db.execute("UPDATE creatures SET data=? WHERE id=?", (json.dumps(c), cid))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self.C = {}

    def _meta(self, k, default=None):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))

    def now(self):
        return time.time() + self.offset

    def weather(self):
        return json.loads(self._meta("weather", json.dumps(DEFAULT_WEATHER)))

    # ---------- bookkeeping
    def _quiet(self, c, t):
        return c["seen"] is None or t - c["seen"] > QUIET_AFTER

    def _state(self, c, t):
        return "quiet" if self._quiet(c, t) else "awake" if c["awake"] else "asleep"

    def _event(self, place, text, t=None):
        self.db.execute("INSERT INTO events (place, t, text) VALUES (?, ?, ?)", (place, t or self.now(), text))
        self.db.execute("DELETE FROM events WHERE place=? AND n NOT IN "
                        "(SELECT n FROM events WHERE place=? ORDER BY n DESC LIMIT ?)", (place, place, EVENTS_KEPT))

    def _deliver(self, cid, item, frm, how):
        cur = self.db.execute("INSERT INTO deliveries (creature, item, frm, how, t) VALUES (?, ?, ?, ?, ?)",
                              (cid, item, frm, how, self.now()))
        return f"d{cur.lastrowid}"

    def _deliveries(self, cid):
        return [{"id": f"d{n}", "item": item, "from": frm, "how": how} for n, item, frm, how in
                self.db.execute("SELECT n, item, frm, how FROM deliveries WHERE creature=? ORDER BY n", (cid,))]

    def _items_at(self, place):
        return [{"id": f"i{n}", "name": name, "by": by, "for": for_} for n, name, by, for_ in
                self.db.execute("SELECT n, name, by, for_ FROM items WHERE place=? ORDER BY n", (place,))]

    def _item(self, iid):
        if not re.fullmatch(r"i\d{1,18}", iid):
            return None
        row = self.db.execute("SELECT n, name, place, by, for_ FROM items WHERE n=?", (int(iid[1:]),)).fetchone()
        return dict(zip(("n", "name", "place", "by", "for"), row)) if row else None

    def _new_item(self, place, name, by, for_):
        cur = self.db.execute("INSERT INTO items (name, place, by, for_) VALUES (?, ?, ?, ?)", (name, place, by, for_))
        return f"i{cur.lastrowid}"

    def _sweep(self, c):
        """Items left for this creature move into its deliveries once it's there and awake."""
        if not c["awake"]:
            return
        rows = self.db.execute("SELECT n, name, by FROM items WHERE for_=? AND place=? ORDER BY n",
                               (c["id"], c["place"])).fetchall()
        for n, name, by in rows:
            self.db.execute("DELETE FROM items WHERE n=?", (n,))
            self._deliver(c["id"], name, by, f"{by} left it for you at {PLACES[c['place']]}")

    def _checkin(self, c, t):
        day = pacific(t).date().isoformat()
        if c["day"] != day:
            c.update(day=day, said=0, gifts=0, hides=0)
        c["seen"] = t
        c["deeds"] = [x for x in c["deeds"] if t - x < DEED_WINDOW]
        for k in c["flood"]:
            c["flood"][k] = [x for x in c["flood"][k] if t - x < FLOOD_WINDOW]
        if c["doing"] and t >= c["doing_until"]:
            c["doing"] = None
        self.db.execute("DELETE FROM heard WHERE t < ?", (t - HEARD_KEEP,))
        self.db.execute("DELETE FROM actions WHERE t < ?", (t - IDS_KEEP,))

    # ---------- what you see
    def _cant_go(self, c):
        if c["place"] == "summit":
            return {}
        if c["place"] != "mountain_trail":
            return {"summit": "You have to climb the mountain_trail first."}
        if self.weather()["kind"] == "thunderstorm":
            return {"summit": "The summit is closed: lightning is cracking over the peak."}
        return {}

    def _talk(self, c, t):
        return {"left_today": max(0, SAY_PER_DAY - c["said"]),
                "break_until": iso(c["break_until"]) if c["break_until"] > t else None}

    def _summary(self, c, t, weather, here, items, recent, deliveries, around=()):
        w = weather
        lines = [f"{part_of_day(pacific(t).hour).capitalize()} at {PLACES[c['place']]}, {w['kind']} and {w['temp_f']}°F."]
        if not c["awake"]:
            lines.append("You're asleep.")
        if here:
            lines.append(" ".join(f"{h['name']} is here, {h['state']}" + (f", {h['doing']}." if h["doing"] else ".")
                                  for h in here))
        else:
            lines.append("Nobody else is here.")
        if around:
            lines.append("Around town: " + "; ".join(f"{o['name']} is at {PLACES[o['place']]} ("
                                                     + ("away" if o["state"] == "quiet" else o["state"]) + ")"
                                                     for o in around) + ".")
        if items:
            bits = [a_or_an(i["name"]) + (f" ({i['by']} left it for {self.C[i['for']]['name']})"
                                          if i["for"] in self.C else "") for i in items[:5]]
            lines.append("On the ground: " + ", ".join(bits) + ("." if len(items) <= 5 else ", and more."))
        if c["break_until"] > t:
            lines.append(f"You're on a talk break until {pacific(c['break_until']).strftime('%H:%M')}.")
        if deliveries:
            lines.append("Waiting for you: " + ", ".join(d["item"] for d in deliveries[:5]) + ".")
        recent = list(recent)
        while True:
            out = lines + (["Lately: " + " ".join(recent)] if recent else [])
            text = "\n".join(out)
            if (len(text) <= SUMMARY_CHARS and len(out) <= SUMMARY_LINES) or not recent:
                break
            recent.pop(0)
        if len(text) > SUMMARY_CHARS:
            text = text[:SUMMARY_CHARS - 1].rstrip() + "…"
        return text

    def perceive(self, c, t):
        lt = pacific(t)
        weather = self.weather()
        here = [{"id": o["id"], "name": o["name"], "state": self._state(o, t), "doing": o["doing"]}
                for o in self.C.values() if o is not c and o["seen"] is not None and o["place"] == c["place"]]
        around = [{"id": o["id"], "name": o["name"], "place": o["place"], "state": self._state(o, t)}
                  for o in self.C.values() if o is not c and o["seen"] is not None and o["place"] != c["place"]]
        items = self._items_at(c["place"])
        recent = [r[0] for r in self.db.execute(
            "SELECT text FROM (SELECT n, text FROM events WHERE place=? ORDER BY n DESC LIMIT 5) ORDER BY n",
            (c["place"],))]
        deliveries = self._deliveries(c["id"])
        return {"ok": True, "now": lt.isoformat(timespec="seconds"), "part": part_of_day(lt.hour),
                "season": season(lt.month), "weather": weather,
                "you": {"id": c["id"], "place": c["place"], "awake": c["awake"]},
                "place": {"id": c["place"], "name": PLACES[c["place"]]},
                "here": here, "around": around, "items": items, "recent": recent, "cant_go": self._cant_go(c),
                "talk": self._talk(c, t), "deliveries": deliveries,
                "summary": self._summary(c, t, weather, here, items, recent, deliveries, around)}

    # ---------- requests
    def handle(self, method, path, query, token, body):
        """Returns (status, reply). Raises BadRequest for a 400. Everything inside is one transaction."""
        if path == "/v1/_test" and self.test and method == "POST":
            with self.lock:
                return self._test_hook(body)
        if method == "GET" and path == "/v1/map":
            with self.lock:
                return 200, self.map_view()
        if (method, path) not in ROUTES:
            return 404, {"ok": False, "reason": "not_found", "message": "No such endpoint."}
        cid = next((k for k, r in self.roster.items() if token and hmac.compare_digest(r["token"], token)), None)
        if cid is None:
            return 401, {"ok": False, "reason": "unauthorized", "message": "Unknown or missing token."}
        if method == "POST" and not isinstance(body, dict):
            raise BadRequest("The body must be a JSON object.")
        with self.lock, self._tx():
            c = self.C[cid]
            t = self.now()
            self._checkin(c, t)
            self._sweep(c)
            if path == "/v1/town":
                return 200, self.town()
            if path == "/v1/perceive":
                return 200, self.perceive(c, t)
            if path == "/v1/inbox":
                return 200, self.read_inbox(c, query)
            if path == "/v1/ack":
                return 200, self.ack(c, body)
            if path == "/v1/hello":
                return 200, self.hello(c, t, body)
            return self.act(c, t, body)

    def map_view(self):
        """GET /v1/map: the whole town right now, for the map page people look at. Read-only and no token
        (the server only listens on the tailnet): it never checks anyone in, and it shows only what any
        creature standing there could see. No deliveries, no limits, no tokens."""
        t = self.now()
        lt = pacific(t)
        cs = {cid: json.loads(d) for cid, d in self.db.execute("SELECT id, data FROM creatures") if cid in self.roster}
        name = lambda cid: cs[cid]["name"] if cid in cs else cid
        creatures = [{"id": c["id"], "name": c["name"], "home": c["home"], "place": c["place"],
                      "state": self._state(c, t), "doing": c["doing"] if c["doing"] and t < c["doing_until"] else None,
                      "seen": int(t - c["seen"]) if c["seen"] is not None else None}
                     for c in sorted(cs.values(), key=lambda c: c["id"])]
        items = [{"name": n, "place": place, "by": by, "for": name(f) if f else None} for n, place, by, f in
                 self.db.execute("SELECT name, place, by, for_ FROM items ORDER BY n")]
        talk = [{"from": frm, "name": nm, "to": name(to) if to else None, "text": text, "place": place, "ago": int(t - at)}
                for frm, nm, to, text, place, at in self.db.execute(
                    "SELECT frm, name, to_, text, place, MAX(t) FROM heard GROUP BY frm, t, text ORDER BY MAX(t) DESC LIMIT ?",
                    (MAP_TALK,))]
        events = [{"place": place, "text": text, "ago": int(t - at)} for place, text, at in self.db.execute(
            "SELECT place, text, t FROM events ORDER BY n DESC LIMIT ?", (MAP_EVENTS,))]
        return {"ok": True, "now": lt.isoformat(timespec="seconds"), "hour": lt.hour, "part": part_of_day(lt.hour),
                "season": season(lt.month), "weather": self.weather(), "places": PLACES,
                "creatures": creatures, "items": items, "talk": talk, "events": events}

    def _test_hook(self, body):
        if not isinstance(body, dict):
            raise BadRequest("The body must be a JSON object.")
        with self._tx():
            if body.get("reset"):
                for table in ("items", "deliveries", "events", "heard", "actions", "meta"):
                    self.db.execute(f"DELETE FROM {table}")
                self.offset = 0.0
                for cid, r in self.roster.items():
                    self.C[cid] = new_creature(cid, r["name"], r["home"])
            if "advance" in body:
                self.offset += float(body["advance"])
                self._set_meta("offset", repr(self.offset))
            if "weather" in body:
                w = body["weather"]
                if w.get("kind") not in WEATHER_KINDS:
                    raise BadRequest(f"weather kind must be one of {', '.join(WEATHER_KINDS)}.")
                self._set_meta("weather", json.dumps({"kind": w["kind"], "temp_f": int(w["temp_f"]),
                                                      "wind_mph": int(w["wind_mph"])}))
            if "event" in body:
                if body["event"].get("place") not in PLACES:
                    raise BadRequest("Unknown place.")
                self._event(body["event"]["place"], str(body["event"]["text"]))
        return 200, {"ok": True, "now": iso(self.now())}

    def town(self):
        homes = {c["home"]: cid for cid, c in self.C.items()}
        return {"ok": True,
                "places": [{"id": p, "name": n, "home_of": homes.get(p)} for p, n in PLACES.items()],
                "creatures": [{"id": c["id"], "name": c["name"], "home": c["home"]} for c in self.C.values()],
                "rules": list(RULES)}

    def read_inbox(self, c, query):
        try:
            after = int((query.get("after") or ["0"])[0])
        except ValueError:
            raise BadRequest("after must be a number.")
        heard = [{"seq": seq, "from": frm, "name": name, "to": to_, "text": text, "place": place, "at": at}
                 for seq, frm, name, to_, text, place, at in self.db.execute(
                     "SELECT seq, frm, name, to_, text, place, at FROM heard WHERE listener=? AND seq>? ORDER BY seq",
                     (c["id"], after))]
        return {"ok": True, "heard": heard, "deliveries": self._deliveries(c["id"]),
                "next": heard[-1]["seq"] if heard else after}

    def ack(self, c, body):
        ids = body.get("deliveries") if set(body) == {"deliveries"} else None
        if not isinstance(ids, list) or len(ids) > 100 or not all(isinstance(x, str) for x in ids):
            raise BadRequest("Send {\"deliveries\": [ids]}.")
        acked = []
        for d in self._deliveries(c["id"]):
            if d["id"] in ids:
                self.db.execute("DELETE FROM deliveries WHERE n=?", (int(d["id"][1:]),))
                acked.append(d["id"])
        return {"ok": True, "acked": acked}

    def hello(self, c, t, body):
        if set(body) != {"place", "awake"}:
            raise BadRequest("hello takes exactly place and awake.")
        if body["place"] not in PLACES or not isinstance(body["awake"], bool):
            raise BadRequest("Unknown place, or awake isn't true/false.")
        if body["place"] != c["place"]:
            self._event(c["place"], f"{c['name']} left.")
            c["place"], c["doing"] = body["place"], None
            self._event(c["place"], f"{c['name']} turned up.")
        c["awake"] = body["awake"]
        self._sweep(c)
        return self.perceive(c, t)

    def _validate(self, body):
        if not isinstance(body.get("id"), str) or not ID_RE.match(body["id"]):
            raise BadRequest("id must be 1-64 characters of A-Z a-z 0-9 : . _ -")
        spec = ACTIONS.get(body.get("action"))
        if spec is None:
            raise BadRequest(f"Unknown action. Actions: {', '.join(ACTIONS)}.")
        extra = set(body) - set(spec) - {"id", "action"}
        if extra:
            raise BadRequest(f"Unknown fields: {', '.join(sorted(extra))}.")
        out = {}
        for field, (kind, required, cap) in spec.items():
            if field not in body:
                if required:
                    raise BadRequest(f"{field} is required.")
                out[field] = None
                continue
            v = body[field]
            if kind == BOOL:
                if not isinstance(v, bool):
                    raise BadRequest(f"{field} must be true or false.")
            else:
                if not isinstance(v, str):
                    raise BadRequest(f"{field} must be text.")
                v = clean(v)
                if not v or len(v) > cap:
                    raise BadRequest(f"{field} must be 1-{cap} characters.")
                if kind == WHO:
                    v = v.lower()
            out[field] = v
        return out

    def act(self, c, t, body):
        f = self._validate(body)
        canon = json.dumps(body, sort_keys=True)
        row = self.db.execute("SELECT canon, reply FROM actions WHERE creature=? AND id=?", (c["id"], body["id"])).fetchone()
        if row:
            if row[0] != canon:
                return 409, {"ok": False, "id": body["id"], "reason": "id_reused",
                             "message": "That action id was already used for a different action."}
            return 200, json.loads(row[1])
        ok, fields = self._do(c, t, body["action"], f)
        reply = {"ok": ok, "id": body["id"], **fields}
        self.db.execute("INSERT INTO actions VALUES (?, ?, ?, ?, ?)", (c["id"], body["id"], canon, json.dumps(reply), t))
        return 200, reply

    def _do(self, c, t, action, f):
        def no(reason, message):
            return False, {"reason": reason, "message": message}

        if action in AWAKE_ONLY and not c["awake"]:
            return no("asleep", "You're asleep.")
        target = f.get("for") if action == "hide" else f.get("to") if action in ("give", "say") else None
        if action in ("give", "say") and target == c["id"]:
            raise BadRequest("You can't do that to yourself.")
        if target and target not in self.C:
            if action == "hide":
                return no("unknown_creature", "There's no creature by that name.")
            return no("not_here", "Nobody by that name is here.")
        if action in DEED_ACTIONS and len(c["deeds"]) >= DEEDS:
            return no("limit", "You've done a lot just now. Wait a few minutes.")
        if action in c["flood"]:
            if len(c["flood"][action]) >= FLOOD:
                return no("limit", "Too many of those just now.")
            c["flood"][action].append(t)
        ok = getattr(self, "_" + action)(c, t, f, c["place"])
        if ok[0] and action in DEED_ACTIONS:
            c["deeds"].append(t)
        return ok

    # each action returns (ok, fields)
    def _go(self, c, t, f, here):
        to = f["to"].lower()
        if to not in PLACES:
            return False, {"reason": "unknown_place", "message": f"There's no place called {f['to']}."}
        if to == here:
            return True, {"result": f"You stayed at {PLACES[here]}."}
        blocked = self._cant_go(c).get(to)
        if blocked:
            return False, {"reason": "blocked", "message": blocked}
        c["place"], c["doing"] = to, None
        self._event(here, f"{c['name']} flew off toward {PLACES[to]}.")
        self._event(to, f"{c['name']} flew in from {PLACES[here]}.")
        self._sweep(c)
        return True, {"result": f"You flew from {PLACES[here]} to {PLACES[to]}."}

    def _say(self, c, t, f, here):
        to = f["to"]
        if to:
            o = self.C[to]
            if o["place"] != here or o["seen"] is None:
                return False, {"reason": "not_here", "message": f"{o['name']} isn't here."}
            if self._quiet(o, t):
                return False, {"reason": "quiet", "message": f"{o['name']} has gone quiet."}
            if not o["awake"]:
                return False, {"reason": "asleep", "message": f"{o['name']} is asleep."}
        if c["break_until"] > t:
            return False, {"reason": "break", "message": "You've talked a lot with no human around. Take a break."}
        new_burst = c["last_say"] is None or t - c["last_say"] > BURST
        if new_burst and c["said"] >= SAY_PER_DAY:
            return False, {"reason": "limit", "message": "You've said enough for today."}
        listeners = [o for o in self.C.values()
                     if o is not c and o["place"] == here and o["awake"] and not self._quiet(o, t)]
        for o in listeners:
            self.db.execute("INSERT INTO heard (listener, frm, name, to_, text, place, at, t) VALUES (?,?,?,?,?,?,?,?)",
                            (o["id"], c["id"], c["name"], to, f["text"], here, iso(t), t))
        c["last_say"] = t
        if new_burst:
            c["said"] += 1
            if listeners:
                c["streak"] += 1
                if c["streak"] >= BREAK_AFTER:
                    c["streak"], c["break_until"] = 0, t + BREAK_FOR
        who = ", ".join(o["name"] for o in listeners)
        return True, {"result": f"{who} heard you." if listeners else "Nobody heard you."}

    def _sound(self, c, t, f, here):
        self._event(here, f"{c['name']}: {f['text']}.")
        return True, {"result": f"You made a sound: {f['text']}."}

    def _doing(self, c, t, f, here):
        c["doing"], c["doing_until"] = f["text"], t + DOING_FOR
        self._event(here, f"{c['name']} is {f['text']}.")
        return True, {"result": f"Others here can see you're {f['text']}."}

    def _give(self, c, t, f, here):
        o = self.C[f["to"]]
        if o["place"] != here:
            return False, {"reason": "not_here", "message": f"{o['name']} isn't here."}
        if c["gifts"] >= GIVES_PER_DAY:
            return False, {"reason": "limit", "message": "You've given enough gifts for today."}
        c["gifts"] += 1
        d = self._deliver(o["id"], f["item"], c["name"], f"a gift from {c['name']}")
        self._event(here, f"{c['name']} gave {o['name']} {a_or_an(f['item'])}.")
        return True, {"result": f"You gave {o['name']} {a_or_an(f['item'])}.", "delivery": d}

    def _take(self, c, t, f, here):
        it = self._item(f["item"])
        if it is None:
            return False, {"reason": "gone", "message": "It's gone. Someone got there first."}
        if it["place"] != here:
            return False, {"reason": "not_here", "message": f"That's at {PLACES[it['place']]}, not here."}
        if it["for"] and it["for"] != c["id"]:
            name = self.C[it["for"]]["name"] if it["for"] in self.C else it["for"]
            return False, {"reason": "reserved", "message": f"That was left for {name}."}
        self.db.execute("DELETE FROM items WHERE n=?", (it["n"],))
        d = self._deliver(c["id"], it["name"], it["by"], f"you picked it up at {PLACES[here]}")
        self._event(here, f"{c['name']} picked up the {it['name']}.")
        return True, {"result": f"You picked up the {it['name']}.", "delivery": d}

    def _drop(self, c, t, f, here):
        iid = self._new_item(here, f["item"], c["name"], None)
        self._event(here, f"{c['name']} dropped {a_or_an(f['item'])}.")
        return True, {"result": f"You left the {f['item']} at {PLACES[here]}.", "item": iid}

    def _hide(self, c, t, f, here):
        place = f["place"].lower()
        if place not in PLACES:
            return False, {"reason": "unknown_place", "message": f"There's no place called {f['place']}."}
        if c["hides"] >= HIDES_PER_DAY:
            return False, {"reason": "limit", "message": "That's enough hiding for today."}
        c["hides"] += 1
        iid = self._new_item(place, f["item"], f["by"], f["for"])
        self._event(place, "Someone left something here.")
        if f["for"]:
            self._sweep(self.C[f["for"]])
        return True, {"result": f"{f['by']}'s {f['item']} is now at {PLACES[place]}.", "item": iid}

    def _presence(self, c, t, f, here):
        c["awake"] = f["awake"]
        self._sweep(c)
        return True, {"result": "You're awake." if c["awake"] else "You're asleep."}

    def _human_spoke(self, c, t, f, here):
        for o in self.C.values():
            if o is c or o["place"] == here:
                o["streak"], o["break_until"] = 0, 0
        return True, {"result": "Noted."}

    # ---------- background work (not in test mode)
    def background_events(self):
        """Add a background event at each place where an awake, present creature is."""
        with self.lock, self._tx():
            t = self.now()
            occupied = {c["place"] for c in self.C.values() if c["awake"] and not self._quiet(c, t)}
            for place in occupied:
                due = float(self._meta(f"next_event:{place}", "0"))
                if due == 0:
                    self._set_meta(f"next_event:{place}", repr(t + self.rng.uniform(*EVENT_EVERY) / 2))
                elif t >= due:
                    self._event(place, background_event(place, t, self.rng), t)
                    self._set_meta(f"next_event:{place}", repr(t + self.rng.uniform(*EVENT_EVERY)))

    def set_weather(self, w):
        with self.lock, self._tx():
            self._set_meta("weather", json.dumps(w))

    def backup(self, folder):
        """One copy a day (Pacific), the last BACKUPS_KEPT kept. Uses sqlite's online backup."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        day = pacific(self.now()).date().isoformat()
        dest = folder / f"world-{day}.db"
        if dest.exists():
            return None
        tmp = dest.with_suffix(".tmp")
        with self.lock:
            out = sqlite3.connect(str(tmp))
            try:
                self.db.backup(out)
            finally:
                out.close()
        tmp.replace(dest)
        for old in sorted(folder.glob("world-*.db"))[:-BACKUPS_KEPT]:
            old.unlink()
        return dest


def make_handler(world):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "shasta-world"

        def log_message(self, *a):
            pass

        def _reply(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _page(self):
            data = MAP_PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _serve(self, method):
            u = urlparse(self.path)
            if method == "GET" and u.path in ("/", "/map"):
                return self._page()
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            body = None
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0:
                    self.close_connection = True
                    return self._reply(400, {"ok": False, "reason": "bad_request", "message": "Bad Content-Length."})
                if n > MAX_BODY:
                    self.close_connection = True
                    return self._reply(413, {"ok": False, "reason": "too_big", "message": "The body is over 8 KB."})
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    return self._reply(400, {"ok": False, "reason": "bad_request", "message": "That isn't valid JSON."})
            try:
                status, obj = world.handle(method, u.path, parse_qs(u.query), token, body)
            except BadRequest as e:
                status, obj = 400, {"ok": False, "reason": "bad_request", "message": str(e)}
            except Exception:
                log("error handling " + self.path + ":\n" + traceback.format_exc())
                status, obj = 500, {"ok": False, "reason": "error", "message": "The world tripped. Try again soon."}
            self._reply(status, obj)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

    return Handler


class Server:
    """The HTTP server plus, outside test mode, the weather, events and backup loop."""

    def __init__(self, world, host="127.0.0.1", port=8642, backups=None, background=True):
        self.world, self.backups = world, backups
        self.httpd = ThreadingHTTPServer((host, port), make_handler(world))
        self.httpd.daemon_threads = True
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self.stopping = threading.Event()
        self.threads = [threading.Thread(target=self.httpd.serve_forever, daemon=True)]
        if background:
            self.threads.append(threading.Thread(target=self._loop, daemon=True))

    def start(self):
        for th in self.threads:
            th.start()
        return self

    def stop(self):
        self.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.world.close()

    def _loop(self):
        next_weather = 0.0
        while not self.stopping.is_set():
            try:
                if time.time() >= next_weather:
                    w = fetch_weather()
                    if w:
                        self.world.set_weather(w)
                    next_weather = time.time() + (WEATHER_EVERY if w else 300)
                self.world.background_events()
                if self.backups:
                    made = self.world.backup(self.backups)
                    if made:
                        log(f"backed up to {made}")
            except Exception:
                log("background loop failed:\n" + traceback.format_exc())
            self.stopping.wait(30)


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    roster = cfg.get("creatures") or {}
    tokens = set()
    for cid, r in roster.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", cid):
            sys.exit(f"config: creature id {cid!r} must be lowercase letters, digits or _")
        if r.get("home") not in PLACES:
            sys.exit(f"config: {cid}'s home must be one of {', '.join(PLACES)}")
        if len(r.get("token", "")) < 24 or r["token"] in tokens:
            sys.exit(f"config: {cid} needs its own token of at least 24 characters")
        tokens.add(r["token"])
        r.setdefault("name", cid.capitalize())
    return cfg, roster


def main():
    ap = argparse.ArgumentParser(description="shasta-world server")
    ap.add_argument("--config", help="config.json with host, port, db, backups and creatures")
    ap.add_argument("--test", action="store_true", help="test mode: fixed roster, /v1/_test, no weather or events")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--db")
    args = ap.parse_args()
    if args.test:
        cfg, roster = {}, TEST_ROSTER
    elif args.config:
        cfg, roster = load_config(args.config)
    else:
        ap.error("give --config config.json (or --test)")
    db = args.db or cfg.get("db") or (":memory:" if args.test else "data/world.db")
    if db != ":memory:":
        Path(db).parent.mkdir(parents=True, exist_ok=True)
    world = World(db, roster, test=args.test)
    server = Server(world, args.host or cfg.get("host", "127.0.0.1"), args.port or cfg.get("port", 8642),
                    backups=None if args.test else cfg.get("backups", "data/backups"), background=not args.test)
    server.start()
    log(f"shasta-world {'(TEST MODE) ' if args.test else ''}listening on {server.url}, "
        f"creatures: {', '.join(roster)}, db: {db}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("stopping")
        server.stop()


if __name__ == "__main__":
    main()
