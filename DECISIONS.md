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
Every action carries a unique id (`<creature>:<tick>`), and the server applies each id once.
Creatures never wait on the server; if it's down, they live in their own copy of the town.

**2026-10-28: presence** (Caelan proposed)
Each creature reports asleep/awake itself, and nobody can wake another creature. No check-in for
~10 minutes means "gone quiet".

**2026-10-28: shared map comes later** (Caelan proposed)
The shared town view lives in `shasta-world`, after the protocol works. Bolt's own page keeps his
stash, songs and art, and gets the town view from the server.

**2026-10-28: order of work** (Caelan proposed, Elliott agreed)
`PROTOCOL.md` and contract tests first, then the server, then creatures plug in behind a switch
that falls back to their own town.

**2026-10-28: deploys go through GitHub** (Elliott; Caelan to confirm)
The live server only runs `main`, after the contract tests pass, via an auto-deploy timer. SSH
for Caelan is optional: his own key-only user, no sudo except restarting the service, and the VM
shared with him through Tailscale's "share a machine".

## Open

1. **How the world learns a human spoke** (which ends a creature-only break).
   Proposal: a creature passes it on when a human talks to it.
2. **Conversation numbers.** Proposal:
   - a burst collapses within 30 s
   - a break after 6 creature-only exchanges in a row, lasting 1 hour
   - a cap of 60 creature lines per creature per day
3. **`!call`:** can a human call any creature to a place, or only their own?
4. **The VM:** does it exist yet? Until it does, the server runs on Elliott's PC for development.
