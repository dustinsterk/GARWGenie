"""GARW Genie — Lap Timer support: the tracks database format and a tiny local web server that
hosts a Leaflet/Esri map editor for one track at a time (Tk cannot draw web maps itself).

tracks.txt format (LapTimer on the device):
    #LAPTIMER_TRACKS v1|<count><RS>\\n
    name|region|cc|country|sf_lat,sf_lng|radius_m|sec1;sec2;…|pit_entry|pit_sf|pit_exit|centre<RS>\\n
Fields are '|'-separated, coordinates are 'lat,lng' with 6 decimals, sectors are ';'-separated,
every record ends with ASCII RS (0x1e) followed by a newline.
Point-to-point courses (autocross, hillclimb) add a 12th field, the finish point, after the centre —
and optionally a 13th, the compass bearing (0–359) a run leaves the start in:
    …|centre|finLat,finLng|startHdg
With a finish present the dash times start→finish instead of lap to lap; pit points are ignored.
"""
from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

RS = "\x1e"
HEADER_RE = re.compile(r"^#LAPTIMER_TRACKS v(\d+)\|(\d+)$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
MAC_ANY_RE = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
RACEBOX_TEMPLATE = ("{mac}\n"
                    "# Your RaceBox Bluetooth MAC address goes on the first line (e.g. D4:F7:FA:9E:08:97).\n"
                    "# Write \"off\" on the first line to not use a RaceBox.\n")


def racebox_parse(data: bytes) -> Tuple[Optional[str], str]:
    """-> (value, status). value is 'AA:BB:CC:DD:EE:FF', 'off', or None (file empty / nothing usable).
    Mirrors the dash: 'off' on the first line disables the RaceBox; otherwise the first MAC anywhere in
    the file is used, in any case, with ':' or '-'. Status describes what was found."""
    text = data.decode("utf-8", "replace").replace("\r", "")
    first = text.split("\n", 1)[0].strip()
    if first.lower().startswith("off"):
        return "off", "RaceBox disabled ('off' on the first line)"
    m = MAC_ANY_RE.search(text)
    if m:
        return m.group(0).upper().replace("-", ":"), "MAC found"
    return None, "no MAC in the file (the dash falls back to its built-in default)"


def racebox_render(value: str, existing: Optional[bytes]) -> bytes:
    """New file contents: the value on line 1, the rest of the existing file (its comments) kept."""
    rest = ""
    if existing:
        parts = existing.decode("utf-8", "replace").replace("\r", "").split("\n", 1)
        rest = parts[1] if len(parts) > 1 else ""
        if not rest.strip():
            rest = ""
    if not rest:
        return RACEBOX_TEMPLATE.format(mac=value).encode("utf-8")
    return (value + "\n" + rest.rstrip("\n") + "\n").encode("utf-8")
LAPTIMER_FILE = "/opt/IC7/library/LapTimer.enc"  # the LapTimer dash (encrypted) — an optional add-on, not on every device
LAPTIMER_DATA_DIR = "/opt/IC7/laptimerdata"      # its data folder; made by the dash, never by this tool
RACEBOX_MAC_FILE = LAPTIMER_DATA_DIR + "/Racebox_MAC_Address.txt"
USER_TRACKS_FILE = LAPTIMER_DATA_DIR + "/UserTracks.txt"   # the only track file the tool reads or writes on the device
USER_TRACKS_TEMPLATE = """# UserTracks.txt - your own tracks, added to the LapTimer's track library.
# One track per line, same format as the library:
#   name|state|cc|country|startLat,startLon|width|splits|pitIn|pitSF|pitOut|ctrLat,ctrLon[|finLat,finLon|startHdg]
# - keep all 11 standard fields (empty ones as ||), splits separated by ;
# - add finLat,finLon (and startHdg) for a point-to-point course
# - a line with the same name as a library track replaces that track
#   (TrackList.txt lists the library's names, regions and countries)
# - lines starting with # are ignored; plain newlines are fine
# Example (remove the # to use it):
#Test Course|KS|US|United States|39.000000,-99.000000|20|39.002000,-99.000000||||39.000000,-99.000000|39.000000,-98.999700|0
"""
MAX_SECTORS = 7
RADIUS_DEFAULT, RADIUS_MIN, RADIUS_MAX = 50, 10, 300   # metres; the database uses 10–150, mostly 50 and 20

LatLng = Tuple[float, float]


def fmt_pt(p: Optional[LatLng]) -> str:
    return f"{p[0]:.6f},{p[1]:.6f}" if p else ""


def bearing(a: LatLng, b: LatLng) -> int:
    """Initial compass bearing from a to b, 0 = north, 90 = east."""
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
    return int(round(math.degrees(math.atan2(x, y)))) % 360


def parse_pt(s: str) -> Optional[LatLng]:
    s = (s or "").strip()
    if not s:
        return None
    a, b = s.split(",")
    return float(a), float(b)


@dataclass
class Track:
    name: str
    region: str = ""
    cc: str = ""
    country: str = ""
    sf: Optional[LatLng] = None
    radius: int = 50
    sectors: List[LatLng] = field(default_factory=list)
    pit_entry: Optional[LatLng] = None
    pit_sf: Optional[LatLng] = None
    pit_exit: Optional[LatLng] = None
    centre: Optional[LatLng] = None
    finish: Optional[LatLng] = None       # point-to-point course: separate finish (12th field)
    start_hdg: Optional[int] = None       # bearing a run leaves the start in, degrees (13th field)

    @property
    def point_to_point(self) -> bool:
        return self.finish is not None

    @classmethod
    def from_line(cls, line: str) -> "Track":
        f = line.split("|")
        if len(f) < 11 or len(f) > 13:
            raise ValueError(f"expected 11–13 fields, got {len(f)}: {line[:60]}")
        hdg = None
        if len(f) >= 13 and f[12].strip():
            hdg = int(round(float(f[12]))) % 360
        return cls(name=f[0], region=f[1], cc=f[2], country=f[3], sf=parse_pt(f[4]),
                   radius=int(float(f[5])) if f[5].strip() else 50,
                   sectors=[parse_pt(p) for p in f[6].split(";") if p.strip()],
                   pit_entry=parse_pt(f[7]), pit_sf=parse_pt(f[8]), pit_exit=parse_pt(f[9]), centre=parse_pt(f[10]),
                   finish=parse_pt(f[11]) if len(f) >= 12 else None, start_hdg=hdg)

    def to_line(self) -> str:
        fields = [self.name, self.region, self.cc, self.country, fmt_pt(self.sf), str(self.radius),
                  ";".join(fmt_pt(p) for p in self.sectors), fmt_pt(self.pit_entry), fmt_pt(self.pit_sf),
                  fmt_pt(self.pit_exit), fmt_pt(self.centre or self.auto_centre())]
        if self.finish:                      # circuits keep the plain 11-field line, byte for byte
            fields.append(fmt_pt(self.finish))
            if self.start_hdg is not None:
                fields.append(str(int(self.start_hdg) % 360))
        return "|".join(fields)

    def points(self) -> List[LatLng]:
        return [p for p in [self.sf, *self.sectors, self.finish, self.pit_entry, self.pit_sf, self.pit_exit] if p]

    def suggested_heading(self) -> Optional[int]:
        """Bearing from the start to the first split (or the finish) — a rough start direction."""
        nxt = self.sectors[0] if self.sectors else self.finish
        if not self.sf or not nxt:
            return None
        return bearing(self.sf, nxt)

    def auto_centre(self) -> Optional[LatLng]:
        pts = self.points()
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def problems(self) -> List[str]:
        out = []
        if not self.name.strip():
            out.append("name is empty")
        if "|" in self.name or RS in self.name:
            out.append("name may not contain '|'")
        if not self.sf:
            out.append("no start/finish point")
        if len(self.sectors) > MAX_SECTORS:
            out.append(f"more than {MAX_SECTORS} sectors")
        if not (RADIUS_MIN <= self.radius <= RADIUS_MAX):
            out.append(f"radius must be {RADIUS_MIN}–{RADIUS_MAX} m")
        if not self.point_to_point and (self.pit_entry is None) != (self.pit_exit is None):
            out.append("pit entry and pit exit go together")
        if self.start_hdg is not None and not (0 <= self.start_hdg < 360):
            out.append("start direction must be 0–359°")
        return out

    def to_json(self) -> dict:
        return {"name": self.name, "region": self.region, "cc": self.cc, "country": self.country,
                "sf": self.sf, "radius": self.radius, "sectors": self.sectors, "pit_entry": self.pit_entry,
                "pit_sf": self.pit_sf, "pit_exit": self.pit_exit, "centre": self.centre or self.auto_centre(),
                "finish": self.finish, "start_hdg": self.start_hdg}

    @classmethod
    def from_json(cls, d: dict) -> "Track":
        pt = lambda v: (float(v[0]), float(v[1])) if v else None  # noqa: E731
        return cls(name=str(d.get("name", "")).strip(), region=str(d.get("region", "")).strip(),
                   cc=str(d.get("cc", "")).strip().upper()[:2], country=str(d.get("country", "")).strip(),
                   sf=pt(d.get("sf")), radius=int(d.get("radius") or RADIUS_DEFAULT),
                   sectors=[pt(p) for p in d.get("sectors") or [] if p], pit_entry=pt(d.get("pit_entry")),
                   pit_sf=pt(d.get("pit_sf")), pit_exit=pt(d.get("pit_exit")), centre=pt(d.get("centre")),
                   finish=pt(d.get("finish")),
                   start_hdg=(int(round(float(d["start_hdg"]))) % 360) if d.get("start_hdg") not in (None, "") else None)


class TrackDB:
    """The whole tracks.txt in memory. parse() → serialize() reproduces the input byte for byte."""

    def __init__(self) -> None:
        self.tracks: List[Track] = []
        self.version = 1

    @classmethod
    def parse(cls, data: bytes) -> "TrackDB":
        db = cls()
        text = data.decode("utf-8")
        recs = [r.lstrip("\n").rstrip("\n") for r in text.split(RS)]
        recs = [r for r in recs if r.strip()]
        if not recs:
            return db
        m = HEADER_RE.match(recs[0])
        if m:
            db.version = int(m.group(1))
            recs = recs[1:]
        for r in recs:
            db.tracks.append(Track.from_line(r))
        return db

    def serialize(self) -> bytes:
        out = [f"#LAPTIMER_TRACKS v{self.version}|{len(self.tracks)}{RS}\n"]
        out += [t.to_line() + RS + "\n" for t in self.tracks]
        return "".join(out).encode("utf-8")

    def find(self, name: str) -> Optional[int]:
        return next((i for i, t in enumerate(self.tracks) if t.name == name), None)

    def sorted_view(self, query: str = "") -> List[int]:
        q = query.strip().lower()
        idx = [i for i, t in enumerate(self.tracks)
               if not q or q in t.name.lower() or q in t.region.lower() or q in t.country.lower() or q in t.cc.lower()]
        return sorted(idx, key=lambda i: (self.tracks[i].country.lower(), self.tracks[i].name.lower()))


@dataclass
class LibraryEntry:
    """A track the LapTimer dash ships with (from TrackList.txt): name, where it is, rough centre."""
    name: str
    region: str = ""
    cc: str = ""
    country: str = ""
    centre: Optional[LatLng] = None


class TrackLibrary:
    """TrackList.txt — read-only list of the dash's built-in tracks (2 dp centres)."""

    def __init__(self) -> None:
        self.entries: List[LibraryEntry] = []
        self.by_name: dict = {}

    @classmethod
    def parse(cls, data: bytes) -> "TrackLibrary":
        lib = cls()
        for line in data.decode("utf-8", "replace").replace("\r", "").split("\n"):
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("|")
            if len(f) < 4:
                continue
            e = LibraryEntry(name=f[0].strip(), region=f[1].strip(), cc=f[2].strip(), country=f[3].strip(),
                             centre=parse_pt(f[4]) if len(f) > 4 and f[4].strip() else None)
            lib.entries.append(e)
            lib.by_name[e.name] = e
        return lib


class UserTrackDB:
    """UserTracks.txt — the user's own tracks: plain newline-separated lines, '#' comments kept,
    no header, no record separators. Same per-line format as the library (11–13 fields)."""

    def __init__(self) -> None:
        self.tracks: List[Track] = []
        self.comments: List[str] = []    # leading comment block, kept verbatim on save

    @classmethod
    def parse(cls, data: bytes) -> "UserTrackDB":
        db = cls()
        bad = []
        for raw in data.decode("utf-8", "replace").replace("\r", "").replace(RS, "").split("\n"):
            line = raw.rstrip()
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                db.comments.append(line)
                continue
            try:
                db.tracks.append(Track.from_line(line))
            except Exception as e:  # noqa: BLE001
                bad.append(f"{line[:50]}… ({e})")
        if bad:
            raise ValueError("Unreadable line(s) in UserTracks.txt:\n" + "\n".join(bad[:5]))
        return db

    def serialize(self) -> bytes:
        comments = self.comments or USER_TRACKS_TEMPLATE.rstrip("\n").split("\n")
        return ("\n".join(comments) + "\n" + "".join(t.to_line() + "\n" for t in self.tracks)).encode("utf-8")

    def find(self, name: str) -> Optional[int]:
        return next((i for i, t in enumerate(self.tracks) if t.name == name), None)


def parse_google_maps_link(text: str) -> Optional[LatLng]:
    """lat,lng out of a pasted Google Maps URL (or a plain 'lat, lng')."""
    s = (text or "").strip()
    if not s:
        return None
    for pat in (r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",      # place pin
                r"@(-?\d+\.\d+),(-?\d+\.\d+)",          # map centre
                r"[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)",   # ?q=lat,lng
                r"[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)",
                r"^\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*$"):
        m = re.search(pat, s)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None


NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_geocode_cache: dict = {}


def parse_admin_tracks(data: bytes):
    """Tracks.txt for admin editing: the full library in the legacy record-separated format (TrackDB,
    byte-identical round trip) — or, if it's a plain newline file, the UserTracks format."""
    text = data.decode("utf-8", "replace")
    if RS in text or text.lstrip().startswith("#LAPTIMER_TRACKS"):
        return TrackDB.parse(data)
    return UserTrackDB.parse(data)


def tracklist_text(db) -> str:
    """TrackList.txt (the stripped list GARW Genie ships) generated from a full track database:
    name|region|cc|country|ctrLat,ctrLon, centres to 2 decimals, in the database's own order."""
    lines = [f"# LapTimer track list ({len(db.tracks)} tracks): name|region|cc|country|ctrLat,ctrLon",
             "# Centres are approximate (2 decimals). To add or change a track, use UserTracks.txt."]
    for t in db.tracks:
        c = t.centre or t.auto_centre()
        lines.append(f"{t.name}|{t.region}|{t.cc}|{t.country}|{c[0]:.2f},{c[1]:.2f}" if c else
                     f"{t.name}|{t.region}|{t.cc}|{t.country}|")
    return "\n".join(lines) + "\n"


def reverse_geocode(lat: float, lng: float, user_agent: str = "GARW-Genie track editor") -> dict:
    """Region / country for a point, via OpenStreetMap's Nominatim (free; ~1 request/s, identify
    yourself — hence the User-Agent). -> {region, cc, country, place} with '' where unknown."""
    import json as _json
    import ssl
    import urllib.parse
    import urllib.request
    key = (round(lat, 3), round(lng, 3))
    if key in _geocode_cache:
        return _geocode_cache[key]
    url = NOMINATIM_URL + "?" + urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lng:.6f}",
                                                       "zoom": 14, "addressdetails": 1, "accept-language": "en"})
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
    a = data.get("address", {}) or {}
    region = a.get("state") or a.get("region") or a.get("province") or a.get("state_district") or a.get("county") or ""
    place = data.get("name") or ""
    # a circuit tends to come back as a leisure/sport feature; keep only names that look like one
    if place and not re.search(r"circuit|raceway|speedway|motor|race|track|ring|autodrom|park|kart", place, re.I):
        place = ""
    out = {"region": region, "cc": (a.get("country_code") or "").upper(), "country": a.get("country") or "", "place": place}
    _geocode_cache[key] = out
    return out


