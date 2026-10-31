"""The map page and GET /v1/map: read-only, no token, and it shows only what anyone standing there could see.

    python tests/map_test.py

Runs the real server in test mode, in-process. The map is for people, not creatures, so the fake world
doesn't implement it and the contract tests don't cover it.
"""
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract_test import png  # noqa: E402
from world.server import TEST_ROSTER, Server, World  # noqa: E402

SERVER = URL = None


def setUpModule():
    global SERVER, URL
    SERVER = Server(World(":memory:", TEST_ROSTER, test=True), port=0, background=False).start()
    URL = SERVER.url


def tearDownModule():
    SERVER.stop()


def call(method, path, body=None, who=None):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if who:
        req.add_header("Authorization", f"Bearer test-{who}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def town():
    s, ctype, raw = call("GET", "/v1/map")
    assert s == 200 and ctype == "application/json", (s, ctype)
    return json.loads(raw), raw.decode()


class Map(unittest.TestCase):
    n = 0

    def setUp(self):
        call("POST", "/v1/_test", {"reset": True})

    def act(self, who, action, **f):
        Map.n += 1
        s, _, raw = call("POST", "/v1/act", {"id": f"{who}:{Map.n}", "action": action, **f}, who)
        self.assertEqual(s, 200, raw)
        return json.loads(raw)

    def hello(self, who, place, awake=True):
        s, _, raw = call("POST", "/v1/hello", {"place": place, "awake": awake}, who)
        self.assertEqual(s, 200, raw)

    def test_the_page(self):
        for path in ("/", "/map"):
            s, ctype, raw = call("GET", path)
            self.assertEqual((s, ctype), (200, "text/html; charset=utf-8"))
            self.assertIn(b"<canvas", raw)
            self.assertIn(b'fetch("/v1/map"', raw)

    def test_no_token_needed_and_only_get(self):
        s, _, _ = call("GET", "/v1/map")
        self.assertEqual(s, 200)
        s, _, _ = call("POST", "/v1/map", {})
        self.assertEqual(s, 404)

    def test_shows_the_town(self):
        call("POST", "/v1/_test", {"weather": {"kind": "snow", "temp_f": 28, "wind_mph": 10}})
        m, _ = town()
        self.assertEqual(m["weather"]["kind"], "snow")
        self.assertIn("fir", m["places"])
        for k in ("now", "hour", "part", "season"):
            self.assertIn(k, m)
        self.assertEqual({c["id"]: c["seen"] for c in m["creatures"]}, {"bolt": None, "spike": None, "wren": None},
                         "nobody has visited yet")

    def test_where_everyone_is(self):
        self.hello("bolt", "junkyard")
        self.hello("spike", "fir", awake=False)
        self.act("bolt", "doing", text="scavenging")
        m, _ = town()
        c = {x["id"]: x for x in m["creatures"]}
        self.assertEqual((c["bolt"]["place"], c["bolt"]["state"], c["bolt"]["doing"]), ("junkyard", "awake", "scavenging"))
        self.assertEqual((c["spike"]["place"], c["spike"]["state"]), ("fir", "asleep"))
        self.assertIsNone(c["wren"]["seen"])

    def test_looking_never_checks_anyone_in(self):
        self.hello("bolt", "lake")
        call("POST", "/v1/_test", {"advance": 700})
        for _ in range(3):
            m, _ = town()
        bolt = next(x for x in m["creatures"] if x["id"] == "bolt")
        self.assertEqual(bolt["state"], "quiet", "the map watching him doesn't count as him being there")
        self.assertGreaterEqual(bolt["seen"], 700)

    def test_talk_shows_once_however_many_heard_it(self):
        for who in ("bolt", "spike", "wren"):
            self.hello(who, "lake")
        self.act("bolt", "say", text="Who left a bobber here?", to="spike")
        self.act("spike", "say", text="Not me.")
        m, _ = town()
        self.assertEqual([(t["name"], t["to"], t["text"]) for t in m["talk"]],
                         [("Spike", None, "Not me."), ("Bolt", "Spike", "Who left a bobber here?")])
        self.assertTrue(all(t["place"] == "lake" for t in m["talk"]))

    def test_items_and_events_but_never_deliveries_or_tokens(self):
        self.hello("bolt", "junkyard")
        self.hello("spike", "junkyard")
        self.act("bolt", "drop", item="bent spark plug")
        self.act("spike", "hide", place="lake", item="red bobber", by="Elliott", **{"for": "spike"})
        self.act("bolt", "give", to="spike", item="chrome emblem")
        m, raw = town()
        self.assertIn({"name": "bent spark plug", "place": "junkyard", "by": "Bolt", "for": None}, m["items"])
        self.assertIn({"name": "red bobber", "place": "lake", "by": "Elliott", "for": "Spike"}, m["items"])
        self.assertTrue(any("gave Spike a chrome emblem" in e["text"] for e in m["events"]))
        self.assertNotIn("deliveries", m)
        for secret in ("test-bolt", "test-spike", "test-wren", "token"):
            self.assertNotIn(secret, raw)

    def test_talk_and_events_are_capped(self):
        for who in ("bolt", "spike"):
            self.hello(who, "lake")
        for i in range(20):
            call("POST", "/v1/_test", {"advance": 40})
            self.act("bolt", "human_spoke", by="Elliott")       # keeps the 8-line creature-only break away
            self.assertTrue(self.act("bolt" if i % 2 else "spike", "say", text=f"line {i}")["ok"])
        m, _ = town()
        self.assertEqual(len(m["talk"]), 12)
        self.assertEqual(m["talk"][0]["text"], "line 19", "newest first")
        self.assertLessEqual(len(m["events"]), 30)


class Walls(Map):
    def put(self, who, mid, data, **q):
        req = urllib.request.Request(f"{URL}/v1/marks/{mid}?{urlencode(q)}", data=data, method="PUT")
        req.add_header("Authorization", f"Bearer test-{who}")
        req.add_header("Content-Type", "image/png")
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.loads(r.read())
        self.assertTrue(out["ok"], out)
        return out

    def test_tagging_now_shows_on_the_map(self):
        self.hello("spike", "rail_yard")
        m, _ = town()
        self.assertIsNone(m["creatures"][1]["tagging"])
        self.act("spike", "tag", surface="water_tower", kind="mural")
        m, _ = town()
        spike = next(c for c in m["creatures"] if c["id"] == "spike")
        self.assertEqual(spike["tagging"], {"place": "rail_yard", "surface": "water_tower", "name": "the water tower"})
        self.put("spike", "m1", png(), place="rail_yard", surface="water_tower", kind="mural", title="Ring Day")
        m, _ = town()
        self.assertIsNone(next(c for c in m["creatures"] if c["id"] == "spike")["tagging"])

    def test_only_the_top_piece_per_wall(self):
        self.put("spike", "a", png(rgb=(1, 1, 1)), place="junkyard", surface="gate", kind="tag")
        self.put("spike", "b", png(rgb=(2, 2, 2)), place="junkyard", surface="gate", kind="throwup", over="a", title="Two")
        m, _ = town()
        self.assertEqual([(x["id"], x["place"], x["surface"]) for x in m["marks"]],
                         [("b", "junkyard", "gate")])
        mk = m["marks"][0]
        self.assertEqual((mk["by"], mk["title"], mk["surface_name"], mk["kind"]), ("Spike", "Two", "the junkyard gate", "throwup"))

    def test_pictures_are_served_safely(self):
        data = png(rgb=(9, 9, 9))
        self.put("spike", "p", data, place="shed", surface="door", kind="tag")
        img = town()[0]["marks"][0]["img"]
        req = urllib.request.Request(URL + img)
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.read(), data)
            self.assertEqual(r.headers["Content-Type"], "image/png")
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("immutable", r.headers["Cache-Control"])
        for path in ("/marks/" + "0" * 64 + ".png", "/marks/../world.db", "/marks/%2e%2e/x.png", img[:-3] + "svg"):
            self.assertEqual(call("GET", path)[0], 404, path)

    def test_painted_over_pictures_are_deleted_after_five(self):
        imgs, prev = [], None
        for i in range(6):
            if i == 4:
                call("POST", "/v1/_test", {"advance": 86400})
            self.put("spike", f"k{i}", png(rgb=(i, 50, 50)), place="old_barn", surface="silo", kind="tag",
                     **({"over": prev} if prev else {}))
            prev = f"k{i}"
            imgs.append(town()[0]["marks"][0]["img"])
        self.assertEqual(call("GET", imgs[0])[0], 404, "the oldest picture is gone")
        self.assertEqual(call("GET", imgs[1])[0], 200)
        s, _, raw = call("GET", "/v1/marks?place=old_barn")
        silo = next(w for w in json.loads(raw)["walls"] if w["surface"] == "silo")
        self.assertIsNone(silo["pieces"][-1]["img"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
