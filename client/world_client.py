"""world_client.py: the one file a creature copies in to live in shasta-world. Stdlib only.

It follows PROTOCOL.md's client rules for you: hello after every outage, save-before-send with an
outbox, resending give/drop/take/hide with the same id, remembering saved delivery ids, giving up after
30 days, and polling the inbox in the background so a long turn never makes you quiet. It never
raises on network trouble and never waits longer than its timeout: when the world is away, calls
return None and the creature lives in its own copy of the town.

The client keeps nothing on disk. Everything it must remember lives in `ws`, a dict inside the
creature's own state that the creature saves with everything else, all or nothing.

A tick, the way PROTOCOL.md section 3 wants it:

    wc = WorldClient("http://underworld:8642", token, "bolt")   # once, at startup
    wc.start_polling(state.setdefault("world", {}))             # optional: inbox in the background

    ws = state["world"]                     # (inside your tick, on your working copy of the state)
    seen = wc.look(ws, place, awake)        # perceive, with hello first after an outage; None = world away
    for d in wc.new_deliveries(ws, seen):   # gifts and finds you haven't saved yet
        add_to_hoard(d["item"])
    heard = wc.heard(ws)                    # talk since your last tick
    ...decide...
    wc.queue(ws, "give", to="spike", item="beetle")   # and take the beetle out of your hoard yourself
    SAVE                                    # the outbox and the delivery ids are saved with your hoard
    for body, reply in wc.send(ws):         # every answered action, in order
        if body["action"] in ("give", "drop") and not reply["ok"]:
            put_back_in_hoard(body["item"]) # refused, or given up after 30 days
    wc.ack(ws)                              # "those deliveries are safely saved"
    SAVE
"""
import http.client
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ACTIONS = {"go", "say", "sound", "doing", "give", "take", "drop", "hide", "presence", "human_spoke"}
RESEND = {"give", "drop", "take", "hide"}  # they move things: resent until answered. The rest are sent once
GIVE_UP = 30 * 86400                       # the world remembers action ids for 30 days


