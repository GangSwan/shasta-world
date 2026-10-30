"""Chaos sim: fake birds hammer the real server, then every item must exist exactly once.

    python tests/chaos_sim.py [steps] [seed]

The birds follow PROTOCOL.md's client rules: save the outbox before sending, resend give/drop/take
with the same id until answered, remember saved delivery ids until they're acked. On top of normal
life, the sim throws in bird crashes at every point between save, send and ack, lost replies,
requests that never arrive, server restarts on the same database, birds vanishing for hours, talk
floods, junk input, and two birds grabbing the same item at once.

At the end every bird drains its outbox and deliveries, and the sim checks:
- every item (starting hoards, plus everything humans hid) is in exactly one place;
- the server never answered 5xx, and junk always got a 4xx;
- a resent action id always got the same reply as the first time;
- every piece of graffiti the world said yes to is on its wall exactly once, with its picture kept.
"""
import copy
import json
import random
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract_test import png  # noqa: E402
from world.graffiti import SURFACES  # noqa: E402
from world.server import TEST_ROSTER, Server, World  # noqa: E402
from world.town import PLACES  # noqa: E402

PLACE_IDS = list(PLACES)


class Sim:
    def __init__(self, steps, seed):
        self.steps, self.rng = steps, random.Random(seed)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "world.db")
        self.server = None
        self.stats = dict(restarts=0, crashes=0, lost_replies=0, never_sent=0, resends=0, junk=0, races=0,
                          vanished=0, duplicates=0, gifts=0, takes=0, drops=0, hides=0, says=0, pieces=0, statuses={})
        self.replies = {}           # (bird, id) -> first reply seen
        self.universe = set()       # every item name that exists somewhere
        self.marked = set()         # (bird, mark id) the world said yes to
        self.hidden = 0
        self.start()

    # ---------- server
    def start(self):
        self.server = Server(World(self.db, TEST_ROSTER, test=True, rng=random.Random(self.rng.random())),
                             port=0, background=False).start()

    def restart(self):
        self.server.stop()
        self.start()
        self.stats["restarts"] += 1

    def call(self, method, path, body=None, token=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.server.url + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, obj = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read()
            status, obj = e.code, (json.loads(text) if text else {})
        self.stats["statuses"][status] = self.stats["statuses"].get(status, 0) + 1
        assert status < 500, (path, body, obj)
        return status, obj

    def hook(self, body):
        assert self.call("POST", "/v1/_test", body)[0] == 200

    # ---------- the run
    def run(self):
        self.hook({"reset": True})
        self.birds = [Bird(self, cid) for cid in TEST_ROSTER]
        for b in self.birds:
            b.boot()
        for step in range(self.steps):
            r = self.rng.random()
            if r < 0.01:
                self.restart()
            elif r < 0.03:
                self.junk()
            elif r < 0.04:
                self.race()
            elif r < 0.045:
                self.hook({"advance": self.rng.uniform(3600, 6 * 3600)})
                self.stats["vanished"] += 1
            else:
                self.rng.choice(self.birds).step()
                self.hook({"advance": self.rng.uniform(5, 120)})
        self.finish()

    def junk(self):
        self.stats["junk"] += 1
        tok = self.rng.choice([TEST_ROSTER["bolt"]["token"], "wrong", None])
        cases = [
            ("POST", "/v1/act", None, b"{nope"),
            ("POST", "/v1/act", None, b"[1, 2, 3]"),
            ("POST", "/v1/act", None, b"\xff\xfe\x00junk"),
            ("POST", "/v1/act", None, json.dumps({"x": "y" * 9000}).encode()),
            ("POST", "/v1/act", None, b"[" * 5000 + b"]" * 5000),
            ("POST", "/v1/act", {"id": "x:1", "action": "go", "to": ["lake"]}, None),
            ("POST", "/v1/act", {"id": 7, "action": "say", "text": "hi"}, None),
            ("POST", "/v1/act", {"id": "x:2", "action": "take", "item": "'; DROP TABLE items; --"}, None),
            ("POST", "/v1/act", {"id": "x:3", "action": "say", "text": "\u0000\u0001"}, None),
            ("POST", "/v1/hello", {"place": None, "awake": True}, None),
            ("POST", "/v1/ack", {"deliveries": [{"id": "d1"}]}, None),
            ("GET", "/v1/inbox?after=lots", None, None),
            ("GET", "/../../etc/passwd", None, None),
            ("PUT", "/v1/marks/x:9?place=junkyard&surface=gate&kind=tag", None, b"<svg onload=alert(1)>"),
            ("PUT", "/v1/marks/x:9?place=junkyard&surface=gate", None, png()),
            ("PUT", "/v1/marks/../../etc?place=junkyard&surface=gate&kind=tag", None, png()),
            ("GET", "/marks/../world.db", None, None),
        ]
        method, path, body, raw = self.rng.choice(cases)
        status, obj = self.call(method, path, body, tok, raw)
        assert 400 <= status < 500 or (status == 200 and obj.get("ok") is False), (path, body, raw, status, obj)

    def race(self):
        """Wren hides something by the lake; two birds there grab it at the same moment."""
        self.stats["races"] += 1
        a, b = self.birds[0], self.birds[1]
        for bird in (a, b):
            bird.live["place"] = "lake"
            bird.live["awake"] = True
            bird.save()
            bird.hello()
        name = self.new_human_item()
        _, r = self.call("POST", "/v1/act", {"id": f"wren:hide:{self.hidden}", "action": "hide", "place": "lake",
                                             "item": name, "by": "Caelan"}, TEST_ROSTER["wren"]["token"])
        if not r["ok"]:
            self.universe.discard(name)
            return
        threads = [threading.Thread(target=bird.take, args=(r["item"],)) for bird in (a, b)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

    def new_human_item(self):
        self.hidden += 1
        name = f"human-thing-{self.hidden}"
        self.universe.add(name)
        return name

    def finish(self):
        self.restart()
        for _ in range(3):
            for b in self.birds:
                b.boot(crash_ok=False)
                b.live["awake"] = True
                b.save()
                b.send({"action": "presence", "awake": True}, outbox=False)
                b.collect(crash_ok=False)
        for b in self.birds:
            assert not b.live["outbox"], (b.cid, b.live["outbox"])
            assert not b.live["inflight"], (b.cid, b.live["inflight"])
        self.server.stop()

        con = sqlite3.connect(self.db)
        marks = con.execute("SELECT creature, id, sha, ext, kept FROM marks").fetchall()
        assert {(c, i) for c, i, *_ in marks} == self.marked, "graffiti the world said yes to isn't on its wall exactly once"
        for c, i, sha, ext, kept in marks:
            assert not kept or (Path(self.db).parent / "marks" / f"{sha}.{ext}").is_file(), (c, i)
        self.stats["pieces"] = len(marks)
        world_items = [n for (n,) in con.execute("SELECT name FROM items")]
        pending = [n for (n,) in con.execute("SELECT item FROM deliveries")]
        con.close()
        assert not pending, f"undelivered after drain: {pending}"
        where = {}
        for b in self.birds:
            for name in b.live["hoard"]:
                where.setdefault(name, []).append(b.cid)
        for name in world_items:
            where.setdefault(name, []).append("world")
        doubles = {k: v for k, v in where.items() if len(v) > 1}
        lost = self.universe - set(where)
        extra = set(where) - self.universe
        assert not doubles, f"items in two places: {doubles}"
        assert not lost, f"items lost: {sorted(lost)}"
        assert not extra, f"items from nowhere: {sorted(extra)}"
        self.tmp.cleanup()
        return where


class Bird:
    def __init__(self, sim, cid):
        self.sim, self.cid = sim, cid
        self.token = TEST_ROSTER[cid]["token"]
        hoard = [f"{cid}-thing-{i}" for i in range(6)]
        sim.universe.update(hoard)
        # live is the bird's memory; saved is what's on its disk. A crash throws live away.
        self.live = {"hoard": hoard, "inflight": [], "outbox": [], "got": [], "place": TEST_ROSTER[cid]["home"],
                     "awake": True, "tick": 0}
        self.saved = copy.deepcopy(self.live)
        self.seen = []
        self.answered = []
        self.lock = threading.Lock()

    @property
    def rng(self):
        return self.sim.rng

    def save(self):
        self.saved = copy.deepcopy(self.live)

    def crash(self):
        self.live = copy.deepcopy(self.saved)
        self.sim.stats["crashes"] += 1

    def maybe_crash(self, p=0.03, crash_ok=True):
        if crash_ok and self.rng.random() < p:
            self.crash()
            return True
        return False

    def hello(self):
        _, r = self.sim.call("POST", "/v1/hello", {"place": self.live["place"], "awake": self.live["awake"]}, self.token)
        self.seen = [i["id"] for i in r["items"]]
        return r

    def boot(self, crash_ok=True):
        """Start (or restart after a crash): hello, then resend whatever is in the outbox."""
        self.hello()
        for entry in list(self.live["outbox"]):
            self.sim.stats["resends"] += 1
            self.deliver_entry(entry, crash_ok)

    def new_id(self):
        self.live["tick"] += 1
        return f"{self.cid}:{self.live['tick']}:{self.rng.randrange(10**6)}:1"

    def post_act(self, body):
        if body["action"] == "mark":
            q = f"place={body['place']}&surface={body['surface']}&kind={body['kind']}"
            _, r = self.sim.call("PUT", f"/v1/marks/{body['id']}?{q}", token=self.token, raw=png(64, 64, body["rgb"]))
            if r.get("ok"):
                self.sim.marked.add((self.cid, body["id"]))
        else:
            _, r = self.sim.call("POST", "/v1/act", body, self.token)
        key = (self.cid, body["id"])
        if key in self.sim.replies:
            assert self.sim.replies[key] == r, (key, self.sim.replies[key], r)
        elif r.get("reason") != "id_reused":
            self.sim.replies[key] = r
            self.answered = (self.answered + [body])[-50:]
        return r

    def deliver_entry(self, entry, crash_ok=True):
        """Send one outbox entry; maybe lose it on the way there or back; apply the reply if it came."""
        if crash_ok and self.rng.random() < 0.05:
            self.sim.stats["never_sent"] += 1
            return
        r = self.post_act(entry)
        if crash_ok and self.rng.random() < 0.1:
            self.sim.stats["lost_replies"] += 1
            return
        self.apply(entry, r)
        self.save()

    def apply(self, entry, r):
        if entry not in self.live["outbox"]:
            return
        self.live["outbox"].remove(entry)
        if entry["action"] in ("give", "drop"):
            self.live["inflight"].remove(entry["item"])
            if not r["ok"]:
                self.live["hoard"].append(entry["item"])
        if entry["action"] == "take" and r["ok"]:
            self.sim.stats["takes"] += 1

    def send(self, body, outbox=True, crash_ok=True):
        """Save first, then send (PROTOCOL.md, "When to send")."""
        entry = {"id": self.new_id(), **body}
        if not outbox:
            return self.post_act(entry)
        if body["action"] in ("give", "drop"):
            self.live["hoard"].remove(body["item"])
            self.live["inflight"].append(body["item"])
        self.live["outbox"].append(entry)
        self.save()
        if self.maybe_crash(crash_ok=crash_ok):
            return None
        self.deliver_entry(entry, crash_ok)
        return entry

    def collect(self, crash_ok=True):
        """Save deliveries into the hoard, then ack. Remember saved ids so a lost ack can't double them."""
        _, r = self.sim.call("GET", "/v1/perceive", token=self.token)
        self.seen = [i["id"] for i in r["items"]]
        self.live["place"], self.live["awake"] = r["you"]["place"], r["you"]["awake"]
        shown = [d["id"] for d in r["deliveries"]]
        self.live["got"] = [g for g in self.live["got"] if g in shown]
        new = [d for d in r["deliveries"] if d["id"] not in self.live["got"]]
        if not new and not shown:
            return
        for d in new:
            self.live["hoard"].append(d["item"])
            self.live["got"].append(d["id"])
        if self.maybe_crash(crash_ok=crash_ok):
            return
        self.save()
        if self.maybe_crash(crash_ok=crash_ok):
            return
        s, a = self.sim.call("POST", "/v1/ack", {"deliveries": shown}, self.token)
        if crash_ok and self.rng.random() < 0.1:
            self.sim.stats["lost_replies"] += 1

    def take(self, iid):
        with self.lock:
            self.send({"action": "take", "item": iid}, crash_ok=False)

    def step(self):
        with self.lock:
            if self.rng.random() < 0.05:
                self.crash()
                self.boot()
                return
            self.collect()
            if not self.live["awake"]:
                if self.rng.random() < 0.4:
                    self.live["awake"] = True
                    self.save()
                    self.send({"action": "presence", "awake": True}, outbox=False)
                return
            _, me = self.sim.call("GET", "/v1/perceive", token=self.token)
            here = [h["id"] for h in me["here"]]
            items = [i for i in me["items"] if not i["for"] or i["for"] == self.cid]
            roll = self.rng.random()
            hoard = self.live["hoard"]
            if roll < 0.15:
                to = self.rng.choice(PLACE_IDS + ["mars"])
                r = self.send({"action": "go", "to": to}, outbox=False)
                if r["ok"]:
                    self.live["place"] = to
                    self.save()
            elif roll < 0.35:
                for _ in range(self.rng.choice([1, 1, 1, 3, 6])):
                    body = {"action": "say", "text": self.rng.choice(["Caw.", "Hey.", "That's mine.", "Nice weather."])}
                    if here and self.rng.random() < 0.5:
                        body["to"] = self.rng.choice(here)
                    if self.send(body, outbox=False)["ok"]:
                        self.sim.stats["says"] += 1
            elif roll < 0.45 and here and hoard:
                if self.send({"action": "give", "to": self.rng.choice(here), "item": self.rng.choice(hoard)}):
                    self.sim.stats["gifts"] += 1
            elif roll < 0.55 and hoard:
                if self.send({"action": "drop", "item": self.rng.choice(hoard)}):
                    self.sim.stats["drops"] += 1
            elif roll < 0.7 and items:
                self.send({"action": "take", "item": self.rng.choice(items)["id"]})
            elif roll < 0.78:
                body = {"action": "hide", "place": self.rng.choice(PLACE_IDS), "item": self.sim.new_human_item(),
                        "by": self.rng.choice(["Caelan", "Elliott"])}
                if self.rng.random() < 0.5:
                    body["for"] = self.rng.choice(list(TEST_ROSTER))
                r = self.send(body, outbox=False)
                if r["ok"]:
                    self.sim.stats["hides"] += 1
                else:
                    self.sim.universe.discard(body["item"])
            elif roll < 0.84:
                self.send({"action": self.rng.choice(["sound", "doing"]), "text": "a loud caw"}, outbox=False)
            elif roll < 0.88:
                self.send({"action": "human_spoke", "by": "Caelan"}, outbox=False)
            elif roll < 0.92:
                self.live["awake"] = False
                self.save()
                self.send({"action": "presence", "awake": False}, outbox=False)
            elif roll < 0.95:
                place = self.rng.choice(list(SURFACES))
                self.send({"action": "mark", "place": place, "surface": self.rng.choice(list(SURFACES[place])),
                           "kind": self.rng.choice(["tag", "throwup", "mural"]),
                           "rgb": [self.rng.randrange(256) for _ in range(3)]})
            elif self.answered:
                self.post_act(self.rng.choice(self.answered))   # a stray duplicate: must replay the same reply
                self.sim.stats["duplicates"] += 1


def main():
    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    sim = Sim(steps, seed)
    sim.run()
    s = sim.stats
    print(f"OK: {steps} steps, seed {seed}: {s['restarts']} server restarts, {s['crashes']} bird crashes, "
          f"{s['lost_replies']} lost replies, {s['never_sent']} never sent, {s['resends']} resends, "
          f"{s['vanished']} vanishings, {s['duplicates']} duplicate sends, {s['junk']} junk requests, {s['races']} races")
    print(f"    {s['gifts']} gifts, {s['takes']} takes, {s['drops']} drops, {s['hides']} hides, {s['says']} lines said, "
          f"{s['pieces']} pieces of graffiti; "
          f"{len(sim.universe)} items, each in exactly one place; statuses {dict(sorted(s['statuses'].items()))}")


if __name__ == "__main__":
    main()
