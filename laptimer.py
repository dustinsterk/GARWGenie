"""GARW Genie — Lap Timer support: the tracks database format and a tiny local web server that
hosts a Leaflet/Esri map editor for one track at a time (Tk cannot draw web maps itself).

tracks.txt format (LapTimer on the device):
    #LAPTIMER_TRACKS v1|<count><RS>\\n
    name|region|cc|country|sf_lat,sf_lng|radius_m|sec1;sec2;…|pit_entry|pit_sf|pit_exit|centre<RS>\\n
Fields are '|'-separated, coordinates are 'lat,lng' with 6 decimals, sectors are ';'-separated,
every record ends with ASCII RS (0x1e) followed by a newline.
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
LAPTIMER_DIR = "/opt/IC7/library/LapTimer"       # the LapTimer dash — an optional add-on, not on every device
RACEBOX_MAC_FILE = LAPTIMER_DIR + "/racebox_mac.txt"
TRACKS_FILE = LAPTIMER_DIR + "/tracks.txt"
MAX_SECTORS = 7

LatLng = Tuple[float, float]


def fmt_pt(p: Optional[LatLng]) -> str:
    return f"{p[0]:.6f},{p[1]:.6f}" if p else ""


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

    @classmethod
    def from_line(cls, line: str) -> "Track":
        f = line.split("|")
        if len(f) != 11:
            raise ValueError(f"expected 11 fields, got {len(f)}: {line[:60]}")
        return cls(name=f[0], region=f[1], cc=f[2], country=f[3], sf=parse_pt(f[4]),
                   radius=int(float(f[5])) if f[5].strip() else 50,
                   sectors=[parse_pt(p) for p in f[6].split(";") if p.strip()],
                   pit_entry=parse_pt(f[7]), pit_sf=parse_pt(f[8]), pit_exit=parse_pt(f[9]), centre=parse_pt(f[10]))

    def to_line(self) -> str:
        return "|".join([self.name, self.region, self.cc, self.country, fmt_pt(self.sf), str(self.radius),
                         ";".join(fmt_pt(p) for p in self.sectors), fmt_pt(self.pit_entry), fmt_pt(self.pit_sf),
                         fmt_pt(self.pit_exit), fmt_pt(self.centre or self.auto_centre())])

    def points(self) -> List[LatLng]:
        return [p for p in [self.sf, *self.sectors, self.pit_entry, self.pit_sf, self.pit_exit] if p]

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
        if self.radius <= 0:
            out.append("radius must be positive")
        if (self.pit_entry is None) != (self.pit_exit is None):
            out.append("pit entry and pit exit go together")
        return out

    def to_json(self) -> dict:
        return {"name": self.name, "region": self.region, "cc": self.cc, "country": self.country,
                "sf": self.sf, "radius": self.radius, "sectors": self.sectors, "pit_entry": self.pit_entry,
                "pit_sf": self.pit_sf, "pit_exit": self.pit_exit, "centre": self.centre or self.auto_centre()}

    @classmethod
    def from_json(cls, d: dict) -> "Track":
        pt = lambda v: (float(v[0]), float(v[1])) if v else None  # noqa: E731
        return cls(name=str(d.get("name", "")).strip(), region=str(d.get("region", "")).strip(),
                   cc=str(d.get("cc", "")).strip().upper()[:2], country=str(d.get("country", "")).strip(),
                   sf=pt(d.get("sf")), radius=int(d.get("radius") or 50),
                   sectors=[pt(p) for p in d.get("sectors") or [] if p], pit_entry=pt(d.get("pit_entry")),
                   pit_sf=pt(d.get("pit_sf")), pit_exit=pt(d.get("pit_exit")), centre=pt(d.get("centre")))


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
<label>Radius (m) — how close the car must pass a point to trigger it</label><input id="radius" type="number" min="5" max="500">
<label>Place on the map (pick a tool, then click; drag any marker to move it)</label>
<div class="tools">
 <button class="tool" data-t="sf"><span class="dot" style="background:#ff7a1a"></span>Start / finish</button>
 <button class="tool" data-t="sector"><span class="dot" style="background:#3b9cff"></span>Add sector</button>
 <button class="tool" data-t="pit_entry"><span class="dot" style="background:#ffd23b"></span>Pit entry</button>
 <button class="tool" data-t="pit_sf"><span class="dot" style="background:#c08bff"></span>Pit start / finish</button>
 <button class="tool" data-t="pit_exit"><span class="dot" style="background:#5fd38a"></span>Pit exit</button>
 <button class="tool" data-t="centre"><span class="dot" style="background:#ffffff"></span>Track centre</button>
</div>
<ul id="list"></ul>
<label>Jump to… (paste a Google Maps link, or lat, lng)</label>
<div class="row"><input id="jump" placeholder="https://www.google.com/maps/@…  or  51.0, -1.0"><button id="go" style="flex:0 0 auto">Go</button></div>
<div class="row" style="margin-top:8px"><button id="gmaps">Open this view in Google Maps</button><button id="fit">Fit track</button></div>
<div class="row" style="margin-top:12px"><button id="save" class="accent">Save track</button><button id="close">Close</button></div>
<div id="unsaved">● Unsaved changes</div>
<div id="status"></div>
<div id="toast"></div>
<div class="hint">Imagery © Esri, Maxar, Earthstar Geographics. Right-click a marker to delete it. Saving writes to the local tracks.txt in GARW Genie; upload it to the device from the Lap Timer tab.</div>
</div><div id="map"></div></div>
<script>
const q = new URLSearchParams(location.search); const idx = parseInt(q.get("i") ?? "-1", 10);
const COL = {sf:"#ff7a1a", sector:"#3b9cff", pit_entry:"#ffd23b", pit_sf:"#c08bff", pit_exit:"#5fd38a", centre:"#ffffff"};
const NAMES = {sf:"Start / finish", sector:"Sector", pit_entry:"Pit entry", pit_sf:"Pit start / finish", pit_exit:"Pit exit", centre:"Track centre"};
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
  if (kind === "sector"){ if (markers.sectors.length >= 7) { status("Maximum 7 sectors", "err"); return; } markers.sectors.push(mk("sector", latlng, markers.sectors.length+1)); }
  else { if (markers[kind]) map.removeLayer(markers[kind]); markers[kind] = mk(kind, latlng); }
  markDirty(true); render();
  if (kind === "sf" && !v("region") && !v("country") && !v("cc")) geocode(false);   // new track: fill the blanks
}
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
  if (markers.sf && markers.sectors.length) { const pts = [markers.sf.getLatLng(), ...markers.sectors.map(m => m.getLatLng()), markers.sf.getLatLng()];
    lapLine = L.polyline(pts, {color: "#3b9cff", weight: 2, dashArray: "6 6", opacity: .7}).addTo(map); }
  const ul = document.getElementById("list"); ul.innerHTML = "";
  const rows = [];
  for (const k of ["sf","centre","pit_entry","pit_sf","pit_exit"]) if (markers[k]) rows.push([NAMES[k], markers[k]]);
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
  return {name: v("name"), region: v("region"), cc: v("cc"), country: v("country"), radius: parseInt(v("radius")||"50",10),
          sf: ll(markers.sf), sectors: markers.sectors.map(ll), pit_entry: ll(markers.pit_entry), pit_sf: ll(markers.pit_sf), pit_exit: ll(markers.pit_exit), centre: ll(markers.centre)};
}
function v(id){ return document.getElementById(id).value.trim(); }
function load(t){
  track = t; for (const k of ["name","region","cc","country","radius"]) document.getElementById(k).value = t[k] ?? "";
  for (const k of ["sf","pit_entry","pit_sf","pit_exit","centre"]) if (t[k]) markers[k] = mk(k, t[k]);
  (t.sectors||[]).forEach((p,i) => markers.sectors.push(mk("sector", p, i+1)));
  render(); fit();
}
function fit(){
  const pts = []; for (const k of ["sf","pit_entry","pit_sf","pit_exit","centre"]) if (markers[k]) pts.push(markers[k].getLatLng()); markers.sectors.forEach(m => pts.push(m.getLatLng()));
  if (pts.length > 1) map.fitBounds(L.latLngBounds(pts).pad(0.3)); else if (pts.length === 1) map.setView(pts[0], 16);
}
document.getElementById("fit").onclick = fit;
document.getElementById("go").onclick = async () => {
  const r = await fetch("/api/parse_link?text=" + encodeURIComponent(v("jump"))); const j = await r.json();
  if (j.latlng) { map.setView(j.latlng, 17); status(`Jumped to ${j.latlng[0]}, ${j.latlng[1]}`, "ok"); } else status("Couldn't find coordinates in that text", "err");
};
document.getElementById("jump").addEventListener("keydown", e => { if (e.key === "Enter") document.getElementById("go").onclick(); });
for (const id of ["name","region","cc","country","radius"]) document.getElementById(id).addEventListener("input", () => markDirty(true));
document.getElementById("gmaps").onclick = () => { const c = map.getCenter(); window.open(`https://www.google.com/maps/@${c.lat.toFixed(6)},${c.lng.toFixed(6)},${Math.min(map.getZoom(),20)}z/data=!3m1!1e3`, "_blank"); };
document.getElementById("save").onclick = async () => {
  const body = collect(); body.index = idx;
  const r = await fetch("/api/track", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  const btn = document.getElementById("save");
  if (j.ok) { markDirty(false); status(`Saved "${body.name}" to the local tracks.txt (${j.count} tracks).`, "ok");
    toast(`✓ Saved "${body.name}" — ${j.count} tracks in the local database`);
    btn.textContent = "✓ Saved"; btn.classList.add("saved"); setTimeout(() => { btn.textContent = "Save track"; btn.classList.remove("saved"); }, 2500);
    if (idx < 0 && j.index >= 0) history.replaceState(null, "", "?i=" + j.index); }
  else { status(j.error || "Save failed", "err"); toast("✗ Not saved: " + (j.error || "save failed"), true); }
};
document.getElementById("close").onclick = () => { if (!dirty || confirm("Discard unsaved changes?")) window.close(); };
window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
fetch("/api/track?i=" + idx).then(r => r.json()).then(j => { if (j.track) load(j.track); else { status("New track — place the start/finish first.", ""); } });
</script></body></html>
"""


class EditorServer:
    """Serves the Leaflet editor on 127.0.0.1 and relays saves back into the TrackDB."""

    def __init__(self, db_getter: Callable[[], TrackDB], on_save: Callable[[int, Track], int],
                 static_dirs: Optional[List[str]] = None) -> None:
        self.db_getter = db_getter
        self.on_save = on_save
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
                    body = EDITOR_HTML.encode()
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
                    self._json({"track": db.tracks[i].to_json() if 0 <= i < len(db.tracks) else None, "index": i})
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

    def url(self, index: int) -> str:
        return f"http://127.0.0.1:{self.start()}/?i={index}"

    def open(self, index: int) -> str:
        url = self.url(index)
        webbrowser.open(url)
        return url

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd = None
