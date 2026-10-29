"""Phase 3's "done when": two fake creatures live a full day through world_client.py.

    python tests/client_day.py [seed]

288 ticks of 5 minutes each. The world goes away twice (an hour each); while it's away the creatures
keep living in their own town. They crash at every point of a tick: before saving, after saving
but before sending, after sending but before saving the replies, and before the ack. At the end
every item must be in exactly one place, no outbox may be left, and no tick may have stalled.
"""
import copy
import json
import random
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "client"))
from world.server import TEST_ROSTER, Server, World  # noqa: E402
from world.town import PLACES  # noqa: E402
from world_client import WorldClient  # noqa: E402

TICKS = 288
TIMEOUT = 1.0
OUTAGES = [(60, 72), (200, 212)]        # ticks when the world is down
CRASH = 0.04                            # chance of a crash at each crash point


class Creature:
    def __init__(self, day, cid):
        self.day, self.cid, self.rng = day, cid, day.rng
        hoard = [f"{cid}-thing-{i}" for i in range(8)]
        day.universe.update(hoard)
        self.state = {"hoard": hoard, "place": TEST_ROSTER[cid]["home"], "awake": True, "world": {}}
        self.disk = copy.deepcopy(self.state)
        self.wc = self.new_client()
        self.crashes = 0

    def new_client(self):
        return WorldClient(self.day.url, TEST_ROSTER[self.cid]["token"], self.cid, timeout=TIMEOUT, retry=(0, 0))

    def save(self):
        self.disk = copy.deepcopy(self.state)

    def crash_point(self):
        if self.rng.random() < CRASH:
            self.state = copy.deepcopy(self.disk)      # lose everything since the last save
            self.wc = self.new_client()                # a fresh process
            self.crashes += 1
            return True
        return False

    def tick(self, others):
        s = copy.deepcopy(self.state)                  # the tick works on a copy, all or nothing
        hidden = []                                    # a human's !leave, relayed in this tick
        ws, wc = s["world"], self.wc
        seen = wc.look(ws, s["place"], s["awake"])
        for d in wc.new_deliveries(ws, seen):
            s["hoard"].append(d["item"])
        wc.heard(ws)
        here = [h["id"] for h in seen["here"] if h["state"] == "awake"] if seen else []
        items = [i for i in seen["items"] if i["for"] in (None, self.cid)] if seen else []
        roll = self.rng.random()
        if roll < 0.2:
            to = self.rng.choice(list(PLACES))
            if seen and to in seen["cant_go"]:
                to = s["place"]
            s["place"] = to                            # move in our own town either way
            wc.queue(ws, "go", to=to)
        elif roll < 0.35 and s["hoard"] and (here or not seen):
            item = self.rng.choice(s["hoard"])
            s["hoard"].remove(item)                    # in flight until the world answers
            wc.queue(ws, "give", to=self.rng.choice(here or others), item=item)
        elif roll < 0.45 and s["hoard"]:
            item = self.rng.choice(s["hoard"])
            s["hoard"].remove(item)
            wc.queue(ws, "drop", item=item)
        elif roll < 0.65 and items:
            wc.queue(ws, "take", item=self.rng.choice(items)["id"])
        elif roll < 0.75:
            name = self.day.new_item()
            hidden.append(name)
            wc.queue(ws, "hide", place=self.rng.choice(list(PLACES)), item=name, by="Caelan",
                     for_=self.rng.choice([None, self.cid] + others))
        elif here:
            wc.queue(ws, "say", text="Caw.", to=self.rng.choice(here))
        if self.crash_point():                         # crashed mid-tick: nothing saved, nothing sent
            return
        self.state = s
        self.save()
        self.day.universe.update(hidden)               # it exists once the tick that relays it is saved
        if self.crash_point():                         # saved, crashed before sending
            return
        for body, reply in wc.send(ws):
            if body["action"] in ("give", "drop") and not reply["ok"]:
                s["hoard"].append(body["item"])
            if body["action"] == "hide" and not reply["ok"]:
                self.day.universe.discard(body["item"])
            if body["action"] == "go" and not reply["ok"] and seen:
                s["place"] = seen["you"]["place"]
        if self.crash_point():                         # sent, crashed before saving the replies
            return
        self.save()
        if self.crash_point():                         # saved, crashed before the ack
            return
        wc.ack(ws)


class Day:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "world.db")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = None
        self.universe, self.hidden = set(), 0
        self.slowest = 0.0

    def up(self):
        self.server = Server(World(self.db, TEST_ROSTER, test=True), port=self.port, background=False).start()

    def down(self):
        self.server.stop()
        self.server = None

    def advance(self, seconds):
        if self.server:
            req = urllib.request.Request(self.url + "/v1/_test", data=json.dumps({"advance": seconds}).encode(),
                                         method="POST")
            urllib.request.urlopen(req, timeout=5).read()

    def new_item(self):
        self.hidden += 1
        return f"human-thing-{self.hidden}"

    def run(self):
        self.up()
        birds = [Creature(self, cid) for cid in ("bolt", "spike")]
        away_ticks = 0
        for t in range(TICKS):
            down = any(a <= t < b for a, b in OUTAGES)
            if down and self.server:
                self.down()
            if not down and not self.server:
                self.up()
            away_ticks += down
            for b in birds:
                start = time.time()
                b.tick([o.cid for o in birds if o is not b])
                self.slowest = max(self.slowest, time.time() - start)
            self.advance(300)
        # evening: everyone settles up with the world, no more crashes
        for _ in range(3):
            for b in birds:
                ws = b.state["world"]
                seen = b.wc.look(ws, b.state["place"], True)
                for d in b.wc.new_deliveries(ws, seen):
                    b.state["hoard"].append(d["item"])
                for body, reply in b.wc.send(ws):
                    if body["action"] in ("give", "drop") and not reply["ok"]:
                        b.state["hoard"].append(body["item"])
                b.save()
                b.wc.ack(ws)
        self.down()
        self.check(birds, away_ticks)

    def check(self, birds, away_ticks):
        for b in birds:
            assert not b.state["world"].get("outbox"), (b.cid, b.state["world"]["outbox"])
        con = sqlite3.connect(self.db)
        in_world = [n for (n,) in con.execute("SELECT name FROM items")]
        pending = [n for (n,) in con.execute("SELECT item FROM deliveries")]
        con.close()
        assert not pending, f"never collected: {pending}"
        where = {}
        for b in birds:
            for name in b.state["hoard"]:
                where.setdefault(name, []).append(b.cid)
        for name in in_world:
            where.setdefault(name, []).append("world")
        doubles = {k: v for k, v in where.items() if len(v) > 1}
        assert not doubles, f"items in two places: {doubles}"
        assert self.universe == set(where), (f"lost: {sorted(self.universe - set(where))}, "
                                             f"from nowhere: {sorted(set(where) - self.universe)}")
        assert self.slowest < 3 * TIMEOUT, f"a tick stalled for {self.slowest:.1f} s"
        self.tmp.cleanup()
        crashes = sum(b.crashes for b in birds)
        print(f"OK: {TICKS} ticks for {len(birds)} creatures, {away_ticks} ticks with the world away, "
              f"{crashes} crashes; {len(self.universe)} items, each in exactly one place "
              f"({len(in_world)} in the world); slowest tick {self.slowest:.2f} s")


if __name__ == "__main__":
    Day(int(sys.argv[1]) if len(sys.argv) > 1 else 1).run()
