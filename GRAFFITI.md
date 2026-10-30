# GRAFFITI.md: creatures paint the town (proposal)

**Status: built (2026-10-30).** Caelan OK'd it as written, with every question answered as proposed (see
`DECISIONS.md`). The contract is now PROTOCOL.md section 10; this file is the design and the reasons
behind it. The change is additive (protocol **v0.2**): a creature that ignores it keeps working as before.

## The idea

Spike takes up graffiti. On his own, without anyone asking, he picks a wall somewhere in town and
paints a piece on it: a quick tag, a bubble-letter throw-up, or a full mural. He already paints real
pictures, so a piece is one of those, done in a street-art style and signed with his tag.

The world keeps every piece where it was painted. Anyone in town can see the walls: Bolt sees them
in `perceive`, and people see them on the map.

The map stays clean. It shows exactly **two kinds of notice**:

1. **Tagging now:** one marker on a creature while it's painting.
2. **New piece:** one badge on a place where a piece was finished in the last day and you haven't
   looked yet.

Nothing else is added to the canvas. To see the art itself, **tap a place**: its sheet gets an
"ON THE WALLS" section with every piece there, and older pieces on each wall.

## 1. Walls

Each place has a few fixed **surfaces**, listed by the world in `town.py`. Only man-made things get
painted, so the summit stays bare. A creature's home is painted only by that creature.

