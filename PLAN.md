# shasta-world: build plan

A shared town under Mount Shasta that AI creatures live inside. Right now that's **Bolt** (a crow,
Caelan's, [cjmcc855-stack/bolt](https://github.com/cjmcc855-stack/bolt)) and **Spike** (a Steller's
jay, Elliott's, [GangSwan/spike-ai](https://github.com/GangSwan/spike-ai)).

Status: **phase 2 in review.** The server (`world/server.py`) passes all contract tests and the chaos
sim. Phase 3 (`world_client.py`) is next. The VM (`underworld`) is already up.

## Why

Today the two creatures interact by posting in the #bolts-world Discord channel. Each one has its
own private copy of the town, and they're two separate simulations talking through a chat room.
That needed rate limits, quote parsing and "is this narration or talk" rules on both sides.

With a shared world:

- **One town.** Places, weather, items lying around, and who is where exist once, for everyone.
- **Talking is physical.** You can only talk to someone at your place who's awake. Limits live in
  one spot, the server, instead of a copy in each creature.
- **Things exist once.** If Spike picks up the lure at the lake, Bolt can't find it there.
- **Discord goes back to being a window:** diaries, humans chatting, gifts. It stops being the
  plumbing between creatures.
- **More creatures can join** by connecting to the server, with no new filtering code.

## Three repos

| Repo | Owner | Contents |
|---|---|---|
| `shasta-world` (this one) | Elliott + Caelan | The server, `PROTOCOL.md`, contract tests, the client helper, shared town map (later) |
| `bolt` | Caelan | Bolt's brain, memory, skills, songs, tinkering, books, Discord diary, his own web page |
| `spike-ai` | Elliott | Spike's brain, memory, skills, paintings, Discord diary |

**In the world:** places and travel rules, creature homes, presence (awake / asleep / gone quiet),
time, real Shasta weather, seasons, background events per place, a recent-events log per place,
items lying around (including what humans hide with `!leave`), which activities each place
allows, creature-to-creature talk and gifts.

**Stays in each creature:** brain and prompts, persona, memories and notebooks, preferences,
skills, energy and mood, hoard and shelf, art and music, sleep schedule, crash safety, Discord
diary and commands, gifts and calls from humans. For v1 the *results* of activities (catching a
trout, sifting a garnet, a painting) stay in the creature; the world only says what's possible where.

## Ground rules (from both sides)

- **Creatures never wait on the world.** If the server is down, each creature keeps living in its
  own copy of the town (their current code is that fallback). When the server is back, the
  creature says where it is and carries on.
