"""Graffiti (PROTOCOL.md v0.2): the walls of the town, and the checks on a piece's picture.

Shared by the real server and the fake world, so both agree on the walls and on what counts as an image.
Stdlib only: image sizes are read straight from the file's own header, never from Content-Type.
"""
import re
import struct

SURFACES = {   # place: {surface id: name}. Only man-made things get painted; the summit stays bare.
    "junkyard": {"crusher": "the car crusher", "fridges": "the stack of fridge doors", "gate": "the junkyard gate"},
    "rail_yard": {"water_tower": "the water tower", "boxcar": "the old boxcar", "signal_shed": "the signal shed"},
    "creek": {"culvert": "the culvert under the tracks"},
    "garage": {"roll_door": "the roll-up door", "back_wall": "the back wall"},
    "diner_roof": {"billboard": "the back of the billboard", "vent": "the big vent hood"},
    "ambulance_station": {"dumpster": "the dumpster fence"},
    "drift_lot": {"barrier": "the concrete barrier", "fence": "the spectator fence"},
    "lake": {"boathouse": "the boathouse wall"},
    "shed": {"door": "the shed door", "side": "the side wall"},
    "mountain_trail": {"trail_box": "the old ranger box"},
    "old_barn": {"barn_door": "the barn door", "silo": "the silo"},
    "machine_shop": {"dock": "the loading dock", "side": "the side wall"},
    "fir": {"fence": "the fence under the fir"},
}
KINDS = {"tag": "tag", "throwup": "throw-up", "mural": "mural"}

MAX_IMAGE = 256 * 1024
MIN_SIDE, MAX_SIDE = 64, 1024
PIECES_PER_DAY = 4
KEEP_PER_SURFACE = 5
TAG_FOR = 1800
TITLE_MAX = 40
UPLOAD_FIELDS = {"place", "surface", "kind", "title", "over"}
MARK_PATH = re.compile(r"^/v1/marks/([A-Za-z0-9:._-]{1,64})$")
FILE_PATH = re.compile(r"^/marks/([0-9a-f]{64})\.(jpg|png|webp)$")
CONTENT_TYPES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


def image_info(data):
    """(ext, width, height) for a JPEG, PNG or WebP, from its own bytes. None for anything else."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return "png", w, h
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                size = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return "jpg", w, h
                i += 2 + size
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", data[26:30])
                return "webp", w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L" and data[20] == 0x2F:
                bits = struct.unpack("<I", data[21:25])[0]
                return "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return "webp", w, h
    except (struct.error, IndexError):
        return None
    return None


def check_image(data):
    """(ext, w, h) if the picture is acceptable, else a plain-English reason (a str)."""
    info = image_info(data)
    if info is None:
        return "That isn't a JPEG, PNG or WebP picture."
    ext, w, h = info
    if not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE):
        return f"Pictures must be {MIN_SIDE}-{MAX_SIDE} pixels on each side (this one is {w}x{h})."
    return info


def upload_fields(query):
    """The query string of PUT /v1/marks/<id>, checked. Raises ValueError with a message for a 400."""
    extra = set(query) - UPLOAD_FIELDS
    if extra:
        raise ValueError(f"Unknown fields: {', '.join(sorted(extra))}.")
    out = {}
    for k in UPLOAD_FIELDS:
        vals = query.get(k)
        if vals and len(vals) > 1:
            raise ValueError(f"{k} given twice.")
        v = re.sub(r"[\x00-\x1f\x7f]", " ", vals[0]).strip() if vals else None
        out[k] = v or None
    for k in ("place", "surface", "kind"):
        if not out[k]:
            raise ValueError(f"{k} is required.")
    if out["kind"] not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}.")
    if out["title"] and len(out["title"]) > TITLE_MAX:
        raise ValueError(f"title must be 1-{TITLE_MAX} characters.")
    if out["over"] and not re.fullmatch(r"[A-Za-z0-9:._-]{1,64}", out["over"]):
        raise ValueError("over must be a mark id.")
    return out


def surface_name(place, surface):
    return SURFACES.get(place, {}).get(surface)


def ago_text(seconds):
    s = max(0, int(seconds))
    return "just now" if s < 60 else f"{s // 60} min" if s < 3600 else f"{s // 3600} h" if s < 86400 else f"{s // 86400} d"


def walls_line(walls):
    """One summary line for the painted walls here (newest piece first), or None. About 90 characters."""
    if not walls:
        return None
    w = min(walls, key=lambda x: x["ago"])
    title = f' "{w["title"]}"' if w["title"] else ""
    more = f", and {len(walls) - 1} more" if len(walls) > 1 else ""
    return f"On the walls: {w['by_name']}'s {KINDS[w['kind']]}{title} on {w['name']} ({ago_text(w['ago'])}){more}."
