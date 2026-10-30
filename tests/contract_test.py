"""Contract tests: every rule in PROTOCOL.md v0.2, checked over real HTTP.

    python tests/contract_test.py                 # against the real server (world/server.py), in-process
    WORLD_IMPL=fake python tests/contract_test.py # against the in-memory fake world
    WORLD_URL=http://host:8642 python tests/contract_test.py   # against a server started in test mode

The server must be in test mode (POST /v1/_test), with the roster bolt / spike / wren.
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
import struct
import zlib
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

URL = os.environ.get("WORLD_URL")
STOP = None
TOKENS = {"bolt": "test-bolt", "spike": "test-spike", "wren": "test-wren"}


def setUpModule():
    global URL, STOP
    if URL:
        return
    if os.environ.get("WORLD_IMPL") == "fake":
        import fake_world
        server, URL = fake_world.serve()
        STOP = server.shutdown
    else:
        from world.server import TEST_ROSTER, Server, World
        server = Server(World(":memory:", TEST_ROSTER, test=True), port=0, background=False).start()
        URL, STOP = server.url, server.stop


def tearDownModule():
    if STOP:
        STOP()


def call(method, path, body=None, who=None, token=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL.rstrip("/") + path, data=data, method=method)
    tok = token if token is not None else TOKENS.get(who)
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        text = e.read()
        return e.code, json.loads(text) if text else {}


def png(w=400, h=300, rgb=(200, 60, 40)):
    """A real PNG of one color (tiny once compressed)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def jpeg_header(w, h):
    """Just enough of a JPEG for the size check: SOI, an APP0 segment, then SOF0 with the size."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


class Base(unittest.TestCase):
    n = 0

    def setUp(self):
        self.hook({"reset": True})

    def hook(self, body):
        s, r = call("POST", "/v1/_test", body)
        self.assertEqual(s, 200, r)
        return r

    def hello(self, who, place, awake=True):
        s, r = call("POST", "/v1/hello", {"place": place, "awake": awake}, who)
        self.assertEqual(s, 200, r)
        return r

    def act(self, who, action, id=None, expect=200, **fields):
        Base.n += 1
        body = {"id": id if id is not None else f"{who}:{Base.n}", "action": action, **fields}
        s, r = call("POST", "/v1/act", body, who)
        self.assertEqual(s, expect, r)
        return r

    def ok(self, who, action, **fields):
        r = self.act(who, action, **fields)
        self.assertTrue(r["ok"], r)
        return r

    def no(self, who, action, reason, **fields):
        r = self.act(who, action, **fields)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["reason"], reason, r)
        self.assertTrue(r["message"])
        return r

    def see(self, who):
        s, r = call("GET", "/v1/perceive", who=who)
        self.assertEqual(s, 200, r)
        return r

    def inbox(self, who, after=0):
        s, r = call("GET", f"/v1/inbox?after={after}", who=who)
        self.assertEqual(s, 200, r)
        return r

    def advance(self, seconds):
        self.hook({"advance": seconds})


class Basics(Base):
    def test_missing_and_wrong_token(self):
        self.assertEqual(call("GET", "/v1/perceive")[0], 401)
        s, r = call("GET", "/v1/perceive", token="nope")
        self.assertEqual((s, r["reason"]), (401, "unauthorized"))

    def test_unknown_endpoint(self):
        s, r = call("GET", "/v1/nothing", who="bolt")
        self.assertEqual((s, r["reason"]), (404, "not_found"))

    def test_town(self):
        s, r = call("GET", "/v1/town", who="spike")
        self.assertEqual(s, 200)
        ids = {p["id"] for p in r["places"]}
        self.assertEqual(len(ids), 15)
        self.assertIn("creek", ids)
        self.assertIn("fir", ids)
        homes = {c["id"]: c["home"] for c in r["creatures"]}
        self.assertEqual((homes["bolt"], homes["spike"]), ("nest", "fir"))
        self.assertTrue(r["rules"])

    def test_hello_puts_you_there_and_returns_perceive(self):
        r = self.hello("spike", "lake")
        self.assertEqual(r["you"], {"id": "spike", "place": "lake", "awake": True})
        for key in ("now", "part", "season", "weather", "place", "here", "items", "recent", "cant_go",
                    "talk", "deliveries", "summary"):
            self.assertIn(key, r)

    def test_creatures_word_wins_on_hello(self):
        self.hello("spike", "lake")
        self.ok("spike", "go", to="creek")
        r = self.hello("spike", "garage")
        self.assertEqual(r["you"]["place"], "garage")

    def test_hello_bad_input(self):
        self.assertEqual(call("POST", "/v1/hello", {"place": "mars", "awake": True}, "spike")[0], 400)
        self.assertEqual(call("POST", "/v1/hello", {"place": "lake", "awake": True, "x": 1}, "spike")[0], 400)
        self.assertEqual(call("POST", "/v1/hello", {"place": "lake", "awake": "yes"}, "spike")[0], 400)

    def test_malformed_json(self):
        s, r = call("POST", "/v1/act", who="bolt", raw=b"{not json")
        self.assertEqual((s, r["reason"]), (400, "bad_request"))

    def test_body_too_big(self):
        s, r = call("POST", "/v1/act", who="bolt", raw=json.dumps({"x": "a" * 9000}).encode())
        self.assertEqual((s, r["reason"]), (413, "too_big"))

    def test_unknown_action_and_fields(self):
        self.act("bolt", "fly_to_moon", expect=400)
        self.act("bolt", "go", expect=400, to="lake", speed="fast")
        self.act("bolt", "go", expect=400)
        self.act("bolt", "say", expect=400, text="x" * 301)
        self.act("bolt", "say", expect=400, text="   ")

    def test_perceive_before_hello_is_your_home(self):
        self.assertEqual(self.see("bolt")["you"]["place"], "nest")


class ActionIds(Base):
    def test_replay_returns_same_answer_and_acts_once(self):
        self.hello("bolt", "lake")
        a = self.act("bolt", "drop", id="bolt:7:1:1", item="bottle cap")
        b = self.act("bolt", "drop", id="bolt:7:1:1", item="bottle cap")
        self.assertEqual(a, b)
        self.assertEqual(len(self.see("bolt")["items"]), 1)

    def test_same_id_different_body_is_refused(self):
        self.hello("bolt", "lake")
        self.act("bolt", "drop", id="bolt:8:1:1", item="bottle cap")
        r = self.act("bolt", "drop", id="bolt:8:1:1", item="spoon", expect=409)
        self.assertEqual(r["reason"], "id_reused")
        self.assertEqual(len(self.see("bolt")["items"]), 1)

    def test_retried_tick_with_new_attempt_id_works(self):
        self.hello("bolt", "lake")
        self.ok("bolt", "go", id="bolt:9:1:1", to="creek")
        self.ok("bolt", "go", id="bolt:9:2:1", to="garage")
        self.assertEqual(self.see("bolt")["you"]["place"], "garage")

    def test_bad_ids(self):
        for bad in ("", "x" * 65, "has space", "émoji"):
            self.act("bolt", "presence", id=bad, expect=400, awake=True)

    def test_a_400_is_not_remembered(self):
        self.act("bolt", "go", id="bolt:10:1:1", expect=400, to="lake", extra=1)
        self.ok("bolt", "go", id="bolt:10:1:1", to="lake")

    def test_a_no_is_remembered(self):
        self.hello("spike", "lake")
        a = self.no("spike", "go", "blocked", id="spike:3:1:1", to="summit")
        self.hello("spike", "mountain_trail")
        b = self.act("spike", "go", id="spike:3:1:1", to="summit")
        self.assertEqual(a, b)

    def test_ids_are_per_creature(self):
        self.hello("bolt", "lake")
        self.hello("spike", "lake")
        self.ok("bolt", "drop", id="x:1", item="spoon")
        self.ok("spike", "drop", id="x:1", item="pine nut")
        self.assertEqual(len(self.see("bolt")["items"]), 2)


class Travel(Base):
    def test_go_moves_and_others_see_it(self):
        self.hello("bolt", "lake")
        self.hello("spike", "creek")
        self.ok("bolt", "go", to="creek")
        r = self.see("spike")
        self.assertEqual([h["id"] for h in r["here"]], ["bolt"])
        self.assertTrue(any("Bolt" in e for e in r["recent"]))

    def test_unknown_place(self):
        self.no("bolt", "go", "unknown_place", to="mars")

    def test_summit_only_from_trail(self):
        self.hello("bolt", "lake")
        r = self.see("bolt")
        self.assertIn("summit", r["cant_go"])
        self.no("bolt", "go", "blocked", to="summit")
        self.ok("bolt", "go", to="mountain_trail")
        self.assertEqual(self.see("bolt")["cant_go"], {})
        self.ok("bolt", "go", to="summit")

    def test_summit_closed_in_thunderstorm(self):
        self.hello("bolt", "mountain_trail")
        self.hook({"weather": {"kind": "thunderstorm", "temp_f": 50, "wind_mph": 25}})
        r = self.see("bolt")
        self.assertIn("summit", r["cant_go"])
        self.assertEqual(r["weather"]["kind"], "thunderstorm")
        self.no("bolt", "go", "blocked", to="summit")

    def test_staying_put_is_ok(self):
        self.hello("bolt", "lake")
        self.ok("bolt", "go", to="lake")

    def test_asleep_cant_move(self):
        self.hello("bolt", "lake", awake=False)
        self.no("bolt", "go", "asleep", to="creek")

    def test_go_clears_doing(self):
        self.hello("bolt", "lake")
        self.hello("spike", "lake")
        self.ok("bolt", "doing", text="fishing")
        self.assertEqual(self.see("spike")["here"][0]["doing"], "fishing")
        self.ok("bolt", "go", to="creek")
        self.ok("spike", "go", to="creek")
        self.assertIsNone(self.see("spike")["here"][0]["doing"])


class Talk(Base):
    def setUp(self):
        super().setUp()
        self.hello("bolt", "lake")
        self.hello("spike", "lake")

    def test_heard_by_awake_creatures_here_only(self):
        self.hello("wren", "creek")
        self.ok("bolt", "say", text="Caw. Nice day.")
        heard = self.inbox("spike")["heard"]
        self.assertEqual([(h["from"], h["text"], h["to"]) for h in heard], [("bolt", "Caw. Nice day.", None)])
        self.assertEqual(self.inbox("wren")["heard"], [])
        self.assertEqual(self.inbox("bolt")["heard"], [])

    def test_asleep_listener_hears_nothing_and_stays_asleep(self):
        self.hello("spike", "lake", awake=False)
        r = self.ok("bolt", "say", text="Hey!")
        self.assertIn("Nobody", r["result"])
        self.assertEqual(self.inbox("spike")["heard"], [])
        self.assertFalse(self.see("spike")["you"]["awake"])

    def test_aimed_talk(self):
        self.ok("bolt", "say", text="Spike, that's mine.", to="spike")
        self.assertEqual(self.inbox("spike")["heard"][0]["to"], "spike")
        self.no("bolt", "say", "not_here", text="Wren?", to="wren")
        self.hello("spike", "lake", awake=False)
        self.no("bolt", "say", "asleep", text="Wake up", to="spike")

    def test_talk_to_quiet_creature(self):
        self.advance(601)
        self.hello("bolt", "lake")
        self.assertEqual(self.see("bolt")["here"][0]["state"], "quiet")
        self.no("bolt", "say", "quiet", text="Spike?", to="spike")

    def test_speaker_asleep(self):
        self.hello("bolt", "lake", awake=False)
        self.no("bolt", "say", "asleep", text="zzz")

    def test_inbox_cursor(self):
        self.ok("bolt", "say", text="one")
        first = self.inbox("spike")
        self.advance(40)
        self.ok("bolt", "say", text="two")
        rest = self.inbox("spike", first["next"])
        self.assertEqual([h["text"] for h in rest["heard"]], ["two"])
        self.assertEqual(self.inbox("spike", rest["next"])["heard"], [])

    def test_burst_counts_once(self):
        self.ok("bolt", "say", text="one")
        self.advance(10)
        self.ok("bolt", "say", text="two")
        self.advance(10)
        self.ok("bolt", "say", text="three")
        self.assertEqual(self.see("bolt")["talk"]["left_today"], 59)
        self.assertEqual(len(self.inbox("spike")["heard"]), 3)

    def test_slow_reply_is_fine(self):
        self.ok("spike", "say", text="Hey Bolt", to="bolt")
        self.advance(90)
        self.ok("bolt", "say", text="Caw. Took me a minute.", to="spike")
        self.advance(240)
        self.ok("spike", "say", text="No rush.", to="bolt")

    def _chat(self, n, who="bolt"):
        for i in range(n):
            self.ok(who, "say", text=f"line {i}")
            self.advance(31)

    def test_break_after_eight_creature_only_bursts(self):
        self._chat(8)
        r = self.see("bolt")
        self.assertIsNotNone(r["talk"]["break_until"])
        self.assertIn("break", r["summary"])
        self.no("bolt", "say", "break", text="one more")
        self.advance(1801)
        self.ok("bolt", "say", text="back again")

    def test_human_spoke_ends_break(self):
        self._chat(8)
        self.ok("spike", "human_spoke", by="Elliott")
        self.ok("bolt", "say", text="Hi Elliott's jay")
        self.assertIsNone(self.see("bolt")["talk"]["break_until"])

    def test_human_spoke_resets_the_count(self):
        self._chat(7)
        self.ok("bolt", "human_spoke", by="Caelan")
        self._chat(7)
        self.assertIsNone(self.see("bolt")["talk"]["break_until"])

    def test_talking_alone_never_triggers_a_break(self):
        self.ok("spike", "go", to="creek")
        self._chat(10)
        self.assertIsNone(self.see("bolt")["talk"]["break_until"])
        self.assertEqual(self.see("bolt")["talk"]["left_today"], 50)

    def test_daily_cap_and_midnight_reset(self):
        self.ok("spike", "go", to="creek")
        self._chat(60)
        self.no("bolt", "say", "limit", text="sixty-one")
        self.advance(86400)
        self.hello("bolt", "lake")
        self.assertEqual(self.see("bolt")["talk"]["left_today"], 60)
        self.ok("bolt", "say", text="a new day")


class Items(Base):
    def setUp(self):
        super().setUp()
        self.hello("bolt", "lake")
        self.hello("spike", "lake")

    def test_drop_then_take_becomes_a_delivery(self):
        iid = self.ok("bolt", "drop", item="bottle cap")["item"]
        self.assertEqual(self.see("spike")["items"][0]["id"], iid)
        d = self.ok("spike", "take", item=iid)["delivery"]
        self.assertEqual(self.see("spike")["items"], [])
        got = self.inbox("spike")["deliveries"]
        self.assertEqual([(x["id"], x["item"]) for x in got], [(d, "bottle cap")])

    def test_first_taker_wins(self):
        iid = self.ok("wren", "hide", place="lake", item="spinner lure", by="Caelan")["item"]
        self.ok("spike", "take", item=iid)
        self.no("bolt", "take", "gone", item=iid)

    def test_simultaneous_takes(self):
        iid = self.ok("wren", "hide", place="lake", item="spinner lure", by="Caelan")["item"]
        results = []

        def grab(who):
            Base.n += 1
            results.append(call("POST", "/v1/act", {"id": f"{who}:race:{Base.n}", "action": "take", "item": iid}, who)[1])

        threads = [threading.Thread(target=grab, args=(w,)) for w in ("bolt", "spike")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(r["ok"] for r in results), [False, True])

    def test_take_from_elsewhere(self):
        iid = self.ok("bolt", "drop", item="spoon")["item"]
        self.ok("spike", "go", to="creek")
        self.no("spike", "take", "not_here", item=iid)

    def test_reserved_items(self):
        self.ok("spike", "go", to="creek")
        iid = self.ok("wren", "hide", place="creek", item="spinner lure", by="Caelan", **{"for": "bolt"})["item"]
        r = self.see("spike")
        self.assertEqual(r["items"][0]["for"], "bolt")
        self.assertIn("Bolt", r["summary"])
        self.no("spike", "take", "reserved", item=iid)
        self.ok("bolt", "go", to="creek")
        got = self.see("bolt")
        self.assertEqual(got["items"], [])
        self.assertEqual(got["deliveries"][0]["item"], "spinner lure")
        self.assertEqual(got["deliveries"][0]["from"], "Caelan")

    def test_reserved_waits_until_awake(self):
        self.hello("bolt", "lake", awake=False)
        self.ok("spike", "hide", place="lake", item="red bobber", by="Caelan", **{"for": "bolt"})
        self.assertEqual(self.see("bolt")["deliveries"], [])
        self.ok("bolt", "presence", awake=True)
        self.assertEqual(self.see("bolt")["deliveries"][0]["item"], "red bobber")

    def test_hide_without_for_is_fair_game(self):
        iid = self.ok("bolt", "hide", place="lake", item="quarter", by="Elliott")["item"]
        self.ok("spike", "take", item=iid)

    def test_hide_rules(self):
        self.hello("bolt", "nest", awake=False)
        self.ok("bolt", "hide", place="summit", item="pencil", by="Caelan")
        self.no("bolt", "hide", "unknown_place", place="mars", item="rock", by="Caelan")
        self.no("bolt", "hide", "unknown_creature", place="lake", item="rock", by="Caelan", **{"for": "nobody"})


class Deliveries(Base):
    def setUp(self):
        super().setUp()
        self.hello("bolt", "lake")
        self.hello("spike", "lake")

    def test_they_stay_until_acked(self):
        d = self.ok("spike", "give", to="bolt", item="beetle")["delivery"]
        for _ in range(3):
            self.assertEqual([x["id"] for x in self.see("bolt")["deliveries"]], [d])
            self.advance(3600)
        s, r = call("POST", "/v1/ack", {"deliveries": [d]}, "bolt")
        self.assertEqual((s, r["acked"]), (200, [d]))
        self.assertEqual(self.see("bolt")["deliveries"], [])
        self.assertEqual(self.inbox("bolt")["deliveries"], [])

    def test_ack_is_safe_to_repeat(self):
        d = self.ok("spike", "give", to="bolt", item="beetle")["delivery"]
        call("POST", "/v1/ack", {"deliveries": [d]}, "bolt")
        s, r = call("POST", "/v1/ack", {"deliveries": [d, "d999"]}, "bolt")
        self.assertEqual((s, r["acked"]), (200, []))
        self.assertEqual(call("POST", "/v1/ack", {"deliveries": "d1"}, "bolt")[0], 400)

    def test_cant_ack_someone_elses(self):
        d = self.ok("spike", "give", to="bolt", item="beetle")["delivery"]
        call("POST", "/v1/ack", {"deliveries": [d]}, "spike")
        self.assertEqual(len(self.see("bolt")["deliveries"]), 1)

    def test_gift_rules(self):
        self.ok("spike", "go", to="creek")
        self.no("spike", "give", "not_here", to="bolt", item="beetle")
        self.no("spike", "give", "not_here", to="nobody", item="beetle")
        self.ok("spike", "go", to="lake")
        self.act("spike", "give", expect=400, to="spike", item="beetle")

    def test_gift_to_sleeping_creature_waits(self):
        self.hello("bolt", "lake", awake=False)
        self.ok("spike", "give", to="bolt", item="beetle")
        self.assertEqual(len(self.see("bolt")["deliveries"]), 1)

    def test_three_gifts_a_day(self):
        for i in range(3):
            self.ok("spike", "give", to="bolt", item=f"beetle {i}")
        self.no("spike", "give", "limit", to="bolt", item="one more")

    def test_resent_give_delivers_once(self):
        # Spike's reply to a give got lost, so he resends the same id: one gift, counted once.
        a = self.act("spike", "give", id="spike:20:1:1", to="bolt", item="beetle")
        b = self.act("spike", "give", id="spike:20:1:1", to="bolt", item="beetle")
        self.assertTrue(a["ok"], a)
        self.assertEqual(a, b)
        self.assertEqual(len(self.see("bolt")["deliveries"]), 1)
        self.ok("spike", "give", to="bolt", item="beetle 2")
        self.ok("spike", "give", to="bolt", item="beetle 3")
        self.no("spike", "give", "limit", to="bolt", item="beetle 4")

    def test_resent_take_delivers_once(self):
        self.ok("bolt", "drop", item="bottle cap")
        item = self.see("spike")["items"][0]["id"]
        a = self.act("spike", "take", id="spike:21:1:1", item=item)
        b = self.act("spike", "take", id="spike:21:1:1", item=item)
        self.assertTrue(a["ok"], a)
        self.assertEqual(a, b)
        self.assertEqual(len(self.see("spike")["deliveries"]), 1)


    def test_resend_weeks_later_still_delivers_once(self):
        # The world was away (or Spike was off) for weeks; his outbox resends the same give.
        a = self.act("spike", "give", id="spike:22:1:1", to="bolt", item="beetle")
        self.advance(29 * 86400)
        self.hello("spike", "lake")
        b = self.act("spike", "give", id="spike:22:1:1", to="bolt", item="beetle")
        self.assertEqual(a, b)
        self.assertEqual(len(self.see("bolt")["deliveries"]), 1)


class Presence(Base):
    def test_quiet_after_ten_minutes_and_back(self):
        self.hello("bolt", "lake")
        self.hello("spike", "lake")
        self.advance(599)
        self.see("spike")
        self.assertEqual(self.see("spike")["here"][0]["state"], "awake")
        self.advance(2)
        self.assertEqual(self.see("spike")["here"][0]["state"], "quiet")
        self.see("bolt")
        self.assertEqual(self.see("spike")["here"][0]["state"], "awake")
        self.assertEqual(self.see("bolt")["you"]["place"], "lake")

    def test_polling_inbox_keeps_you_present(self):
        # A long turn: Bolt's model thinks for 20 minutes, his background thread polls the inbox.
        self.hello("bolt", "lake")
        self.hello("spike", "lake")
        for _ in range(4):
            self.advance(300)
            self.inbox("bolt")
            self.see("spike")
        self.assertEqual(self.see("spike")["here"][0]["state"], "awake")
        self.ok("spike", "say", text="Still thinking?", to="bolt")
        self.assertEqual(self.inbox("bolt")["heard"][-1]["text"], "Still thinking?")

    def test_asleep_can_still_do_some_things(self):
        self.hello("bolt", "lake", awake=False)
        self.assertEqual(self.see("bolt")["here"], [])
        self.inbox("bolt")
        self.ok("bolt", "human_spoke", by="Caelan")
        self.ok("bolt", "hide", place="lake", item="spoon", by="Caelan")
        for action, fields in (("sound", {"text": "caw"}), ("doing", {"text": "fishing"}),
                               ("drop", {"item": "spoon"}), ("say", {"text": "hi"})):
            self.no("bolt", action, "asleep", **fields)
        self.ok("bolt", "presence", awake=True)
        self.ok("bolt", "sound", text="a sleepy caw")

    def test_states_shown(self):
        self.hello("bolt", "lake")
        self.hello("spike", "lake", awake=False)
        self.assertEqual(self.see("bolt")["here"][0]["state"], "asleep")

    def test_around_town(self):
        self.hello("bolt", "junkyard")
        self.hello("spike", "fir", awake=False)
        seen = self.see("bolt")
        self.assertEqual(seen["around"], [{"id": "spike", "name": "Spike", "place": "fir", "state": "asleep"}])
        self.assertIn("Around town: Spike is at Spike's fir (asleep).", seen["summary"])
        self.assertEqual(seen["here"], [])
        self.hello("spike", "junkyard")
        seen = self.see("bolt")
        self.assertEqual(seen["around"], [], "someone right here isn't 'around'")
        self.assertNotIn("Around town", seen["summary"])
        self.hook({"advance": 700})
        self.hello("bolt", "lake")
        around = self.see("bolt")["around"]
        self.assertEqual([(o["id"], o["state"]) for o in around], [("spike", "quiet")], "wren never connected")


class Limits(Base):
    def test_six_deeds_in_five_minutes(self):
        self.hello("bolt", "lake")
        for i in range(6):
            self.ok("bolt", "sound", text=f"caw {i}")
        self.no("bolt", "sound", "limit", text="caw 7")
        self.ok("bolt", "say", text="talk isn't a deed")
        self.advance(301)
        self.ok("bolt", "sound", text="caw again")

    def test_hide_cap(self):
        for i in range(10):
            self.ok("bolt", "hide", place="lake", item=f"penny {i}", by="Caelan")
        self.no("bolt", "hide", "limit", place="lake", item="penny 11", by="Caelan")


class Seeing(Base):
    def test_summary_stays_small(self):
        self.hello("bolt", "lake")
        self.hello("spike", "lake")
        self.hello("wren", "lake")
        for who in ("spike", "wren"):
            self.ok(who, "doing", text="x" * 60)
        for i in range(10):
            self.ok("bolt", "hide", place="lake", item=("a very long item name " * 3)[:60], by="Caelan")
            self.hook({"event": {"place": "lake", "text": "An osprey dove for a fish, and the whole lake heard it."}})
        r = self.see("bolt")
        self.assertLessEqual(len(r["summary"]), 700)
        self.assertLessEqual(len(r["summary"].splitlines()), 8)
        self.assertLessEqual(len(r["recent"]), 5)
        self.assertNotIn("{", r["summary"])

    def test_events_show_in_recent(self):
        self.hello("bolt", "lake")
        self.hook({"event": {"place": "lake", "text": "An osprey dove for a fish."}})
        r = self.see("bolt")
        self.assertEqual(r["recent"][-1], "An osprey dove for a fish.")
        self.assertIn("osprey", r["summary"])

    def test_doing_expires(self):
        self.hello("bolt", "lake")
        self.hello("spike", "lake")
        self.ok("bolt", "doing", text="fishing")
        self.assertIn("fishing", self.see("spike")["summary"])
        self.advance(901)
        self.see("bolt")
        self.assertIsNone(self.see("spike")["here"][0]["doing"])

    def test_weather_and_time_fields(self):
        r = self.see("bolt")
        self.assertIn(r["part"], ("night", "morning", "afternoon", "evening"))
        self.assertIn(r["season"], ("winter", "spring", "summer", "fall"))
        self.assertTrue(r["now"][-6:] in ("-07:00", "-08:00"), r["now"])
        self.assertEqual(set(r["weather"]), {"kind", "temp_f", "wind_mph"})


class Graffiti(Base):
    def put(self, who, mid, data=None, expect=200, ctype="image/png", **q):
        s, r = call("PUT", f"/v1/marks/{mid}?{urlencode(q)}", who=who, raw=png() if data is None else data, ctype=ctype)
        self.assertEqual(s, expect, r)
        return r

    def piece(self, who, mid, place, surface, kind="tag", data=None, **q):
        r = self.put(who, mid, data, place=place, surface=surface, kind=kind, **q)
        self.assertTrue(r["ok"], r)
        return r

    def test_walls_are_in_town(self):
        s, r = call("GET", "/v1/town", who="spike")
        walls = r["surfaces"]
        self.assertIn({"id": "water_tower", "name": "the water tower"}, walls["rail_yard"])
        self.assertNotIn("summit", walls)
        self.assertNotIn("nest", walls)

    def test_tag_rules(self):
        self.hello("spike", "lake")
        self.no("spike", "tag", "unknown_surface", surface="water_tower", kind="tag")   # that's at the rail yard
        self.no("spike", "tag", "not_yours", surface="boathouse", kind="tag")          # the lake is Wren's home
        self.hello("bolt", "fir")
        self.no("bolt", "tag", "not_yours", surface="fence", kind="tag")
        self.hello("spike", "fir")
        self.ok("spike", "tag", surface="fence", kind="throwup")
        self.act("spike", "tag", expect=400, surface="fence", kind="sticker")
        self.act("spike", "tag", expect=400, surface="fence")
        self.hello("spike", "rail_yard", awake=False)
        self.no("spike", "tag", "asleep", surface="water_tower", kind="tag")

    def test_tagging_shows_then_ends_with_one_event(self):
        self.hello("bolt", "rail_yard")
        self.hello("spike", "rail_yard")
        before = self.see("bolt")["recent"]
        r = self.ok("spike", "tag", surface="water_tower", kind="mural")
        self.assertTrue(r["until"])
        seen = self.see("bolt")
        self.assertEqual(seen["here"][0]["doing"], "painting the water tower")
        self.assertEqual(seen["recent"], before, "starting a piece adds no event")
        r = self.piece("spike", "m1", "rail_yard", "water_tower", kind="mural", title="Ring Day")
        self.assertEqual(r["mark"], "m1")
        seen = self.see("bolt")
        self.assertIsNone(seen["here"][0]["doing"])
        self.assertEqual(seen["recent"][-1], "Spike painted something new on the water tower.")
        self.assertEqual(sum("painted" in e for e in seen["recent"]), 1)

    def test_tagging_ends_on_go_sleep_and_time(self):
        self.hello("bolt", "rail_yard")
        self.hello("spike", "rail_yard")
        self.ok("spike", "tag", surface="boxcar", kind="tag")
        self.ok("spike", "go", to="junkyard")
        self.ok("spike", "go", to="rail_yard")
        self.assertIsNone(self.see("bolt")["here"][0]["doing"])
        self.ok("spike", "tag", surface="boxcar", kind="tag")
        self.ok("spike", "presence", awake=False)
        self.assertIsNone(self.see("bolt")["here"][0]["doing"])
        self.ok("spike", "presence", awake=True)
        self.ok("spike", "tag", surface="boxcar", kind="tag")
        self.advance(1801)
        self.see("spike")
        self.assertIsNone(self.see("bolt")["here"][0]["doing"])

    def test_pictures_are_checked_by_their_bytes(self):
        fake = b"\x89PNG\r\n\x1a\n" + b"not really a picture" * 10
        for i, data in enumerate((b"hello", fake, b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', png(32, 32),
                                  png(2000, 64), jpeg_header(64, 4000))):
            r = self.put("spike", f"bad{i}", data, place="rail_yard", surface="boxcar", kind="tag")
            self.assertEqual(r["reason"], "bad_image", r)
        self.piece("spike", "j1", "rail_yard", "boxcar", data=jpeg_header(400, 300))
        self.piece("spike", "p1", "rail_yard", "signal_shed", data=png(1024, 64))

    def test_bad_requests(self):
        self.put("spike", "x1", expect=400, place="rail_yard", surface="boxcar")                   # no kind
        self.put("spike", "x2", expect=400, place="rail_yard", surface="boxcar", kind="sticker")
        self.put("spike", "x3", expect=400, place="rail_yard", surface="boxcar", kind="tag", title="t" * 41)
        self.put("spike", "x4", expect=400, place="rail_yard", surface="boxcar", kind="tag", color="red")
        self.put("spike", "x5", data=b"", expect=400, place="rail_yard", surface="boxcar", kind="tag")
        s, r = call("PUT", "/v1/marks/x6?place=rail_yard&surface=boxcar&kind=tag", raw=png())
        self.assertEqual(s, 401)
        s, r = call("PUT", "/v1/marks/" + "x" * 65 + "?place=rail_yard&surface=boxcar&kind=tag", who="spike", raw=png())
        self.assertEqual(s, 404)
        r = self.put("spike", "x7", place="rail_yard", surface="moon", kind="tag")
        self.assertEqual(r["reason"], "unknown_surface")

    def test_too_big(self):
        try:
            s, r = call("PUT", "/v1/marks/big?place=rail_yard&surface=boxcar&kind=tag", who="spike",
                        raw=b"\x89PNG" + b"x" * (256 * 1024 + 10), ctype="image/png")
        except (ConnectionError, urllib.error.URLError):
            return          # the server may hang up before the whole body is sent; that's a refusal too
        self.assertEqual((s, r["reason"]), (413, "too_big"))

    def test_resent_upload_lands_once(self):
        a = self.piece("spike", "m7", "junkyard", "gate", title="Hi")
        b = self.piece("spike", "m7", "junkyard", "gate", title="Hi")
        self.assertEqual(a, b)
        s, r = call("GET", "/v1/marks?place=junkyard")
        gate = next(w for w in r["walls"] if w["surface"] == "gate")
        self.assertEqual(len(gate["pieces"]), 1)
        r = self.put("spike", "m7", png(rgb=(1, 2, 3)), expect=409, place="junkyard", surface="gate", kind="tag", title="Hi")
        self.assertEqual(r["reason"], "id_reused")
        self.put("spike", "m7", expect=409, place="junkyard", surface="gate", kind="tag", title="Bye")

    def test_works_from_anywhere_and_asleep(self):
        self.hello("spike", "fir", awake=False)
        self.piece("spike", "m1", "rail_yard", "water_tower")
        self.hello("bolt", "rail_yard")
        self.assertEqual(self.see("bolt")["walls"][0]["mark"], "m1")

    def test_nobody_paints_over_someone_elses_piece(self):
        self.piece("spike", "s1", "garage", "roll_door")
        self.hello("bolt", "garage")
        self.no("bolt", "tag", "not_yours", surface="roll_door", kind="tag")
        r = self.put("bolt", "b1", place="garage", surface="roll_door", kind="tag")
        self.assertEqual(r["reason"], "not_yours")
        self.piece("bolt", "b2", "garage", "back_wall")
        r = self.put("spike", "s2", place="garage", surface="back_wall", kind="tag", over="b2")
        self.assertEqual(r["reason"], "not_yours")
        r = self.put("spike", "s3", place="garage", surface="back_wall", kind="tag")
        self.assertEqual(r["reason"], "not_yours")
        r = self.put("spike", "s4", place="garage", surface="roll_door", kind="mural", over="b2")
        self.assertEqual(r["reason"], "not_yours", "over has to be your own piece on the same wall")
        self.piece("spike", "s5", "garage", "roll_door", kind="mural", over="s1", title="Bigger")

    def test_four_pieces_a_day(self):
        for i in range(4):
            self.piece("spike", f"d{i}", "shed", "door" if i % 2 else "side")
        self.piece("spike", "d3", "shed", "door")                       # a replay isn't a new piece
        r = self.put("spike", "d4", place="shed", surface="door", kind="tag")
        self.assertEqual(r["reason"], "limit")
        self.advance(86400)
        self.piece("spike", "d5", "shed", "door")

    def test_each_wall_keeps_five_pictures(self):
        prev = None
        for i in range(7):
            if i == 4:
                self.advance(86400)
            self.piece("spike", f"k{i}", "old_barn", "silo", data=png(rgb=(i, 0, 0)), **({"over": prev} if prev else {}))
            prev = f"k{i}"
        s, r = call("GET", "/v1/marks?place=old_barn")
        silo = next(w for w in r["walls"] if w["surface"] == "silo")
        self.assertEqual([p["id"] for p in silo["pieces"]], [f"k{i}" for i in range(6, -1, -1)])
        self.assertEqual([p["img"] is not None for p in silo["pieces"]], [True] * 5 + [False] * 2)
        self.assertEqual(silo["pieces"][0]["over"], "k5")
        barn_door = next(w for w in r["walls"] if w["surface"] == "barn_door")
        self.assertEqual(barn_door["pieces"], [])
        self.assertEqual(call("GET", "/v1/marks?place=mars")[0], 400)

    def test_walls_in_perceive_and_summary(self):
        self.hello("bolt", "rail_yard")
        self.assertEqual(self.see("bolt")["walls"], [])
        self.assertNotIn("On the walls", self.see("bolt")["summary"])
        self.piece("spike", "w1", "rail_yard", "water_tower", kind="mural", title="Ring Day")
        self.advance(7200)
        self.piece("spike", "w2", "rail_yard", "boxcar")
        seen = self.see("bolt")
        self.assertEqual([(w["surface"], w["by"], w["by_name"]) for w in seen["walls"]],
                         [("water_tower", "spike", "Spike"), ("boxcar", "spike", "Spike")])
        self.assertEqual(seen["walls"][0]["title"], "Ring Day")
        self.assertIn("On the walls: Spike's tag on the old boxcar (just now), and 1 more.", seen["summary"])
        self.hello("spike", "rail_yard")
        self.hello("wren", "rail_yard")
        for who in ("spike", "wren"):
            self.ok(who, "doing", text="x" * 60)
        for i in range(10):
            self.ok("bolt", "hide", place="rail_yard", item=("a very long item name " * 3)[:60], by="Caelan")
        r = self.see("bolt")
        self.assertLessEqual(len(r["summary"]), 700)
        self.assertLessEqual(len(r["summary"].splitlines()), 8)
        self.assertIn("On the walls", r["summary"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
