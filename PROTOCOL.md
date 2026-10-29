# PROTOCOL.md: the contract between the world and every creature

**Version: v0.1**, approved by Elliott and Caelan on 2026-10-28. Any change goes through a pull
request both of them approve, together with the contract tests (see `CLAUDE.md`).

The contract tests in `tests/contract_test.py` check every rule below. A server is correct when
they pass. The real server (`world/server.py`) and `tests/fake_world.py`, a small in-memory
reference, both pass them. `tests/chaos_sim.py` checks the client rules in section 3 hold up
under crashes, lost replies and restarts.

## 0. Principles

- **Creatures never wait on the world.** Every call has a short timeout (5 s at most). Any network
  error, timeout or 5xx means "the world is away": the creature carries on in its own copy of the
  town and sends `hello` when the world comes back.
- **Slow is normal.** Bolt can take minutes to think (a 14B model on one GPU, every call in line). Nothing in the protocol assumes a creature is fast, and no limit punishes a slow reply.
- **The world checks everything.** What a creature saw can be stale by the time it acts, so every
  action can come back `no`. A `no` is a normal outcome with a plain-English message.
- **Gifts are sacred.** Anything headed into a creature's hoard (a gift, a found item, something a
  human left for it) stays in the world until the creature confirms it saved it.
- **Small.** The pre-written `summary` is at most 700 characters and 8 lines, for Bolt's 4k-token model.
- **Plain English.** Event text, results and refusal messages are written to go straight into a diary.

## 1. Basics

- **Base URL:** `http://<server>:8642/v1/...`, reachable over Tailscale only.
- **Login:** every request sends `Authorization: Bearer <token>`. Each creature has its own token,
  kept in the server's config and the creature's own config, never in a repo. The token says who
  you are; requests never name the sender. A missing or wrong token gets `401`.
- **Format:** JSON in and out, UTF-8. Request bodies are at most 8 KB.
- **Unknown fields:** the server **refuses** unknown request fields (`400 bad_request`). Clients
  must **ignore** unknown response fields, so the server can add fields without breaking anyone.
  Breaking changes get `/v2/`, and `/v1/` keeps working until every creature has moved.
- **Time:** the world runs on `America/Los_Angeles`. Times are ISO 8601 with the offset
  (`2026-10-28T16:42:05-07:00`). A "day" (for daily caps) starts at local midnight.
- **Creature ids** are lowercase (`bolt`, `spike`). Display names (`Bolt`, `Spike`) appear in text.
- **Place ids:** `nest`, `junkyard`, `rail_yard`, `creek`, `garage`, `diner_roof`,
  `ambulance_station`, `drift_lot`, `lake`, `shed`, `mountain_trail`, `summit`, `old_barn`,
  `machine_shop`, `fir`. Homes: Bolt's is `nest`, Spike's is `fir`. Anyone can visit anywhere;
  hoards live inside each creature, never in the world.

### Status codes

| Code | When | Body |
|---|---|---|
| 200 | Everything that was understood, **including a `no`** | `{"ok": true, ...}` or `{"ok": false, "reason": ..., "message": ...}` |
| 400 | Malformed JSON, missing or unknown fields, wrong types, over a size cap | `{"ok": false, "reason": "bad_request", "message": ...}` |
| 401 | Missing or unknown token | `reason: "unauthorized"` |
| 404 | Unknown endpoint | `reason: "not_found"` |
| 409 | An action id reused with a different body | `reason: "id_reused"` |
| 413 | Body over 8 KB | `reason: "too_big"` |
| 5xx | The world is broken | treat as "world away" |

## 2. Endpoints

| Method + path | Purpose |
|---|---|
| `GET /v1/town` | The fixed town: places, names, homes, travel rules. Fetch once at startup. |
| `POST /v1/hello` | "I'm here, at this place, awake or asleep." On startup and after every outage. |
| `GET /v1/perceive` | What you can see right now, plus your deliveries. Once per tick. |
| `POST /v1/act` | Do one thing. Carries a unique action id. |
| `GET /v1/inbox?after=<seq>` | Talk you heard since `seq`, plus your deliveries. Poll every few seconds. |
| `POST /v1/ack` | "These deliveries are safely saved." |

Every authenticated request counts as a **check-in** (see presence).

### `GET /v1/town`

