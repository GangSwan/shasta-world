"""Tests for client/world_client.py against the real server: outages, crashes, lost replies, dedupe.

    python tests/client_test.py
"""
import copy
import json
import socket
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "client"))
from world.server import TEST_ROSTER, Server, World  # noqa: E402
from world_client import WorldClient  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
from contract_test import png  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "world.db")
        self.port = free_port()
        self.server = None
        self.up()

    def tearDown(self):
        if self.server:
            self.server.stop()
        self.tmp.cleanup()

    def up(self):
        self.server = Server(World(self.db, TEST_ROSTER, test=True), port=self.port, background=False).start()

    def down(self):
        self.server.stop()
        self.server = None

    def hook(self, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/_test", data=json.dumps(body).encode(),
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def client(self, who, **kw):
        kw.setdefault("timeout", 1.0)
        kw.setdefault("retry", (0, 0))
        return WorldClient(f"http://127.0.0.1:{self.port}", TEST_ROSTER[who]["token"], who, **kw)


class Seeing(Base):
    def test_look_says_hello_first_with_our_place(self):
        wc, ws = self.client("spike"), {}
        seen = wc.look(ws, "lake", True)
        self.assertEqual(seen["you"]["place"], "lake")
        self.assertFalse(wc.needs_hello)
        self.assertTrue(wc.online)
        self.assertEqual(wc.look(ws, "ignored now", True)["you"]["place"], "lake")
        self.assertEqual(len(wc.town()["places"]), 15)

    def test_world_away_returns_none_quickly_and_hello_after(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "lake", True)
        self.down()
        t = time.time()
        self.assertIsNone(wc.look(ws, "lake", True))
        self.assertLess(time.time() - t, 2.5)
        self.assertFalse(wc.online)
        self.assertTrue(wc.needs_hello)
        self.up()
        seen = wc.look(ws, "creek", True)          # moved while the world was away: our word wins
        self.assertEqual(seen["you"]["place"], "creek")

    def test_a_hanging_world_costs_one_timeout(self):
        hang = socket.socket()
        hang.bind(("127.0.0.1", 0))
        hang.listen(5)                              # accepts connections, never answers
        try:
            wc = WorldClient(f"http://127.0.0.1:{hang.getsockname()[1]}", "x", "spike", timeout=0.5, retry=(30, 300))
            t = time.time()
            self.assertIsNone(wc.look({}, "lake", True))
            self.assertIsNone(wc.look({}, "lake", True))     # backing off: no second wait
            self.assertEqual(wc.send({"outbox": []}), [])
            self.assertLess(time.time() - t, 1.5)
        finally:
            hang.close()

    def test_wrong_token_is_away_not_a_crash(self):
        logs = []
        wc = WorldClient(f"http://127.0.0.1:{self.port}", "wrong", "spike", timeout=1, retry=(0, 0), log=logs.append)
        self.assertIsNone(wc.look({}, "lake", True))
        self.assertTrue(any("token" in m for m in logs))


class Outbox(Base):
    def setUp(self):
        super().setUp()
        self.bolt, self.bws = self.client("bolt"), {}
        self.bolt.look(self.bws, "lake", True)

    def test_give_while_away_is_kept_and_sent_later(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "lake", True)
        self.down()
        wc.queue(ws, "give", to="bolt", item="beetle")
        self.assertEqual(wc.send(ws), [])
        self.assertEqual(len(ws["outbox"]), 1)
        self.up()
        wc.look(ws, "lake", True)
        [(body, reply)] = wc.send(ws)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(ws["outbox"], [])
        self.assertEqual([d["item"] for d in self.bolt.look(self.bws, "lake", True)["deliveries"]], ["beetle"])

    def test_crash_after_send_resends_same_id_and_gives_once(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "lake", True)
        wc.queue(ws, "give", to="bolt", item="beetle")
        saved = copy.deepcopy(ws)                     # SAVE, then send...
        [(_, first)] = wc.send(ws)                    # ...the world gives it, and we crash before saving
        wc2, ws2 = self.client("spike"), saved        # restart from what's on disk
        wc2.look(ws2, "lake", True)
        [(_, again)] = wc2.send(ws2)
        self.assertEqual(first, again)
        self.assertEqual(len(self.bolt.look(self.bws, "lake", True)["deliveries"]), 1)

    def test_only_give_drop_take_survive_a_crash(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "lake", True)
        wc.queue(ws, "say", text="hello Bolt", to="bolt")
        wc.queue(ws, "drop", item="pine nut")
        saved = copy.deepcopy(ws)                     # crash right after saving, before sending
        wc2 = self.client("spike")
        wc2.look(saved, "lake", True)
        sent = [b["action"] for b, _ in wc2.send(saved)]
        self.assertEqual(sent, ["drop"])
        self.assertEqual(self.bolt.heard(self.bws), [])

    def test_refused_give_comes_back(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "creek", True)
        wc.queue(ws, "give", to="bolt", item="beetle")
        [(body, reply)] = wc.send(ws)
        self.assertEqual((body["item"], reply["ok"], reply["reason"]), ("beetle", False, "not_here"))

    def test_bad_request_doesnt_loop(self):
        logs = []
        wc, ws = self.client("spike", log=logs.append), {}
        wc.look(ws, "lake", True)
        wc.queue(ws, "give", to="spike", item="beetle")      # to yourself: 400
        [(_, reply)] = wc.send(ws)
        self.assertEqual(reply["reason"], "bad_request")
        self.assertEqual(ws["outbox"], [])
        self.assertTrue(logs)

    def test_gives_up_after_thirty_days(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "lake", True)
        self.down()
        wc.queue(ws, "give", to="bolt", item="beetle")
        ws["outbox"][0]["queued"] -= 31 * 86400
        [(body, reply)] = wc.send(ws)
        self.assertEqual((body["item"], reply["reason"]), ("beetle", "gave_up"))
        self.assertEqual(ws["outbox"], [])

    def test_hide_for(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "creek", True)
        wc.queue(ws, "hide", place="lake", item="spinner lure", by="Caelan", for_="bolt")
        [(_, reply)] = wc.send(ws)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.bolt.look(self.bws, "lake", True)["deliveries"][0]["item"], "spinner lure")

    def test_unknown_action(self):
        with self.assertRaises(ValueError):
            self.client("spike").queue({}, "fly_to_moon")


