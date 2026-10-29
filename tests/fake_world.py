"""A small in-memory shasta-world that passes the contract tests (PROTOCOL.md v0.1).

It exists to pin the protocol down before the real server (phase 2) is written. Stdlib only.
Run it by hand with:  python tests/fake_world.py [port]   (always in test mode)
"""
import json
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PLACES = {
    "nest": "Bolt's nest", "junkyard": "the junkyard", "rail_yard": "the rail yard", "creek": "the creek",
    "garage": "the garage", "diner_roof": "the diner roof", "ambulance_station": "the ambulance station",
    "drift_lot": "the drift lot", "lake": "the lake", "shed": "the shed", "mountain_trail": "the mountain trail",
    "summit": "the summit", "old_barn": "the old barn", "machine_shop": "the machine shop", "fir": "Spike's fir",
}
TEST_ROSTER = {   # id: (display name, token, home)
    "bolt": ("Bolt", "test-bolt", "nest"),
    "spike": ("Spike", "test-spike", "fir"),
    "wren": ("Wren", "test-wren", "lake"),
}
RULES = ["The summit is only reachable from the mountain_trail.", "The summit is closed in a thunderstorm."]

MAX_BODY = 8192
QUIET_AFTER = 600
BURST = 30
BREAK_AFTER, BREAK_FOR = 8, 1800
SAY_PER_DAY, GIVES_PER_DAY, HIDES_PER_DAY = 60, 3, 10
DEEDS, DEED_WINDOW = 6, 300
FLOOD, FLOOD_WINDOW = 30, 300
DOING_FOR = 900
HEARD_KEEP, IDS_KEEP = 86400, 30 * 86400
SUMMARY_CHARS, SUMMARY_LINES = 700, 8

ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,64}$")
DEED_ACTIONS = {"go", "sound", "doing", "give", "take", "drop"}
AWAKE_ONLY = DEED_ACTIONS | {"say"}