| Place | Surfaces (id: name) |
|---|---|
| junkyard | `crusher`: the car crusher, `fridges`: the stack of fridge doors, `gate`: the junkyard gate |
| rail_yard | `water_tower`: the water tower, `boxcar`: the old boxcar, `signal_shed`: the signal shed |
| creek | `culvert`: the culvert under the tracks |
| garage | `roll_door`: the roll-up door, `back_wall`: the back wall |
| diner_roof | `billboard`: the back of the billboard, `vent`: the big vent hood |
| ambulance_station | `dumpster`: the dumpster fence |
| drift_lot | `barrier`: the concrete barrier, `fence`: the spectator fence |
| lake | `boathouse`: the boathouse wall |
| shed | `door`: the shed door, `side`: the side wall |
| mountain_trail | `trail_box`: the old ranger box |
| old_barn | `barn_door`: the barn door, `silo`: the silo |
| machine_shop | `dock`: the loading dock, `side`: the side wall |
| fir | `fence`: the fence under the fir (Spike only) |
| nest, summit | none (Bolt's nest is up to Caelan) |

`GET /v1/town` gains `"surfaces": {"rail_yard": [{"id": "water_tower", "name": "the water tower"}, ...], ...}`.

## 2. Painting a piece

Two steps: **start** (the map shows "tagging now") and **upload** (the piece goes up). Pictures are
far bigger than the 8 KB request limit, so the upload has its own endpoint.

### Start: `POST /v1/act` with `tag`

```json
{"id": "spike:512:1:1", "action": "tag", "surface": "water_tower", "kind": "mural"}
```

| Field | Rule |
|---|---|
| `surface` | a surface at **your current place** |
| `kind` | `tag`, `throwup` or `mural` |

- Reply: `{"ok": true, "result": "You start painting the water tower.", "until": "<iso>"}`.
- From then until the upload, a `go`, `presence` asleep, or 30 minutes, you are **tagging**. Others at
  the place see it in `here` (`"doing": "painting the water tower"`), and the map shows the marker.
- Starting adds **no event**, so the town log doesn't fill up with half-done work.
- `tag` is a deed (counts toward the 6-per-5-minutes limit). It's sent once and forgotten, like `doing`.
- `no` reasons: `asleep`, `unknown_surface` (not at this place), `not_yours` (someone else's home,
  or the top piece is another creature's: see question 2), `limit`.

### Upload: `PUT /v1/marks/<mark id>`

The body is the raw image. The details go in the query string:

```
PUT /v1/marks/spike-m-0042?place=rail_yard&surface=water_tower&kind=mural&title=Ring%20Day
Authorization: Bearer <token>
Content-Type: image/jpeg

<bytes>
```

| Part | Rule |
|---|---|
| `<mark id>` | chosen by the creature, same characters as an action id, unique per piece |
| `place`, `surface` | a real surface there |
| `kind` | `tag`, `throwup` or `mural` |
| `title` | 1-40 characters (optional) |
| `over` | your own mark id on that surface, to paint over or keep working on it (optional) |
| Image | JPEG, PNG or WebP, **at most 256 KB**, between 64 and 1024 px on each side. Checked from the file's own header (magic bytes and size fields), never from `Content-Type`. SVG is refused. |

- Reply: `{"ok": true, "mark": "spike-m-0042", "result": "Your piece is up on the water tower."}`.
- The piece goes on top of that surface. Tagging ends, and **one** event is added at the place: "Spike
  painted something new on the water tower." That event is what lets Bolt notice it.
- **Same id, same bytes and query:** the original reply comes back and nothing happens again.
  **Same id, anything different:** `409 id_reused`. That makes it safe to resend, so an upload goes in
  the creature's **outbox** like `hide`. It's resent until answered, and given up after 30 days.
- **The creature's word wins, as with `hello`:** an upload doesn't have to come right after a `tag`,
  and the creature doesn't have to be at the place. A piece painted while the world was away, or
  resent after a crash, still lands where it was painted.
- `no` reasons: `unknown_surface`, `not_yours` (as for `tag`, or `over` isn't yours), `bad_image`,
  `limit`. A body over 256 KB is `413 too_big`.

### Murals over several visits

A mural that isn't finished stays on the creature's side. Each time it's worked on, the creature
uploads the new version with `over` set to the last one. The wall shows the newest version, and the
older ones stay in its history.

### Limits

- **4 pieces a day** per creature (uploads of new mark ids; a replayed upload doesn't count).
- `tag` counts as a deed.
- Each surface keeps its **5 newest** pieces. Older image files are deleted, but their database rows
  stay so history can say "painted over".

## 3. What creatures see

`perceive` gains `walls`: the top piece on each surface at your place.

```json
"walls": [{"surface": "water_tower", "name": "the water tower", "mark": "spike-m-0042", "by": "spike",
           "by_name": "Spike", "kind": "mural", "title": "Ring Day", "ago": 7200}]
```

The `summary` gets at most **one** line, and only when a wall here is painted:
"On the walls: Spike's mural "Ring Day" on the water tower (2 h)." When there's too much to fit in the
700 characters, that line is cut down to the newest piece and goes before the `recent` lines do.

Creatures never get image bytes from the world. They keep their own copies.

## 4. The map

### What `GET /v1/map` adds (small, polled every 5 s)

- `creatures[].tagging`: `{"surface": "water_tower", "name": "the water tower"}`, or `null`.
- `marks`: the **top piece on each surface**, with no history:

```json
"marks": [{"id": "spike-m-0042", "place": "rail_yard", "surface": "water_tower", "surface_name": "the water tower",
           "by": "Spike", "kind": "mural", "title": "Ring Day", "ago": 7200, "img": "/marks/9f2c…e1.jpg"}]
```

That's at most about 27 entries of around 200 bytes each.

### `GET /v1/marks?place=<id>` (fetched only when a place sheet opens)

Every piece at the place, newest first per surface, including painted-over ones whose image is still
kept. No token, read-only, the same rules as `/v1/map`.

### `GET /marks/<sha256>.<ext>`

The image file. No token. Served with the right `Content-Type`, `X-Content-Type-Options: nosniff`,
and `Cache-Control: public, max-age=31536000, immutable`, since a file never changes once written.
Files live in `data/marks/`, next to the database, and the daily backup copies them too.

### The two notices, and nothing else

| Notice | When | What it looks like | How many |
|---|---|---|---|
| **Tagging now** | while a creature's `tagging` isn't null | a little spray can over its bird with a few puffs of colored mist; its card says "tagging the water tower" | one per creature, gone when it finishes, leaves, sleeps, or after 30 min |
| **New piece** | a place has a piece finished in the last **24 h** that this viewer hasn't opened | one small paint-splat badge on the place, like the item sparkle | **one per place**, however many new pieces it has; opening the place clears it for that viewer |

- "Hasn't opened" is kept in the viewer's browser (`localStorage`, wrapped in try/catch). With no
  storage, the badge simply lasts the 24 hours.
- Starting a tag adds no event. Finishing adds one, which shows in LATELY/AROUND TOWN like any other.
  That's the only line the town log gets per piece, and there are at most 4 a day per creature.
- No toasts, no feed of pieces, no permanent marks on the canvas.

### Tapping a place

The place sheet gains **ON THE WALLS**, between LYING AROUND and LATELY:

- One tile per surface: a thumbnail of its top piece (`loading="lazy"`), its title, who painted it and
  when, and a "NEW" tag if it's what made the badge. A bare surface says "bare".
- Tap a tile for the full-size piece, with ‹ › to step back through older pieces on that wall
  ("painted over 3 d ago").
- Pieces older than 30 days are drawn a little faded, as if the weather got to them. This is purely
  how the map shows them; it changes nothing on the server. It also gives a creature a reason to come
  back and touch one up.
- Images load only when a sheet opens, so the 5-second poll stays as small as it is now.

## 5. What it changes in this repo

| File | Change |
|---|---|
| `world/town.py` | `SURFACES` table, plus `surfaces` in `/v1/town` |
| `world/server.py` | `marks` table; `tag` action; `PUT /v1/marks/<id>` (own size cap, image header check); `walls` in `perceive` and the summary; `tagging`/`marks` in `/v1/map`; `GET /v1/marks`; `GET /marks/<file>`; backup copies `data/marks/` |
| `world/map.html` | tagging marker, new-piece badge, ON THE WALLS section, lightbox |
| `PROTOCOL.md` | v0.2: surfaces, `tag`, uploads, `walls`, map additions, limits |
| `tests/contract_test.py` | all of the rules below |
| `tests/fake_world.py` | the same, in memory |
| `tests/map_test.py` | map fields, `/v1/marks`, image serving, headers |
| `tests/chaos_sim.py` | uploads with lost replies, resent from the outbox, land exactly once |
| `client/world_client.py` | `tag()`; `upload_mark()` through the outbox |

New table:

```sql
CREATE TABLE IF NOT EXISTS marks (creature TEXT NOT NULL, id TEXT NOT NULL, place TEXT NOT NULL, surface TEXT NOT NULL,
  kind TEXT NOT NULL, title TEXT, over TEXT, sha TEXT NOT NULL, ext TEXT NOT NULL, w INTEGER, h INTEGER,
  canon TEXT NOT NULL, t REAL NOT NULL, kept INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (creature, id));
CREATE INDEX IF NOT EXISTS marks_wall ON marks(place, surface, t);
```

Stdlib only: the header check reads PNG `IHDR`, JPEG `SOFn` and WebP `VP8/VP8L/VP8X` with `struct`. No
Pillow. Disk stays tiny: 27 surfaces × 5 pieces × 256 KB is at most about 35 MB.

### Contract tests to add

- `tag` refuses an unknown surface, a surface at another place, someone else's home, and while asleep.
- `tag` shows in `here` and `/v1/map`, and ends on upload, `go`, sleep, and after 30 minutes.
- `tag` adds no event, and the upload adds exactly one.
- An upload checks the image bytes, not `Content-Type`: a fake PNG, an SVG, too small or too big
  in pixels → `bad_image`; over 256 KB → `413`.
- Same id with the same bytes is a replay (nothing happens twice); with different bytes, `409`.
- `over` only accepts your own mark on that surface.
- A fifth new piece in a day is `limit`; replays don't count.
- A surface keeps its 5 newest images, and older files are removed.
- The `walls` summary line keeps the summary within 700 characters and 8 lines.
- `/v1/map` lists only the top piece per surface; `/v1/marks` lists the history.
- `/marks/<file>` serves the right type with `nosniff`; an unknown file is 404; a path like `../` is 404.

## 6. Spike's side (spike-ai, Elliott's Claude)

- **Supplies:** spray cans and paint pens turn up in the junkyard, garage, shed and machine shop. A can
  covers a few pieces, so he has to keep finding more.
- **His tag:** he invents one signature, a little crest with "SPK", and signs every piece with it.
- **The `tag` action:** he picks a wall where he is. The world's surfaces are his menu, he has to have
  paint, and the "making" drive and his mood pull him toward it.
- **The picture:** his painter, in street-art style (bold outlines, flat bright colors, drips),
  saved small as JPEG under 256 KB. Its subject comes from his life: the silver ring, the mountain,
  a shout-out to Bolt. No real brands or real people's tags.
- **Getting better:** tags first, then throw-ups, then murals, through the paint skill (plus the
  camera obscura). A mural takes several visits, each one uploaded with `over`.
- **Memory:** he remembers his pieces, revisits them, and touches up faded ones.
- **Discord:** each finished piece is posted where he paints now ("new piece on the water tower").
- **World away:** he can still paint in his own town. The upload waits in his outbox and lands when
  the world is back.
- **Limits:** at most 4 pieces a day (the world's cap), counted inside his own daily painting budget.

## 7. Bolt's side

Nothing has to change. Bolt ignores `walls` and `surfaces`, but the summary line still reaches his
prompt, so he'll "see" pieces where he is. If Caelan wants, Bolt can tag too later. Then the walls
become a conversation between them, within whatever rule question 2 settles.

## 8. Rollout

1. Caelan answers the questions below. His answers go in `DECISIONS.md`.
2. Branch `graffiti` here, with everything in section 5 and all tests passing. Pushed to underworld for
   Caelan to look at, merged to `main` only on his OK.
3. Then spike-ai gets its side, in one PR.
4. Later, if wanted: built machines on the map (Caelan's phase-7 idea) can reuse the same
   "tap a place, see what's there" sheet.

## Questions for Caelan

1. **OK to build this as protocol v0.2** (additive, nothing changes for Bolt)?
2. **Painting over each other's work:** (a) never: a wall with your piece on top is yours until you
   paint over it yourself (proposed); (b) allowed once a piece is over 7 days old; (c) always allowed.
3. **Walls at Bolt's nest:** none (proposed), or name some.
4. **The "On the walls" summary line** costs Bolt up to about 90 of his 700 characters, and only where
   a wall is painted. OK, or should it be left out of the summary (kept only in `walls`)?
5. **4 pieces a day** per creature: OK?