class Graffiti(Base):
    def picture(self, rgb=(200, 60, 40)):
        f = Path(self.tmp.name) / f"piece-{rgb[0]}.png"
        f.write_bytes(png(rgb=rgb))
        return f

    def test_tag_then_a_piece_goes_up(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "rail_yard", True)
        wc.queue(ws, "tag", surface="water_tower", kind="mural")
        [(_, r)] = wc.send(ws)
        self.assertTrue(r["ok"], r)
        mid = wc.queue_mark(ws, self.picture(), "rail_yard", "water_tower", "mural", title="Ring Day")
        [(body, r)] = wc.send(ws)
        self.assertEqual((body["action"], r["ok"], r["mark"]), ("mark", True, mid))
        self.assertEqual(wc.look(ws, "rail_yard", True)["walls"][0]["title"], "Ring Day")

    def test_painted_while_away_goes_up_once_after_a_crash(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "fir", True)
        self.down()
        wc.queue_mark(ws, self.picture(), "junkyard", "gate", "tag")
        self.assertEqual(wc.send(ws), [])
        self.up()
        saved = copy.deepcopy(ws)
        wc.look(ws, "fir", True)
        [(_, first)] = wc.send(ws)                    # it lands; we crash before saving
        wc2 = self.client("spike")
        wc2.look(saved, "fir", True)
        [(_, again)] = wc2.send(saved)
        self.assertEqual(first, again)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/marks?place=junkyard")
        with urllib.request.urlopen(req, timeout=5) as r:
            gate = next(w for w in json.loads(r.read())["walls"] if w["surface"] == "gate")
        self.assertEqual(len(gate["pieces"]), 1)

    def test_a_lost_picture_is_dropped_not_retried(self):
        wc, ws = self.client("spike"), {}
        wc.look(ws, "fir", True)
        wc.queue_mark(ws, Path(self.tmp.name) / "gone.png", "junkyard", "gate", "tag")
        [(_, r)] = wc.send(ws)
        self.assertEqual(r["reason"], "no_file")
        self.assertEqual(ws["outbox"], [])