def google_maps_url(p: LatLng, zoom: int = 17) -> str:
    return f"https://www.google.com/maps/@{p[0]:.6f},{p[1]:.6f},{zoom}z/data=!3m1!1e3"


# --------------------------------------------------------------------------- #
#  Local editor server
# --------------------------------------------------------------------------- #
EDITOR_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>GARW Genie — Track editor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/leaflet/leaflet.css">
<script src="/leaflet/leaflet.js"></script>
<script>if (typeof L === "undefined") { document.write('<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"><script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"><\/script>'); }</script>
<style>
 :root{--bg:#14171c;--panel:#1b1f26;--field:#1f242c;--line:#2a303a;--text:#e6e8ec;--muted:#8b93a1;--accent:#ff7a1a;--ok:#5fd38a;--err:#ff6b6b}
 html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
 #app{display:grid;grid-template-columns:360px 1fr;height:100%}
 #side{background:var(--panel);border-right:1px solid var(--line);padding:14px;overflow:auto}
 #map{height:100%}
 h1{font-size:16px;margin:0 0 10px;color:var(--accent)}
 label{display:block;color:var(--muted);font-size:12px;margin-top:8px}
 input,select{width:100%;box-sizing:border-box;background:var(--field);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
 .row{display:flex;gap:6px}.row>*{flex:1}
 button{background:var(--field);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:7px 10px;font:inherit;cursor:pointer}
 button:hover{border-color:var(--accent)} button.accent{background:var(--accent);color:#111318;border-color:var(--accent);font-weight:600}
 button.tool.on{outline:2px solid var(--accent)}
 .tools{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}
 .tools button{text-align:left} .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
 ul{list-style:none;padding:0;margin:6px 0} li{display:flex;align-items:center;gap:6px;padding:3px 0;color:var(--muted);font-size:12px}
 li b{color:var(--text)} li button{padding:2px 6px;font-size:11px}
 #status{margin-top:10px;font-size:12px;color:var(--muted);min-height:18px} .ok{color:var(--ok)} .err{color:var(--err)}
 .hint{font-size:12px;color:var(--muted);margin-top:6px}
 .leaflet-container{background:#000}
 .lbl{background:rgba(20,23,28,.85);color:#fff;border:1px solid var(--line);border-radius:4px;padding:1px 5px;font-size:11px;white-space:nowrap}
 #toast{position:fixed;left:50%;top:18px;transform:translateX(-50%) translateY(-30px);background:var(--ok);color:#111318;font-weight:700;font-size:16px;padding:12px 22px;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.6);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:1000;max-width:70vw;text-align:center}
 #toast.show{opacity:1;transform:translateX(-50%) translateY(0)} #toast.err{background:var(--err);color:#fff}
 #save.saved{background:var(--ok);border-color:var(--ok);color:#111318}
 #unsaved{display:none;color:var(--accent);font-size:12px;margin-top:6px} #unsaved.show{display:block}
</style></head><body><div id="app"><div id="side">
<h1>GARW Genie · Track editor</h1>
<label>Track name</label><input id="name">
<div class="row"><div><label>Region</label><input id="region"></div><div><label>Country code</label><input id="cc" maxlength="2"></div></div>
<label>Country</label><input id="country">
<div class="row" style="margin-top:6px"><button id="geo">Fill region / country from the start / finish location</button></div>
<label>Radius (m) — how close the car must pass a point to trigger it (10–300; 50 suits most circuits, 20 for small or kart tracks)</label><input id="radius" type="number" min="10" max="300" step="5" value="50">
<label>Place on the map (pick a tool, then click; drag any marker to move it)</label>
<div class="tools">
 <button class="tool" data-t="sf"><span class="dot" style="background:#ff7a1a"></span>Start / finish</button>
 <button class="tool" data-t="sector"><span class="dot" style="background:#3b9cff"></span>Add sector</button>
 <button class="tool" data-t="pit_entry"><span class="dot" style="background:#ffd23b"></span>Pit entry</button>
 <button class="tool" data-t="pit_sf"><span class="dot" style="background:#c08bff"></span>Pit start / finish</button>
 <button class="tool" data-t="pit_exit"><span class="dot" style="background:#5fd38a"></span>Pit exit</button>
 <button class="tool" data-t="centre"><span class="dot" style="background:#ffffff"></span>Track centre</button>
 <button class="tool" data-t="finish"><span class="dot" style="background:#ff3b6b"></span>Finish (point-to-point)</button>
 <button class="tool" data-t="hdg"><span class="dot" style="background:#ffffff;border:2px solid #ff7a1a;box-sizing:border-box"></span>Start direction: click ahead</button>
</div>
<div class="row" style="margin-top:6px;align-items:end"><div style="flex:0 0 150px"><label>Start direction (° , 0 = N)</label><input id="hdg" type="number" min="0" max="359" placeholder="not set"></div>
<div class="hint" id="kind" style="margin:0 0 6px 8px">Circuit — laps time start/finish to start/finish.</div></div>
<div class="hint" id="hdghint"></div>
<ul id="list"></ul>
<label>Jump to… (paste a Google Maps link, or lat, lng)</label>
<div class="row"><input id="jump" placeholder="https://www.google.com/maps/@…  or  51.0, -1.0"><button id="go" style="flex:0 0 auto">Go</button></div>
<div class="row" style="margin-top:8px"><button id="gmaps">Open this view in Google Maps</button><button id="fit">Fit track</button></div>
<div class="row" style="margin-top:12px"><button id="save" class="accent">Save track</button><button id="close">Close</button></div>
<div id="unsaved">● Unsaved changes</div>
<div id="status"></div>
<div id="toast"></div>
<div class="hint">Imagery © Esri, Maxar, Earthstar Geographics. Right-click a marker to delete it. Saving writes to your local UserTracks.txt in GARW Genie; upload it to the device from the Lap Timer tab.</div>
</div><div id="map"></div></div>
<script>
const q = new URLSearchParams(location.search); const idx = parseInt(q.get("i") ?? "-1", 10); const libName = q.get("lib");
const COL = {sf:"#ff7a1a", sector:"#3b9cff", pit_entry:"#ffd23b", pit_sf:"#c08bff", pit_exit:"#5fd38a", centre:"#ffffff", finish:"#ff3b6b"};
const NAMES = {sf:"Start / finish", sector:"Sector", pit_entry:"Pit entry", pit_sf:"Pit start / finish", pit_exit:"Pit exit", centre:"Track centre", finish:"Finish"};
let hdgArrow = null;
function bearing(a, b){ const r = Math.PI/180, la1 = a.lat*r, la2 = b.lat*r, dl = (b.lng-a.lng)*r;
  const x = Math.sin(dl)*Math.cos(la2), y = Math.cos(la1)*Math.sin(la2) - Math.sin(la1)*Math.cos(la2)*Math.cos(dl);
  return (Math.round(Math.atan2(x, y)/r) + 360) % 360; }
function destPoint(a, brgDeg, metres){ const R = 6371000, r = Math.PI/180, d = metres/R, b = brgDeg*r, la1 = a.lat*r, lo1 = a.lng*r;
  const la2 = Math.asin(Math.sin(la1)*Math.cos(d) + Math.cos(la1)*Math.sin(d)*Math.cos(b));
  const lo2 = lo1 + Math.atan2(Math.sin(b)*Math.sin(d)*Math.cos(la1), Math.cos(d) - Math.sin(la1)*Math.sin(la2));
  return L.latLng(la2/r, lo2/r); }
let tool = null, track = null, markers = {sectors: []}, dirty = false, lapLine = null;
const map = L.map("map", {zoomControl:true}).setView([20, 0], 2);
const esri = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {maxZoom: 20, attribution: "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"}).addTo(map);
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: "© OpenStreetMap contributors"});
L.control.layers({"Satellite (Esri)": esri, "Streets (OSM)": osm}).addTo(map);
L.control.scale().addTo(map);
function status(msg, cls){ const s=document.getElementById("status"); s.textContent=msg; s.className=cls||""; }
let toastTimer = null;
function toast(msg, isErr){ const t = document.getElementById("toast"); t.textContent = msg; t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ""; }, isErr ? 5000 : 3200); }
function markDirty(d){ dirty = d; document.getElementById("unsaved").classList.toggle("show", d); document.title = (d ? "● " : "") + "GARW Genie — Track editor"; }
function mk(kind, latlng, n){
  const m = L.circleMarker(latlng, {radius: kind==="sf"?9:7, color: COL[kind], fillColor: COL[kind], fillOpacity: .85, weight: 2}).addTo(map);
  m.kind = kind; m.n = n;
  m.bindTooltip(kind==="sector" ? `S${n}` : NAMES[kind], {permanent: true, direction: "top", className: "lbl", offset: [0, -8]});
  // circleMarker isn't draggable: emulate with mousedown/move
  m.on("mousedown", e => { map.dragging.disable(); const mv = ev => { m.setLatLng(ev.latlng); }; map.on("mousemove", mv);
     map.once("mouseup", () => { map.off("mousemove", mv); map.dragging.enable(); markDirty(true); render(); }); });
  m.on("contextmenu", () => { removeMarker(m); });
  return m;
}
function removeMarker(m){
  map.removeLayer(m);
  if (m.kind === "sector") markers.sectors = markers.sectors.filter(x => x !== m); else delete markers[m.kind];
  markDirty(true); render();
}
function setPoint(kind, latlng){
  if (kind === "hdg"){ if (!markers.sf) { status("Place the start first, then click a point the car heads towards", "err"); return; }
    document.getElementById("hdg").value = bearing(markers.sf.getLatLng(), latlng); markDirty(true); render(); return; }
  if (kind === "sector"){ if (markers.sectors.length >= 7) { status("Maximum 7 sectors", "err"); return; } markers.sectors.push(mk("sector", latlng, markers.sectors.length+1)); }
  else { if (markers[kind]) map.removeLayer(markers[kind]); markers[kind] = mk(kind, latlng); }
  markDirty(true); render();
  if (kind === "sf" && !v("region") && !v("country") && !v("cc")) geocode(false);   // new track: fill the blanks
}
function isP2P(){ return !!markers.finish; }
async function geocode(force){
  if (!markers.sf) { status("Place the start / finish first", "err"); return; }
  const p = markers.sf.getLatLng(); status("Looking up region / country …", "");
  try {
    const r = await fetch(`/api/geocode?lat=${p.lat.toFixed(6)}&lng=${p.lng.toFixed(6)}`); const j = await r.json();
    if (j.error) { status("Lookup failed: " + j.error, "err"); return; }
    const set = (id, val) => { if (val && (force || !v(id))) { document.getElementById(id).value = val; markDirty(true); } };
    set("region", j.region); set("country", j.country); set("cc", j.cc); if (!v("name")) set("name", j.place);
    status(`Location: ${[j.region, j.country].filter(Boolean).join(", ") || "unknown"}${j.place ? " · " + j.place : ""} (OpenStreetMap)`, "ok");
  } catch (e) { status("Lookup failed — no internet?", "err"); }
}
document.getElementById("geo").onclick = () => geocode(true);
function render(){
  markers.sectors.forEach((m,i) => { m.n = i+1; m.setTooltipContent(`S${i+1}`); });
  if (lapLine) { map.removeLayer(lapLine); lapLine = null; }
  if (markers.sf && (markers.sectors.length || markers.finish)) {
    const pts = [markers.sf.getLatLng(), ...markers.sectors.map(m => m.getLatLng()), (markers.finish || markers.sf).getLatLng()];
    lapLine = L.polyline(pts, {color: isP2P() ? "#ff3b6b" : "#3b9cff", weight: 2, dashArray: "6 6", opacity: .7}).addTo(map); }
  // start-direction arrow (typed value, or the suggested one from the first split / finish)
  if (hdgArrow) { map.removeLayer(hdgArrow); hdgArrow = null; }
  const hh = document.getElementById("hdghint");
  if (markers.sf) { const typed = v("hdg"); const nxt = markers.sectors[0] || markers.finish;
    if (typed !== "") {   // set by you: solid arrow — this is what gets written to the file
      const h = +typed, a = markers.sf.getLatLng(), tip = destPoint(a, h, 60), l1 = destPoint(tip, h + 150, 18), l2 = destPoint(tip, h - 150, 18);
      hdgArrow = L.polyline([[a, tip], [l1, tip, l2]], {color: "#ff7a1a", weight: 3, opacity: .9}).addTo(map);
      hh.textContent = `Start direction ${h}° will be saved.`; hh.style.color = "";
    } else {
      // Not set: nothing is written; the dash learns the direction from the first start crossing. Show the
      // straight line to S1 only as a faint reference — on a hairpin start it points the WRONG way.
      if (nxt) { const g = bearing(markers.sf.getLatLng(), nxt.getLatLng()), a = markers.sf.getLatLng();
        hdgArrow = L.polyline([a, destPoint(a, g, 60)], {color: "#8b93a1", weight: 2, opacity: .6, dashArray: "2 6"}).addTo(map); }
      hh.textContent = isP2P()
        ? "Start direction not set — the dash will learn it from your first start crossing (a wrong-way crossing before the first run starts a bogus run). Set it with 'click ahead': click a spot the car heads towards just after the start." + (nxt ? ` The grey dashes point straight at S1 — that is NOT the direction if the start is followed by a hairpin.` : "")
        : "Optional. Set it with 'click ahead' if you want the dash to know which way runs leave the start.";
      hh.style.color = isP2P() ? "var(--accent)" : "";
    } } else hh.textContent = "";
  const p2p = isP2P();
  document.getElementById("kind").textContent = p2p ? "Point-to-point — runs time start → finish; pit points are ignored." : "Circuit — laps time start/finish to start/finish.";
  document.querySelectorAll(".tool").forEach(b => { if (["pit_entry","pit_sf","pit_exit"].includes(b.dataset.t)) { b.disabled = p2p; b.style.opacity = p2p ? .4 : 1; } });
  document.querySelector(".tool[data-t=sf]").lastChild.textContent = p2p ? "Start" : "Start / finish";
  if (markers.sf) markers.sf.setTooltipContent(p2p ? "Start" : "Start / finish");
  const ul = document.getElementById("list"); ul.innerHTML = "";
  const rows = [];
  for (const k of ["sf","finish","centre","pit_entry","pit_sf","pit_exit"]) if (markers[k]) rows.push([k === "sf" && isP2P() ? "Start" : NAMES[k], markers[k]]);
  markers.sectors.forEach((m,i) => rows.push([`Sector ${i+1}`, m]));
  for (const [label, m] of rows){
    const ll = m.getLatLng(); const li = document.createElement("li");
    li.innerHTML = `<span class="dot" style="background:${COL[m.kind]}"></span><b>${label}</b> ${ll.lat.toFixed(6)}, ${ll.lng.toFixed(6)}`;
    const b1 = document.createElement("button"); b1.textContent = "go"; b1.onclick = () => map.setView(ll, Math.max(map.getZoom(), 17));
    const b2 = document.createElement("button"); b2.textContent = "✕"; b2.onclick = () => removeMarker(m);
    if (m.kind === "sector"){ const up = document.createElement("button"); up.textContent = "↑"; up.onclick = () => { const i = markers.sectors.indexOf(m); if (i>0){ [markers.sectors[i-1], markers.sectors[i]] = [markers.sectors[i], markers.sectors[i-1]]; markDirty(true); render(); } }; li.appendChild(up); }
    li.appendChild(b1); li.appendChild(b2); ul.appendChild(li);
  }
}
document.querySelectorAll(".tool").forEach(b => b.onclick = () => { tool = b.dataset.t === tool ? null : b.dataset.t; document.querySelectorAll(".tool").forEach(x => x.classList.toggle("on", x.dataset.t === tool)); map.getContainer().style.cursor = tool ? "crosshair" : ""; });
map.on("click", e => { if (tool) setPoint(tool, e.latlng); });
function ll(m){ if(!m) return null; const p = m.getLatLng(); return [+p.lat.toFixed(6), +p.lng.toFixed(6)]; }
function collect(){
  return {name: v("name"), region: v("region"), cc: v("cc"), country: v("country"), radius: Math.min(300, Math.max(10, parseInt(v("radius")||"50",10) || 50)),
          sf: ll(markers.sf), sectors: markers.sectors.map(ll), pit_entry: ll(markers.pit_entry), pit_sf: ll(markers.pit_sf), pit_exit: ll(markers.pit_exit), centre: ll(markers.centre),
          finish: ll(markers.finish), start_hdg: v("hdg") === "" ? null : +v("hdg")};
}
function v(id){ return document.getElementById(id).value.trim(); }
function load(t){
  track = t; for (const k of ["name","region","cc","country","radius"]) document.getElementById(k).value = t[k] ?? "";
  if (!document.getElementById("radius").value) document.getElementById("radius").value = 50;
  for (const k of ["sf","pit_entry","pit_sf","pit_exit","centre","finish"]) if (t[k]) markers[k] = mk(k, t[k]);
  if (t.library_prefill_no_centre_marker) {}
  document.getElementById("hdg").value = (t.start_hdg === null || t.start_hdg === undefined) ? "" : t.start_hdg;
  (t.sectors||[]).forEach((p,i) => markers.sectors.push(mk("sector", p, i+1)));
  render(); fit();
}
function fit(){
  const pts = []; for (const k of ["sf","pit_entry","pit_sf","pit_exit","centre","finish"]) if (markers[k]) pts.push(markers[k].getLatLng()); markers.sectors.forEach(m => pts.push(m.getLatLng()));
  if (pts.length > 1) map.fitBounds(L.latLngBounds(pts).pad(0.3)); else if (pts.length === 1) map.setView(pts[0], 16);
}
document.getElementById("fit").onclick = fit;
document.getElementById("go").onclick = async () => {
  const r = await fetch("/api/parse_link?text=" + encodeURIComponent(v("jump"))); const j = await r.json();
  if (j.latlng) { map.setView(j.latlng, 17); status(`Jumped to ${j.latlng[0]}, ${j.latlng[1]}`, "ok"); } else status("Couldn't find coordinates in that text", "err");
};
document.getElementById("jump").addEventListener("keydown", e => { if (e.key === "Enter") document.getElementById("go").onclick(); });
for (const id of ["name","region","cc","country","radius"]) document.getElementById(id).addEventListener("input", () => markDirty(true));
document.getElementById("hdg").addEventListener("input", () => { markDirty(true); render(); });
document.getElementById("radius").addEventListener("change", () => { const r = document.getElementById("radius"); const n = parseInt(r.value, 10);
  if (isNaN(n)) r.value = 50; else if (n < 10) { r.value = 10; status("Radius raised to 10 m — smaller than that and GPS error can miss the crossing", "err"); }
  else if (n > 300) { r.value = 300; status("Radius capped at 300 m", "err"); } });