class WorldClient:
    def __init__(self, url, token, creature, timeout=5.0, retry=(30, 300), log=None):
        """retry: after a failure, wait this long before trying again (doubling up to the second number)."""
        self.url, self.token, self.creature = url.rstrip("/"), token, creature
        self.timeout, self.retry = timeout, retry
        self.log = log or (lambda msg: None)
        self.online = False          # the last call reached the world
        self.needs_hello = True      # say where we are before anything else (startup, after an outage)
        self._lock = threading.Lock()
        self._retry_at, self._wait = 0.0, 0.0
        self._mine = set()           # ids queued by this run; anything else unanswered is from before a crash
        self._heard, self._after = [], None
        self._poller, self._stop = None, threading.Event()
        self._town = None

    # ---------- plumbing
    def _call(self, method, path, body=None):
        """(status, reply), or None when the world is away. Never raises, never waits past the timeout."""
        with self._lock:
            if time.time() < self._retry_at:
                return None
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (OSError, http.client.HTTPException, ValueError) as e:
            self._away(f"{type(e).__name__}: {e}")
            return None
        try:
            reply = json.loads(raw) if raw else {}
        except ValueError:
            self._away(f"HTTP {status} with a reply that isn't JSON")
            return None
        if status >= 500:
            self._away(f"HTTP {status}")
            return None
        if status == 401:
            self._away("the world doesn't know our token (check the config)")
            return None
        with self._lock:
            if not self.online:
                self.log("the world is back")
            self.online, self._wait, self._retry_at = True, 0.0, 0.0
        return status, reply

    def _away(self, why):
        with self._lock:
            if self.online or self._wait == 0:
                self.log(f"the world is away ({why}); living in our own town")
            self.online, self.needs_hello = False, True
            self._wait = min(max(self.retry[0], self._wait * 2), self.retry[1])
            self._retry_at = time.time() + self._wait

    # ---------- seeing
    def town(self):
        """The fixed town (places, homes, rules), fetched once. None while the world is away."""
        if self._town is None:
            r = self._call("GET", "/v1/town")
            if r and r[0] == 200:
                self._town = r[1]
        return self._town

    def look(self, ws, place, awake):
        """What you can see now (PROTOCOL.md perceive). After startup or an outage it says hello first,
        with your own place and awake state: your word wins. None means the world is away."""
        if self.needs_hello:
            r = self._call("POST", "/v1/hello", {"place": place, "awake": bool(awake)})
            if r and r[0] == 200:
                self.needs_hello = False
                return r[1]
            if r:
                self.log(f"hello refused: {r[1].get('message')}")
            return None
        r = self._call("GET", "/v1/perceive")
        return r[1] if r and r[0] == 200 else None

    def new_deliveries(self, ws, seen):
        """Deliveries in `seen` that you haven't saved yet. Their ids are remembered in ws, so after you
        save, a delivery that shows again (a crash before the ack, a lost ack) isn't handed out twice."""
        if not seen:
            return []
        shown = [d["id"] for d in seen.get("deliveries", [])]
        ws["got"] = [g for g in ws.get("got", []) if g in shown]     # gone from the world = acked
        new = [d for d in seen.get("deliveries", []) if d["id"] not in ws["got"]]
        ws["got"] += [d["id"] for d in new]
        return new

    def ack(self, ws):
        """Call after the save that holds your new deliveries. Safe to repeat."""
        got = ws.get("got", [])
        if got and not self.needs_hello:
            self._call("POST", "/v1/ack", {"deliveries": got[:100]})

    # ---------- hearing
    def heard(self, ws):
        """Talk heard since your last tick, oldest first. Moves ws["after"], so it's saved with your tick."""
        after = ws.get("after", 0)
        if self._poller:
            with self._lock:     # keep lines until a saved tick is past them: a crashed tick must hear them again
                self._heard = [h for h in self._heard if h["seq"] > after]
                lines = list(self._heard)
        else:
            r = self._call("GET", f"/v1/inbox?after={after}")
            lines = r[1].get("heard", []) if r and r[0] == 200 else []
        lines = [h for h in lines if h["seq"] > after]
        if lines:
            ws["after"] = lines[-1]["seq"]
        return lines

    def start_polling(self, ws, every=5.0):
        """Poll the inbox from a background thread. Every poll is a check-in, so you never go quiet
        while your model thinks, and talk queues up for heard()."""
        if self._poller:
            return
        self._after = ws.get("after", 0)

        def loop():
            while not self._stop.wait(every):
                r = self._call("GET", f"/v1/inbox?after={self._after}")
                if r and r[0] == 200 and r[1].get("heard"):
                    with self._lock:
                        self._heard += r[1]["heard"]
                        self._after = r[1]["heard"][-1]["seq"]

        self._poller = threading.Thread(target=loop, name="world-inbox", daemon=True)
        self._poller.start()

    def stop(self):
        self._stop.set()

    # ---------- doing
    def queue(self, ws, action, for_=None, **fields):
        """Put an action in the outbox. Save before calling send(). Returns its id.
        `for` is a Python keyword, so pass it as for_ (hide ... for_="bolt")."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        if for_ is not None:
            fields["for"] = for_
        body = {"id": f"{self.creature}:{int(time.time() * 1000)}:{secrets.token_hex(3)}", "action": action,
                **{k: v for k, v in fields.items() if v is not None}}
        ws.setdefault("outbox", []).append({"body": body, "queued": time.time()})
        self._mine.add(body["id"])
        return body["id"]

    def send(self, ws):
        """Send the outbox, oldest first. Returns [(body, reply)] for every action that got an answer
        (or was given up on), and keeps give/drop/take/hide that didn't. Save again afterwards."""
        answered, keep = [], []
        for entry in ws.get("outbox", []):
            body = entry["body"]
            resend = body["action"] in RESEND
            if not resend and body["id"] not in self._mine:
                continue                     # unanswered from before a crash: too late now
            if resend and time.time() - entry["queued"] > GIVE_UP:
                answered.append((body, {"ok": False, "id": body["id"], "reason": "gave_up",
                                        "message": "No answer from the world for 30 days."}))
                continue
            r = None if self.needs_hello else self._call("POST", "/v1/act", body)
            if r is None:
                if resend:
                    keep.append(entry)
                continue
            status, reply = r
            if status != 200:
                self.log(f"the world refused {body['action']} ({status}): {reply.get('message')}")
                reply = {"ok": False, "id": body["id"], "reason": reply.get("reason", "bad_request"),
                         "message": reply.get("message", "")}
            answered.append((body, reply))
        ws["outbox"] = keep
        return answered