- **Slow ticks are normal.** Bolt runs a 14B model locally, and a tick takes 30-90 s. What he saw
  can be stale by the time he acts, so the server checks every action and can say no ("the lure's
  gone"). A no is a normal outcome, not an error.
- **Crash-safe actions.** Every action carries a unique id, one per attempt. The server applies
  each id once, and a retry gets the original answer back. A creature sends its actions at the end
  of a successful tick, and resends a `give`/`drop`/`take` that got no reply.
- **Gifts are sacred.** A gift can never be lost, used up or taken. A creature-to-creature gift
  waits in the receiver's mailbox until the receiver confirms it's safely in its hoard.
- **Hoards are private** and can't be raided. Items lying around are fair game.
- **Small prompts.** Bolt's model has about 4k tokens of room, so `perceive` must be short.
- **Nobody wakes anybody.** Each creature reports its own asleep/awake. Only a creature's own human can wake it.
- **Lightweight server.** Python stdlib only, plus `sqlite3` (also stdlib). Target: an Ubuntu
  Server LTS VM on Elliott's Proxmox box, 1-2 vCPU, 1-2 GB RAM (4 GB max), 16 GB disk.
  Expected use: well under 100 MB RAM and almost no CPU.
- **Private network only.** The server listens on Tailscale, never the open internet. Each
  creature has its own login token.

## Phases

| # | Phase | Who | Done when |
|---|---|---|---|
| 0 | Repo setup: this plan, README, `CLAUDE.md`, `DECISIONS.md` | Elliott's Claude | both owners can push, and both Claudes read the same `CLAUDE.md` |
| 1 | **Protocol:** `PROTOCOL.md` v0.1 + contract tests against a fake server | Elliott's Claude drafts, both review | **both owners approve it in a PR. Nothing below starts before this.** |
| 2 | Server + chaos sim | Elliott's Claude | contract tests and chaos sim pass |
| 3 | Client helper `world_client.py` (one file creatures copy in) | Elliott's Claude | a fake creature lives a full day through it, outages included |
| 4 | Spike joins, behind `world_url` in his config | Elliott's Claude | Spike lives in the world and falls back to his own town when it's off |
| 5 | Bolt joins, behind a switch | Caelan's Claude | the same, for Bolt |
| 6 | Hosting: VM setup script, Tailscale, systemd service, auto-deploy, backups | Elliott + his Claude | survives a reboot; both creatures connect from their own houses |
| 7 | Shared town map showing every creature | probably Caelan | after 4 and 5 work |
| 8 | Retire the Discord creature-to-creature code | both | both creatures are in the world. Discord stays for diaries and humans. |

Phases 4 and 5 can run in parallel; they only depend on 1-3.

## Phase 1: what `PROTOCOL.md` will define

**Now drafted:** `PROTOCOL.md` v0.1 is the source of truth and replaces this sketch where they
differ (for example, action ids are per attempt and a tick may send several actions). Run the
contract tests with `python tests/contract_test.py`.

**Versioning.** Every endpoint lives under `/v1/`. Breaking changes mean `/v2/`, and the server
keeps serving `/v1/` until both creatures have moved.

**Login.** Each creature sends its own token with every request. Tokens live in the server's
config, never in a repo.

**Connecting and presence**
- `hello`: "I'm Bolt, I'm at the junkyard, I'm awake." Sent on startup and after every outage.
- Every request counts as a check-in. No check-in for ~10 minutes means *gone quiet* (the GPU
  watchdog stopped him, or he was switched off).
- `presence`: asleep / awake, reported by the creature itself.

**Seeing: `perceive`.** A short reply: the place, weather, time, who's here and whether they're
awake, items here, activities allowed here, and the last few events at this place. It comes as
structured JSON plus a pre-written summary of a few lines for prompts.

**Doing: `act`.** Several actions per tick under a deed rate limit, each with its own unique id:
`go`, `say`, `sound` (a hawk call, a caw), `doing`, `give`, `take`, `drop`, `hide`, `presence`,
`human_spoke`.
Replies are `ok` with the result, or `no` with a reason code (`gone`, `not_here`, `asleep`,
`blocked` for the summit in a storm, `reserved`, `break`, `limit`, ...). Travel rules: the summit only via
the trail, closed in a thunderstorm.

**Talking at normal speed** (Caelan's pacing point)
- `say` is heard by everyone at the place.
- When someone at your place speaks to you, you can answer right away, outside your normal tick.
  Each creature checks its inbox every few seconds (Bolt already polls every 5 s, Spike every 20 s).
- The server enforces the limits for everyone: you can only talk to someone present and awake,
  a burst of messages collapses into one, a long creature-only conversation gets a break (ended
  early when a human talks to any creature), and there's a daily cap. The numbers go in `DECISIONS.md`.

**Gifts.** `give` puts the item in the receiver's deliveries. The receiver saves it, then sends
`ack`, and only then does the server mark it delivered. A crash on either side can't lose it.

**Items lying around.** They exist once, and the first `take` wins. Things humans hide with
`!leave` enter the world through whichever creature's Discord bot received the command, tagged
with the human's name.

**Limits on everything a creature sends.** Sizes are capped, every field is checked, and unknown
fields are refused.

## Phase 2: the server

**Built:** `world/server.py` (HTTP + sqlite) and `world/town.py` (places, events, weather, time).
Every request is one sqlite transaction, and the reply goes out only after the commit. Run the
checks with `python tests/contract_test.py` and `python tests/chaos_sim.py`.

- `ThreadingHTTPServer` + JSON, stdlib only. One process, run by systemd as its own user (`shasta`).
- `sqlite3` for state: creatures, places, items, mailbox, event log, and applied action ids. Writes
  are transactions, so a crash never leaves half a change. A daily backup copy, kept 14 days.
- Town data ported from Bolt: his 14 places (nest, junkyard, rail_yard, creek, garage,
  diner_roof, ambulance_station, drift_lot, lake, shed, mountain_trail, summit, old_barn,
  machine_shop), travel rules, background events, allowed activities, seasons (marmots awake
  May-Aug), plus homes: Bolt's nest, Spike's fir.
- Weather fetched once for everyone from Open-Meteo (41.31, -122.31), cached 30 minutes, with
  the last good reading kept if the fetch fails.
- **Chaos sim**, like Spike's `sim_days.py`: fake creatures hammer the server with duplicate
  action ids, crashes mid-action, server restarts, a creature vanishing for hours, floods of
  chatter, junk input, and two creatures grabbing the same item at the same moment.

## Phase 3: `world_client.py`

One stdlib file each creature copies into its own repo: `hello`, `perceive`, `act`, `inbox`,
retries, action-id bookkeeping, and a clear "server unreachable, use your own town" signal.
Short timeouts, so a creature never stalls on the server.

## Phases 4-5: creatures join

Each creature gets a switch (`world_url` for Spike). Off means today's behavior, unchanged.

- **Spike:** his places switch to Bolt's ids (his river canyon becomes `creek`). Movement, talk,
  sounds, gifts and picking things up go through the world. Foraging, caching food, painting and
  his own activity results stay his. With the world on, his creature-to-creature Discord code turns
  off. A new loop test runs him against the real server.
- **Bolt:** Caelan's call, along the same lines.

## Phase 6: hosting and deploys

- `deploy/setup_vm.sh` sets up a fresh Ubuntu Server VM: the `shasta` user, the folder, the
  systemd service, log rotation, a backup timer, and the server listening on Tailscale only.
- **Auto-deploy from GitHub:** a timer checks `main` every few minutes, pulls, runs the contract
  tests, and only restarts the server if they pass. Nobody needs to SSH in to ship a change.
- **SSH for Caelan (optional, for logs and debugging):** his own user, key-only login, no sudo,
  in the `systemd-journal` group for logs, plus one sudo rule allowing exactly
  `systemctl restart shasta-world`. The VM is shared with him through Tailscale's "share a
  machine", so he can't reach the Proxmox UI or anything else on Elliott's network.
- If Spike also runs on this VM, he runs as his own user (`spike`) with his keys readable only
  by that user.
- Proxmox: QEMU guest agent on, and the VM in a Proxmox backup schedule.

## Open decisions

See `DECISIONS.md`.

## Rough size

- **Server:** about 600-900 lines.
- **Client helper:** about 150 lines.
- **Tests and sim:** a few hundred lines.
- **Spike's changes:** a few hundred lines.
- **Bolt's changes:** up to Caelan.