document.getElementById("gmaps").onclick = () => { const c = map.getCenter(); window.open(`https://www.google.com/maps/@${c.lat.toFixed(6)},${c.lng.toFixed(6)},${Math.min(map.getZoom(),20)}z/data=!3m1!1e3`, "_blank"); };
document.getElementById("save").onclick = async () => {
  const body = collect(); body.index = idx;
  const r = await fetch("/api/track", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  const btn = document.getElementById("save");
  if (j.ok) { markDirty(false); status(`Saved "${body.name}" to the local UserTracks.txt (${j.count} custom tracks).`, "ok");
    toast(`✓ Saved "${body.name}" — ${j.count} custom track${j.count === 1 ? "" : "s"} in UserTracks.txt` + (body.finish && body.start_hdg === null ? "  (no start direction set)" : ""));
    btn.textContent = "✓ Saved"; btn.classList.add("saved"); setTimeout(() => { btn.textContent = "Save track"; btn.classList.remove("saved"); }, 2500);
    if (idx < 0 && j.index >= 0) history.replaceState(null, "", "?i=" + j.index); }
  else { status(j.error || "Save failed", "err"); toast("✗ Not saved: " + (j.error || "save failed"), true); }
};
document.getElementById("close").onclick = () => { if (!dirty || confirm("Discard unsaved changes?")) window.close(); };
window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
fetch("/api/track?i=" + idx + (libName ? "&lib=" + encodeURIComponent(libName) : "")).then(r => r.json()).then(j => {
  if (j.track) { load(j.track); if (j.library) { if (j.track.centre) map.setView(j.track.centre, 15);
      status(`"${j.track.name}" is a GARW library track. Place the start/finish and splits here; saving creates your own version in UserTracks.txt, which replaces the library one on the device.`, ""); } }
  else { status("New track — place the start/finish first.", ""); } });
</script></body></html>
"""


class EditorServer:
    """Serves the Leaflet editor on 127.0.0.1 and relays saves back into the TrackDB."""

    def __init__(self, db_getter: Callable[[], object], on_save: Callable[[int, Track], int],
                 static_dirs: Optional[List[str]] = None, library: Optional[TrackLibrary] = None,
                 admin: bool = False) -> None:
        self.admin = admin          # editing the full library (Tracks.txt) rather than the user's UserTracks.txt
        self.db_getter = db_getter
        self.on_save = on_save
        self.library = library
        here = os.path.dirname(os.path.abspath(__file__))
        self.static_dirs = static_dirs or [os.path.join(getattr(sys, "_MEIPASS", here), "assets", "leaflet"),
                                           os.path.join(here, "assets", "leaflet")]
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.port = 0

    def start(self) -> int:
        if self.httpd:
            return self.port
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                qs = parse_qs(u.query)
                if u.path == "/":
                    html = EDITOR_HTML
                    if server.admin:
                        html = (html.replace("Saving writes to your local UserTracks.txt in GARW Genie; upload it to the device from the Lap Timer tab.",
                                             "ADMIN — saving writes to the GARW library file Tracks.txt; regenerate TrackList.txt from the Lap Timer tab.")
                                    .replace("to the local UserTracks.txt (${j.count} custom tracks)", "to Tracks.txt (${j.count} tracks)")
                                    .replace("custom track${j.count === 1 ? \"\" : \"s\"} in UserTracks.txt", "track${j.count === 1 ? \"\" : \"s\"} in Tracks.txt")
                                    .replace("<title>", "<title>[ADMIN] "))
                    body = html.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path.startswith("/leaflet/"):
                    rel = os.path.normpath(u.path[len("/leaflet/"):]).replace("\\", "/")
                    if rel.startswith(".."):
                        self._json({"error": "not found"}, 404)
                        return
                    for d in server.static_dirs:
                        fp = os.path.join(d, rel)
                        if os.path.isfile(fp):
                            with open(fp, "rb") as fh:
                                body = fh.read()
                            self.send_response(200)
                            self.send_header("Content-Type", mimetypes.guess_type(fp)[0] or "application/octet-stream")
                            self.send_header("Content-Length", str(len(body)))
                            self.send_header("Cache-Control", "max-age=86400")
                            self.end_headers()
                            self.wfile.write(body)
                            return
                    self._json({"error": "not found"}, 404)
                elif u.path == "/api/track":
                    i = int(qs.get("i", ["-1"])[0])
                    db = server.db_getter()
                    if 0 <= i < len(db.tracks):
                        self._json({"track": db.tracks[i].to_json(), "index": i})
                    elif qs.get("lib") and server.library and qs["lib"][0] in server.library.by_name:
                        e = server.library.by_name[qs["lib"][0]]   # a GARW library track: prefill name/place, user adds the points
                        self._json({"track": {"name": e.name, "region": e.region, "cc": e.cc, "country": e.country, "centre": e.centre,
                                              "radius": RADIUS_DEFAULT, "sectors": []}, "index": -1, "library": True})
                    else:
                        self._json({"track": None, "index": i})
                elif u.path == "/api/geocode":
                    try:
                        self._json(reverse_geocode(float(qs["lat"][0]), float(qs["lng"][0])))
                    except Exception as e:  # noqa: BLE001
                        self._json({"error": str(e)}, 502)
                elif u.path == "/api/parse_link":
                    self._json({"latlng": parse_google_maps_link(qs.get("text", [""])[0])})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                if u.path == "/api/track":
                    try:
                        t = Track.from_json(data)
                        probs = t.problems()
                        if probs:
                            self._json({"ok": False, "error": "; ".join(probs)})
                            return
                        new_index = server.on_save(int(data.get("index", -1)), t)
                        self._json({"ok": True, "index": new_index, "count": len(server.db_getter().tracks)})
                    except Exception as e:  # noqa: BLE001
                        self._json({"ok": False, "error": str(e)})
                else:
                    self._json({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.port

    def url(self, index: int, lib_name: Optional[str] = None) -> str:
        from urllib.parse import quote
        return f"http://127.0.0.1:{self.start()}/?i={index}" + (f"&lib={quote(lib_name)}" if lib_name else "")

    def open(self, index: int, lib_name: Optional[str] = None) -> str:
        url = self.url(index, lib_name)
        webbrowser.open(url)
        return url

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd = None