class Deliveries(Base):
    def setUp(self):
        super().setUp()
        self.spike, self.sws = self.client("spike"), {}
        self.spike.look(self.sws, "lake", True)

    def give_bolt(self, item):
        self.spike.queue(self.sws, "give", to="bolt", item=item)
        self.spike.send(self.sws)

    def test_saved_delivery_isnt_handed_out_twice(self):
        wc, ws = self.client("bolt"), {}
        wc.look(ws, "lake", True)
        self.give_bolt("beetle")
        new = wc.new_deliveries(ws, wc.look(ws, "lake", True))
        self.assertEqual([d["item"] for d in new], ["beetle"])
        saved = copy.deepcopy(ws)                     # SAVE (beetle in the hoard), crash before the ack
        wc2 = self.client("bolt")
        self.assertEqual(wc2.new_deliveries(saved, wc2.look(saved, "lake", True)), [])
        wc2.ack(saved)
        self.assertEqual(wc2.look(saved, "lake", True)["deliveries"], [])
        self.assertEqual(wc2.new_deliveries(saved, wc2.look(saved, "lake", True)), [])
        self.assertEqual(saved["got"], [])

    def test_crash_before_saving_hands_it_out_again(self):
        wc, ws = self.client("bolt"), {}
        wc.look(ws, "lake", True)
        self.give_bolt("beetle")
        before = copy.deepcopy(ws)
        wc.new_deliveries(ws, wc.look(ws, "lake", True))     # added to the hoard, then crash: not saved
        again = wc.new_deliveries(before, wc.look(before, "lake", True))
        self.assertEqual([d["item"] for d in again], ["beetle"])

    def test_nothing_to_ack_while_away(self):
        wc, ws = self.client("bolt"), {}
        self.assertEqual(wc.new_deliveries(ws, None), [])
        wc.ack(ws)


class Hearing(Base):
    def setUp(self):
        super().setUp()
        self.spike, self.sws = self.client("spike"), {}
        self.spike.look(self.sws, "lake", True)

    def say(self, text):
        self.spike.queue(self.sws, "say", text=text)
        self.spike.send(self.sws)

    def test_heard_moves_the_cursor(self):
        wc, ws = self.client("bolt"), {}
        wc.look(ws, "lake", True)
        self.say("one")
        self.assertEqual([h["text"] for h in wc.heard(ws)], ["one"])
        self.assertEqual(wc.heard(ws), [])
        saved = copy.deepcopy(ws)
        self.say("two")
        self.assertEqual([h["text"] for h in wc.heard(ws)], ["two"])
        self.assertEqual([h["text"] for h in self.client("bolt").heard(saved)], ["two"])   # a crash replays it

    def test_background_polling_queues_talk_and_keeps_us_present(self):
        wc, ws = self.client("bolt"), {}
        wc.look(ws, "lake", True)
        wc.start_polling(ws, every=0.05)
        try:
            for _ in range(3):                         # a 15-minute think, with the poller checking in
                self.hook({"advance": 300})
                time.sleep(0.3)
            self.assertEqual(self.spike.look(self.sws, "lake", True)["here"][0]["state"], "awake")
            self.say("still thinking?")
            time.sleep(0.3)
            self.assertEqual([h["text"] for h in wc.heard(ws)], ["still thinking?"])
            self.assertGreater(ws["after"], 0)
        finally:
            wc.stop()

    def test_a_crashed_tick_hears_it_again_with_polling(self):
        # Bolt's tick works on a deep copy of his state and throws it away if it fails. Talk the failed
        # tick read from the background poller must still be there for the retry.
        wc, ws = self.client("bolt"), {}
        wc.look(ws, "lake", True)
        wc.start_polling(ws, every=0.05)
        try:
            self.say("you there?")
            time.sleep(0.3)
            trial = copy.deepcopy(ws)
            self.assertEqual([h["text"] for h in wc.heard(trial)], ["you there?"])   # then the tick crashes
            self.assertEqual([h["text"] for h in wc.heard(ws)], ["you there?"])      # the retry still hears it
            self.assertEqual(wc.heard(ws), [])                                       # and only once it's saved
            self.say("and now?")
            time.sleep(0.3)
            self.assertEqual([h["text"] for h in wc.heard(ws)], ["and now?"])
        finally:
            wc.stop()


if __name__ == "__main__":
    unittest.main(verbosity=1)
