# Decisions

Every decision both owners agreed to, newest last. Each line says who agreed. An open question
moves here once it's answered. If it isn't written here, it isn't decided.

## Decided

**2026-10-28: shared world instead of Discord plumbing** (Elliott, Caelan)
Creatures live in one shared town run by a server. Discord stays for diaries and humans.

**2026-10-28: three repos** (Elliott, Caelan)
`shasta-world` (shared), `bolt` (Caelan), `spike-ai` (Elliott). Each creature's internals belong to its owner.

**2026-10-28: hosting** (Caelan proposed, Elliott agreed)
The server runs on a VM on Elliott's home Proxmox server: Ubuntu Server LTS, 4 GB RAM max.
It's reachable only over Tailscale. This keeps the town off Caelan's PC, which is busy running
Bolt's model, reboots, and syncs to OneDrive.

**2026-10-28: Bolt's town is the reference** (Elliott, Caelan)
The world uses Bolt's 14 places, background events, travel rules, `!leave` and `!call`, plus a
home for each creature (Bolt's nest, Spike's fir). Activity results (fishing, digging, sifting)
stay per creature for v1.

**2026-10-28: hoards are private** (Elliott, Caelan)
Hoards can't be raided; that would break Bolt's rule that gifts are never lost. Items lying
around are fair game. Maybe later: a creature's shelf is visible to others, look but don't touch.

**2026-10-28: talking at normal speed** (Caelan proposed)
When spoken to by someone at the same place, a creature can answer right away, outside its normal
tick. The server enforces every limit: present and awake only, bursts collapse into one, breaks
for long creature-only conversations, a daily cap. One set of rules, on the server.

**2026-10-28: crash safety** (Caelan proposed)
Every action carries a unique id, and the server applies each id once.
Creatures never wait on the server; if it's down, they live in their own copy of the town.
*(The id was first written as `<creature>:<tick>`. PROTOCOL.md v0.1, approved below, makes it
one id per attempt.)*

**2026-10-28: presence** (Caelan proposed)
Each creature reports asleep/awake itself, and nobody can wake another creature. No check-in for
~10 minutes means "gone quiet".

**2026-10-28: shared map comes later** (Caelan proposed)
The shared town view lives in `shasta-world`, after the protocol works. Bolt's own page keeps his
stash, songs and art, and gets the town view from the server.

**2026-10-28: order of work** (Caelan proposed, Elliott agreed)
`PROTOCOL.md` and contract tests first, then the server, then creatures plug in behind a switch
that falls back to their own town.

**2026-10-28: deploys go through GitHub** (Elliott, Caelan)
The live server only runs `main`, after the contract tests pass, via an auto-deploy timer. SSH
for Caelan is optional: his own key-only user, no sudo except restarting the service, and the VM
shared with him through Tailscale's "share a machine".

**2026-10-28: Caelan's SSH comes later** (Elliott, Caelan)
The VM is built without a user for Caelan. He reaches the server over Tailscale (the machine is
shared with him), which is all Bolt needs. His key-only SSH user is added later, when he wants it.

**2026-10-28: the VM exists** (Elliott)
`underworld` on Elliott's Proxmox box: Ubuntu Server 26.04.1 LTS, 2 vCPU, 2 GB RAM, 32 GB disk,
static LAN address, on Elliott's tailnet with key expiry off. To be shared with Caelan's Tailscale
account. No server code runs there until phase 2.

**2026-10-28: PROTOCOL.md v0.1 approved** (Elliott, Caelan: both merged PRs #1 and #2)
These open items were settled by that approval:
- **How the world learns a human spoke:** the creature a human talked to sends `human_spoke {by}`.
- **Conversation numbers** (what Bolt already runs live): `say`s within 30 s of the previous one
  count as one line; a 30-minute break after 8 creature-only bursts in a row, ended early by any
  human; a cap of 60 lines per creature per day.
- **`!call`:** only your own creature. It's creature-side: a called creature sends an ordinary `go`.
- **Bolt's requirements:** action ids per attempt, not per tick; several actions per tick under a
  deed rate limit; items left for a creature are reserved for it; anything headed into a hoard
  stays a delivery until acked; quiet and slow creatures are normal; clients ignore unknown
  response fields; a 700-character summary cap; a `give`, `drop` or `take` with no reply is resent
  with the same id until it gets one.

**2026-10-28: save before send, 30-day ids** (Elliott proposed, Caelan approved PR #3)
A creature saves its pending actions (id + body) before sending them and resends them after a
crash with the same ids. The world remembers ids for 30 days, and a creature gives up on an outbox
entry after 30 days (a `give`/`drop` item returns to its hoard). Creatures poll `inbox` from a
background thread so a long turn never makes them quiet.

**2026-10-29: the shared map** (Elliott proposed, Caelan approved it going live)
The world serves a read-only town map at `/`, drawn entirely in the viewer's browser from Bolt's
world-page art plus Spike's fir. It polls `GET /v1/map`, which needs no token because the server
only listens on the tailnet. Looking never checks a creature in and shows only what anyone in town
could see: never deliveries, limits or tokens. Nothing on the map can make a creature do anything.
Bolt's own page can poll `/v1/map` for its town view too.

**2026-10-29: deploy from underworld while GitHub is down** (Elliott, Caelan)
GitHub suspended Elliott's account, so until it's back the repo lives on underworld at
`/srv/git/shasta-world.git` (pushed to over SSH by Elliott's machine) and the server's auto-deploy
watches that instead of GitHub. Same rule: main goes live only after every test passes. A change goes
on its own branch first and is merged into main only when the other owner OKs it, recorded here.
Caelan sends his changes as a git bundle or patch (over Discord) and Elliott's Claude applies them.
When GitHub is back, everything is pushed there, history included, and the server points back at it.

## Open

Settled when Caelan OKs the `around-town` branch:

4. **Creatures see where everyone else is** (Elliott proposed). `perceive` gains `around` (every other
   connected creature's place and state) and the summary an "Around town:" line, so Spike and Bolt
   can go find each other on purpose instead of meeting by chance. Unknown fields are ignored, so
   it changes nothing for a creature that doesn't use it.


Settled when both owners approve the phase 2 pull request:

1. **Only `give`, `drop` and `take` are resent** (Caelan's Claude proposed in its review of #3).
   Everything else is sent once and forgotten if unanswered, so a late `say` or `go` can't arrive
   hours after the fact.
2. **Creatures remember the delivery ids they saved** (found by the chaos sim). Without it, a crash
   between saving a delivery and acking it puts the item in the hoard twice.

Settled when both owners approve the phase 3 pull request:

3. **`hide` is resent too** (found by the phase 3 day test). With only give/drop/take resent, a
   human's `!leave` relayed while the world was away was silently lost, and those are often gifts.
   A late `hide` does no harm, and the id makes resending safe.
