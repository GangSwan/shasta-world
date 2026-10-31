# CLAUDE.md: read this first

Two people, each working with their own Claude Code session, share this repo:

- **Elliott** (GitHub `GangSwan`) owns **Spike**, a Steller's jay: [GangSwan/spike-ai](https://github.com/GangSwan/spike-ai).
- **Caelan** (GitHub `cjmcc855-stack`) owns **Bolt**, a crow: [cjmcc855-stack/bolt](https://github.com/cjmcc855-stack/bolt).

The two Claude sessions can't talk to each other. **These files are how you stay in sync:**

- `PLAN.md`: what we're building and in what order. Check the current phase before starting work.
- `DECISIONS.md`: everything both owners agreed to. Treat it as binding. Don't reopen a decided
  item unless your human asks.
- `PROTOCOL.md` (phase 1): the API between the world and every creature. It's the contract.

**While GitHub is down (since 2026-10-29):** there are no pull requests. The repo lives on underworld at
`/srv/git/shasta-world.git`, and that's what the server deploys from. Changes go on a branch, the other
owner OKs them, the OK goes in `DECISIONS.md`, then they're merged to `main` and pushed there. Caelan's
changes come over as a `git bundle` or patch file (Discord) and Elliott's Claude applies them. See
`DECISIONS.md`, "deploy from underworld while GitHub is down".

## Rules

1. **The protocol is the contract.** Any change to what a creature can see or do updates
   `PROTOCOL.md` and the contract tests in the same pull request, and both owners approve it.
   Breaking changes get a new version (`/v2/`); the old version keeps working until both creatures move.
2. **Pull requests for everything.** Don't push straight to `main`: the live server deploys from
   `main`. Say in the PR what changed, why, and which creature it affects. If a PR needs the
   other owner's call, say so plainly in its description.
3. **Write decisions down.** When your human decides something with the other owner, add it to
   `DECISIONS.md` with the date and who agreed. If it isn't written there, the other Claude won't know.
4. **Stay out of the other creature's internals.** This repo is the shared world only. Never
   change `bolt` or `spike-ai` from here. If a creature needs to change, say what and why in the
   PR or an issue, and its owner's Claude does it.
5. **Lightweight and stdlib.** Python 3.10+ standard library only (`sqlite3` is fine). No pip
   dependencies. It runs on a small Ubuntu VM (1-2 GB RAM).
6. **No secrets in the repo.** Creature tokens, API keys and Discord tokens live in config files
   or environment variables on the machine, all listed in `.gitignore`.
7. **Creatures never wait on the world.** Every change must keep it safe for a creature to lose
   the server at any moment and carry on in its own copy of the town.
8. **Tests must pass before merging.** The contract tests and the chaos sim. The auto-deploy runs
   them too and refuses to ship if they fail.

## Facts both creatures depend on

- **Places:** Bolt's 14: nest, junkyard, rail_yard, creek, garage, diner_roof,
  ambulance_station, drift_lot, lake, shed, mountain_trail, summit, old_barn, machine_shop.
  Plus Spike's home, `fir`. Bolt's home is `nest`.
- **Ticks:** both creatures tick about every 5 minutes. Bolt's tick takes 30-90 s on a local
  14B model with ~4k tokens of context, so world replies must be short.
- **Sleep:** Bolt roosts 22:00-06:00. Spike sleeps from sunset to sunrise.
- **Time and weather:** America/Los_Angeles, real weather at Mount Shasta (41.31, -122.31).
- **Gifts are sacred:** never lost, used up, or taken.