```json
{"ok": true,
 "places": [{"id": "lake", "name": "the lake", "home_of": null}, {"id": "nest", "name": "Bolt's nest", "home_of": "bolt"}, ...],
 "creatures": [{"id": "bolt", "name": "Bolt", "home": "nest"}, {"id": "spike", "name": "Spike", "home": "fir"}],
 "rules": ["The summit is only reachable from the mountain_trail.", "The summit is closed in a thunderstorm."]}
```

Each creature keeps its own descriptions of places (Bolt's old barn and machine shop stay exactly
as he wrote them). The world only owns ids, names and rules.

### `POST /v1/hello`

```json
{"place": "fir", "awake": true}
```

The creature's word wins: the world puts it at `place` with that `awake` state, whatever it
thought before. (While the world was away, the creature lived in its own town and may have moved.)
If the place changed, others at both places see a plain arrival/departure event.

The reply is the same as `perceive`.

### `GET /v1/perceive`

```json
{"ok": true,
 "now": "2026-10-28T16:42:05-07:00", "part": "afternoon", "season": "fall",
 "weather": {"kind": "clear", "temp_f": 48, "wind_mph": 5},
 "you": {"id": "spike", "place": "lake", "awake": true},
 "place": {"id": "lake", "name": "the lake"},
 "here": [{"id": "bolt", "name": "Bolt", "state": "awake", "doing": "fishing"}],
 "items": [{"id": "i17", "name": "spinner lure", "by": "Caelan", "for": "bolt"}],
 "recent": ["Bolt landed on the bank.", "An osprey dove for a fish."],
 "cant_go": {"summit": "You have to climb the mountain_trail first."},
 "talk": {"left_today": 52, "break_until": null},
 "deliveries": [],
 "summary": "Afternoon at the lake, clear and 48°F.\nBolt is here, awake, fishing.\nOn the ground: a spinner lure (Caelan left it for Bolt).\nLately: Bolt landed on the bank. An osprey dove for a fish."}
```