# action: {field: (type, required, max length or None)}
TEXT, NAME, PLACE, WHO, BOOL = "text", "name", "place", "who", "bool"
ACTIONS = {
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


class BadRequest(Exception):
    pass


def pacific(ts):
    """Local time in America/Los_Angeles without tzdata (US rules since 2007)."""
    utc = datetime.fromtimestamp(ts, timezone.utc)

    def nth_sunday(month, n):
        d = date(utc.year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
        return d

    start = datetime.combine(nth_sunday(3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=10)
    end = datetime.combine(nth_sunday(11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=9)
    hours = -7 if start <= utc < end else -8
    return utc.astimezone(timezone(timedelta(hours=hours)))


def clean(text):
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()


def a_or_an(thing):
    return ("an " if thing[:1].lower() in "aeiou" else "a ") + thing


class World:
    def __init__(self, roster=TEST_ROSTER, test=True):
        self.roster, self.test = roster, test
        self.lock = threading.Lock()
        self.offset = 0.0
        self.reset()

    def reset(self):
        self.offset = 0.0
        self.weather = {"kind": "clear", "temp_f": 55, "wind_mph": 3}
        self.creatures = {cid: {"id": cid, "name": n, "home": h, "place": h, "awake": True, "seen": None,
                                "doing": None, "doing_until": 0, "day": None, "said": 0, "gifts": 0, "hides": 0,
                                "streak": 0, "break_until": 0, "last_say": None, "deeds": [],
                                "flood": {"presence": [], "human_spoke": []}}
                          for cid, (n, _, h) in self.roster.items()}
        self.items, self.next_item = {}, 1
        self.deliveries, self.next_delivery = {cid: [] for cid in self.roster}, 1
        self.events = {p: [] for p in PLACES}
        self.inbox, self.seq = {cid: [] for cid in self.roster}, 0
        self.done = {}

    def now(self):
        return time.time() + self.offset

    # ---------- bookkeeping
    def _quiet(self, c, t):
        return c["seen"] is None or t - c["seen"] > QUIET_AFTER

    def _state(self, c, t):
        return "quiet" if self._quiet(c, t) else "awake" if c["awake"] else "asleep"

    def _event(self, place, text):
        self.events[place] = (self.events[place] + [(self.now(), text)])[-20:]

    def _deliver(self, cid, item, frm, how):
        d = {"id": f"d{self.next_delivery}", "item": item, "from": frm, "how": how}
        self.next_delivery += 1
        self.deliveries[cid].append(d)
        return d

    def _sweep(self, c):
        """Items left for this creature move into its deliveries once it's there and awake."""
        if not c["awake"]:
            return
        for iid, it in list(self.items.items()):
            if it["for"] == c["id"] and it["place"] == c["place"]:
                del self.items[iid]
                self._deliver(c["id"], it["name"], it["by"], f"{it['by']} left it for you at {PLACES[it['place']]}")

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
        self.inbox[c["id"]] = [h for h in self.inbox[c["id"]] if t - h["t"] < HEARD_KEEP]
        self.done = {k: v for k, v in self.done.items() if t - v[2] < IDS_KEEP}

    # ---------- what you see
    def _cant_go(self, c):
        out = {}
        if c["place"] != "summit":
            if c["place"] != "mountain_trail":
                out["summit"] = "You have to climb the mountain_trail first."
            elif self.weather["kind"] == "thunderstorm":
                out["summit"] = "The summit is closed: lightning is cracking over the peak."
        return out

    def _talk(self, c, t):
        return {"left_today": max(0, SAY_PER_DAY - c["said"]),
                "break_until": pacific(c["break_until"]).isoformat(timespec="seconds") if c["break_until"] > t else None}

    def _summary(self, c, t, here, items, recent, deliveries, around=()):
        lt = pacific(t)
        part = self._part(lt.hour)
        w = self.weather
        lines = [f"{part.capitalize()} at {PLACES[c['place']]}, {w['kind']} and {w['temp_f']}°F."]
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
            bits = [a_or_an(i["name"]) + (f" ({i['by']} left it for {self.creatures[i['for']]['name']})" if i["for"] else "")
                    for i in items[:5]]
            lines.append("On the ground: " + ", ".join(bits) + ("." if len(items) <= 5 else ", and more."))
        talk = self._talk(c, t)
        if talk["break_until"]:
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

    @staticmethod
    def _part(hour):
        return "night" if hour < 6 or hour >= 21 else "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"

    def perceive(self, c, t):
        lt = pacific(t)
        m = lt.month
        season = "winter" if m in (12, 1, 2) else "spring" if m in (3, 4, 5) else "summer" if m in (6, 7, 8) else "fall"
        here = [{"id": o["id"], "name": o["name"], "state": self._state(o, t), "doing": o["doing"]}
                for o in self.creatures.values()
                if o is not c and o["seen"] is not None and o["place"] == c["place"]]
        around = [{"id": o["id"], "name": o["name"], "place": o["place"], "state": self._state(o, t)}
                  for o in self.creatures.values() if o is not c and o["seen"] is not None and o["place"] != c["place"]]
        items = [{"id": i["id"], "name": i["name"], "by": i["by"], "for": i["for"]}
                 for i in self.items.values() if i["place"] == c["place"]]
        recent = [e for _, e in self.events[c["place"]][-5:]]
        deliveries = list(self.deliveries[c["id"]])
        return {"ok": True, "now": lt.isoformat(timespec="seconds"), "part": self._part(lt.hour), "season": season,
                "weather": dict(self.weather),
                "you": {"id": c["id"], "place": c["place"], "awake": c["awake"]},
                "place": {"id": c["place"], "name": PLACES[c["place"]]},
                "here": here, "around": around, "items": items, "recent": recent, "cant_go": self._cant_go(c),
                "talk": self._talk(c, t), "deliveries": deliveries,
                "summary": self._summary(c, t, here, items, recent, deliveries, around)}

    # ---------- requests
    def handle(self, method, path, query, token, body):
        """Returns (status, reply dict). body is the parsed JSON object (or None for GET)."""
        with self.lock:
            if path == "/v1/_test" and self.test and method == "POST":
                return self._test_hook(body)
            routes = {("GET", "/v1/town"), ("POST", "/v1/hello"), ("GET", "/v1/perceive"), ("POST", "/v1/act"),
                      ("GET", "/v1/inbox"), ("POST", "/v1/ack")}
            if (method, path) not in routes:
                return 404, {"ok": False, "reason": "not_found", "message": "No such endpoint."}
            c = next((self.creatures[cid] for cid, r in self.roster.items() if token and r[1] == token), None)
            if c is None:
                return 401, {"ok": False, "reason": "unauthorized", "message": "Unknown or missing token."}
            if method == "POST" and not isinstance(body, dict):
                raise BadRequest("The body must be a JSON object.")
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

    def _test_hook(self, body):
        if not isinstance(body, dict):
            raise BadRequest("The body must be a JSON object.")
        if body.get("reset"):
            self.reset()
        if "advance" in body:
            self.offset += float(body["advance"])
        if "weather" in body:
            self.weather = dict(body["weather"])
        if "event" in body:
            self._event(body["event"]["place"], body["event"]["text"])
        return 200, {"ok": True, "now": pacific(self.now()).isoformat(timespec="seconds")}

    def town(self):
        homes = {c["home"]: cid for cid, c in self.creatures.items()}
        return {"ok": True,
                "places": [{"id": p, "name": n, "home_of": homes.get(p)} for p, n in PLACES.items()],
                "creatures": [{"id": c["id"], "name": c["name"], "home": c["home"]} for c in self.creatures.values()],
                "rules": list(RULES)}

    def read_inbox(self, c, query):
        try:
            after = int((query.get("after") or ["0"])[0])
        except ValueError:
            raise BadRequest("after must be a number.")
        heard = [{k: v for k, v in h.items() if k != "t"} for h in self.inbox[c["id"]] if h["seq"] > after]
        return {"ok": True, "heard": heard, "deliveries": list(self.deliveries[c["id"]]),
                "next": heard[-1]["seq"] if heard else after}

    def ack(self, c, body):
        if set(body) != {"deliveries"} or not isinstance(body["deliveries"], list) \
                or not all(isinstance(x, str) for x in body["deliveries"]) or len(body["deliveries"]) > 100:
            raise BadRequest("Send {\"deliveries\": [ids]}.")
        ids = set(body["deliveries"])
        acked = [d["id"] for d in self.deliveries[c["id"]] if d["id"] in ids]
        self.deliveries[c["id"]] = [d for d in self.deliveries[c["id"]] if d["id"] not in ids]
        return {"ok": True, "acked": acked}

    def hello(self, c, t, body):
        if set(body) != {"place", "awake"}:
            raise BadRequest("hello takes exactly place and awake.")
        if body["place"] not in PLACES or not isinstance(body["awake"], bool):
            raise BadRequest("Unknown place, or awake isn't true/false.")
        if body["place"] != c["place"]:
            self._event(c["place"], f"{c['name']} left.")
            c["place"] = body["place"]
            c["doing"] = None
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
        key = (c["id"], body["id"])
        canon = json.dumps(body, sort_keys=True)
        if key in self.done:
            prev_canon, reply, _ = self.done[key]
            if prev_canon != canon:
                return 409, {"ok": False, "id": body["id"], "reason": "id_reused",
                             "message": "That action id was already used for a different action."}
            return 200, reply
        reply = self._do(c, t, body["action"], f)
        reply = {"ok": reply[0], "id": body["id"], **reply[1]}
        self.done[key] = (canon, reply, t)
        return 200, reply

    def _do(self, c, t, action, f):
        def no(reason, message):
            return False, {"reason": reason, "message": message}

        if action in AWAKE_ONLY and not c["awake"]:
            return no("asleep", "You're asleep.")
        if action in ("give", "say") and f.get("to") == c["id"]:
            raise BadRequest("You can't do that to yourself.")
        if action in ("give", "say", "hide") and f.get("to" if action != "hide" else "for") \
                and f["to" if action != "hide" else "for"] not in self.creatures:
            if action == "hide":
                return no("unknown_creature", "There's no creature by that name.")
            return no("not_here", "Nobody by that name is here.")
        if action in DEED_ACTIONS:
            if len(c["deeds"]) >= DEEDS:
                return no("limit", "You've done a lot just now. Wait a few minutes.")
        if action in c["flood"]:
            if len(c["flood"][action]) >= FLOOD:
                return no("limit", "Too many of those just now.")
            c["flood"][action].append(t)

        here = c["place"]
        ok = self._act_handlers[action](self, c, t, f, here)
        if ok[0] and action in DEED_ACTIONS:
            c["deeds"].append(t)
        return ok

    # each returns (ok, fields)
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
            o = self.creatures[to]
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
        listeners = [o for o in self.creatures.values()
                     if o is not c and o["place"] == here and o["awake"] and not self._quiet(o, t)]
        for o in listeners:
            self.seq += 1
            self.inbox[o["id"]].append({"seq": self.seq, "from": c["id"], "name": c["name"], "to": to,
                                        "text": f["text"], "place": here,
                                        "at": pacific(t).isoformat(timespec="seconds"), "t": t})
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
        o = self.creatures[f["to"]]
        if o["place"] != here:
            return False, {"reason": "not_here", "message": f"{o['name']} isn't here."}
        if c["gifts"] >= GIVES_PER_DAY:
            return False, {"reason": "limit", "message": "You've given enough gifts for today."}
        c["gifts"] += 1
        d = self._deliver(o["id"], f["item"], c["name"], f"a gift from {c['name']}")
        self._event(here, f"{c['name']} gave {o['name']} {a_or_an(f['item'])}.")
        return True, {"result": f"You gave {o['name']} {a_or_an(f['item'])}.", "delivery": d["id"]}

    def _take(self, c, t, f, here):
        it = self.items.get(f["item"])
        if it is None:
            return False, {"reason": "gone", "message": "It's gone. Someone got there first."}
        if it["place"] != here:
            return False, {"reason": "not_here", "message": f"That's at {PLACES[it['place']]}, not here."}
        if it["for"] and it["for"] != c["id"]:
            return False, {"reason": "reserved",
                           "message": f"That was left for {self.creatures[it['for']]['name']}."}
        del self.items[it["id"]]
        d = self._deliver(c["id"], it["name"], it["by"], f"you picked it up at {PLACES[here]}")
        self._event(here, f"{c['name']} picked up the {it['name']}.")
        return True, {"result": f"You picked up the {it['name']}.", "delivery": d["id"]}

    def _new_item(self, place, name, by, for_):
        iid = f"i{self.next_item}"
        self.next_item += 1
        self.items[iid] = {"id": iid, "name": name, "place": place, "by": by, "for": for_}
        return iid

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
        for o in self.creatures.values():
            if o["id"] == f["for"]:
                self._sweep(o)
        return True, {"result": f"{f['by']}'s {f['item']} is now at {PLACES[place]}.", "item": iid}

    def _presence(self, c, t, f, here):
        c["awake"] = f["awake"]
        self._sweep(c)
        return True, {"result": "You're awake." if c["awake"] else "You're asleep."}

    def _human_spoke(self, c, t, f, here):
        for o in self.creatures.values():
            if o is c or o["place"] == here:
                o["streak"], o["break_until"] = 0, 0
        return True, {"result": "Noted."}

    _act_handlers = {"go": _go, "say": _say, "sound": _sound, "doing": _doing, "give": _give, "take": _take,
                     "drop": _drop, "hide": _hide, "presence": _presence, "human_spoke": _human_spoke}


def make_handler(world):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _reply(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _go(self, method):
            u = urlparse(self.path)
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    self.close_connection = True
                    return self._reply(413, {"ok": False, "reason": "too_big", "message": "The body is over 8 KB."})
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._reply(400, {"ok": False, "reason": "bad_request", "message": "That isn't valid JSON."})
            try:
                status, obj = world.handle(method, u.path, parse_qs(u.query), token, body)
            except BadRequest as e:
                status, obj = 400, {"ok": False, "reason": "bad_request", "message": str(e)}
            self._reply(status, obj)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    return Handler


def serve(port=0, host="127.0.0.1"):
    """Start a fake world in a background thread. Returns (server, base_url)."""
    world = World()
    server = ThreadingHTTPServer((host, port), make_handler(world))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


if __name__ == "__main__":
    srv, url = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8642)
    print(f"fake shasta-world (test mode) at {url}  - Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
