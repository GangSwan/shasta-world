"""The fixed town: places, homes, travel rules, background events, seasons, weather and time.

Ported from Bolt's creature.py (his places and events, the reference town) plus Spike's fir.
Stdlib only, no tzdata needed.
"""
import json
import random
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

PLACES = {
    "nest": "Bolt's nest", "junkyard": "the junkyard", "rail_yard": "the rail yard", "creek": "the creek",
    "garage": "the garage", "diner_roof": "the diner roof", "ambulance_station": "the ambulance station",
    "drift_lot": "the drift lot", "lake": "the lake", "shed": "the shed", "mountain_trail": "the mountain trail",
    "summit": "the summit", "old_barn": "the old barn", "machine_shop": "the machine shop", "fir": "Spike's fir",
}
RULES = ["The summit is only reachable from the mountain_trail.", "The summit is closed in a thunderstorm."]

# Bolt's EVENTS, made neutral where they spoke to him ("your nest" -> "the nest"), plus Spike's fir.
EVENTS = {
    "nest": ["wind rocks the branch", "a squirrel eyes the collection", "nothing happens, it's cozy"],
    "junkyard": ["a tow truck drops off a crunched car", "the guard dog snores", "sun glints off a chrome bumper"],
    "rail_yard": ["a long freight train thunders past", "a train horn echoes off the hills", "the tracks are empty and humming"],
    "creek": ["a trout jumps", "a fisherman casts a shiny lure", "a freight train rolls along the river"],
    "garage": ["the radio plays an old synth song", "someone revs an engine", "a cat sleeps on a toolbox"],
    "diner_roof": ["bacon smell drifts up the vent", "a waitress tosses out fries", "clouds wrap the mountain peak"],
    "ambulance_station": ["an ambulance rolls out with lights on", "an impact gun rattles in the shop",
                          "a mechanic swears at a stuck bolt"],
    "drift_lot": ["the white coupe links a long slide", "tire smoke drifts over the lot", "someone cheers from the fence"],
    "lake": ["an osprey dives for a fish", "a fisherman snags a willow and curses", "the mountain reflects perfectly in the water"],
    "shed": ["the welder buzzes and sparks fly", "a grinder screams", "the shed is quiet and smells like metal"],
    "mountain_trail": ["a marmot whistles", "wind howls through the rocks", "the snowline is right above the trail"],
    "old_barn": ["dust floats in a sunbeam over an old Ford", "the barn creaks warmly in the wind",
                 "an old radio in the corner plays soft country"],
    "machine_shop": ["brass curls glitter on the floor", "old blueprints flutter on the wall", "a lathe sits silent, waiting for someone"],
    "summit": ["the wind nearly knocks everyone over", "clouds roll by below", "the sun hits the ice and everything glitters"],
    "fir": ["a chickaree steals from the cone pile", "sap glows amber in the sun", "the mountain looms, enormous, right there"],
}
SEASON_EVENTS = {
    "fall": {"mountain_trail": ["golden aspen leaves skitter across the trail", "the first dusting of snow sits on the rocks"],
             "lake": ["the water is glassy and cold"], "creek": ["leaves swirl in the eddies"],
             "nest": ["the pine needles smell like cold"]},
    "winter": {"mountain_trail": ["deep snow buries the trail", "everything is silent under the snow"],
               "summit": ["the ice is thick and blue"], "lake": ["thin ice creeps out from the shore"],
               "creek": ["ice crusts the rocks at the edges"], "nest": ["frost coats the nest"]},
    "spring": {"mountain_trail": ["snowmelt trickles everywhere"], "creek": ["the river is high and loud with snowmelt"],
               "lake": ["the lake is brimming with snowmelt"]},
    "summer": {"mountain_trail": ["wildflowers carpet the meadow"], "lake": ["dragonflies zip over the water"],
               "summit": ["the snow has shrunk back to the high ice"]},
}
WEATHER_SPOT = (41.31, -122.31)          # Mount Shasta, CA
WEATHER_KINDS = ("clear", "cold and bright", "cloudy", "windy", "foggy", "light rain", "rain", "snow", "thunderstorm")


def pacific(ts):
    """Local time in America/Los_Angeles without tzdata (US rules since 2007)."""
    utc = datetime.fromtimestamp(ts, timezone.utc)

    def nth_sunday(month, n):
        d = date(utc.year, month, 1)
        return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))

    start = datetime.combine(nth_sunday(3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=10)
    end = datetime.combine(nth_sunday(11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=9)
    return utc.astimezone(timezone(timedelta(hours=-7 if start <= utc < end else -8)))


def part_of_day(hour):
    return "night" if hour < 6 or hour >= 21 else "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"


def season(month):
    return "winter" if month in (12, 1, 2) else "spring" if month in (3, 4, 5) else "summer" if month in (6, 7, 8) else "fall"


def background_event(place, ts, rng=random):
    """One plain-English background event for a place (Bolt's pick_event: no marmots September-April)."""
    lt = pacific(ts)
    pool = list(EVENTS[place]) + SEASON_EVENTS[season(lt.month)].get(place, [])
    if lt.month not in (5, 6, 7, 8):
        pool = [e for e in pool if "marmot" not in e]
        if place in ("mountain_trail", "summit"):
            pool.append("the boulder field is silent; the marmots are asleep in their burrows")
    e = rng.choice(pool)
    return e[0].upper() + e[1:] + "."


def fetch_weather(timeout=15):
    """Real Mount Shasta weather, mapped the way Bolt maps it. Returns None if the fetch fails."""
    q = urllib.parse.urlencode({"latitude": WEATHER_SPOT[0], "longitude": WEATHER_SPOT[1],
                                "current": "temperature_2m,weather_code,wind_speed_10m",
                                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto"})
    req = urllib.request.Request(f"https://api.open-meteo.com/v1/forecast?{q}",
                                 headers={"User-Agent": "shasta-world (github.com/GangSwan/shasta-world)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cur = json.loads(r.read())["current"]
        code, temp, wind = int(cur["weather_code"]), round(cur["temperature_2m"]), round(cur["wind_speed_10m"])
    except Exception:
        return None
    kind = ("thunderstorm" if code >= 95 else "snow" if code in (71, 73, 75, 77, 85, 86)
            else "rain" if code in (63, 65, 67, 81, 82) else "light rain" if 51 <= code <= 82
            else "foggy" if code in (45, 48) else "cloudy" if code in (2, 3) else "clear")
    if kind in ("clear", "cloudy") and wind >= 20:
        kind = "windy"
    if kind == "clear" and temp <= 40:
        kind = "cold and bright"
    return {"kind": kind, "temp_f": temp, "wind_mph": wind}