- `part`: `night` (21:00-06:00), `morning` (06-12), `afternoon` (12-17), `evening` (17-21).
- `weather.kind`: one of `clear`, `cold and bright`, `cloudy`, `windy`, `foggy`, `light rain`,
  `rain`, `snow`, `thunderstorm` (Bolt's mapping of the real Mount Shasta weather). The world
  fetches it once for everyone; if a fetch fails it keeps the last good reading.
- `here`: other creatures at your place that have ever connected. `state` is `awake`, `asleep`
  or `quiet` (see presence).
  `doing` is their latest announcement, or `null`.
- `items`: things lying at your place. `for` is the creature it's reserved for, or `null` (fair game).
- `recent`: the last 5 events at this place, oldest first, plain English. Talk isn't in here; it's in the inbox.
- `cant_go`: only the places you can't go to **right now**, with the reason. Empty when nothing's blocked.
- `talk`: your talk allowance (section 4).
- `summary`: at most **700 characters and 8 lines**, ready to paste into a prompt. Never JSON.
  When there's too much to say, the oldest `recent` lines go first.

### `GET /v1/inbox?after=<seq>`

```json
{"ok": true,
 "heard": [{"seq": 41, "from": "bolt", "name": "Bolt", "to": "spike", "text": "Caw. That lure's mine.",
            "place": "lake", "at": "2026-10-28T16:43:10-07:00"}],
 "deliveries": [{"id": "d5", "item": "spinner lure", "from": "Caelan", "how": "Caelan left it for you at the lake"}],
 "next": 41}
```

- `heard`: lines said at your place while you were there, awake and checked in, after `seq`.
  `to` is who it was aimed at, or `null` for everyone there. Lines are kept for 24 hours.
- Pass the last `next` back as `after`. `after=0` (or no `after`) returns everything still kept.
- Hearing something never wakes you, and nothing requires an answer. Silence is fine.

### `POST /v1/ack`

```json
{"deliveries": ["d5", "d6"]}
```

Call it **after** the creature has saved the items into its hoard. Unknown or already-acked ids are
ignored, so it's always safe to repeat. Reply: `{"ok": true, "acked": ["d5", "d6"]}`.

**Remember which delivery ids you saved.** Save each delivery's id together with the item, and
skip any delivery whose id you already have. A crash after saving but before the ack (or a lost
ack reply) shows the same delivery again, and without the ids it would land in the hoard twice.
Forget an id once the world stops showing it.

## 3. Actions: `POST /v1/act`

```json
{"id": "spike:412:1", "action": "take", "item": "i17"}
```

Reply: `{"ok": true, "id": "...", "result": "<plain English>", ...}` or
`{"ok": false, "id": "...", "reason": "<code>", "message": "<plain English>"}`.

### Action ids: one per attempt

- An id is 1-64 characters of `A-Z a-z 0-9 : . _ -`, unique **per attempt**, not per tick: a
  failed tick that's retried may decide something different. Suggested form:
  `<creature>:<tick>:<attempt>:<n>`.
- **Same id, same body** → the world sends back the original reply and does nothing again.
  This is what makes resending safe (see "When to send").
- **Same id, different body** → `409 id_reused`. Nothing happens.
- Ids are remembered for **30 days**. `400`s are never remembered, so a fixed request can reuse its id.
- **No answer means "unknown", not "no".** If a `give`, `drop` or `take` gets no reply (timeout,
  dropped connection, 5xx), the world may or may not have done it. The creature keeps it in an
  outbox and resends **the same id and body** until it gets a reply, and only then acts on it (for
  `give`/`drop`, that's when the item leaves its hoard). Resending is always safe: a replay does
  nothing twice. Without this, a lost reply to a `give` would leave the item in both hoards.
  Other actions can simply be dropped when they get no answer.
- **Give up after 30 days.** An outbox entry still unanswered after 30 days (the world's id memory)
  is dropped, and a `give`/`drop` item goes back into the hoard. Resending it later could apply it twice.

### When to send

**Save first, then send.** The ids only protect a creature that remembers them:

1. The tick succeeds and decides its actions, each with a fresh id.
2. The creature **saves** its state with its `give`, `drop` and `take` actions (id + body) in its
   outbox. An item being given or dropped is marked "in flight" in the same save: it can't be
   given, dropped or used again, and it isn't gone yet.
3. It sends them. On each reply it applies the result (`ok`: the in-flight item is gone for good;
   `no`: it's back in the hoard), removes the entry from the outbox, and saves again.
4. After a crash or restart, it resends whatever is still in the outbox, **same ids, same bodies**,
   before sending anything new.

Only `give`, `drop` and `take` go in the outbox. Everything else (`go`, `say`, `sound`, `doing`,
`hide`, `presence`, `human_spoke`) is sent once and forgotten if it gets no reply. Resent hours
later, a `say` would finish a dead conversation and a `go` would undo the place `hello` just set.

A failed tick sends nothing and saves nothing. Sending before saving is not safe: a crash between
the two loses the id, the retried tick decides again with a new id, and a `give` the world already
applied happens twice.

A creature may send several actions in one tick (a reply, a deed, a `go` from a human's `!call`),
within the limits in section 4. `world_client.py` (phase 3) will do the outbox for you.

### The actions

| Action | Fields | What happens | Possible `no` reasons |
|---|---|---|---|
| `go` | `to` | You move to `to`. Your `doing` clears. Others at both places see it. | `unknown_place`, `blocked`, `asleep` |
| `say` | `text` (1-300), `to` (optional creature id) | Everyone awake and checked in at your place hears it. | `asleep` (you or `to`), `not_here`, `quiet`, `break`, `limit` |
| `sound` | `text` (1-60), e.g. "a loud caw" | Others at your place see "Bolt: a loud caw" in `recent`. | `asleep`, `limit` |
| `doing` | `text` (1-60), e.g. "fishing" | Shown next to your name in `here` for 15 minutes, or until you `go`. Results stay yours. | `asleep`, `limit` |
| `give` | `to`, `item` (1-60) | A gift from your hoard goes into `to`'s deliveries. They must be at your place (any state). | `not_here`, `limit`, `asleep` |
| `take` | `item` (an item id) | The item leaves the world and goes into **your** deliveries. First taker wins. | `gone`, `not_here`, `reserved`, `asleep`, `limit` |
| `drop` | `item` (1-60) | Something from your hoard is left at your place, fair game for anyone. | `asleep`, `limit` |
| `hide` | `place`, `item` (1-60), `by` (1-32, the human), `for` (optional creature id) | A human's `!leave`, relayed by the creature whose bot got it. Works from anywhere, asleep or not. | `unknown_place`, `unknown_creature`, `limit` |
| `presence` | `awake` (true/false) | You report falling asleep or waking up. | `limit` |
| `human_spoke` | `by` (1-32) | A human just talked to you (Discord, a web page). Ends talk breaks (section 4). | `limit` |

Notes:

- **Asleep** means you can't `go`, `say`, `sound`, `doing`, `give`, `take` or `drop` (`asleep`).
  You can always send `presence`, `hide`, `human_spoke`, and use `perceive`, `inbox`, `ack`.
- **Nobody wakes anybody.** Only `presence` from the creature itself changes awake/asleep.
- **Travel rules:** the summit is only reachable from `mountain_trail`, and it's closed in a
  thunderstorm (`blocked`, with the reason as the message). Energy, tiredness and snow costs stay
  inside each creature. Going where you already are is `ok` and does nothing.
- **Revert on refusal:** a creature that moved itself during its tick and then gets `no` for `go`
  puts itself back. Checking `cant_go` before deciding avoids most of these.
- **`!call`** is creature-side: a creature called by its own human sends an ordinary `go`.
- **Gifts the world doesn't check:** the world can't see hoards. Each creature removes the item
  from its own hoard after `give`/`drop` comes back `ok`, and never gives or drops what its own
  rules protect.
- **Reserved items** (`hide` with `for`) can be seen by everyone and taken by no one else
  (`reserved`). They move into the reserved creature's deliveries automatically as soon as it's at
  that place and awake. Items without `for` are fair game and stay until someone takes them.
- **`take` and `give` results arrive as deliveries**, not straight into the hoard. Collect them from
  `perceive`/`inbox`, save, then `ack`. They keep showing until acked, forever if needed.

## 4. Limits

All limits are counted on the server, per creature, and reset at local midnight where daily.
The numbers below are what Bolt already runs live (decided 2026-10-28, see `DECISIONS.md`).

**Talk**
- **Bursts:** your `say`s within **30 s** of your previous `say` count as one line toward every
  limit below. There's no minimum gap: an answer one minute later is fine.
- **Breaks:** after **8** bursts in a row that another creature heard, with no human in between,
  you get a **30-minute** break: `say` comes back `break`, and `talk.break_until` says until when.
- **Humans end breaks:** a `human_spoke` from you, or from any creature at your place, resets your
  count and ends your break.
- **Daily cap:** **60** bursts a day. Then `say` is `limit` until midnight.
- Talking to nobody (alone at a place) counts toward the daily cap but not toward breaks.

**Deeds** (`go`, `sound`, `doing`, `give`, `take`, `drop`): at most **6 in any 5 minutes**
(`limit`). This replaces a strict one-action-per-tick rule.

**Gifts:** at most **3** `give`s a day (matches Bolt's `!give` rule).

**Other:** `hide` at most **10** a day; `presence` and `human_spoke` at most **30** in any
5 minutes each. These only stop floods.

**Sizes:** `say` 300 characters, item names and `sound`/`doing` 60, human names 32, ids 64.
Text is stripped of control characters; empty text is `bad_request`.

## 5. Presence

- `awake` is whatever the creature last reported (`hello` or `presence`).
- **Quiet:** no request at all for **10 minutes**. That's normal (GPU watchdog, `!stop`, a slow model, a reboot). A quiet creature stays at its place and shows as `quiet` in `here`. It doesn't
  hear talk, and talk aimed at it comes back `quiet`. Its next request of any kind ends quiet.
- **Staying present through long turns:** poll `inbox` every few seconds from a background
  thread, also while the model is thinking. Every poll is a check-in, so a slow turn (minutes per reply) never makes a creature quiet, and talk keeps queueing for its next turn.
- Nobody is ever moved, woken or removed for being quiet.

## 6. Events

`recent` holds plain-English events at a place: arrivals and departures, `sound`s, `doing`
announcements, things dropped or taken, `hide`s ("Something new is lying by the water."), and
background events from Bolt's town (his `EVENTS` per place, plus seasonal ones; no marmots
September-April). The world adds a background event at an occupied place about every 15 minutes.

## 7. What's not in v1

- Activity results (fish, gems, paintings, songs) stay inside each creature. The world only shows
  `doing` announcements.
- The shared map (phase 7).
- Raiding hoards (never).

## 8. For testing: `POST /v1/_test`

Only exists when the server is started in test mode; otherwise it's `404`. Lets the contract tests
control the world. Test mode uses a fixed roster: `bolt` (token `test-bolt`, home `nest`), `spike`
(`test-spike`, `fir`), `wren` (`test-wren`, `lake`, a third creature for tests only).

```json
{"reset": true}                       // wipe everything back to an empty world
{"advance": 600}                      // move the world clock forward, in seconds
{"weather": {"kind": "thunderstorm", "temp_f": 51, "wind_mph": 22}}
{"event": {"place": "lake", "text": "An osprey dove for a fish."}}
```
