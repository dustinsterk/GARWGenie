#!/usr/bin/env python3
"""
GARW Genie
==========
Cross-platform (Windows / macOS / Linux) tool for managing dash screens on a
GARW IC7 cluster over SSH/SFTP.

  * Upload .zip   — validate a zip of one or many dashes and push it to the unit
  * GitHub repos  — track dash repos, see when a new commit lands, install/update
  * Device        — list installed dashes (with the repo/commit they came from),
                    delete dashes, reboot

Every dash lives in /opt/IC7/library/<Name>/ and must contain <Name>.qml and
<Name>.qml.png.  Dashes installed from GitHub also carry a small hidden
.garw_source.json recording the repo, branch and commit, so any laptop running
this tool can tell whether an update is available.

Firmware gate: v5+ firmware lives in /opt/IC7; v4 lived in /opt/Garw_IC7.
The tool refuses to touch a v4 unit (or a unit with both / neither).

Dependencies:  pip install paramiko      (GitHub access uses only the stdlib)
"""

import base64
import io
import json
import logging
import logging.handlers
import os
import platform
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None

APP_NAME = "GARW Genie"
APP_VERSION = "4.9.0"

TARGET_SSID = "GARW"
WIFI_PASSWORD = "garwicxX"      # the unit's own hotspot; editable in the header
HOST = "192.168.42.1"
PORT = 22
USERNAME = "root"
PASSWORD = "root"
LIBRARY_DIR = "/opt/IC7/library"
BINARY_PATH = "/opt/IC7/bin/IC7"
VERSION_FILE = "/opt/IC7/version.txt"        # firmware 5.5+: the version as a plain string, e.g. "5.5"
# v4 firmware lived here. Its presence (with no /opt/IC7) means the unit is too old.
LEGACY_DIR = "/opt/Garw_IC7"
LEGACY_BINARY_PATH = f"{LEGACY_DIR}/bin/Garw_IC7"

# Version gate: v5+ firmware moved from /opt/Garw_IC7 to /opt/IC7, so the
# directory layout is the authoritative check. The float32 version literal in
# the binary is read only for display — its offset shifts between builds
# (57832 documented, 56808 in the reference v5.2 binary; neither is valid in v4).
VERSION_OFFSETS = (57832, 56808)
VERSION_SANE_RANGE = (1.0, 99.0)
MIN_VERSION = 5.0

SCREEN_CONFIGS_DIR = "/opt/IC7/screen_configs"   # per-dash settings files, named inside each .qml
CONFIG_REF_RE = re.compile(r"screen_configs/([A-Za-z0-9_.\-]+)")
CONFIG_PREVIEW_MAX = 64 * 1024
SOURCE_MARKER = ".garw_source.json"   # written inside each dash folder installed from GitHub
CONFIG_DIR = Path.home() / ".garw_genie"
CONFIG_PATH = CONFIG_DIR / "config.json"
_OLD_CONFIG_DIRS = (Path.home() / ".garw_v5_manager", Path.home() / ".garw_uploader")  # earlier names, migrated
ASSETS_DIR_NAME = "assets"
DEFAULT_REPOS_FILE = "default_repos.txt"               # bundled list of dash repos, one URL per line
CACHE_DIR = CONFIG_DIR / "cache"            # downloaded dash zipballs, installable offline
LOG_DIR = CONFIG_DIR / "logs"               # one file per day, every action + every SSH command
# The ccrypt passphrase for firmware packages is NOT stored here. The GARW binary itself launches the
# stock USB updater as "/etc/init.d/K99updater start <passphrase> <ver>", so the tool reads that string
# off the binary on the unit at install time — whatever the firmware author ships is what gets used.
UPDATER_MARKER = "K99updater start "
FIRMWARE_SCRATCH = "/mnt"                   # where the stock updater unpacks (the tar's run script cd's here)
INTERNET_PROBE = ("api.github.com", 443)
CONNECT_TIMEOUT = 8  # seconds
HTTP_TIMEOUT = 30
GITHUB_API = "https://api.github.com"  # overridable (tests / GitHub Enterprise)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IGNORED_PARTS = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def version_cmd(offset: int) -> str:
    return f"od -A n -t f4 -j {offset} -N 4 {BINARY_PATH}"


VERSION_CMD = version_cmd(VERSION_OFFSETS[0])


# --------------------------------------------------------------------------- #
#  Dash packages & validation
# --------------------------------------------------------------------------- #
@dataclass
class DashPackage:
    zip_path: str                 # where it came from (zip path or repo URL)
    name: str
    files: List[Tuple[str, bytes]] = field(default_factory=list)  # (rel path, data)
    warnings: List[str] = field(default_factory=list)  # user must acknowledge
    notes: List[str] = field(default_factory=list)     # informational only
    source: Optional[dict] = None  # GitHub provenance, written as SOURCE_MARKER

    @property
    def remote_dir(self) -> str:
        return f"{LIBRARY_DIR}/{self.name}"


class ValidationError(Exception):
    pass


def _is_ignored(parts: Tuple[str, ...]) -> bool:
    """Skip OS junk and anything hidden (dot-prefixed): .DS_Store, .gitattributes,
    .garw_migration_backup/, __MACOSX/, etc. The unit never needs these."""
    return any(p in IGNORED_PARTS or p.startswith(".") for p in parts)


def _check_dash(name: str, files: List[Tuple[str, bytes]], origin: str) -> DashPackage:
    """Validate one dash given its files (paths relative to the dash folder)."""
    if not NAME_RE.match(name):
        raise ValidationError(
            f"Dash name '{name}' contains invalid characters. "
            "Use letters, digits, underscore or dash only."
        )
    qml_name = f"{name}.qml"
    png_name = f"{name}.qml.png"
    pkg = DashPackage(zip_path=origin, name=name, files=list(files))

    # v4 layout guard: v4 dashes were <X>.qml (loader) + <X>_main.qml + <X>_main.qml.png.
    # v5 wants everything merged into <X>.qml, so a *_main.qml at the root means "not
    # converted yet" — refuse rather than install a half-migrated dash.
    v4_mains = sorted(rel for rel, _ in files if "/" not in rel and rel.lower().endswith("_main.qml"))
    if v4_mains:
        raise ValidationError(
            f"{name}: still in the v4 layout — found {', '.join(v4_mains)}. "
            f"v5 needs the dash merged into a single {qml_name} with {png_name} beside it "
            f"(no *_main.qml). Convert the dash first.")

    found_qml = found_png = False
    root_qmls = [rel for rel, _ in files if "/" not in rel and rel.lower().endswith(".qml")]
    for rel, data in files:
        if rel == qml_name:
            found_qml = True
            if not data.strip():
                raise ValidationError(f"{name}/{qml_name} is empty.")
            if "import QtQuick" not in data.decode("utf-8", errors="replace"):
                pkg.warnings.append(
                    f"{name}/{qml_name} does not contain 'import QtQuick' — is it a QML file?")
        elif rel == png_name:
            found_png = True
            if not data.startswith(PNG_MAGIC):
                raise ValidationError(f"{name}/{png_name} is not a real PNG image.")

    problems = []
    if not found_qml:
        hint = ""
        others = [q for q in root_qmls if q != qml_name]
        sub_dashes = sorted({rel.split("/")[0] for rel, _ in files
                             if rel.count("/") == 1 and rel.split("/")[1] == rel.split("/")[0] + ".qml"})
        loose = [rel for rel, _ in files if "/" not in rel]
        if others:
            hint = f"  (found {', '.join(others)} — the folder and .qml names must match)"
        elif sub_dashes:
            hint = (f"\n  '{name}/' looks like a folder of dashes ({', '.join(sub_dashes[:6])}"
                    f"{'…' if len(sub_dashes) > 6 else ''}) but also has loose file(s): "
                    f"{', '.join(loose[:5])}. Remove them or zip the dash folders directly.")
        problems.append(f"Missing {name}/{qml_name}{hint}")
    if not found_png:
        problems.append(f"Missing {name}/{png_name}  (preview image, must be named <name>.qml.png)")
    if problems:
        raise ValidationError("\n".join(problems))
    refs = config_refs(pkg)
    if refs:
        pkg.notes.append(f"{name} keeps its settings in {SCREEN_CONFIGS_DIR}/" + ", ".join(refs))
    return pkg


# -- settings-file annotation: which QML property does each line feed? --------
_PROP_DECL_RE = re.compile(r"^\s*(?:readonly\s+)?property\s+(int|real|double|bool|string|color|var)\s+(\w+)\s*:\s*([^\n;]+?)\s*$", re.M)


def parse_config_map(qml: str) -> Dict[int, dict]:
    """
    Map settings-file line index -> {'name', 'type', 'default'} by reading the
    dash's own load/save code. Handles the idioms used across the GARW dashes:
        root.x = pI(rline(7), ...)          var s0 = rline(0); root.x = pI(s0, ...)
        configstring[7] = root.x            root.x = configstring_function(7)
        var vals = [root.a, root.b, ...]    (save order == line order)
    """
    decl = {m.group(2): {"type": m.group(1), "default": re.sub(r"\s*//.*$", "", m.group(3)).strip().rstrip(";")}
            for m in _PROP_DECL_RE.finditer(qml)}
    idx: Dict[int, str] = {}

    def put(i, name):
        # ignore helper locals like `s` / `s0`; when the dash declares properties,
        # only accept names that are declared (root.<prop>).
        if not name or i in idx:
            return
        if decl and name not in decl and name != "reserved":
            return
        idx[i] = name

    aliases = {m.group(1): int(m.group(2)) for m in re.finditer(r"var\s+(\w+)\s*=\s*rline\((\d+)\)", qml)}
    for m in re.finditer(r"(?:root\.)?(\w+)\s*=\s*(?:\w+\(\s*)?rline\((\d+)\)", qml):
        put(int(m.group(2)), m.group(1))
    for m in re.finditer(r"(?:root\.)?(\w+)\s*=\s*(?:\w+\(\s*)?(\w+)\s*[,;]", qml):
        if m.group(2) in aliases:                       # root.x = pI(s0, ...)   or   root.x = s0;
            put(aliases[m.group(2)], m.group(1))
    for m in re.finditer(r"configstring\s*\[\s*(\d+)\s*\]\s*=\s*(?:root\.)?(\w+)", qml):
        put(int(m.group(1)), m.group(2))
    for m in re.finditer(r"(?:root\.)?(\w+)\s*=\s*configstring_function\(\s*(\d+)\s*\)", qml):
        put(int(m.group(2)), m.group(1))
    if not idx:
        m = re.search(r"var\s+vals\s*=\s*\[(.*?)\];", qml, re.S)
        if m:
            depth, item, items = 0, "", []
            for ch in m.group(1):
                if ch in "([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
                if ch == "," and depth == 0:
                    items.append(item)
                    item = ""
                else:
                    item += ch
            items.append(item)
            for i, it in enumerate(items):
                it = re.sub(r"//.*", "", it)
                mm = re.search(r"root\.(\w+)", it)
                put(i, mm.group(1) if mm else ("reserved" if re.search(r"[\"']\s*0\s*[\"']", it) else ""))
    out = {}
    for i, name in idx.items():
        d = decl.get(name, {})
        out[i] = {"name": name, "type": d.get("type", ""), "default": d.get("default", "")}
    return out


def config_refs(pkg: DashPackage) -> List[str]:
    """Settings files this dash's .qml refers to under /opt/IC7/screen_configs."""
    qml = dict(pkg.files).get(f"{pkg.name}.qml", b"")
    refs = (r.rstrip(".") for r in CONFIG_REF_RE.findall(qml.decode("utf-8", errors="replace")))
    return sorted({r for r in refs if r})


def _read_zip_tree(zf: "zipfile.ZipFile"):
    """Return (entries, skipped) where entries = [(parts, info)] minus hidden/OS junk."""
    entries = []
    skipped: Dict[str, int] = {}
    for info in zf.infolist():
        parts = tuple(p for p in info.filename.replace("\\", "/").split("/") if p)
        if not parts:
            continue
        if _is_ignored(parts):
            if not info.is_dir():
                if parts[0] == "__MACOSX":
                    cat = "__MACOSX/ (Finder resource forks)"
                else:
                    hidden = next(p for p in parts if p in IGNORED_PARTS or p.startswith("."))
                    cat = hidden + ("/" if hidden != parts[-1] else "")
                skipped[cat] = skipped.get(cat, 0) + 1
            continue
        if any(p == ".." for p in parts):
            raise ValidationError(f"Unsafe path in zip: {info.filename}")
        entries.append((parts, info))
    return entries, skipped


def _strip_wrapper(entries):
    """If the only top-level folder holds no files of its own, descend into it."""
    top_levels = {p[0] for p, _ in entries}
    if len(top_levels) != 1:
        return entries, None
    top = next(iter(top_levels))
    files_in_top = [p for p, i in entries if not i.is_dir() and len(p) == 2]
    deeper = [p for p, i in entries if not i.is_dir() and len(p) >= 3]
    if not files_in_top and deeper:
        return [(p[1:], i) for p, i in entries if len(p) > 1], top
    return entries, None


def _packages_from_zipfile(zf: "zipfile.ZipFile", origin: str) -> List[DashPackage]:
    entries, skipped = _read_zip_tree(zf)
    if not entries:
        raise ValidationError("The zip is empty.")
    entries, wrapper = _strip_wrapper(entries)
    file_entries = [(p, i) for p, i in entries if not i.is_dir()]

    root_files = [p for p, i in file_entries if len(p) == 1]
    if root_files:
        where = f"in the '{wrapper}/' folder" if wrapper else "at the zip root"
        raise ValidationError(
            f"Loose files found {where}: " + ", ".join(p[0] for p in root_files[:5])
            + "\nEach dash must be in its own folder named after it, e.g.  MyDash/MyDash.qml")

    packages, problems = [], []
    for name in sorted({p[0] for p, _ in entries}):
        files = [("/".join(p[1:]), zf.read(i)) for p, i in file_entries if p[0] == name]
        if not files:
            problems.append(f"Folder '{name}' is empty.")
            continue
        try:
            packages.append(_check_dash(name, files, origin))
        except ValidationError as e:
            problems.append(str(e))
    if problems:
        raise ValidationError("\n".join(problems))

    if wrapper:
        packages[0].notes.append(
            f"Zip has a wrapper folder '{wrapper}/'; using the {len(packages)} folder(s) inside it.")
    if skipped:
        total = sum(skipped.values())
        detail = ", ".join(f"{n}× {cat}" for cat, n in sorted(skipped.items(), key=lambda kv: -kv[1]))
        packages[0].notes.append(
            f"Not uploading {total} hidden/OS file(s): {detail}. "
            "(__MACOSX and .DS_Store are added by Finder and are harmless.)")
    return packages


def validate_zip(zip_path: str) -> List[DashPackage]:
    """
    Inspect a zip and return one DashPackage per dash folder.

        MyDash.zip            Screens.zip              library.zip (Finder wrapper)
        └── MyDash/           ├── GTDash/              └── library/
            ├── MyDash.qml    │   ├── GTDash.qml           ├── GTDash/…
            └── MyDash.qml.png│   └── GTDash.qml.png       └── LFA/…
                              └── LFA/…
    Every folder must pass or ValidationError lists all problems.
    """
    if not os.path.isfile(zip_path):
        raise ValidationError(f"File not found: {zip_path}")
    if not zipfile.is_zipfile(zip_path):
        raise ValidationError("That file is not a valid .zip archive.")
    with zipfile.ZipFile(zip_path) as zf:
        return _packages_from_zipfile(zf, zip_path)


# --------------------------------------------------------------------------- #
#  Config (repo list, settings)
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {"repos": [], "github_token": "", "after_changes": "restart", "removed_defaults": [],
                  "ssh_user": USERNAME, "ssh_password": PASSWORD,   # login for the unit (editable in the header)
                  "wifi_ssid": TARGET_SSID, "wifi_password": WIFI_PASSWORD,
                  "auto_check_minutes": 30,   # auto "Check & download" on internet only if last check is older; 0 = never
                  "retro_font": False}  # 8-bit Press Start 2P UI font; toggle in the header


FILE_LOG = logging.getLogger("garw_genie")
_LOG_PATH: Optional[Path] = None


def setup_file_log() -> Optional[Path]:
    """Append everything the app does to ~/.garw_genie/logs/garw_genie_YYYY-MM-DD.log
    (kept 30 days). Called once at startup; safe to call again."""
    global _LOG_PATH
    if FILE_LOG.handlers:
        return _LOG_PATH
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "garw_genie.log"
        h = logging.handlers.TimedRotatingFileHandler(path, when="midnight", backupCount=30, encoding="utf-8")
        h.suffix = "%Y-%m-%d"
        h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s", "%Y-%m-%d %H:%M:%S"))
        FILE_LOG.addHandler(h)
        FILE_LOG.setLevel(logging.DEBUG)
        FILE_LOG.propagate = False
        _LOG_PATH = path
        FILE_LOG.info("=" * 70)
        FILE_LOG.info("%s v%s starting  (python %s, %s)", APP_NAME, APP_VERSION, platform.python_version(), platform.platform())
        return path
    except OSError:
        return None


def open_folder(path: Path) -> None:
    """Reveal a folder in Finder / Explorer / the desktop file manager."""
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def app_dir() -> Path:
    """Folder holding the script — or, in a PyInstaller build, the executable."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        for parent in exe.parents:              # macOS: climb out of Foo.app/Contents/MacOS/
            if parent.suffix == ".app":
                return parent.parent
        return exe.parent
    return Path(__file__).resolve().parent


def default_repos_paths() -> List[Path]:
    """Where default_repos.txt may live, most-editable first: next to the app, then
    inside a PyInstaller bundle, then the user's config folder."""
    paths = [app_dir() / DEFAULT_REPOS_FILE]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        paths.append(Path(bundle) / DEFAULT_REPOS_FILE)
    paths.append(CONFIG_DIR / DEFAULT_REPOS_FILE)
    return paths


def load_default_repos() -> List[str]:
    """URLs from the first default_repos.txt found. '#' starts a comment; blank lines ignored."""
    for path in default_repos_paths():
        if path.is_file():
            urls = []
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    urls.append(line)
            return urls
    return []


def merge_default_repos(cfg: dict) -> List[str]:
    """Add bundled default repos that aren't tracked yet (and weren't deliberately
    removed by the user). Returns the labels added."""
    added = []
    removed = set(cfg.get("removed_defaults", []))
    known = {d.get("url") for d in cfg["repos"]}
    for url in load_default_repos():
        try:
            entry = RepoEntry.from_url(url)
        except GitHubError:
            continue
        if entry.url in known or entry.url in removed:
            continue
        d = entry.to_dict()
        d["default"] = True
        cfg["repos"].append(d)
        known.add(entry.url)
        added.append(entry.label)
    return added


def _migrate_old_config():
    if CONFIG_DIR.exists():
        return
    for old in _OLD_CONFIG_DIRS:
        if old.exists():
            try:
                import shutil
                shutil.copytree(old, CONFIG_DIR)
            except OSError:
                pass
            return


def asset_path(name: str) -> Optional[Path]:
    """Find a bundled asset (logo/icon) next to the script, in a PyInstaller bundle, or beside the app."""
    candidates = [app_dir() / ASSETS_DIR_NAME / name, app_dir() / name]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates += [Path(bundle) / ASSETS_DIR_NAME / name, Path(bundle) / name]
    if not getattr(sys, "frozen", False):
        candidates.append(Path(__file__).resolve().parent / ASSETS_DIR_NAME / name)
    return next((c for c in candidates if c.is_file()), None)


def load_config() -> dict:
    _migrate_old_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            raise ValueError
    except (OSError, ValueError):
        cfg = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    merged["repos"] = [r for r in merged.get("repos", []) if isinstance(r, dict) and r.get("url")]
    apply_credentials(merged.get("ssh_user"), merged.get("ssh_password"))
    apply_wifi(merged.get("wifi_ssid"), merged.get("wifi_password"))
    return merged


def apply_wifi(ssid: Optional[str], password: Optional[str]) -> None:
    global TARGET_SSID, WIFI_PASSWORD
    if ssid:
        TARGET_SSID = ssid
    if password is not None:
        WIFI_PASSWORD = password


def _wifi_joined(ssid: str, wait_s: float = 20.0) -> bool:
    """Poll until the machine reports `ssid` or the unit answers (macOS may hide the SSID)."""
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        cur = current_ssid() or ""
        if cur == ssid or unit_reachable(timeout=1.0):
            return True
        time.sleep(1.0)
    return False


def open_wifi_settings() -> bool:
    """Open the OS Wi-Fi picker so the user can choose the network by hand. Best effort."""
    system = platform.system()
    try:
        if system == "Darwin":
            for target in ("x-apple.systempreferences:com.apple.wifi-settings-extension",
                           "/System/Library/PreferencePanes/Network.prefPane"):
                if subprocess.run(["open", target], capture_output=True, timeout=8).returncode == 0:
                    return True
        elif system == "Windows":
            return subprocess.run(["cmd", "/c", "start", "ms-availablenetworks:"], capture_output=True, timeout=8).returncode == 0
        else:
            return subprocess.run(["nm-connection-editor"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        pass
    return False


def join_wifi(ssid: str, password: str, log=None) -> Tuple[bool, str]:
    """Ask the OS to join a Wi-Fi network, then verify it actually happened. Returns (ok, message).
    `log` (optional) receives every raw OS reply so 'nothing happened' is always explained."""
    system = platform.system()
    say = log or (lambda *_: None)
    try:
        if system == "Darwin":
            ports = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=8).stdout
            dev = "en0"
            m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\w+)", ports)
            if m:
                dev = m.group(1)
            # Make sure the radio is on, then ask for the network.
            subprocess.run(["networksetup", "-setairportpower", dev, "on"], capture_output=True, text=True, timeout=8)
            cmd = ["networksetup", "-setairportnetwork", dev, ssid, password]
            say(f"$ networksetup -setairportnetwork {dev} {ssid} ********")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            out = (r.stdout + r.stderr).strip()
            say(f"  networksetup exit {r.returncode}: {out or '(no output)'}")
            bad = r.returncode != 0 or "Could not" in out or "Failed" in out or "Error" in out
            if not bad:
                say(f"  Waiting for '{ssid}' to come up …")
                if _wifi_joined(ssid):
                    return True, f"Joined '{ssid}' on {dev}."
                out = out or (f"macOS accepted the request but never joined '{ssid}'. Recent macOS versions "
                              "often ignore this command unless the app has Location Services permission.")
            return False, out or "networksetup failed"
        if system == "Windows":
            import tempfile
            import xml.sax.saxutils as su
            xml = f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{su.escape(ssid)}</name>
  <SSIDConfig><SSID><name>{su.escape(ssid)}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
    <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{su.escape(password)}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>"""
            with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as fh:
                fh.write(xml)
                path = fh.name
            try:
                r1 = subprocess.run(["netsh", "wlan", "add", "profile", f"filename={path}", "user=current"],
                                    capture_output=True, text=True, timeout=20)
                say(f"  netsh add profile exit {r1.returncode}: {(r1.stdout + r1.stderr).strip()}")
                r2 = subprocess.run(["netsh", "wlan", "connect", f"name={ssid}"], capture_output=True, text=True, timeout=20)
                say(f"  netsh connect exit {r2.returncode}: {(r2.stdout + r2.stderr).strip()}")
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            out = (r1.stdout + r2.stdout + r1.stderr + r2.stderr).strip()
            if r2.returncode == 0 and _wifi_joined(ssid):
                return True, f"Joined '{ssid}'."
            return False, out or f"Windows did not join '{ssid}'."
        r = subprocess.run(["nmcli", "dev", "wifi", "connect", ssid, "password", password],
                           capture_output=True, text=True, timeout=45)
        out = (r.stdout + r.stderr).strip()
        say(f"  nmcli exit {r.returncode}: {out}")
        return r.returncode == 0, out
    except FileNotFoundError as e:
        return False, f"Wi-Fi tool not found: {e}"
    except subprocess.TimeoutExpired:
        return False, "Timed out asking the OS to join the network."
    except Exception as e:  # pragma: no cover
        return False, str(e)


def apply_credentials(user: Optional[str], password: Optional[str]) -> None:
    """Make the configured unit login the default for every new IC7Device."""
    global USERNAME, PASSWORD
    if user:
        USERNAME = user
    if password is not None:
        PASSWORD = password


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- #
#  GitHub
# --------------------------------------------------------------------------- #
class GitHubError(Exception):
    pass


GITHUB_URL_RE = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+?)(?:\.git)?"
    r"(?:/tree/(?P<branch>[^/\s]+))?/?\s*$"
)


def parse_github_url(url: str) -> Tuple[str, str, Optional[str]]:
    """'https://github.com/o/r', 'https://github.com/o/r/tree/dev', 'git@github.com:o/r.git'
    -> (owner, repo, branch-or-None). Also accepts bare 'owner/repo'."""
    url = url.strip()
    m = GITHUB_URL_RE.match(url)
    if not m:
        m2 = re.match(r"^(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+)$", url)
        if not m2:
            raise GitHubError(f"Not a GitHub repository URL: {url}")
        return m2["owner"], m2["repo"], None
    return m["owner"], m["repo"], m["branch"]


RATE = {"remaining": None, "limit": None, "reset": None}   # last seen X-RateLimit-* headers


def _note_rate(headers) -> None:
    try:
        rem, lim, rst = headers.get("X-RateLimit-Remaining"), headers.get("X-RateLimit-Limit"), headers.get("X-RateLimit-Reset")
        if rem is not None:
            RATE.update(remaining=int(rem), limit=int(lim or 0), reset=int(rst or 0))
    except (TypeError, ValueError):
        pass


def _http_request(url: str, token: str = "", accept: str = "application/vnd.github+json",
                  etag: Optional[str] = None):
    """-> (status, headers, body). With `etag`, a 304 Not Modified comes back as
    (304, headers, b'') — and per GitHub's rules a 304 does NOT count against the rate limit."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": accept,
    })
    token = token or os.environ.get("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            _note_rate(resp.headers)
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        _note_rate(e.headers)
        if e.code == 304:
            return 304, e.headers, b""
        if e.code == 404:
            raise GitHubError(f"Not found on GitHub (404): {url}\nIs the repo public and the URL correct?")
        if e.code in (403, 429):
            reset = e.headers.get("X-RateLimit-Reset")
            when = ""
            if reset and reset.isdigit():
                when = " (resets " + datetime.fromtimestamp(int(reset)).strftime("%H:%M") + ")"
            raise GitHubError(f"GitHub rate limit or access denied ({e.code}){when}. "
                              "Add a token to config.json 'github_token' to raise the limit.")
        raise GitHubError(f"GitHub returned HTTP {e.code} for {url}")
    except urllib.error.URLError as e:
        raise GitHubError(f"Cannot reach GitHub: {e.reason}")


def _http_get(url: str, token: str = "", accept: str = "application/vnd.github+json") -> bytes:
    return _http_request(url, token, accept)[2]


def gh_api(path: str, token: str = "") -> dict:
    return json.loads(_http_get(f"{GITHUB_API}{path}", token).decode("utf-8"))


def gh_default_branch(owner: str, repo: str, token: str = "") -> str:
    return gh_api(f"/repos/{owner}/{repo}", token).get("default_branch", "main")


def gh_latest_commit(owner: str, repo: str, branch: str, token: str = "",
                     etag: Optional[str] = None) -> Optional[dict]:
    """-> {'sha', 'short', 'date' (ISO), 'message' (first line), 'author', 'etag'},
    or None when `etag` is given and GitHub answers 304 Not Modified (free of charge)."""
    status, headers, body = _http_request(f"{GITHUB_API}/repos/{owner}/{repo}/commits/{branch}", token, etag=etag)
    if status == 304:
        return None
    data = json.loads(body.decode("utf-8"))
    commit = data.get("commit", {})
    return {
        "sha": data["sha"],
        "short": data["sha"][:7],
        "date": (commit.get("committer") or commit.get("author") or {}).get("date", ""),
        "message": (commit.get("message") or "").splitlines()[0][:100],
        "author": (commit.get("author") or {}).get("name", ""),
        "etag": headers.get("ETag"),
    }


def gh_download_zip(owner: str, repo: str, ref: str, token: str = "") -> bytes:
    """
    Repository snapshot at `ref` (a sha or branch) as zip bytes.
    The API zipball endpoint answers 415 to a non-JSON Accept header, so use the
    default one (it 302-redirects to codeload, which urllib follows). If the API
    route fails for any reason, fall back to codeload directly.
    """
    try:
        data = _http_get(f"{GITHUB_API}/repos/{owner}/{repo}/zipball/{ref}", token)
    except GitHubError as api_err:
        if GITHUB_API != "https://api.github.com":
            raise
        try:
            data = _http_get(f"https://codeload.github.com/{owner}/{repo}/zip/{ref}", token, accept="*/*")
        except GitHubError:
            raise api_err
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise GitHubError(f"GitHub did not return a zip archive for {owner}/{repo}@{ref[:7]}.")
    return data


def packages_from_github_zip(zip_bytes: bytes, source: dict) -> List[DashPackage]:
    """
    A GitHub zipball is  <repo>-<sha>/<repo contents>.  Two layouts are accepted:
      * one dash per repo: <Name>.qml + <Name>.qml.png at the repo root
        (the dash is named after the .qml, not the repo)
      * a repo holding several dash folders (same rules as an uploaded zip)
    Each package gets `source` and a SOURCE_MARKER file for provenance.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        entries, _ = _read_zip_tree(zf)
        if not entries:
            raise ValidationError("The repository is empty.")
        # GitHub always wraps the tree in one folder; strip it unconditionally.
        top = {p[0] for p, _ in entries}
        if len(top) == 1:
            entries = [(p[1:], i) for p, i in entries if len(p) > 1]
        file_entries = [(p, i) for p, i in entries if not i.is_dir()]

        root_qml = [p[0] for p, _ in file_entries if len(p) == 1 and p[0].lower().endswith(".qml")]
        pairs = [q for q in root_qml if any(len(p) == 1 and p[0] == q + ".png" for p, _ in file_entries)]
        v4_mains = [q for q in root_qml if q.lower().endswith("_main.qml")]
        if v4_mains:
            raise ValidationError(
                "Repo is still in the v4 layout — found " + ", ".join(sorted(v4_mains)) + ". "
                "v5 needs the dash merged into a single <Name>.qml with <Name>.qml.png beside it "
                "(no *_main.qml). Convert the dash first.")

        origin = source.get("url", "github")
        if pairs:
            # Layout 1: dash at the repo root.
            if len(pairs) > 1:
                raise ValidationError(
                    "Repo root has several <Name>.qml/<Name>.qml.png pairs: " + ", ".join(pairs)
                    + ". One dash per repo, or put each dash in its own folder.")
            name = pairs[0][:-4]
            files = [("/".join(p), zf.read(i)) for p, i in file_entries]
            pkgs = [_check_dash(name, files, origin)]
        else:
            # Layout 2: folders of dashes. Loose files at the root (README, LICENSE) are fine here.
            names = sorted({p[0] for p, _ in file_entries if len(p) >= 2})
            if not names:
                raise ValidationError(
                    "No dash found: expected <Name>.qml and <Name>.qml.png at the repo root, "
                    "or dash folders.")
            pkgs, problems = [], []
            for name in names:
                files = [("/".join(p[1:]), zf.read(i)) for p, i in file_entries if p[0] == name]
                try:
                    pkgs.append(_check_dash(name, files, origin))
                except ValidationError as e:
                    problems.append(str(e))
            if problems and not pkgs:
                raise ValidationError("\n".join(problems))
            if problems:
                pkgs[0].notes.append("Ignored non-dash folder(s): " + "; ".join(
                    p.splitlines()[0] for p in problems))

    for pkg in pkgs:
        pkg.source = dict(source, dash=pkg.name, tool=f"{APP_NAME} {APP_VERSION}")
        pkg.files.append((SOURCE_MARKER, json.dumps(pkg.source, indent=2).encode("utf-8")))
    return pkgs


@dataclass
class RepoEntry:
    """A tracked GitHub repo (persisted in config)."""
    url: str
    owner: str
    repo: str
    branch: Optional[str] = None      # None -> default branch (resolved on first check)
    dash: Optional[str] = None        # dash name(s) discovered on install, comma-joined
    latest_sha: Optional[str] = None  # last commit seen on GitHub
    latest_date: Optional[str] = None
    latest_message: Optional[str] = None
    last_checked: Optional[str] = None
    etag: Optional[str] = None        # ETag of the last commit lookup -> free 304 re-checks
    error: Optional[str] = None       # transient, not persisted
    default: bool = False             # came from the bundled default_repos.txt

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.repo}"

    @classmethod
    def from_url(cls, url: str) -> "RepoEntry":
        owner, repo, branch = parse_github_url(url)
        return cls(url=f"https://github.com/{owner}/{repo}", owner=owner, repo=repo, branch=branch)

    @classmethod
    def from_dict(cls, d: dict) -> "RepoEntry":
        try:
            e = cls.from_url(d["url"])
        except GitHubError:
            return None
        for k in ("branch", "dash", "latest_sha", "latest_date", "latest_message", "last_checked", "etag"):
            if d.get(k):
                setattr(e, k, d[k])
        e.default = bool(d.get("default"))
        return e

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in
             ("url", "owner", "repo", "branch", "dash", "latest_sha", "latest_date",
              "latest_message", "last_checked", "etag")}
        if getattr(self, "default", False):
            d["default"] = True
        return d

    def check(self, token: str = "") -> dict:
        """Query GitHub; update latest_* fields; return the commit dict (with 'unchanged': True
        when GitHub answered 304 to our ETag — that call did not count against the rate limit)."""
        if not self.branch:
            self.branch = gh_default_branch(self.owner, self.repo, token)
        c = gh_latest_commit(self.owner, self.repo, self.branch, token, etag=self.etag if self.latest_sha else None)
        self.last_checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.error = None
        if c is None:
            return {"sha": self.latest_sha, "short": (self.latest_sha or "")[:7], "date": self.latest_date,
                    "message": self.latest_message or "", "author": "", "unchanged": True}
        self.latest_sha, self.latest_date, self.latest_message = c["sha"], c["date"], c["message"]
        self.etag = c.get("etag")
        c["unchanged"] = False
        return c

    def checked_within(self, minutes: float) -> bool:
        if not self.last_checked:
            return False
        try:
            t = datetime.fromisoformat(self.last_checked.replace("Z", "+00:00"))
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - t).total_seconds() < minutes * 60

    # -- local cache ---------------------------------------------------------
    @property
    def cache_dir(self) -> Path:
        return CACHE_DIR / f"{self.owner}__{self.repo}"

    def cached(self) -> Optional[Tuple[str, Path]]:
        """(sha, zip path) of the newest cached snapshot, or None."""
        if not self.cache_dir.is_dir():
            return None
        zips = sorted(self.cache_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        return (zips[0].stem, zips[0]) if zips else None

    def cached_sha(self) -> Optional[str]:
        c = self.cached()
        return c[0] if c else None

    def is_downloaded(self) -> bool:
        """True when the latest known commit is sitting in the cache."""
        return bool(self.latest_sha) and self.cached_sha() == self.latest_sha

    def download(self, token: str = "", log=lambda m: None) -> Path:
        """Fetch latest_sha into the cache (needs internet). Older snapshots are pruned."""
        if not self.latest_sha:
            self.check(token)
        target = self.cache_dir / f"{self.latest_sha}.zip"
        if target.is_file():
            # Re-validate the cached copy with the *current* rules (they get stricter over
            # time); a stale-but-invalid snapshot is evicted and the error surfaces.
            try:
                pkgs = packages_from_github_zip(target.read_bytes(), {"url": self.url})
            except ValidationError:
                target.unlink(missing_ok=True)
                raise
            self.dash = ", ".join(p.name for p in pkgs)
            log(f"  {self.label}@{self.latest_sha[:7]} already cached (re-validated OK)")
            return target
        log(f"Downloading {self.label}@{self.latest_sha[:7]} ...")
        data = gh_download_zip(self.owner, self.repo, self.latest_sha, token)
        # validate before caching so a broken repo never poisons the cache
        pkgs = packages_from_github_zip(data, {"url": self.url})
        self.dash = ", ".join(p.name for p in pkgs)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        for old in self.cache_dir.glob("*.zip"):
            if old != target:
                old.unlink(missing_ok=True)
        log(f"  cached {len(data):,} bytes → {target}")
        return target

    def validate_cache(self) -> bool:
        """Re-check the newest cached snapshot against the current rules. Evicts and raises
        ValidationError if it no longer qualifies; False if nothing is cached."""
        c = self.cached()
        if not c:
            return False
        sha, path = c
        try:
            pkgs = packages_from_github_zip(path.read_bytes(), {"url": self.url})
        except ValidationError:
            path.unlink(missing_ok=True)
            raise
        self.dash = ", ".join(p.name for p in pkgs)
        return True

    def fetch_packages(self, token: str = "", log=lambda m: None, offline: bool = False) -> List[DashPackage]:
        """
        Packages for install. Uses the cache when it holds latest_sha (or, offline,
        whatever snapshot is cached); downloads otherwise.
        """
        cached = self.cached()
        if cached and (cached[0] == self.latest_sha or not self.latest_sha or offline):
            sha, path = cached
            if self.latest_sha and sha != self.latest_sha:
                log(f"  {self.label}: offline — installing cached {sha[:7]} (GitHub has {self.latest_sha[:7]})")
            else:
                log(f"  {self.label}: using cached {sha[:7]}")
            data = path.read_bytes()
        else:
            if offline:
                raise GitHubError(f"{self.label} is not in the local cache and there is no internet. "
                                  "Run 'Check & download' while online first.")
            path = self.download(token, log)
            sha, data = self.latest_sha, path.read_bytes()
        source = {"url": self.url, "owner": self.owner, "repo": self.repo, "branch": self.branch,
                  "sha": sha, "commit_date": self.latest_date if sha == self.latest_sha else None,
                  "installed": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        pkgs = packages_from_github_zip(data, source)
        self.dash = ", ".join(p.name for p in pkgs)
        return pkgs


def repo_status(entry: RepoEntry, installed: Dict[str, dict]) -> str:
    """Human status given the device's installed dashes {name: marker-or-{}}."""
    if entry.error:
        return entry.error if entry.error.startswith("Not a v5") else f"Error: {entry.error}"
    if not entry.latest_sha:
        return "Not checked"
    names = [n.strip() for n in (entry.dash or "").split(",") if n.strip()]
    if not names:
        # Never installed by this tool; see if any installed dash claims this repo.
        names = [n for n, m in installed.items() if m.get("url") == entry.url]
    ready = "  · ready" if entry.is_downloaded() else "  · download first"
    if not names:
        return "Not installed" + ready
    missing = [n for n in names if n not in installed]
    if missing:
        return ("Not installed" + ready) if len(missing) == len(names) else f"Missing on device: {', '.join(missing)}"
    shas = {installed[n].get("sha") for n in names}
    if None in shas or "" in shas:
        return "Installed (no version info — reinstall to track)"
    if shas == {entry.latest_sha}:
        return "Up to date"
    old = ", ".join(sorted(s[:7] for s in shas))
    return f"Update {old} → {entry.latest_sha[:7]}{ready}"


# --------------------------------------------------------------------------- #
#  Network helpers
# --------------------------------------------------------------------------- #
_SSID_CACHE = {"t": 0.0, "v": None}


def _macos_ssid_profiler() -> Optional[str]:
    now = time.monotonic()
    if now - _SSID_CACHE["t"] < 30:
        return _SSID_CACHE["v"]
    ssid = None
    try:
        out = subprocess.run(["system_profiler", "SPAirPortDataType", "-json"],
                             capture_output=True, text=True, timeout=12).stdout
        data = json.loads(out)
        for iface in data.get("SPAirPortDataType", [{}])[0].get("spairport_airport_interfaces", []):
            cur = iface.get("spairport_current_network_information") or {}
            if cur.get("_name"):
                ssid = cur["_name"]
                break
    except Exception:
        pass
    _SSID_CACHE.update(t=now, v=ssid)
    return ssid


def current_ssid() -> Optional[str]:
    """Best-effort: return the SSID the machine is currently connected to."""
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                 capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                s = line.strip()
                if s.lower().startswith("ssid") and not s.lower().startswith("bssid"):
                    return s.split(":", 1)[1].strip() or None
        elif system == "Darwin":
            found = None
            for dev in ("en0", "en1"):
                out = subprocess.run(["ipconfig", "getsummary", dev],
                                     capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
                if m:
                    found = m.group(1).strip()
                    break
            if not found:
                out = subprocess.run(["networksetup", "-getairportnetwork", "en0"],
                                     capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"Current Wi-Fi Network:\s*(.+)$", out, re.M)
                if m:
                    found = m.group(1).strip()
            if found and "redacted" not in found.lower():
                return found
            # macOS 14.4+ redacts the SSID from apps without Location Services permission;
            # system_profiler still reports it (slower, so the result is cached for 30 s).
            return _macos_ssid_profiler() or found
        else:
            out = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=5).stdout.strip()
            if out:
                return out
    except Exception:
        pass
    return None


def unit_reachable(host: Optional[str] = None, port: Optional[int] = None, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host or HOST, port or PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve(host: str, port: int, timeout: float) -> Optional[str]:
    """getaddrinfo has no timeout of its own — on a LAN with no resolver (GARW) it can
    hang 10 s+. Run it in a helper thread and give up after `timeout`."""
    box: List[Optional[str]] = [None]

    def work():
        try:
            box[0] = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        except OSError:
            box[0] = None
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if not t.is_alive() else None


def internet_reachable(timeout: float = 3.0) -> bool:
    host, port = INTERNET_PROBE
    ip = _resolve(host, port, timeout)
    if not ip:
        return False
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def preflight(log, ssid: Optional[str] = None) -> bool:
    """Wi-Fi / reachability checks. Returns True if the unit is reachable."""
    if ssid is None:
        ssid = current_ssid()
    if ssid and "redacted" in ssid.lower():
        ssid = None
    if ssid is None:
        log("Wi-Fi name not available from the OS (macOS hides it without Location Services) — "
            "checking whether the unit answers instead.")
    elif ssid != TARGET_SSID:
        log(f"WARNING: connected to Wi-Fi '{ssid}', expected '{TARGET_SSID}'.")
    else:
        log(f"Wi-Fi: connected to '{ssid}'.")
    log(f"Checking {HOST}:{PORT} ...")
    if unit_reachable():
        log("GARW device is reachable.")
        return True
    log(f"GARW device not reachable at {HOST}:{PORT}. Join the '{TARGET_SSID}' network and try again.")
    return False


# --------------------------------------------------------------------------- #
#  The device: SSH session with upload / list / delete / reboot
# --------------------------------------------------------------------------- #
class UploadAborted(Exception):
    pass


class AuthFailed(RuntimeError):
    """Wrong SSH username/password for the unit."""


def _sq(s: str) -> str:
    """Single-quote for a POSIX shell."""
    return "'" + s.replace("'", "'\\''") + "'"


class IC7Device:
    """One SSH session to the cluster. Callbacks keep it GUI-agnostic."""

    def __init__(self, log, confirm=None, progress=None,
                 host=None, port=None, username=None, password=None):
        self.log = log                                      # log(str)
        self._version_source: Optional[str] = None
        self.confirm = confirm or (lambda t, m: True)       # confirm(title, message) -> bool
        self.progress = progress or (lambda done, total: None)
        self.host, self.port = host or HOST, port or PORT
        self.username, self.password = username or USERNAME, password or PASSWORD
        self.client: Optional["paramiko.SSHClient"] = None

    # -- session -------------------------------------------------------------
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        if paramiko is None:
            raise RuntimeError("paramiko is not installed. Run:  pip install paramiko")
        self.log(f"Connecting to {self.username}@{self.host}:{self.port} ...")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.host, port=self.port, username=self.username, password=self.password,
                           timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
                           auth_timeout=CONNECT_TIMEOUT, look_for_keys=False, allow_agent=False)
        except paramiko.AuthenticationException:
            raise AuthFailed(f"SSH login failed for user '{self.username}' at {self.host} — "
                             "check the username and password in the header.")
        self.client = client
        FILE_LOG.info("SSH session opened to %s@%s:%s", self.username, self.host, self.port)
        self.log("Connected.")

    def close(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    def _run(self, cmd: str, timeout: float = 15) -> Tuple[int, str, str]:
        secret = getattr(self, "_fw_pass", None)
        shown = cmd.replace(secret, "*****") if secret and secret in cmd else cmd
        FILE_LOG.debug("SSH$ %s", shown if len(shown) < 600 else shown[:600] + " …")
        _, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        if rc != 0 or err.strip():
            FILE_LOG.debug("   -> rc=%s%s", rc, ("  stderr: " + err.strip()[:300]) if err.strip() else "")
        return rc, out, err

    def _remote_exists(self, path: str) -> bool:
        rc, _, _ = self._run(f"test -e {_sq(path)}")
        return rc == 0

    # -- firmware gate -------------------------------------------------------
    def _read_version(self) -> Tuple[Optional[str], Optional[float]]:
        """(display, number). Firmware 5.5+ writes the version to /opt/IC7/version.txt; older v5
        units only have the float literal inside the binary, which is read as a fallback."""
        lo, hi = VERSION_SANE_RANGE
        rc, out, _ = self._run(f"cat {VERSION_FILE} 2>/dev/null")
        text = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
        if text:
            text = text.lstrip("vV").strip()
            m = re.match(r"(\d+(?:\.\d+)?)", text)
            number = float(m.group(1)) if m else None
            self._version_source = VERSION_FILE
            return text, number
        for offset in VERSION_OFFSETS:
            rc, out, _ = self._run(version_cmd(offset))
            raw = out.strip()
            if rc != 0 or not raw:
                continue
            try:
                value = float(raw.split()[0])
            except ValueError:
                continue
            if value == value and lo <= value <= hi:
                self._version_source = f"{BINARY_PATH} @{offset}"
                return f"{value:g}", value
        self._version_source = None
        return None, None

    def _read_version_number(self) -> Optional[float]:
        return self._read_version()[1]

    def detect_layout(self) -> str:
        """'v5' | 'v4' | 'both' | 'none' from the install directories."""
        has_v5 = self._remote_exists(BINARY_PATH)
        has_v4 = self._remote_exists(LEGACY_BINARY_PATH) or self._remote_exists(LEGACY_DIR)
        return "both" if (has_v5 and has_v4) else "v5" if has_v5 else "v4" if has_v4 else "none"

    def check_version(self) -> Optional[float]:
        """
            /opt/IC7/bin/IC7             -> v5 or newer   (OK)
            /opt/Garw_IC7/bin/Garw_IC7   -> v4            (refuse)
            both / neither               -> unexpected    (refuse)
        """
        self.log(f"Checking firmware layout: {BINARY_PATH} ...")
        layout = self.detect_layout()
        has_v5, has_v4 = layout in ("v5", "both"), layout in ("v4", "both")
        if not has_v5:
            if has_v4:
                raise RuntimeError(
                    f"This unit is running v4 firmware ({LEGACY_DIR} present, {BINARY_PATH} missing).\n"
                    f"v{MIN_VERSION:g} or newer is required — update the unit first. Aborting.")
            raise RuntimeError(
                f"Neither {BINARY_PATH} nor {LEGACY_BINARY_PATH} found — is this a GARW device? Aborting.")
        if has_v4:
            raise RuntimeError(
                f"Both {BINARY_PATH} and {LEGACY_DIR} exist on this unit — that should never "
                "happen after an upgrade. Aborting; check the unit's firmware install.")
        display, version = self._read_version()
        if version is None:
            self.log(f"Firmware: v{MIN_VERSION:g}+ layout detected (no {VERSION_FILE}, and the version number "
                     f"is not readable from the binary at offsets {', '.join(map(str, VERSION_OFFSETS))}).")
        else:
            self.log(f"Firmware: v{display} ({self._version_source})")
            if version < MIN_VERSION:
                raise RuntimeError(
                    f"GARW device reports v{display}; v{MIN_VERSION:g} or newer is required. Aborting.")
        return version

    # -- inventory -----------------------------------------------------------
    def list_dashes(self) -> List[dict]:
        """
        [{name, has_qml, has_png, files, source: {…} or {}}] for every folder in
        the library. One shell round-trip; marker JSON travels base64-encoded.
        """
        script = (
            f"cd {_sq(LIBRARY_DIR)} 2>/dev/null || exit 0; "
            "for d in */; do d=${d%/}; [ -d \"$d\" ] || continue; "
            "q=0; p=0; [ -f \"$d/$d.qml\" ] && q=1; [ -f \"$d/$d.qml.png\" ] && p=1; "
            "n=$(find \"$d\" -type f | wc -l); "
            f"m=''; [ -f \"$d/{SOURCE_MARKER}\" ] && m=$(od -An -tx1 -v \"$d/{SOURCE_MARKER}\" | tr -d ' \\n'); "
            "echo \"$d|$q|$p|$n|$m\"; done"
        )
        rc, out, err = self._run(script, timeout=60)
        dashes = []
        for line in out.splitlines():
            parts = line.rstrip("\n").split("|", 4)
            if len(parts) != 5 or parts[0].startswith("."):
                continue
            name, q, p, n, m = parts
            source = {}
            if m:
                try:
                    source = json.loads(bytes.fromhex(m).decode("utf-8"))
                except Exception:
                    source = {"error": "unreadable marker"}
            dashes.append({"name": name, "has_qml": q == "1", "has_png": p == "1",
                           "files": int(n.strip() or 0), "source": source})
        return dashes

    # -- upload --------------------------------------------------------------
    def confirm_replacements(self, pkgs: List[DashPackage]) -> None:
        rc, _, err = self._run(f"mkdir -p {_sq(LIBRARY_DIR)}")
        if rc != 0:
            raise RuntimeError(f"Cannot create {LIBRARY_DIR}: {err.strip()}")
        existing = [p.name for p in pkgs if self._remote_exists(p.remote_dir)]
        if not existing:
            return
        plural = len(existing) > 1
        if not self.confirm(
            "Replace existing dash" + ("es?" if plural else "?"),
            ("These dashes already exist" if plural else "This dash already exists")
            + f" on the unit in {LIBRARY_DIR}:\n\n  " + "\n  ".join(existing)
            + "\n\nReplace " + ("them" if plural else "it") + "?"):
            raise UploadAborted("Upload cancelled — nothing was changed on the unit.")

    def upload(self, pkg: DashPackage, done_before: int = 0, grand_total: int = 0):
        remote_dir = pkg.remote_dir
        grand_total = grand_total or len(pkg.files)
        if self._remote_exists(remote_dir):
            self.log(f"Removing existing {remote_dir} ...")
            rc, _, err = self._run(f"rm -rf {_sq(remote_dir)}")
            if rc != 0:
                raise RuntimeError(f"Could not remove {remote_dir}: {err.strip()}")

        # Upload into a temp dir and rename into place so a dropped connection
        # never leaves a half-written dash in the library.
        tmp_dir = f"{LIBRARY_DIR}/.{pkg.name}.uploading"
        self._run(f"rm -rf {_sq(tmp_dir)}")
        sftp = self.client.open_sftp()
        try:
            made = set()

            def ensure_dir(path):
                if path in made:
                    return
                parent = path.rsplit("/", 1)[0]
                if parent and parent != LIBRARY_DIR and parent not in made:
                    ensure_dir(parent)
                try:
                    sftp.stat(path)
                except IOError:
                    sftp.mkdir(path)
                made.add(path)

            ensure_dir(tmp_dir)
            total = len(pkg.files)
            for i, (rel, data) in enumerate(pkg.files, 1):
                remote_path = f"{tmp_dir}/{rel}"
                if "/" in rel:
                    ensure_dir(remote_path.rsplit("/", 1)[0])
                self.log(f"  [{i}/{total}] {pkg.name}/{rel}  ({len(data):,} bytes)")
                FILE_LOG.debug("SFTP put %s (%d bytes)", remote_path, len(data))
                with sftp.file(remote_path, "wb") as fh:
                    fh.set_pipelined(True)
                    fh.write(data)
                self.progress(done_before + i, grand_total)
        finally:
            sftp.close()

        rc, _, err = self._run(
            f"mv {_sq(tmp_dir)} {_sq(remote_dir)} && chmod -R a+rX {_sq(remote_dir)} && sync")
        if rc != 0:
            self._run(f"rm -rf {_sq(tmp_dir)}")
            raise RuntimeError(f"Failed to finalise {remote_dir}: {err.strip()}")
        for fname in (f"{pkg.name}.qml", f"{pkg.name}.qml.png"):
            if not self._remote_exists(f"{remote_dir}/{fname}"):
                raise RuntimeError(f"Verification failed: {remote_dir}/{fname} missing after upload.")
        self.log(f"Uploaded {total} file(s) to {remote_dir}")

    def upload_all(self, pkgs: List[DashPackage]):
        self.confirm_replacements(pkgs)
        grand_total = sum(len(p.files) for p in pkgs)
        done = 0
        for n, pkg in enumerate(pkgs, 1):
            self.log(f"--- Dash {n}/{len(pkgs)}: {pkg.name} ---")
            self.upload(pkg, done, grand_total)
            done += len(pkg.files)
        self.log(f"All {len(pkgs)} dash(es) uploaded: " + ", ".join(p.name for p in pkgs))

    # -- delete --------------------------------------------------------------
    def delete_dashes(self, names: List[str]) -> List[str]:
        """rm -rf each named dash after one confirmation. Returns names removed."""
        names = [n for n in names if NAME_RE.match(n)]
        if not names:
            return []
        plural = len(names) > 1
        if not self.confirm(
            "Delete dash" + ("es?" if plural else "?"),
            f"Permanently delete from {LIBRARY_DIR} on the unit:\n\n  " + "\n  ".join(names)
            + "\n\nThis cannot be undone."):
            raise UploadAborted("Delete cancelled — nothing was changed on the unit.")
        removed = []
        for n in names:
            path = f"{LIBRARY_DIR}/{n}"
            self.log(f"Deleting {path} ...")
            rc, _, err = self._run(f"rm -rf {_sq(path)} && sync")
            if rc != 0 or self._remote_exists(path):
                raise RuntimeError(f"Could not delete {path}: {err.strip() or 'still present'}")
            removed.append(n)
        self.log(f"Deleted {len(removed)} dash(es): {', '.join(removed)}")
        return removed

    # -- settings files (/opt/IC7/screen_configs) ----------------------------
    def config_usage(self) -> Dict[str, List[str]]:
        """{config filename: [dash names whose .qml mentions it]} from the installed library."""
        rc, out, _ = self._run(
            f"grep -o 'screen_configs/[A-Za-z0-9_.-]*' {LIBRARY_DIR}/*/*.qml 2>/dev/null", timeout=30)
        usage: Dict[str, List[str]] = {}
        for line in out.splitlines():
            # /opt/IC7/library/GTDash/GTDash.qml:screen_configs/gtdash_config.txt
            path, _, ref = line.partition(":screen_configs/")
            if not ref:
                continue
            ref = ref.strip().rstrip(".")   # prose in QML comments can end "…config.txt."
            if not ref:
                continue
            dash = path.rsplit("/", 2)[-2] if path.count("/") >= 2 else path
            usage.setdefault(ref, [])
            if dash not in usage[ref]:
                usage[ref].append(dash)
        return usage

    def list_configs(self) -> List[dict]:
        """[{name, size, mtime (epoch), dashes: [..]}] for every file in SCREEN_CONFIGS_DIR."""
        self._run(f"mkdir -p {_sq(SCREEN_CONFIGS_DIR)}")
        usage = self.config_usage()
        sftp = self.client.open_sftp()
        try:
            rows = []
            for a in sftp.listdir_attr(SCREEN_CONFIGS_DIR):
                if a.filename.startswith(".") or not (a.st_mode or 0) & 0o100000:  # regular files only
                    continue
                rows.append({"name": a.filename, "size": a.st_size or 0, "mtime": a.st_mtime or 0,
                             "dashes": usage.get(a.filename, [])})
        finally:
            sftp.close()
        # Configs a dash expects but that don't exist yet are worth showing too.
        present = {r["name"] for r in rows}
        for ref, dashes in usage.items():
            if ref not in present:
                rows.append({"name": ref, "size": None, "mtime": None, "dashes": dashes})
        rows.sort(key=lambda r: r["name"].lower())
        return rows

    def read_dash_qml(self, dash: str) -> str:
        """The installed <dash>/<dash>.qml (for settings-file annotation)."""
        if not NAME_RE.match(dash):
            raise RuntimeError(f"Bad dash name: {dash}")
        sftp = self.client.open_sftp()
        try:
            with sftp.file(f"{LIBRARY_DIR}/{dash}/{dash}.qml", "rb") as fh:
                return fh.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        finally:
            sftp.close()

    def read_config(self, name: str) -> str:
        if "/" in name or name.startswith("."):
            raise RuntimeError(f"Bad config name: {name}")
        sftp = self.client.open_sftp()
        try:
            with sftp.file(f"{SCREEN_CONFIGS_DIR}/{name}", "rb") as fh:
                data = fh.read(CONFIG_PREVIEW_MAX + 1)
        finally:
            sftp.close()
        text = data[:CONFIG_PREVIEW_MAX].decode("utf-8", errors="replace")
        if len(data) > CONFIG_PREVIEW_MAX:
            text += "\n… (truncated)"
        return text

    def write_config(self, name: str, text: str) -> None:
        """Save edited text as SCREEN_CONFIGS_DIR/name (staged, then moved into place)."""
        if "/" in name or name.startswith(".") or not re.match(r"^[A-Za-z0-9_.\-]+$", name):
            raise RuntimeError(f"Bad config name: {name}")
        self._run(f"mkdir -p {_sq(SCREEN_CONFIGS_DIR)}")
        remote = f"{SCREEN_CONFIGS_DIR}/{name}"
        tmp = f"{SCREEN_CONFIGS_DIR}/.{name}.editing"
        data = text.encode("utf-8")
        FILE_LOG.debug("SFTP put %s (%d bytes, staged as %s)", remote, len(data), tmp)
        sftp = self.client.open_sftp()
        try:
            with sftp.file(tmp, "wb") as fh:
                fh.write(data)
        finally:
            sftp.close()
        rc, _, err = self._run(f"mv {_sq(tmp)} {_sq(remote)} && chmod a+r {_sq(remote)} && sync")
        if rc != 0:
            self._run(f"rm -f {_sq(tmp)}")
            raise RuntimeError(f"Failed to save {remote}: {err.strip()}")
        self.log(f"Saved {remote}  ({len(data):,} bytes)")

    def download_configs(self, names: List[str], local_dir: str) -> List[str]:
        """Copy the named settings files into local_dir. Returns local paths."""
        os.makedirs(local_dir, exist_ok=True)
        saved = []
        sftp = self.client.open_sftp()
        try:
            for i, name in enumerate(names, 1):
                if "/" in name or name.startswith("."):
                    continue
                dest = os.path.join(local_dir, name)
                self.log(f"  [{i}/{len(names)}] {name} → {dest}")
                FILE_LOG.debug("SFTP get %s/%s -> %s", SCREEN_CONFIGS_DIR, name, dest)
                sftp.get(f"{SCREEN_CONFIGS_DIR}/{name}", dest)
                saved.append(dest)
                self.progress(i, len(names))
        finally:
            sftp.close()
        self.log(f"Downloaded {len(saved)} settings file(s) to {local_dir}")
        return saved

    def upload_configs(self, local_paths: List[str]) -> List[str]:
        """Copy local files into SCREEN_CONFIGS_DIR (asks once before overwriting)."""
        local_paths = [p for p in local_paths if os.path.isfile(p)]
        if not local_paths:
            return []
        self._run(f"mkdir -p {_sq(SCREEN_CONFIGS_DIR)}")
        names = [os.path.basename(p) for p in local_paths]
        bad = [n for n in names if not re.match(r"^[A-Za-z0-9_.\-]+$", n)]
        if bad:
            raise RuntimeError("Unsafe file name(s): " + ", ".join(bad))
        existing = [n for n in names if self._remote_exists(f"{SCREEN_CONFIGS_DIR}/{n}")]
        if existing and not self.confirm(
                "Overwrite settings file" + ("s?" if len(existing) > 1 else "?"),
                f"Already on the unit in {SCREEN_CONFIGS_DIR}:\n\n  " + "\n  ".join(existing)
                + "\n\nOverwrite with your local copy?"):
            raise UploadAborted("Upload cancelled — settings on the unit unchanged.")
        sent = []
        sftp = self.client.open_sftp()
        try:
            for i, (path, name) in enumerate(zip(local_paths, names), 1):
                remote = f"{SCREEN_CONFIGS_DIR}/{name}"
                tmp = f"{SCREEN_CONFIGS_DIR}/.{name}.uploading"
                self.log(f"  [{i}/{len(names)}] {path} → {remote}  ({os.path.getsize(path):,} bytes)")
                FILE_LOG.debug("SFTP put %s -> %s", path, remote)
                sftp.put(path, tmp)
                rc, _, err = self._run(f"mv {_sq(tmp)} {_sq(remote)} && chmod a+r {_sq(remote)} && sync")
                if rc != 0:
                    self._run(f"rm -f {_sq(tmp)}")
                    raise RuntimeError(f"Failed to write {remote}: {err.strip()}")
                sent.append(name)
                self.progress(i, len(names))
        finally:
            sftp.close()
        self.log(f"Uploaded {len(sent)} settings file(s): {', '.join(sent)}")
        return sent

    # -- system information ----------------------------------------------------
    def system_info(self) -> List[Tuple[str, str]]:
        """Ordered (label, value) pairs describing the unit. Tolerant of missing tools."""
        script = r"""
echo "@@layout_v5=$([ -e /opt/IC7/bin/IC7 ] && echo 1 || echo 0)"
echo "@@layout_v4=$([ -e /opt/Garw_IC7 ] && echo 1 || echo 0)"
echo "@@bootloader=$(cat /opt/IC7/bootloader_version.txt 2>/dev/null | tr -d '\r\n')"
echo "@@hostname=$(hostname 2>/dev/null)"
echo "@@kernel=$(uname -r 2>/dev/null)"
echo "@@arch=$(uname -m 2>/dev/null)"
echo "@@uname=$(uname -a 2>/dev/null)"
echo "@@os=$( (grep -s PRETTY_NAME /etc/os-release || grep -s NAME= /etc/os-release || cat /etc/issue 2>/dev/null) | head -1 | sed 's/^[A-Z_]*=//; s/\"//g')"
echo "@@cpu_model=$( (grep -m1 -E '^(model name|Processor|Hardware)' /proc/cpuinfo) | cut -d: -f2- | sed 's/^ *//')"
echo "@@cpu_hw=$(grep -m1 '^Hardware' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
echo "@@cpu_cores=$(grep -c '^processor' /proc/cpuinfo)"
echo "@@cpu_mhz=$(grep -m1 -i 'MHz' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
echo "@@cpu_freq=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null)"
echo "@@cpu_temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
echo "@@mem_total=$(grep MemTotal /proc/meminfo | awk '{print $2}')"
echo "@@mem_avail=$(grep -E 'MemAvailable' /proc/meminfo | awk '{print $2}')"
echo "@@mem_free=$(grep -E '^MemFree' /proc/meminfo | awk '{print $2}')"
echo "@@uptime=$(cut -d. -f1 /proc/uptime)"
echo "@@load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "@@date=$(date 2>/dev/null)"
echo "@@disk_root=$(df -k / 2>/dev/null | tail -1)"
echo "@@disk_opt=$(df -k /opt 2>/dev/null | tail -1)"
echo "@@disk_mnt=$(df -k /mnt 2>/dev/null | tail -1)"
echo "@@ip=$( (ip -4 addr show 2>/dev/null || ifconfig 2>/dev/null) | grep -o 'inet [0-9.]*' | grep -v 127.0.0.1 | awk '{print $2}' | tr '\n' ' ')"
echo "@@mac=$( (cat /sys/class/net/wlan0/address || cat /sys/class/net/eth0/address) 2>/dev/null)"
echo "@@ic7_running=$( (pidof IC7 || pgrep -x IC7) 2>/dev/null | wc -w)"
echo "@@ccrypt=$(which ccrypt 2>/dev/null)"
echo "@@dashes=$(ls -1 /opt/IC7/library 2>/dev/null | grep -v '^\.' | wc -l)"
echo "@@qt_linked=$( (LD_TRACE_LOADED_OBJECTS=1 /opt/IC7/bin/IC7 2>/dev/null || ldd /opt/IC7/bin/IC7 2>/dev/null) | grep -m1 -o '/[^ ]*libQt5Core[^ ]*')"
echo "@@qt_files=$(ls /usr/lib/libQt5Core.so.5.*.* /usr/lib/arm-linux-gnueabihf/libQt5Core.so.5.*.* /build/qt512/output/host/usr/arm-buildroot-linux-gnueabihf/sysroot/usr/lib/libQt5Core.so.5.*.* 2>/dev/null | tr '\n' ' ')"
echo "@@qml_dir=$( (ls -d /usr/qml /usr/lib/qt/qml /build/qt512/output/host/usr/arm-buildroot-linux-gnueabihf/sysroot/usr/qml 2>/dev/null) | tr '\n' ' ')"
echo "@@ic7_bin=$(ls -l /opt/IC7/bin/IC7 2>/dev/null | awk '{print $5}')"
"""
        rc, out, _ = self._run(script, timeout=40)
        raw = {}
        for line in out.splitlines():
            if line.startswith("@@") and "=" in line:
                k, _, v = line[2:].partition("=")
                raw[k] = v.strip()
        vdisplay, version = self._read_version() if raw.get("layout_v5") == "1" else (None, None)

        def kb(v):
            try:
                n = int(v)
            except ValueError:
                return v
            return f"{n / 1024:.0f} MB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.2f} GB"

        def df(v):
            parts = v.split()
            if len(parts) < 5:
                return v or "—"
            try:
                total, used = int(parts[1]), int(parts[2])
                return f"{kb(str(used))} used of {kb(str(total))} ({parts[4]})"
            except ValueError:
                return v

        def uptime(v):
            try:
                secs = int(v)
            except ValueError:
                return v
            d, r = divmod(secs, 86400)
            h, r = divmod(r, 3600)
            m = r // 60
            return (f"{d}d " if d else "") + f"{h}h {m:02d}m"

        layout = {"11": "v5 + legacy v4 dir (unexpected)", "10": "v5", "01": "v4", "00": "not a GARW device?"}[
            raw.get("layout_v5", "0") + raw.get("layout_v4", "0")]
        temp = raw.get("cpu_temp", "")
        if temp.isdigit():
            temp = f"{int(temp) / (1000 if int(temp) > 1000 else 1):.0f} °C"
        freq = raw.get("cpu_freq", "")
        if freq.isdigit():
            freq = f"{int(freq) / 1000:.0f} MHz"
        # Qt: prefer the library the binary really loads; resolve symlink -> real versioned file.
        qt_path = raw.get("qt_linked", "")
        qt_files = raw.get("qt_files", "").split()
        if qt_path:
            rc, real, _ = self._run(f"readlink -f {_sq(qt_path)} 2>/dev/null || echo {_sq(qt_path)}")
            qt_path = real.strip() or qt_path
        elif qt_files:
            qt_path = qt_files[0]
        qt_ver = re.search(r"libQt5Core\.so\.(\d+\.\d+\.\d+)", qt_path or "")
        qt_str = (f"{qt_ver.group(1)}  ·  {qt_path}" if qt_ver else (qt_path or "—"))
        if qt_files and qt_path and any(f != qt_path for f in qt_files):
            others = [f for f in qt_files if f != qt_path]
            qt_str += f"   (+{len(others)} other cop{'y' if len(others) == 1 else 'ies'})"
        cpu = raw.get("cpu_model") or raw.get("cpu_hw") or "—"
        if raw.get("cpu_hw") and raw.get("cpu_hw") != cpu:
            cpu += f"  ({raw['cpu_hw']})"
        info = [
            ("Firmware", (f"v{vdisplay}  ({self._version_source})" if vdisplay else "v5+ (number unreadable)")
                         if raw.get("layout_v5") == "1" else layout),
            ("Install layout", layout),
            ("Bootloader", raw.get("bootloader") or "—"),
            ("GARW binary process", "running" if raw.get("ic7_running", "0") not in ("", "0") else "not running"),
            ("Dashes in library", raw.get("dashes") or "0"),
            ("OS", raw.get("os") or "—"),
            ("Kernel", f"{raw.get('kernel', '—')} ({raw.get('arch', '')})".strip()),
            ("Qt", qt_str),
            ("QML imports", raw.get("qml_dir", "").strip() or "—"),
            ("GARW binary", f"{BINARY_PATH}  ({int(raw['ic7_bin']):,} bytes)" if raw.get("ic7_bin", "").isdigit() else BINARY_PATH),
            ("CPU", cpu),
            ("Cores / clock", f"{raw.get('cpu_cores', '?')} × {freq or raw.get('cpu_mhz') or '?'}"
                              + (f"  ·  {temp}" if temp else "")),
            ("RAM", f"{kb(raw['mem_total'])} total, {kb(raw.get('mem_avail') or raw.get('mem_free', '0'))} available"
                    if raw.get("mem_total") else "—"),
            ("Storage /", df(raw.get("disk_root", ""))),
            ("Storage /opt", df(raw.get("disk_opt", "")) if raw.get("disk_opt") != raw.get("disk_root") else "(same as /)"),
            ("Scratch /mnt", df(raw.get("disk_mnt", ""))),
            ("Uptime / load", f"{uptime(raw.get('uptime', ''))}   load {raw.get('load', '—')}"),
            ("Device clock", raw.get("date") or "—"),
            ("Hostname", raw.get("hostname") or "—"),
            ("IP / MAC", f"{raw.get('ip', '').strip() or '—'}   {raw.get('mac', '')}".rstrip()),
            ("ccrypt on unit", raw.get("ccrypt") or "not found"),
        ]
        return info

    # -- firmware ------------------------------------------------------------
    def read_firmware_passphrase(self, layout: str) -> str:
        """The passphrase the unit's own updater uses, read from the GARW binary on the device
        (`… K99updater start <passphrase> <ver>`). BusyBox has no `strings`, so `grep -a -o`."""
        binaries = [BINARY_PATH, LEGACY_BINARY_PATH] if layout in ("v5", "both") else [LEGACY_BINARY_PATH, BINARY_PATH]
        for b in binaries:
            if not self._remote_exists(b):
                continue
            rc, out, _ = self._run(f"grep -a -o {_sq(UPDATER_MARKER + '[^ ]*')} {_sq(b)} | head -1", timeout=60)
            m = re.search(re.escape(UPDATER_MARKER) + r"(\S+)", out)
            if rc == 0 and m:
                self._fw_pass = m.group(1)
                self.log(f"Update passphrase read from {b} (masked in logs).")
                return self._fw_pass
        raise RuntimeError("Could not find the updater passphrase in the GARW binary on the unit "
                           f"(looked for '{UPDATER_MARKER.strip()}' in {', '.join(binaries)}). "
                           "This firmware may use a different update mechanism — aborting before touching anything.")

    @staticmethod
    def _local_ccrypt() -> Optional[str]:
        import shutil
        return shutil.which("ccrypt")

    def install_firmware(self, archive_path: str, on_step=lambda step: None) -> dict:
        """
        Apply a GARW firmware package (a .zip holding <name>.tar.cpt, or the .tar.cpt itself):
          1. upload to /mnt on the unit
          2. ccrypt -d -K <passphrase read from the GARW binary>   (on the unit; local ccrypt fallback)
          3. tar xf into /mnt, run /mnt/run (the package's own installer), sync
          4. verify /opt/IC7/bin/IC7 and report the new version
        Caller reboots. Returns {'before': layout, 'after': layout, 'version': float|None}.
        """
        import shutil
        import tarfile
        import tempfile

        # -- unpack the outer zip locally ---------------------------------
        src = archive_path
        tmpdir = tempfile.mkdtemp(prefix="garw_fw_")
        try:
            if zipfile.is_zipfile(src):
                with zipfile.ZipFile(src) as zf:
                    cpts = [n for n in zf.namelist() if n.lower().endswith(".tar.cpt") and not n.startswith("__MACOSX")]
                    if len(cpts) != 1:
                        raise RuntimeError(f"Expected exactly one .tar.cpt inside the zip, found {len(cpts)}: {cpts[:5]}")
                    self.log(f"Extracting {cpts[0]} from {os.path.basename(src)} ...")
                    src = zf.extract(cpts[0], tmpdir)
            if not src.lower().endswith(".tar.cpt"):
                raise RuntimeError("Firmware must be a .zip containing a .tar.cpt, or the .tar.cpt itself.")
            size = os.path.getsize(src)
            base = os.path.basename(src)
            self.log(f"Firmware package: {base}  ({size / 1024 / 1024:.1f} MB)")

            before = self.detect_layout()
            if before == "none":
                raise RuntimeError(f"Neither {BINARY_PATH} nor {LEGACY_DIR} found — refusing to flash an unknown device.")
            self.log(f"Unit layout before update: {before}")
            passphrase = self.read_firmware_passphrase(before)

            # -- where to decrypt --------------------------------------------
            rc, which, _ = self._run("which ccrypt 2>/dev/null")
            unit_ccrypt = rc == 0 and which.strip()
            local_ccrypt = None if unit_ccrypt else self._local_ccrypt()
            if not unit_ccrypt and not local_ccrypt:
                raise RuntimeError("ccrypt is not available on the unit or on this computer, so the package "
                                   "cannot be decrypted. Install ccrypt locally (brew install ccrypt / apt install ccrypt).")
            upload_path = src
            remote_name = base
            if local_ccrypt:
                self.log(f"Unit has no ccrypt — decrypting locally with {local_ccrypt} ...")
                dec = os.path.join(tmpdir, base[:-4])  # strip .cpt -> .tar
                with open(dec, "wb") as fh:
                    r = subprocess.run([local_ccrypt, "-d", "-K", passphrase, "-c", src],
                                       stdout=fh, stderr=subprocess.PIPE, timeout=600)
                if r.returncode != 0:
                    raise RuntimeError("Local ccrypt failed: " + r.stderr.decode(errors="replace").strip())
                with tarfile.open(dec) as tf:
                    names = tf.getnames()
                if "run" not in names and "./run" not in names:
                    raise RuntimeError("Decrypted archive has no 'run' installer script — not a GARW firmware package.")
                upload_path, remote_name, size = dec, base[:-4], os.path.getsize(dec)
                self.log(f"  decrypted OK: {len(names)} entries, run script present")

            # -- space check on the unit -------------------------------------
            rc, out, _ = self._run(f"df -k {FIRMWARE_SCRATCH} | tail -1")
            free_kb = 0
            parts = out.split()
            if len(parts) >= 4 and parts[3].isdigit():
                free_kb = int(parts[3])
            need_kb = int(size / 1024 * 2.3) + 4096
            if free_kb and free_kb < need_kb:
                raise RuntimeError(f"{FIRMWARE_SCRATCH} has {free_kb // 1024} MB free; the update needs about "
                                   f"{need_kb // 1024} MB to unpack. Free space on the unit first.")
            self.log(f"{FIRMWARE_SCRATCH}: {free_kb // 1024} MB free (need ≈{need_kb // 1024} MB)")
            self._run(f"rm -rf {FIRMWARE_SCRATCH}/opt {FIRMWARE_SCRATCH}/build {FIRMWARE_SCRATCH}/etc "
                      f"{FIRMWARE_SCRATCH}/usr {FIRMWARE_SCRATCH}/run {FIRMWARE_SCRATCH}/*.tar {FIRMWARE_SCRATCH}/*.tar.cpt")

            # -- upload --------------------------------------------------------
            on_step("upload")
            remote = f"{FIRMWARE_SCRATCH}/{remote_name}"
            self.log(f"Uploading {remote_name} → {remote} ...")
            FILE_LOG.info("SFTP put firmware %s -> %s (%d bytes)", upload_path, remote, size)
            sftp = self.client.open_sftp()
            try:
                sftp.put(upload_path, remote, callback=lambda done, total: self.progress(done, total))
            finally:
                sftp.close()
            rc, out, _ = self._run(f"wc -c < {_sq(remote)}")
            if rc != 0 or int(out.strip() or 0) != size:
                raise RuntimeError(f"Upload size mismatch ({out.strip()} vs {size} bytes) — not applying.")
            self.log("Upload verified.")

            # -- decrypt + extract on the unit ---------------------------------
            on_step("unpack")
            if unit_ccrypt:
                self.log(f"Decrypting on the unit: ccrypt -d -K ***** {remote_name}")
                rc, out, err = self._run(f"cd {FIRMWARE_SCRATCH} && ccrypt -d -K {_sq(passphrase)} {_sq(remote_name)}", timeout=900)
                if rc != 0:
                    raise RuntimeError(f"ccrypt failed on the unit: {(err or out).strip()}")
                tar_name = remote_name[:-4]
            else:
                tar_name = remote_name
            self.log(f"Extracting {tar_name} in {FIRMWARE_SCRATCH} ...")
            rc, out, err = self._run(f"cd {FIRMWARE_SCRATCH} && tar xf {_sq(tar_name)} && rm -f {_sq(tar_name)} && sync", timeout=900)
            if rc != 0:
                raise RuntimeError(f"tar failed on the unit: {(err or out).strip()}")
            if not self._remote_exists(f"{FIRMWARE_SCRATCH}/run"):
                raise RuntimeError(f"Package has no {FIRMWARE_SCRATCH}/run installer — aborting before touching the system.")
            rc, out, _ = self._run(f"ls {FIRMWARE_SCRATCH}")
            self.log("Unpacked: " + " ".join(out.split()))

            # -- run the package's installer -------------------------------------
            on_step("install")
            self.log(f"Running {FIRMWARE_SCRATCH}/run — do not power off the unit ...")
            rc, out, err = self._run(f"cd {FIRMWARE_SCRATCH} && sh ./run; echo \"@@rc=$?\"; sync", timeout=1200)
            m = re.search(r"@@rc=(\d+)", out)
            run_rc = int(m.group(1)) if m else rc
            for line in (out.replace(m.group(0), "") if m else out).strip().splitlines():
                self.log(f"  run: {line}")
            if err.strip():
                for line in err.strip().splitlines():
                    self.log(f"  run! {line}")
            if run_rc != 0:
                raise RuntimeError(f"The firmware's run script exited with status {run_rc}. "
                                   "Check the log above; the unit may be partially updated — do not power off, "
                                   "retry the install.")
            self.log(f"Cleaning up {FIRMWARE_SCRATCH} ...")
            self._run(f"rm -rf {FIRMWARE_SCRATCH}/run {FIRMWARE_SCRATCH}/*.tar {FIRMWARE_SCRATCH}/*.tar.cpt "
                      f"{FIRMWARE_SCRATCH}/opt {FIRMWARE_SCRATCH}/build {FIRMWARE_SCRATCH}/etc {FIRMWARE_SCRATCH}/usr; sync")

            # -- verify ------------------------------------------------------------
            on_step("verify")
            after = self.detect_layout()
            display, version = self._read_version() if after in ("v5", "both") else (None, None)
            if after not in ("v5", "both"):
                raise RuntimeError(f"After running the installer {BINARY_PATH} is still missing (layout: {after}).")
            self.log(f"Firmware installed. Layout: {before} → {after}; GARW device reports "
                     + (f"v{display} ({self._version_source})" if display else "v5+ (number unreadable)"))
            return {"before": before, "after": after, "version": version, "display": display}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def remove_legacy_dir(self) -> bool:
        """Delete /opt/Garw_IC7 (v4 remnants). Caller must have confirmed."""
        self.log(f"Removing legacy {LEGACY_DIR} ...")
        rc, _, err = self._run(f"rm -rf {_sq(LEGACY_DIR)} && sync", timeout=120)
        if rc != 0 or self._remote_exists(LEGACY_DIR):
            raise RuntimeError(f"Could not remove {LEGACY_DIR}: {err.strip()}")
        self.log(f"{LEGACY_DIR} removed.")
        return True

    # -- restart just the dash application (no OS reboot) ---------------------
    def restart_dash_app(self, timeout: float = 12.0) -> bool:
        """
        Kill /opt/IC7/bin/IC7 and start it again, keeping the boot-time environment.
        /etc/init.d/S50screen's `stop` only echoes, so a plain stop/start would leave two
        IC7 processes running; instead we read the live process's environment and cwd from
        /proc, kill it, wait for it to go, and relaunch detached (setsid) so it survives
        the SSH session closing. Falls back to `S50screen start` if IC7 wasn't running.
        """
        self.log("Restarting the GARW binary …")
        rc, out, _ = self._run("pidof IC7 2>/dev/null || pgrep -x IC7 2>/dev/null")
        pids = out.split()
        env_lines, cwd = [], "/"
        if pids:
            pid = pids[0]
            rc, envout, _ = self._run(f"tr '\\0' '\\n' < /proc/{pid}/environ 2>/dev/null")
            env_lines = [l for l in envout.splitlines() if "=" in l and not l.startswith(("SSH_", "PWD=", "OLDPWD=", "_="))]
            rc, cwdout, _ = self._run(f"readlink /proc/{pid}/cwd 2>/dev/null")
            cwd = cwdout.strip() or "/"
            self.log(f"  GARW binary pid {pid}, {len(env_lines)} env var(s) captured, cwd {cwd}")
            self._run(f"kill {' '.join(pids)} 2>/dev/null")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rc, out, _ = self._run("pidof IC7 2>/dev/null || pgrep -x IC7 2>/dev/null")
                if not out.strip():
                    break
                time.sleep(0.3)
            else:
                self.log("  GARW binary did not exit on SIGTERM — forcing")
                self._run("kill -9 $(pidof IC7 2>/dev/null || pgrep -x IC7) 2>/dev/null; sleep 0.5")
        else:
            self.log("  GARW binary was not running")
        if env_lines:
            envs = " ".join(_sq(l) for l in env_lines)
            cmd = (f"cd {_sq(cwd)} && (setsid env -i {envs} {BINARY_PATH} >/dev/null 2>&1 </dev/null &) "
                   f"|| (nohup env -i {envs} {BINARY_PATH} >/dev/null 2>&1 </dev/null &)")
        else:
            cmd = ("(setsid /etc/init.d/S50screen start >/dev/null 2>&1 </dev/null &) "
                   "|| (nohup /etc/init.d/S50screen start >/dev/null 2>&1 </dev/null &)")
        self._run(cmd, timeout=15)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            rc, out, _ = self._run("pidof IC7 2>/dev/null || pgrep -x IC7 2>/dev/null")
            if out.strip():
                self.log(f"GARW binary restarted (pid {out.split()[0]}).")
                return True
        raise RuntimeError("GARW binary did not come back after restart — use 'Reboot device' to recover.")

    # -- reboot --------------------------------------------------------------
    def reboot(self):
        self.log("Issuing 'reboot now' ...")
        try:
            chan = self.client.get_transport().open_session()
            chan.settimeout(5)
            chan.exec_command("reboot now")
            try:
                chan.recv_exit_status()
            except Exception:
                pass
        except Exception as e:  # connection reset during reboot is normal
            self.log(f"(connection closed during reboot: {e})")
        self.log("Reboot command sent. The cluster will restart in a moment.")

    # -- one-shot convenience (used by CLI and tests) -----------------------
    def run(self, pkgs, do_reboot: bool = True):
        if isinstance(pkgs, DashPackage):
            pkgs = [pkgs]
        try:
            self.connect()
            self.check_version()
            self.upload_all(pkgs)
            if do_reboot:
                self.reboot()
        finally:
            self.close()


Uploader = IC7Device  # backwards-compatible name


# --------------------------------------------------------------------------- #
#  Remote controller (UDP D-pad, replaces the discontinued phone apps)
# --------------------------------------------------------------------------- #
CONTROL_PORT = 1234
BTN = {"none": b"\x01\x00", "up": b"\x01\x01", "down": b"\x01\x02", "left": b"\x01\x04", "right": b"\x01\x08"}
HEARTBEAT_S = 0.2          # the unit latches the last byte; 01 00 every 200 ms = "nothing pressed"
SETTINGS_HOLD_S = 3.0      # 's': Left held 3 s, then Right (the original script used 5 s)
OS_SETTINGS_HOLD_S = 3.0   # hold Left or Right this long to enter the OS main settings
SETTINGS_FLIP_S = 2.0      # after saving a settings file: Right, wait this long, Left (dash re-reads its file)


class Controller:
    """
    Background sender for the IC7's UDP button protocol.

    Protocol (from GARWController.js): two bytes, 0x01 then a button mask
    (0x01 up, 0x02 down, 0x04 left, 0x08 right, 0x00 none). The unit keeps the
    last received state until the next packet, so:
      * a tap      = button byte, then 01 00
      * a hold     = keep sending the button byte (or stay silent), then 01 00
      * heartbeat  = 01 00 every 200 ms while idle, so a lost release can't stick
    Repeat mode alternates button/none at `rate` Hz while a key is held, so a
    value under the cursor increments quickly — exactly like tapping fast.
    """

    def __init__(self, host: Optional[str] = None, port: int = CONTROL_PORT, on_send=None):
        self.host, self.port = host or HOST, port
        self.on_send = on_send or (lambda name, data: None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.Lock()
        self.held: Optional[str] = None       # button currently held via keyboard/mouse
        self.mode = "repeat"                  # "repeat" (fast taps) or "hold" (continuous)
        self.rate = 5.0                       # taps per second in repeat mode (8 skipped screens on the unit)
        self.hold_until = 0.0                 # timed hold (OS settings / 's' sequence)
        self.hold_button: Optional[str] = None
        self.after_hold: Optional[str] = None  # button to tap when a timed hold ends
        self.enabled = False
        self.sent = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # -- raw ---------------------------------------------------------------
    def _send(self, name: str):
        try:
            self.sock.sendto(BTN[name], (self.host, self.port))
            self.sent += 1
            self.on_send(name, BTN[name])
        except OSError:
            pass

    # -- public ------------------------------------------------------------
    def tap(self, name: str):
        with self.lock:
            self._send(name)
            self._send("none")

    def press(self, name: str):
        with self.lock:
            self.held = name

    def release(self, name: Optional[str] = None):
        with self.lock:
            if name is None or self.held == name:
                self.held = None
                self._send("none")

    def hold(self, name: str, seconds: float, then: Optional[str] = None):
        """Timed hold (e.g. Left for 3 s to open the OS settings)."""
        with self.lock:
            self.hold_button, self.after_hold = name, then
            self.hold_until = time.monotonic() + seconds
            self._send(name)

    def settings_sequence(self):
        """The 's' key: Left held SETTINGS_HOLD_S seconds, then Right."""
        self.hold("left", SETTINGS_HOLD_S, then="right")

    def cancel(self):
        with self.lock:
            self.held = None
            self.hold_until = 0.0
            self.hold_button = self.after_hold = None
            self._send("none")

    def holding_for(self) -> float:
        """Seconds left on a timed hold (0 if none)."""
        return max(0.0, self.hold_until - time.monotonic()) if self.hold_button else 0.0

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # -- sender loop -------------------------------------------------------
    def _loop(self):
        last_hb = 0.0
        phase = False
        next_tick = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            with self.lock:
                if not self.enabled:
                    delay = 0.1
                elif self.hold_button:
                    if now >= self.hold_until:
                        then, self.hold_button, self.after_hold = self.after_hold, None, None
                        if then:
                            self._send(then)
                        self._send("none")
                        last_hb = now
                    else:
                        self._send(self.hold_button)   # re-assert so a dropped packet can't cut the hold short
                    delay = HEARTBEAT_S
                elif self.held:
                    if self.mode == "hold":
                        self._send(self.held)
                        delay = HEARTBEAT_S
                    else:
                        phase = not phase
                        self._send(self.held if phase else "none")
                        delay = 0.5 / max(self.rate, 0.5)
                else:
                    if now - last_hb >= HEARTBEAT_S:
                        self._send("none")
                        last_hb = now
                    delay = 0.05
            next_tick = max(next_tick + delay, now)
            self._stop.wait(max(0.0, next_tick - time.monotonic()))


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


RETRO_FONT_FILE = "PressStart2P.ttf"      # bundled in assets/ (SIL Open Font License)
RETRO_FONT_FAMILY = "Press Start K"       # the family name inside that TTF


def set_process_app_name(name: str) -> None:
    """When run as a plain script on macOS the Dock/menu bar call the app "Python".
    Rewrite the process's own bundle info before Cocoa starts so it says GARW Genie.
    (A built .app has a real Info.plist and needs none of this.)"""
    if platform.system() != "Darwin" or getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        import ctypes.util
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cf.CFBundleGetMainBundle.restype = ctypes.c_void_p
        cf.CFBundleGetInfoDictionary.restype = ctypes.c_void_p
        cf.CFBundleGetInfoDictionary.argtypes = [ctypes.c_void_p]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDictionarySetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        kCFStringEncodingUTF8 = 0x08000100

        def cfstr(t):
            return cf.CFStringCreateWithCString(None, t.encode("utf-8"), kCFStringEncodingUTF8)
        info = cf.CFBundleGetInfoDictionary(cf.CFBundleGetMainBundle())
        if info:
            for key in ("CFBundleName", "CFBundleDisplayName"):
                cf.CFDictionarySetValue(info, cfstr(key), cfstr(name))
    except Exception:
        pass


def register_font(path: Path) -> bool:
    """Make a TTF visible to Tk for this process without a system install.
    Windows: GDI private font. macOS: CoreText process-scope registration.
    Linux: copy into ~/.fonts and refresh fontconfig. Best effort; returns success."""
    try:
        system = platform.system()
        if system == "Windows":
            import ctypes
            FR_PRIVATE = 0x10
            n = ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0)
            return n > 0
        if system == "Darwin":
            import ctypes
            import ctypes.util
            cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
            ct = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreText"))
            cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
            cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
            ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
            ct.CTFontManagerRegisterFontsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
            b = str(path).encode("utf-8")
            url = cf.CFURLCreateFromFileSystemRepresentation(None, b, len(b), False)
            kCTFontManagerScopeProcess = 1
            ok = ct.CTFontManagerRegisterFontsForURL(url, kCTFontManagerScopeProcess, None)
            cf.CFRelease(ctypes.c_void_p(url))
            return bool(ok)
        # Linux / other X11
        import shutil
        dest = Path.home() / ".fonts" / path.name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, dest)
            subprocess.run(["fc-cache", "-f", str(dest.parent)], capture_output=True, timeout=30)
        return True
    except Exception:
        return False


# Dark "cockpit" palette. Everything visual hangs off these so a re-skin is one edit.
PALETTE = {
    "bg":       "#0f1216",   # window
    "panel":    "#171b21",   # cards / tabs
    "field":    "#1f242c",   # entries, tables, log
    "line":     "#2a303a",   # borders
    "hover":    "#262c36",
    "text":     "#e8ebf0",
    "muted":    "#8a93a3",
    "accent":   "#ff7a1a",   # GARW orange — primary actions
    "accent_hi": "#ff9447",
    "ok":       "#3ddc84",
    "warn":     "#ffb648",
    "err":      "#ff5c5c",
    "sel":      "#2d3a4d",
}


def apply_theme(root, tk, ttk, retro: bool = True) -> dict:
    """Dark ttk theme built on 'clam' (the only stock theme that honours colours everywhere).
    With retro=True and the bundled Press Start 2P font available, every widget uses it."""
    P = PALETTE
    is_mac = platform.system() == "Darwin"
    ui_font = ("SF Pro Text", 12) if is_mac else ("Segoe UI", 10) if platform.system() == "Windows" else ("TkDefaultFont", 10)
    mono = ("Menlo", 11) if is_mac else ("Consolas", 10) if platform.system() == "Windows" else ("DejaVu Sans Mono", 10)
    title_font = (ui_font[0], 18, "bold")
    retro_on = False
    if retro:
        import tkinter.font as tkfont
        ttf = asset_path(RETRO_FONT_FILE)
        if ttf:
            register_font(ttf)
        fams = set(tkfont.families(root))
        fam = next((f for f in (RETRO_FONT_FAMILY, "Press Start 2P") if f in fams), None)
        if fam:
            # Press Start 2P is a pixel font: 8 px per em is its native grid, so sizes are kept
            # small and the whole UI scales up via tk scaling. It is monospaced, so it doubles as mono.
            ui_font = (fam, 8)
            mono = (fam, 8)
            title_font = (fam, 14)
            retro_on = True

    root.configure(bg=P["bg"])
    root.option_add("*Font", ui_font)
    # tk (non-ttk) widgets used by dialogs
    root.option_add("*Dialog.msg.font", ui_font)
    # Only colour the plain-tk widgets we actually use; a blanket *foreground breaks native
    # buttons in tk dialogs (light text on a light macOS button = invisible "Cancel").
    root.option_add("*Entry.background", P["field"])
    root.option_add("*Entry.foreground", P["text"])
    root.option_add("*Entry.insertBackground", P["text"])
    root.option_add("*Text.background", P["field"])
    root.option_add("*Text.foreground", P["text"])
    root.option_add("*Text.insertBackground", P["text"])
    root.option_add("*Listbox.background", P["field"])
    root.option_add("*Listbox.foreground", P["text"])

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=P["panel"], foreground=P["text"], fieldbackground=P["field"],
                bordercolor=P["line"], lightcolor=P["panel"], darkcolor=P["panel"],
                troughcolor=P["field"], focuscolor=P["accent"], font=ui_font)
    s.configure("TFrame", background=P["panel"])
    s.configure("Bg.TFrame", background=P["bg"])
    s.configure("TLabel", background=P["panel"], foreground=P["text"])
    s.configure("Bg.TLabel", background=P["bg"], foreground=P["text"])
    s.configure("Title.TLabel", background=P["bg"], foreground=P["text"], font=title_font)
    s.configure("Muted.TLabel", background=P["panel"], foreground=P["muted"])
    s.configure("BgMuted.TLabel", background=P["bg"], foreground=P["muted"])
    s.configure("Ok.TLabel", background=P["panel"], foreground=P["ok"])
    s.configure("Warn.TLabel", background=P["panel"], foreground=P["warn"])
    s.configure("Err.TLabel", background=P["panel"], foreground=P["err"])
    s.configure("Pill.TLabel", background=P["field"], foreground=P["muted"], padding=(10, 3))

    # Buttons: flat, dark; "Accent.TButton" for the primary action of each tab.
    s.configure("TButton", background=P["field"], foreground=P["text"], borderwidth=0,
                focusthickness=0, padding=(14, 7), relief="flat")
    s.map("TButton",
          background=[("disabled", P["panel"]), ("pressed", P["sel"]), ("active", P["hover"])],
          foreground=[("disabled", P["muted"])])
    s.configure("Accent.TButton", background=P["accent"], foreground="#111318", font=(ui_font[0], ui_font[1]) + (() if retro_on else ("bold",)))
    s.map("Accent.TButton",
          background=[("disabled", P["line"]), ("pressed", P["accent"]), ("active", P["accent_hi"])],
          foreground=[("disabled", P["muted"])])
    s.configure("Pad.TButton", background=P["field"], foreground=P["text"], padding=(0, 0),
                font=(ui_font[0], 20) + (() if retro_on else ("bold",)), width=4)
    s.map("Pad.TButton", background=[("pressed", P["accent"]), ("active", P["hover"])],
          foreground=[("pressed", "#111318")])
    s.configure("PadOn.TButton", background=P["accent"], foreground="#111318", padding=(0, 0),
                font=(ui_font[0], 20) + (() if retro_on else ("bold",)), width=4)
    s.configure("Danger.TButton", background=P["field"], foreground=P["err"])
    s.map("Danger.TButton", background=[("active", "#3a2326"), ("disabled", P["panel"])],
          foreground=[("disabled", P["muted"])])

    s.configure("TEntry", fieldbackground=P["field"], foreground=P["text"], insertcolor=P["text"],
                bordercolor=P["line"], lightcolor=P["line"], darkcolor=P["line"], padding=6)
    s.map("TEntry", bordercolor=[("focus", P["accent"])], lightcolor=[("focus", P["accent"])],
          darkcolor=[("focus", P["accent"])],
          fieldbackground=[("disabled", P["panel"])], foreground=[("disabled", P["muted"])])

    s.configure("TCheckbutton", background=P["bg"], foreground=P["text"], indicatorcolor=P["field"],
                indicatorrelief="flat", padding=4)
    s.map("TCheckbutton", indicatorcolor=[("selected", P["accent"]), ("active", P["hover"])],
          background=[("active", P["bg"])])

    s.configure("TRadiobutton", background=P["panel"], foreground=P["text"], indicatorcolor=P["field"], padding=4)
    s.map("TRadiobutton", indicatorcolor=[("selected", P["accent"]), ("active", P["hover"])],
          background=[("active", P["panel"])])
    s.configure("Horizontal.TScale", background=P["panel"], troughcolor=P["field"], bordercolor=P["panel"],
                lightcolor=P["accent"], darkcolor=P["accent"], sliderlength=18)
    s.configure("TNotebook", background=P["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0),
                bordercolor=P["panel"], lightcolor=P["panel"], darkcolor=P["panel"])
    s.configure("TNotebook.Tab", background=P["bg"], foreground=P["muted"], padding=(18, 9),
                borderwidth=0, bordercolor=P["bg"], lightcolor=P["bg"], darkcolor=P["bg"],
                font=(ui_font[0], ui_font[1]) + (() if retro_on else ("bold",)))
    s.map("TNotebook.Tab",
          background=[("selected", P["panel"]), ("active", P["hover"])],
          foreground=[("selected", P["accent"]), ("active", P["text"])],
          bordercolor=[("selected", P["panel"]), ("active", P["hover"])],
          lightcolor=[("selected", P["panel"]), ("active", P["hover"])],
          darkcolor=[("selected", P["panel"]), ("active", P["hover"])],
          # clam expands *unselected* tabs by default, which makes the selected one look sunk;
          # pin both states so every tab sits on the same baseline.
          expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
          padding=[("selected", (18, 9)), ("!selected", (18, 9))])
    s.layout("TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children":
             [("Notebook.padding", {"side": "top", "sticky": "nswe", "children":
              [("Notebook.label", {"side": "top", "sticky": ""})]})]})])

    s.configure("Treeview", background=P["field"], fieldbackground=P["field"], foreground=P["text"],
                rowheight=28 if retro_on else 26, borderwidth=0, relief="flat")
    s.map("Treeview", background=[("selected", P["sel"])], foreground=[("selected", P["text"])])
    s.configure("Treeview.Heading", background=P["panel"], foreground=P["muted"], relief="flat",
                borderwidth=0, padding=(8, 6), anchor="center",
                font=(ui_font[0], ui_font[1] if retro_on else ui_font[1] - 1) + (() if retro_on else ("bold",)))
    s.map("Treeview.Heading", background=[("active", P["hover"])])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # drop the border

    s.configure("Horizontal.TProgressbar", background=P["accent"], troughcolor=P["field"],
                bordercolor=P["bg"], lightcolor=P["accent"], darkcolor=P["accent"], thickness=6)
    s.configure("Vertical.TScrollbar", background=P["line"], troughcolor=P["panel"], bordercolor=P["panel"],
                arrowcolor=P["muted"], relief="flat", width=10)
    s.map("Vertical.TScrollbar", background=[("active", P["muted"])])
    s.configure("Horizontal.TScrollbar", background=P["line"], troughcolor=P["panel"], bordercolor=P["panel"],
                arrowcolor=P["muted"], relief="flat", width=10)
    s.map("Horizontal.TScrollbar", background=[("active", P["muted"])])
    s.configure("TCombobox", fieldbackground=P["field"], background=P["field"], foreground=P["text"],
                arrowcolor=P["muted"], bordercolor=P["line"], lightcolor=P["line"], darkcolor=P["line"], padding=4)
    s.map("TCombobox", fieldbackground=[("readonly", P["field"])], foreground=[("readonly", P["text"])],
          selectbackground=[("readonly", P["field"])], selectforeground=[("readonly", P["text"])])
    root.option_add("*TCombobox*Listbox.background", P["field"])
    root.option_add("*TCombobox*Listbox.foreground", P["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", P["sel"])
    s.configure("TSeparator", background=P["line"])
    s.configure("TPanedwindow", background=P["bg"])
    return {"ui": ui_font, "mono": mono, "retro": retro_on}


def ask_string(root, tk, ttk, title: str, prompt: str, initial: str = "") -> Optional[str]:
    """Themed replacement for simpledialog.askstring (which uses unstyled tk widgets)."""
    P = PALETTE
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=P["panel"])
    win.transient(root)
    win.resizable(False, False)
    frm = ttk.Frame(win, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=prompt, wraplength=520, justify="left").pack(anchor="w")
    var = tk.StringVar(value=initial)
    ent = ttk.Entry(frm, textvariable=var, width=64)
    ent.pack(fill="x", pady=(10, 14))
    result = {"v": None}
    row = ttk.Frame(frm)
    row.pack(fill="x")

    def ok(_e=None):
        result["v"] = var.get().strip()
        win.destroy()

    def cancel(_e=None):
        win.destroy()
    ttk.Button(row, text="Cancel", command=cancel).pack(side="right")
    ttk.Button(row, text="OK", style="Accent.TButton", command=ok).pack(side="right", padx=(0, 8))
    win.bind("<Return>", ok)
    win.bind("<Escape>", cancel)
    # Minimising the main window hides this transient too; on macOS it doesn't always come
    # back with the parent (and the grab makes the app look frozen). Re-show it whenever the
    # main window is mapped or focused again.
    def restore(_e=None):
        try:
            if win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                ent.focus_set()
        except tk.TclError:
            pass
    root._dialog_restore = restore
    bind_ids = [root.bind("<Map>", restore, add="+"), root.bind("<FocusIn>", restore, add="+")]

    def _cleanup(_e=None):
        for seq, bid in zip(("<Map>", "<FocusIn>"), bind_ids):
            try:
                root.unbind(seq, bid)
            except tk.TclError:
                pass
        root._dialog_restore = None
    win.bind("<Destroy>", _cleanup, add="+")
    win.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - win.winfo_reqwidth()) // 2
    y = root.winfo_rooty() + (root.winfo_height() - win.winfo_reqheight()) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    ent.focus_set()
    win.grab_set()
    root.wait_window(win)
    return result["v"] or None


def run_gui(initial_zip: Optional[str] = None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    log_path = setup_file_log()
    cfg = load_config()
    added_defaults = merge_default_repos(cfg)
    if added_defaults:
        save_config(cfg)
    repos: List[RepoEntry] = [e for e in (RepoEntry.from_dict(d) for d in cfg["repos"]) if e]
    installed: Dict[str, dict] = {}        # name -> source marker (last device refresh)
    _inv = cfg.get("device_inventory") or {}
    installed.update(_inv.get("dashes") or {})   # last inventory seen on the device, so status shows offline
    installed_seen = {"when": _inv.get("when")}  # ISO time of that inventory; None once live
    device_rows: List[dict] = []
    config_rows: List[dict] = []

    set_process_app_name(APP_NAME)
    root = tk.Tk()
    root.title(f"{APP_NAME} v{APP_VERSION}")
    try:
        root.tk.call("tk", "appname", APP_NAME)
    except tk.TclError:
        pass
    root.minsize(960, 660)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(1380, int(sw * 0.88)), min(900, int(sh * 0.86))
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")   # fallback size if zoom fails

    def maximize():
        for attempt in (lambda: root.state("zoomed"), lambda: root.attributes("-zoomed", True)):
            try:
                attempt()
                return
            except tk.TclError:
                continue
        root.geometry(f"{sw}x{sh - 60}+0+0")   # last resort: fill the screen
    root.after(50, maximize)
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    fonts = apply_theme(root, tk, ttk, retro=bool(cfg.get("retro_font", False)))
    mono = fonts["mono"]
    P = PALETTE
    import tkinter.font as tkfont
    _cell_font = tkfont.Font(family=fonts["ui"][0], size=fonts["ui"][1])
    _head_font = (tkfont.Font(family=fonts["ui"][0], size=fonts["ui"][1]) if fonts["retro"]
                  else tkfont.Font(family=fonts["ui"][0], size=fonts["ui"][1] - 1, weight="bold"))

    def autosize(tree, min_w: int = 60, max_w: int = 900, pad: int = 26):
        """Fit every column to its widest header/cell so nothing is cut off. Columns stay
        user-resizable (no stretch), and the table scrolls horizontally if it must."""
        cols = tree["columns"]
        rows = [tree.item(i, "values") for i in tree.get_children()]
        for idx, c in enumerate(cols):
            widths = [_head_font.measure(tree.heading(c, "text"))]
            widths += [_cell_font.measure(str(v[idx])) for v in rows if idx < len(v)]
            tree.column(c, width=max(min_w, min(max_w, max(widths) + pad)), stretch=False)
        if cols:
            tree.column(cols[-1], stretch=True)   # soak up leftover width so the table fills its panel

    log_q: "queue.Queue[tuple]" = queue.Queue()
    state = {"pkgs": None, "busy": False}

    # ---------- shell ----------
    outer = ttk.Frame(root, padding=(16, 12, 16, 12), style="Bg.TFrame")
    outer.pack(fill="both", expand=True)
    head = ttk.Frame(outer, style="Bg.TFrame")
    head.pack(fill="x")
    _imgs = {}   # keep PhotoImage refs alive
    icon_png = asset_path("garw_genie_icon.png")
    if icon_png:
        try:
            _imgs["icon"] = tk.PhotoImage(file=str(icon_png))
            root.iconphoto(True, _imgs["icon"])                        # window / dock / taskbar icon
        except tk.TclError:
            pass
    wordmark = asset_path("garw_genie_header.png")
    if wordmark:
        try:
            _imgs["head"] = tk.PhotoImage(file=str(wordmark))
            ttk.Label(head, image=_imgs["head"], style="Bg.TLabel").pack(side="left", padx=(0, 12))
        except tk.TclError:
            wordmark = None
    if not wordmark:
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
    ttk.Label(head, text=f"v{APP_VERSION}", style="BgMuted.TLabel").pack(side="left", padx=(0, 0), pady=(9, 0))
    net_pill = ttk.Label(head, text="● Internet", style="Pill.TLabel")
    net_pill.pack(side="left", padx=(16, 6), pady=(6, 0))
    status_pill = ttk.Label(head, text=f"● Looking for GARW device at {HOST} …", style="Pill.TLabel")
    status_pill.pack(side="left", pady=(6, 0))
    head2 = ttk.Frame(outer, style="Bg.TFrame")
    head2.pack(fill="x", pady=(4, 0))
    cred = ttk.Frame(head2, style="Bg.TFrame")
    cred.pack(side="left", padx=(0, 0))
    ttk.Label(cred, text="SSH login", style="BgMuted.TLabel").pack(side="left", padx=(0, 6))
    user_var = tk.StringVar(value=cfg.get("ssh_user") or USERNAME)
    pass_var = tk.StringVar(value=cfg.get("ssh_password") if cfg.get("ssh_password") is not None else PASSWORD)
    user_ent = ttk.Entry(cred, textvariable=user_var, width=10)
    user_ent.pack(side="left")
    ttk.Label(cred, text="/", style="BgMuted.TLabel").pack(side="left", padx=4)
    pass_ent = ttk.Entry(cred, textvariable=pass_var, width=12, show="•")
    pass_ent.pack(side="left")
    show_pw = ttk.Button(cred, text="👁", width=2, takefocus=False,
                         command=lambda: pass_ent.configure(show="" if pass_ent.cget("show") else "•"))
    show_pw.pack(side="left", padx=(4, 0))

    def creds_changed(_e=None):
        u, pw = user_var.get().strip(), pass_var.get()
        if not u:
            user_var.set(cfg.get("ssh_user") or USERNAME)
            return
        if u == cfg.get("ssh_user") and pw == cfg.get("ssh_password"):
            return
        cfg["ssh_user"], cfg["ssh_password"] = u, pw
        apply_credentials(u, pw)
        try:
            save_config(cfg)
        except OSError:
            pass
        log(f"SSH login changed to user '{u}' — reconnecting …")
        mon["auth_failed"] = False
        if mon.get("unit"):
            mon["pending_unit"] = True
    for w in (user_ent, pass_ent):
        w.bind("<Return>", creds_changed)
        w.bind("<FocusOut>", creds_changed)

    wifi = ttk.Frame(head2, style="Bg.TFrame")
    wifi.pack(side="left", padx=(24, 0))
    ttk.Label(wifi, text="GARW Wi-Fi", style="BgMuted.TLabel").pack(side="left", padx=(0, 6))
    ssid_var = tk.StringVar(value=cfg.get("wifi_ssid") or TARGET_SSID)
    wpass_var = tk.StringVar(value=cfg.get("wifi_password") if cfg.get("wifi_password") is not None else WIFI_PASSWORD)
    ssid_ent = ttk.Entry(wifi, textvariable=ssid_var, width=10)
    ssid_ent.pack(side="left")
    ttk.Label(wifi, text="/", style="BgMuted.TLabel").pack(side="left", padx=4)
    wpass_ent = ttk.Entry(wifi, textvariable=wpass_var, width=12, show="•")
    wpass_ent.pack(side="left")
    show_wpw = ttk.Button(wifi, text="👁", width=2, takefocus=False,
                          command=lambda: wpass_ent.configure(show="" if wpass_ent.cget("show") else "•"))
    show_wpw.pack(side="left", padx=(4, 0))
    join_btn = ttk.Button(wifi, text="Join", width=5)
    join_btn.pack(side="left", padx=(6, 0))

    def wifi_changed(_e=None):
        ssid, pw = ssid_var.get().strip(), wpass_var.get()
        if not ssid:
            ssid_var.set(cfg.get("wifi_ssid") or TARGET_SSID)
            return
        if ssid == cfg.get("wifi_ssid") and pw == cfg.get("wifi_password"):
            return
        cfg["wifi_ssid"], cfg["wifi_password"] = ssid, pw
        apply_wifi(ssid, pw)
        try:
            save_config(cfg)
        except OSError:
            pass
        log(f"GARW Wi-Fi set to '{ssid}'.")
    for w in (ssid_ent, wpass_ent):
        w.bind("<Return>", wifi_changed)
        w.bind("<FocusOut>", wifi_changed)

    def do_join_wifi():
        wifi_changed()
        ssid, pw = ssid_var.get().strip(), wpass_var.get()

        def worker():
            log(f"Asking the OS to join Wi-Fi '{ssid}' …")
            ui(set_status, f"● Joining '{ssid}' …", "warn")
            ok, msg = join_wifi(ssid, pw, log=log)
            log(("Joined: " if ok else "Could not join automatically: ") + (msg or ssid))
            if ok:
                ui(set_status, f"● Joined '{ssid}' … waiting for the GARW device", "warn")
                return
            # Fall back: open the OS Wi-Fi picker with the password on the clipboard.
            def fallback(m=msg):
                try:
                    root.clipboard_clear()
                    root.clipboard_append(pw)
                    clip = f"The password ({pw}) is on your clipboard — paste it if asked."
                except tk.TclError:
                    clip = f"Password: {pw}"
                opened = open_wifi_settings()
                set_status(f"● Pick '{ssid}' in the Wi-Fi menu — the GARW device is detected automatically", "warn")
                messagebox.showinfo(
                    APP_NAME,
                    f"Your computer didn't join '{ssid}' by itself.\n\n"
                    + (f"Wi-Fi settings have been opened — choose '{ssid}' there.\n" if opened
                       else f"Choose '{ssid}' from the Wi-Fi menu.\n")
                    + f"{clip}\n\nThe app notices the GARW device on its own once you're connected.\n\n(Details: {m})",
                    parent=root)
            ui(fallback)
        start(worker)
    join_btn.configure(command=do_join_wifi)
    AFTER_CHOICES = ("Restart GARW Binary", "Reboot unit", "Do nothing")
    _after_default = {"restart": 0, "reboot": 1, "none": 2}.get(cfg.get("after_changes"),
                                                                  1 if cfg.get("reboot_after_upload") is False and "after_changes" not in cfg else 0)
    after_var = tk.StringVar(value=AFTER_CHOICES[_after_default])
    after_box = ttk.Combobox(head, textvariable=after_var, values=AFTER_CHOICES, state="readonly", width=17)
    after_box.pack(side="right", pady=(6, 0))
    ttk.Label(head, text="After changes:", style="BgMuted.TLabel").pack(side="right", padx=(0, 6), pady=(8, 0))

    def after_mode() -> str:
        return {"Restart GARW Binary": "restart", "Reboot unit": "reboot", "Do nothing": "none"}[after_var.get()]

    def apply_after(dev):
        """Post-change action for dash add/update/delete."""
        m = after_mode()
        if m == "restart":
            dev.restart_dash_app()
        elif m == "reboot":
            dev.reboot()


    retro_var = tk.BooleanVar(value=bool(cfg.get("retro_font", False)))

    def toggle_retro():
        want = bool(retro_var.get())
        if not messagebox.askyesno(APP_NAME, ("Switch to the 8-bit font" if want else "Switch back to the normal font")
                                   + "?\n\nThe app restarts to apply it.", parent=root):
            retro_var.set(not want)      # declined: put the checkbox back, change nothing
            return
        cfg["retro_font"] = want
        try:
            save_config(cfg)
        except OSError:
            pass
        root.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    ttk.Checkbutton(head, text="8-bit font", variable=retro_var, command=toggle_retro).pack(side="right", padx=(0, 14), pady=(6, 0))

    nb = ttk.Notebook(outer)   # packed after the bottom bar so the log keeps its height
    tab_repo = ttk.Frame(nb, padding=14)
    tab_dev = ttk.Frame(nb, padding=14)
    tab_cfg = ttk.Frame(nb, padding=14)
    tab_zip = ttk.Frame(nb, padding=14)
    tab_fw = ttk.Frame(nb, padding=14)
    tab_ctl = ttk.Frame(nb, padding=14)
    nb.add(tab_repo, text="GitHub repos")
    nb.add(tab_dev, text="Device Dashes")
    nb.add(tab_cfg, text="Dash Settings")
    nb.add(tab_zip, text="Upload .zip")
    nb.add(tab_fw, text="Firmware / System Info")
    nb.add(tab_ctl, text="Controller")

    bottom = ttk.Frame(outer, style="Bg.TFrame")
    bottom.pack(fill="x", side="bottom")
    prow_ = ttk.Frame(bottom, style="Bg.TFrame")
    prow_.pack(fill="x", pady=(0, 6))
    progress = ttk.Progressbar(prow_, mode="determinate")
    progress.pack(side="left", fill="x", expand=True)
    logs_btn = ttk.Button(prow_, text="Logs…", width=7, command=lambda: open_folder(LOG_DIR))
    logs_btn.pack(side="right", padx=(8, 0))
    log_wrap = ttk.Frame(bottom, style="Bg.TFrame")
    log_wrap.pack(fill="x")
    _sp = {"spacing1": 3, "spacing3": 3} if fonts["retro"] else {}
    log_box = tk.Text(log_wrap, height=10, wrap="word", state="disabled", font=mono,
                      bg=P["field"], fg=P["text"], insertbackground=P["text"],
                      relief="flat", bd=0, padx=10, pady=8, highlightthickness=0, **_sp)
    log_box.pack(side="left", fill="both", expand=True)
    log_sb = ttk.Scrollbar(log_wrap, command=log_box.yview)
    log_sb.pack(side="right", fill="y")
    log_box.configure(yscrollcommand=log_sb.set)
    log_box.tag_configure("err", foreground=P["err"])
    log_box.tag_configure("ok", foreground=P["ok"])
    log_box.tag_configure("warn", foreground=P["warn"])
    log_box.tag_configure("muted", foreground=P["muted"])
    nb.pack(fill="both", expand=True, pady=(12, 8))

    def set_status(text: str, tone: str = "muted"):
        colour = {"ok": P["ok"], "err": P["err"], "warn": P["warn"]}.get(tone, P["muted"])
        status_pill.configure(text=text, foreground=colour)

    def set_net(online: bool, ssid: Optional[str] = None):
        via = f"  ·  {ssid}" if (online and ssid and ssid != TARGET_SSID) else ""
        net_pill.configure(text=("● Internet" + via) if online else "● No internet",
                           foreground=P["ok"] if online else P["muted"])

    # ---------- connection monitor ----------
    # Polls Wi-Fi SSID, the unit and the internet in the background. When the
    # unit first becomes reachable (you joined GARW), every tab is refreshed from
    # it automatically; when the internet first appears and repos are tracked,
    # GitHub is checked and the cache filled.
    mon = {"unit": None, "net": None, "ssid": None, "tick": 0, "pending_unit": False, "pending_net": False}
    MONITOR_MS = 4000

    DEVICE_TABS = (tab_dev, tab_cfg, tab_zip, tab_fw, tab_ctl)

    def on_tab_click(e):
        try:
            idx = nb.index(f"@{e.x},{e.y}")
        except tk.TclError:
            return
        tab_id = nb.tabs()[idx]
        if str(nb.tab(tab_id, "state")) != "disabled":
            return
        name = nb.tab(tab_id, "text")
        if paramiko is None:
            show_tip(e.x_root, e.y_root + 18, PARAMIKO_HINT)
            return
        where = mon.get("ssid")
        on_wifi = f" (you're on '{where}')" if where and where != TARGET_SSID else ""
        show_tip(e.x_root, e.y_root + 18,
                 f"{name} needs the GARW device — join Wi-Fi '{TARGET_SSID}'{on_wifi}. Unlocks automatically.")
        blink(lock_hint)
        return "break"
    nb.bind("<Button-1>", on_tab_click, add="+")

    tip = {"win": None, "job": None}

    def show_tip(x: int, y: int, text: str, ms: int = 2600):
        hide_tip()
        w = tk.Toplevel(root)
        w.overrideredirect(True)
        try:
            w.attributes("-topmost", True)
        except tk.TclError:
            pass
        w.configure(bg=P["warn"])
        tk.Label(w, text=text, bg=P["warn"], fg="#111318", font=fonts["ui"], padx=10, pady=6,
                 wraplength=420, justify="left").pack()
        w.update_idletasks()
        x = min(x, root.winfo_screenwidth() - w.winfo_reqwidth() - 8)
        w.geometry(f"+{x}+{y}")
        tip["win"] = w
        tip["job"] = root.after(ms, hide_tip)

    def hide_tip():
        if tip["job"]:
            root.after_cancel(tip["job"])
            tip["job"] = None
        if tip["win"] is not None:
            try:
                tip["win"].destroy()
            except tk.TclError:
                pass
            tip["win"] = None

    def blink(widget, times: int = 6, ms: int = 180):
        styles = ["Err.TLabel", "Warn.TLabel"]

        def step(i=0):
            if i >= times or not widget.winfo_exists():
                widget.configure(style="Warn.TLabel")
                return
            widget.configure(style=styles[i % 2])
            root.after(ms, step, i + 1)
        step()

    PARAMIKO_HINT = ("The SSH library 'paramiko' is not installed, so nothing can talk to the GARW device. "
                     "Install it with:   pip install paramiko   — then restart GARW Genie.")

    def set_device_tabs(enabled: bool):
        if paramiko is None:
            enabled = False   # hard lock: without SSH no device feature can work, whatever the monitor says
        for t in DEVICE_TABS:
            nb.tab(t, state="normal" if enabled else "disabled")
        # Once the unit answers, the SSH login / GARW Wi-Fi / Join controls are greyed out —
        # changing them mid-session would only break a working connection.
        for w in (user_ent, pass_ent, show_pw, ssid_ent, wpass_ent, show_wpw, join_btn):
            w.configure(state="disabled" if enabled else "normal")
        if not enabled and nb.select() != str(tab_repo):
            nb.select(tab_repo)
        lock_hint.configure(text="" if enabled else PARAMIKO_HINT if paramiko is None else
                            f"The other tabs unlock automatically once the GARW device answers at {HOST} — join Wi-Fi '{TARGET_SSID}'.")

    def monitor_apply(ssid, unit, net):
        first = mon["unit"] is None
        if first or bool(unit) != bool(mon["unit"]):
            set_device_tabs(bool(unit))
            if not unit and mon["unit"]:   # just dropped off: what we hold is now "last seen"
                installed_seen["when"] = (cfg.get("device_inventory") or {}).get("when")
                ui(fill_repo_tree)
        unit_up = unit and not mon["unit"]
        net_up = net and not mon["net"]
        mon.update(ssid=ssid, unit=unit, net=net)
        set_net(net, ssid)
        if paramiko is None:
            set_status("● paramiko missing — run: pip install paramiko, then restart", "err")
            return
        if unit:
            if first or unit_up:
                set_status(f"● GARW live  ·  {HOST}", "ok")
        else:
            where = f"on '{ssid}'" if ssid and ssid != TARGET_SSID else (f"not on Wi-Fi '{TARGET_SSID}'" if not ssid else "unit not answering")
            set_status(f"● GARW offline  ·  {where}", "muted")
        if mon.get("auth_failed") and unit:
            return   # wrong login: wait for the user to change credentials rather than retrying every tick
        if unit_up or (first and unit):
            log(f"GARW device detected at {HOST}" + (f" (Wi-Fi '{ssid}')" if ssid else "") + " — refreshing all tabs …")
            mon["pending_unit"] = True
        if (net_up or (first and net)) and repos:
            every = float(cfg.get("auto_check_minutes", 30) or 0)
            stale = [r for r in repos if not r.checked_within(every)] if every > 0 else []
            if stale:
                mon["pending_net"] = True
            elif every > 0:
                log(f"Internet detected — repos were checked within the last {every:g} min, skipping the automatic GitHub check.")
        # run whatever is pending when the UI is free (one job at a time)
        if not state["busy"]:
            if mon["pending_unit"]:
                mon["pending_unit"] = False
                auto_refresh_all()
            elif mon["pending_net"]:
                mon["pending_net"] = False
                every = float(cfg.get("auto_check_minutes", 30) or 0)
                # Re-evaluate now: if the user pressed 'Check & download' while this was queued,
                # everything is fresh and the automatic check is redundant.
                if every > 0 and any(not r.checked_within(every) for r in repos):
                    log("Internet detected — checking GitHub for dash updates …")
                    check_repos(auto=True)

    def monitor_tick():
        mon["tick"] += 1
        want_net = mon["tick"] % 5 == 1  # internet every ~20 s, unit + SSID every tick

        def worker():
            ssid = current_ssid()
            unit = unit_reachable(timeout=1.5)
            if unit and (not ssid or "redacted" in ssid.lower()):
                ssid = TARGET_SSID   # the unit only exists on GARW, so that is where we are
            net = internet_reachable(1.5) if want_net or mon["net"] is None else mon["net"]
            log_q.put(("call", (monitor_apply, (ssid, unit, net))))
        threading.Thread(target=worker, daemon=True).start()
        root.after(MONITOR_MS, monitor_tick)

    def auto_refresh_all():
        def worker():
            try:
                with IC7Device(log, confirm) as dev:
                    version = dev.check_version()
                    refresh_installed(dev)
                    refresh_configs(dev)
                    info = dev.system_info()
                ui(fill_sys_tree, info)
                ui(set_status, f"● GARW live  ·  {HOST}  ·  firmware " + (f"v{version:g}" if version else "v5+"), "ok")
                log("All tabs refreshed from the unit.")
            except AuthFailed as e:
                mon["auth_failed"] = True
                log(f"ERROR: {e}")
                ui(set_status, f"● GARW found  ·  login failed for '{USERNAME}'", "err")
                ui(lambda m=str(e): messagebox.showerror(APP_NAME, m, parent=root))
                return
            except RuntimeError as e:
                # e.g. a v4 unit: still show what we can (system info works on any layout)
                log(f"GARW device reachable but: {e}")
                layout = None
                try:
                    with IC7Device(log, confirm) as dev:
                        layout = dev.detect_layout()
                        ui(fill_sys_tree, dev.system_info())
                except Exception:
                    pass
                if layout == "v4":
                    ui(set_status, f"● GARW live  ·  {HOST}  ·  v4 firmware — dashes need v5, update it on the Firmware tab", "warn")
                    log("Dash and settings features are unavailable on v4. The Firmware / System Info tab still works — "
                        "use 'Install firmware…' there with the v4→v5 package.")
                else:
                    ui(set_status, f"● GARW live  ·  {HOST}  ·  needs attention (see log)", "warn")
        start(worker)

    # ---------- helpers ----------
    def log(msg: str):
        log_q.put(("log", msg))
        lvl = logging.ERROR if msg.startswith(("ERROR", "Validation failed")) else \
              logging.WARNING if msg.startswith("WARNING") or "cancelled" in msg else logging.INFO
        FILE_LOG.log(lvl, msg)

    def set_progress(done, total):
        log_q.put(("progress", (done, total)))

    def ui(fn, *a):
        """Run fn(*a) on the Tk thread."""
        log_q.put(("call", (fn, a)))

    def pump():
        try:
            while True:
                kind, payload = log_q.get_nowait()
                if kind == "log":
                    tag = ("err" if payload.startswith(("ERROR", "Validation failed")) or " not reachable" in payload
                           else "warn" if payload.startswith(("WARNING", "  warning")) or "cancelled" in payload
                           else "ok" if payload.startswith(("Done", "Connected", "Uploaded", "All ", "Deleted", "Downloaded", "Installed", "Connection test OK"))
                           else "muted" if payload.startswith(("  [", "  note", "Checking", "Settings:", "=")) else "")
                    log_box.configure(state="normal")
                    log_box.insert("end", payload + "\n", tag)
                    log_box.see("end")
                    log_box.configure(state="disabled")
                elif kind == "progress":
                    done, total = payload
                    if state.get("marquee"):
                        progress.stop()
                        progress.configure(mode="determinate")
                        state["marquee"] = False
                    progress["maximum"] = max(total, 1)
                    progress["value"] = done
                elif kind == "call":
                    fn, a = payload
                    fn(*a)
                elif kind == "done":
                    state["busy"] = False
                    if state.get("marquee"):
                        progress.stop()
                        progress.configure(mode="determinate")
                        progress["value"] = 0
                        state["marquee"] = False
                    root.configure(cursor="")
                    set_buttons()
        except queue.Empty:
            pass
        root.after(60, pump)

    def confirm(title, message) -> bool:
        result = {}
        ev = threading.Event()

        def ask():
            result["ok"] = messagebox.askyesno(title, message, parent=root)
            ev.set()
        root.after(0, ask)
        ev.wait()
        return result["ok"]

    def start(worker, reset_progress=True):
        if state["busy"]:
            return
        state["busy"] = True
        if reset_progress:
            # Marquee until the job reports real progress, so a press is visibly "working"
            # even while it waits on DNS / SSH / GitHub.
            progress.configure(mode="indeterminate")
            progress.start(12)
            state["marquee"] = True
        root.configure(cursor="watch")
        set_buttons()

        def wrapped():
            try:
                worker()
            except UploadAborted as e:
                log(str(e))
            except Exception as e:
                msg = str(e)
                log(f"ERROR: {msg}")
                ui(lambda m=msg: messagebox.showerror(APP_NAME, m, parent=root))
            finally:
                log_q.put(("done", None))
        threading.Thread(target=wrapped, daemon=True).start()

    def persist():
        cfg["repos"] = [r.to_dict() for r in repos]
        cfg["after_changes"] = after_mode()
        try:
            save_config(cfg)
        except OSError as e:
            log(f"Could not save settings: {e}")

    def refresh_installed(dev: IC7Device):
        """Refresh the device inventory (called from worker threads)."""
        rows = dev.list_dashes()
        device_rows[:] = rows
        installed.clear()
        installed.update({r["name"]: r["source"] for r in rows})
        installed_seen["when"] = None
        cfg["device_inventory"] = {"when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "dashes": dict(installed)}
        try:
            save_config(cfg)
        except OSError:
            pass
        ui(fill_device_tree)
        ui(fill_repo_tree)
        return rows

    def known_online() -> bool:
        """Internet state from the monitor if it has one; a bounded probe otherwise."""
        return mon["net"] if mon["net"] is not None else internet_reachable(1.5)

    def gui_preflight() -> bool:
        ok = preflight(log, ssid=mon["ssid"] if mon["unit"] is not None else None)
        if not ok:
            ui(set_status, f"● GARW offline  ·  join Wi-Fi '{TARGET_SSID}'", "err")
        return ok

    def install_packages(pkgs: List[DashPackage], origin: str):
        """Shared install path for zip and GitHub sources."""
        warnings = [w for p in pkgs for w in p.warnings]
        if warnings and not confirm(APP_NAME, "There are validation warnings:\n\n"
                                    + "\n".join(warnings) + "\n\nInstall anyway?"):
            raise UploadAborted("Install cancelled.")
        log("=" * 60)
        log(f"Installing {len(pkgs)} dash(es) from {origin}: " + ", ".join(p.name for p in pkgs))
        if not gui_preflight():
            raise RuntimeError(f"GARW device not reachable at {HOST}. Join Wi-Fi '{TARGET_SSID}' first.")
        with IC7Device(log, confirm, set_progress) as dev:
            dev.check_version()
            dev.upload_all(pkgs)
            refresh_installed(dev)
            apply_after(dev)
        log("Done.")
        tail = {"restart": "\n\nThe GARW binary was restarted.", "reboot": "\n\nThe unit is rebooting.", "none": ""}[after_mode()]
        ui(lambda: messagebox.showinfo(
            APP_NAME, f"Installed to {LIBRARY_DIR}/:\n  " + "\n  ".join(p.name for p in pkgs) + tail, parent=root))

    # ---------- Tab 1: Upload .zip ----------
    ttk.Label(tab_zip, text="Dash .zip:").grid(row=0, column=0, sticky="w")
    zip_var = tk.StringVar(value=initial_zip or "")
    zip_entry = ttk.Entry(tab_zip, textvariable=zip_var)
    zip_entry.grid(row=0, column=1, sticky="ew", padx=6)
    browse_btn = ttk.Button(tab_zip, text="Browse…")
    browse_btn.grid(row=0, column=2)
    info_var = tk.StringVar(value="Select a .zip to validate it. A zip may hold one dash or many.")
    ttk.Label(tab_zip, textvariable=info_var, wraplength=760, justify="left").grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(8, 8))
    zrow = ttk.Frame(tab_zip)
    zrow.grid(row=2, column=0, columnspan=3, sticky="w")
    upload_btn = ttk.Button(zrow, text="Upload to GARW", state="disabled", style="Accent.TButton")
    upload_btn.pack(side="left")
    tab_zip.columnconfigure(1, weight=1)

    def validate(path: str):
        state["pkgs"] = None
        if not path:
            info_var.set("Select a .zip to validate it.")
            set_buttons()
            return
        try:
            pkgs = validate_zip(path)
        except ValidationError as e:
            info_var.set(f"✗ {e}")
            log(f"Validation failed: {e}")
            set_buttons()
            return
        state["pkgs"] = pkgs
        lines = [f"✓ {len(pkgs)} dash{'es' if len(pkgs) != 1 else ''} → {LIBRARY_DIR}/"]
        lines += [f"    {p.name}  ({len(p.files)} file{'s' if len(p.files) != 1 else ''})" for p in pkgs]
        warnings = [w for p in pkgs for w in p.warnings]
        lines += ["⚠ " + w for w in warnings]
        info_var.set("\n".join(lines))
        log(f"Validated {os.path.basename(path)}: " + ", ".join(f"{p.name} ({len(p.files)} files)" for p in pkgs))
        for n in (n for p in pkgs for n in p.notes):
            log(f"  note: {n}")
        for w in warnings:
            log(f"  warning: {w}")
        set_buttons()

    def browse():
        path = filedialog.askopenfilename(title="Select dash .zip",
                                          filetypes=[("Zip files", "*.zip"), ("All files", "*.*")])
        if path:
            zip_var.set(path)
            validate(path)

    def on_entry_change(*_):
        p = zip_var.get().strip().strip('"')
        if p and os.path.isfile(p):
            validate(p)

    def do_upload():
        pkgs = state["pkgs"]
        if pkgs:
            start(lambda: install_packages(pkgs, os.path.basename(pkgs[0].zip_path)))

    browse_btn.configure(command=browse)
    upload_btn.configure(command=do_upload)
    zip_entry.bind("<Return>", on_entry_change)
    zip_entry.bind("<FocusOut>", on_entry_change)

    # ---------- Tab 2: GitHub repos ----------
    rcols = ("repo", "branch", "dash", "latest", "cached", "status")
    repo_tree = ttk.Treeview(tab_repo, columns=rcols, show="headings", selectmode="extended", height=8)
    for c, txt, w in (("repo", "Repository", 180), ("branch", "Branch", 64), ("dash", "Dash", 96),
                      ("latest", "Latest commit", 290), ("cached", "Downloaded", 90), ("status", "Status", 230)):
        repo_tree.heading(c, text=txt)
        repo_tree.column(c, width=w, anchor="center", stretch=False)
    repo_tree.grid(row=0, column=0, columnspan=6, sticky="nsew")
    repo_sb = ttk.Scrollbar(tab_repo, command=repo_tree.yview)
    repo_sb.grid(row=0, column=6, sticky="ns")
    repo_hsb = ttk.Scrollbar(tab_repo, orient="horizontal", command=repo_tree.xview)
    repo_hsb.grid(row=1, column=0, columnspan=6, sticky="ew")
    repo_tree.configure(yscrollcommand=repo_sb.set, xscrollcommand=repo_hsb.set)

    rrow = ttk.Frame(tab_repo)
    rrow.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(8, 0))
    add_btn = ttk.Button(rrow, text="Add repo…")
    add_btn.pack(side="left")
    rm_btn = ttk.Button(rrow, text="Remove")
    rm_btn.pack(side="left", padx=(6, 0))
    token_btn = ttk.Button(rrow, text="GitHub token…")
    token_btn.pack(side="right")
    check_btn = ttk.Button(rrow, text="Check & download updates", style="Accent.TButton")
    check_btn.pack(side="left", padx=(18, 0))
    install_sel_btn = ttk.Button(rrow, text="Install / update selected")
    install_sel_btn.pack(side="left", padx=(6, 0))
    install_all_btn = ttk.Button(rrow, text="Install all updates")
    install_all_btn.pack(side="left", padx=(6, 0))
    _dr = next((p for p in default_repos_paths() if p.is_file()), None)
    repo_hint = ttk.Label(tab_repo, style="Muted.TLabel", wraplength=900, justify="left",
                          text="Step 1 (on internet): 'Check & download updates' asks GitHub for each repo's latest commit "
                               "and stores the dash in a local cache.   Step 2 (on GARW Wi-Fi, no internet): "
                               "'Install / update selected' pushes the cached dashes to the unit. "
                               "One dash per repo: <Name>.qml + <Name>.qml.png at the repo root.\n"
                               + (f"Default repos are read from {_dr} — edit that file to change the bundled list."
                                  if _dr else f"Put a {DEFAULT_REPOS_FILE} next to the app (one GitHub URL per line) to bundle default repos."))
    repo_hint.grid(row=3, column=0, columnspan=7, sticky="w", pady=(6, 0))
    lock_hint = ttk.Label(tab_repo, style="Warn.TLabel", wraplength=900, justify="left", text="")
    lock_hint.grid(row=4, column=0, columnspan=7, sticky="w", pady=(4, 0))
    tab_repo.rowconfigure(0, weight=1)
    tab_repo.columnconfigure(5, weight=1)

    def fill_repo_tree():
        sel = {repo_tree.item(i, "values")[0] for i in repo_tree.selection()}
        repo_tree.delete(*repo_tree.get_children())
        for r in repos:
            latest = f"{r.latest_sha[:7]}  {_fmt_date(r.latest_date)}  {r.latest_message or ''}" if r.latest_sha else "—"
            status = repo_status(r, installed)
            if not mon.get("unit") and installed and installed_seen["when"] and not status.startswith(("Error", "Not a v5", "Not checked")):
                status += f"  (device as of {_fmt_date(installed_seen['when'])})"
            csha = r.cached_sha()
            cached = ("✓ " + csha[:7]) if csha and csha == r.latest_sha else (csha[:7] + " (old)" if csha else "—")
            iid = repo_tree.insert("", "end", values=(r.label, r.branch or "(default)", r.dash or "—", latest, cached, status))
            if r.label in sel:
                repo_tree.selection_add(iid)
            tag = ("update" if status.startswith("Update") else
                   "ok" if status.startswith("Up to date") else "err" if status.startswith(("Error", "Not a v5")) else "")
            if tag:
                repo_tree.item(iid, tags=(tag,))
        repo_tree.tag_configure("update", foreground=P["warn"])
        repo_tree.tag_configure("ok", foreground=P["ok"])
        repo_tree.tag_configure("err", foreground=P["err"])
        autosize(repo_tree, max_w=520)

    def selected_repos() -> List[RepoEntry]:
        labels = {repo_tree.item(i, "values")[0] for i in repo_tree.selection()}
        return [r for r in repos if r.label in labels]

    def add_repo():
        url = ask_string(root, tk, ttk, "Add GitHub repo",
                         "Repository URL (e.g. https://github.com/owner/GTDash):")
        if not url:
            return
        try:
            entry = RepoEntry.from_url(url)
        except GitHubError as e:
            messagebox.showerror(APP_NAME, str(e), parent=root)
            return
        if any(r.label == entry.label for r in repos):
            messagebox.showinfo(APP_NAME, f"{entry.label} is already in the list.", parent=root)
            return

        def worker():
            """Only keep the repo if it really holds a v5 dash: look it up, download it and
            run the same validation an install would. Offline, add it unchecked."""
            token = cfg.get("github_token", "")
            online = known_online()
            ui(set_net, online)
            if not online:
                try:
                    cached = entry.validate_cache()
                except ValidationError as e:
                    msg = str(e)
                    log(f"  {entry.label} rejected (from cache): {msg.splitlines()[0]}")
                    ui(lambda: messagebox.showerror("Repo not added", f"{entry.label} was not added.\n\n{msg}", parent=root))
                    return
                repos.append(entry)
                ui(persist)
                ui(fill_repo_tree)
                log(f"No internet — {entry.label} added unchecked; it will be validated on the next 'Check & download'.")
                ui(lambda: messagebox.showwarning(
                    APP_NAME, f"{entry.label} was added but could NOT be validated — there is no internet.\n\n"
                    "It will be checked (and refused if it isn't a v5 dash) the next time you press "
                    "'Check & download updates' while online.", parent=root))
                return
            log(f"Checking {entry.label} …")
            try:
                c = entry.check(token)
                log(f"  {entry.label} [{entry.branch}]  {c['short']}  {_fmt_date(c['date'])}  {c['message']}")
                entry.download(token, log)          # validates the layout; raises ValidationError if not a dash
            except (GitHubError, ValidationError) as e:
                msg = str(e)
                log(f"  {entry.label} rejected: {msg.splitlines()[0]}")
                ui(lambda: messagebox.showerror(
                    "Repo not added",
                    f"{entry.label} was not added.\n\n{msg}\n\n"
                    "A v5 dash repo needs <Name>.qml and <Name>.qml.png at its root "
                    "(or one folder per dash laid out that way).", parent=root))
                return
            repos.append(entry)
            ui(persist)
            ui(fill_repo_tree)
            log(f"Added {entry.label} — dash '{entry.dash}' downloaded and ready.")
        start(worker)

    def set_token():
        cur = cfg.get("github_token", "")
        shown = (cur[:4] + "…" + cur[-4:]) if len(cur) > 8 else ("(set)" if cur else "(none)")
        val = ask_string(root, tk, ttk, "GitHub personal access token",
                         "Paste a GitHub token to raise the API limit from 60 to 5,000 requests/hour.\n"
                         "Public repos need no scopes — create a fine-grained token with no permissions at\n"
                         "github.com → Settings → Developer settings → Personal access tokens.\n"
                         f"Current: {shown}.  Leave empty and press OK to remove it.",
                         initial=cur)
        if val is None and cur:
            if messagebox.askyesno(APP_NAME, "Remove the stored GitHub token?", parent=root):
                cfg["github_token"] = ""
                persist()
                log("GitHub token removed.")
            return
        if val is None:
            return
        cfg["github_token"] = val.strip()
        persist()
        log("GitHub token saved (stored in config.json).")

    def remove_repos():
        sel = selected_repos()
        if not sel:
            return
        if messagebox.askyesno(APP_NAME, "Remove from the list (the dash stays on the device):\n\n  "
                               + "\n  ".join(r.label for r in sel), parent=root):
            for r in sel:
                repos.remove(r)
                if r.default:   # don't let default_repos.txt re-add it next launch
                    cfg.setdefault("removed_defaults", [])
                    if r.url not in cfg["removed_defaults"]:
                        cfg["removed_defaults"].append(r.url)
            persist()
            fill_repo_tree()

    def check_repos(targets: Optional[List[RepoEntry]] = None, auto: bool = False):
        targets = targets or list(repos)
        if not targets:
            log("No repos to check — use 'Add repo…' first.")
            return
        if not auto:
            mon["pending_net"] = False   # a manual check supersedes any queued automatic one

        def worker():
            log("Checking internet connection …")
            online = known_online()
            ui(set_net, online)
            if not online:
                cached = [r for r in targets if r.cached_sha()]
                log(f"No internet — cannot reach GitHub. {len(cached)}/{len(targets)} repo(s) have a cached "
                    "download and can still be installed from the cache.")
                where = mon.get("ssid")
                ui(lambda: show_tip(check_btn.winfo_rootx(), check_btn.winfo_rooty() - 44,
                                    f"Needs internet — you're on '{where or 'GARW'}' which has none. "
                                    f"{len(cached)}/{len(targets)} repo(s) are cached and can still be installed."))
                if unit_reachable(timeout=1.5):
                    with IC7Device(log, confirm) as dev:
                        dev.check_version()
                        refresh_installed(dev)
                return
            log(f"Checking {len(targets)} repo(s) on GitHub ...")
            token = cfg.get("github_token", "")
            for r in targets:
                try:
                    c = r.check(token)
                    log(f"  {r.label} [{r.branch}]  {c['short']}  {_fmt_date(c['date'])}  {c['message']}"
                        + ("   (unchanged — free re-check)" if c.get("unchanged") else ""))
                    if not r.is_downloaded():
                        r.download(token, log)
                    else:
                        r.validate_cache()
                except ValidationError as e:
                    r.error = "Not a v5 dash repo: " + str(e).splitlines()[0]
                    log(f"  {r.label}: {r.error}")
                except GitHubError as e:
                    r.error = str(e).splitlines()[0]
                    log(f"  {r.label}: {r.error}")
                ui(fill_repo_tree)
            ui(persist)
            ready = sum(1 for r in targets if r.is_downloaded())
            log(f"{ready}/{len(targets)} repo(s) downloaded and ready to install on the unit.")
            if RATE["remaining"] is not None:
                when = datetime.fromtimestamp(RATE["reset"]).strftime("%H:%M") if RATE["reset"] else "?"
                log(f"GitHub API budget: {RATE['remaining']}/{RATE['limit']} requests left this hour (resets {when}).")
            if not installed:
                if unit_reachable(timeout=1.5):
                    log("Reading installed dashes from the device ...")
                    with IC7Device(log, confirm) as dev:
                        dev.check_version()
                        refresh_installed(dev)
                else:
                    log("Device not reachable — showing GitHub status only. "
                        "Connect to GARW and press 'Check for updates' again to compare.")
            for r in targets:
                log(f"  {r.label}: {repo_status(r, installed)}")
        start(worker)

    def install_repos(targets: List[RepoEntry]):
        if not targets:
            return

        def worker():
            token = cfg.get("github_token", "")
            names = ", ".join(r.label for r in targets)
            log("=" * 60)
            log(f"Preparing install of {names} …")
            offline = not known_online()
            ui(set_net, not offline)
            log("No internet — installing from the local cache." if offline else "Internet available.")
            pkgs: List[DashPackage] = []
            for r in targets:
                pkgs.extend(r.fetch_packages(token, log, offline=offline))
            for n in (n for p in pkgs for n in p.notes):
                log(f"  note: {n}")
            ui(persist)
            ui(fill_repo_tree)
            install_packages(pkgs, "GitHub")
        start(worker)

    def install_selected():
        sel = selected_repos()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select one or more repos first.", parent=root)
            return
        install_repos(sel)

    def install_all_updates():
        todo = [r for r in repos if r.cached_sha() and not r.error
                and repo_status(r, installed).startswith(("Update", "Not installed"))]
        if not todo:
            log("Nothing to install — every downloaded repo is already up to date on the unit "
                "(run 'Check & download updates' while online first).")
            return
        install_repos(todo)

    add_btn.configure(command=add_repo)
    rm_btn.configure(command=remove_repos)
    token_btn.configure(command=set_token)
    check_btn.configure(command=lambda: check_repos())
    install_sel_btn.configure(command=install_selected)
    install_all_btn.configure(command=install_all_updates)
    repo_tree.bind("<Double-1>", lambda e: install_selected())

    # ---------- Tab 3: Device ----------
    dcols = ("name", "valid", "files", "source", "commit", "installed")
    dev_tree = ttk.Treeview(tab_dev, columns=dcols, show="headings", selectmode="extended", height=8)
    for c, txt, w in (("name", "Dash", 150), ("valid", "Valid", 90), ("files", "Files", 55),
                      ("source", "Source", 220), ("commit", "Commit", 80), ("installed", "Installed", 130)):
        dev_tree.heading(c, text=txt)
        dev_tree.column(c, width=w, anchor="center", stretch=False)
    dev_tree.grid(row=0, column=0, columnspan=6, sticky="nsew")
    dev_sb = ttk.Scrollbar(tab_dev, command=dev_tree.yview)
    dev_sb.grid(row=0, column=6, sticky="ns")
    dev_hsb = ttk.Scrollbar(tab_dev, orient="horizontal", command=dev_tree.xview)
    dev_hsb.grid(row=1, column=0, columnspan=6, sticky="ew")
    dev_tree.configure(yscrollcommand=dev_sb.set, xscrollcommand=dev_hsb.set)

    drow = ttk.Frame(tab_dev)
    drow.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(8, 0))
    refresh_btn = ttk.Button(drow, text="Refresh")
    refresh_btn.pack(side="left")
    delete_btn = ttk.Button(drow, text="Delete selected…", style="Danger.TButton")
    delete_btn.pack(side="left", padx=(6, 0))
    reboot_btn = ttk.Button(drow, text="Reboot device", style="Danger.TButton")
    reboot_btn.pack(side="right")
    restart_btn = ttk.Button(drow, text="Restart GARW Binary")
    restart_btn.pack(side="right", padx=(0, 6))
    dev_status = ttk.Label(tab_dev, style="Muted.TLabel", text="Press Refresh to read the library from the unit.")
    dev_status.grid(row=3, column=0, columnspan=7, sticky="w", pady=(6, 0))
    tab_dev.rowconfigure(0, weight=1)
    tab_dev.columnconfigure(5, weight=1)

    def fill_device_tree():
        dev_tree.delete(*dev_tree.get_children())
        for r in device_rows:
            s = r["source"] or {}
            valid = "✓" if (r["has_qml"] and r["has_png"]) else "✗ " + (
                "no .qml" if not r["has_qml"] else "no .png")
            src = f"{s.get('owner')}/{s.get('repo')} [{s.get('branch')}]" if s.get("repo") else (
                "manual upload" if not s else "?")
            iid = dev_tree.insert("", "end", values=(
                r["name"], valid, r["files"], src, (s.get("sha") or "")[:7], _fmt_date(s.get("installed"))))
            if not (r["has_qml"] and r["has_png"]):
                dev_tree.item(iid, tags=("bad",))
        dev_tree.tag_configure("bad", foreground=P["err"])
        autosize(dev_tree)
        dev_status.configure(text=f"{len(device_rows)} dash(es) in {LIBRARY_DIR}   ·   "
                                  f"read {datetime.now().strftime('%H:%M:%S')}")

    def do_refresh():
        def worker():
            if not gui_preflight():
                return
            with IC7Device(log, confirm) as dev:
                dev.check_version()
                rows = refresh_installed(dev)
            log(f"{len(rows)} dash(es) on device: " + (", ".join(r['name'] for r in rows) or "(none)"))
        start(worker)

    def do_delete():
        names = [dev_tree.item(i, "values")[0] for i in dev_tree.selection()]
        if not names:
            messagebox.showinfo(APP_NAME, "Select one or more dashes to delete.", parent=root)
            return

        def worker():
            if not gui_preflight():
                raise RuntimeError(f"GARW device not reachable at {HOST}.")
            with IC7Device(log, confirm) as dev:
                dev.check_version()
                removed = dev.delete_dashes(names)
                refresh_installed(dev)
                if removed:
                    apply_after(dev)
        start(worker)

    def do_reboot():
        if not messagebox.askyesno(APP_NAME, "Reboot the GARW device now?", parent=root):
            return

        def worker():
            if not gui_preflight():
                raise RuntimeError(f"GARW device not reachable at {HOST}.")
            with IC7Device(log, confirm) as dev:
                dev.reboot()
        start(worker)

    def do_restart_app():
        def worker():
            if not gui_preflight():
                raise RuntimeError(f"GARW device not reachable at {HOST}.")
            with IC7Device(log, confirm) as dev:
                dev.restart_dash_app()
        start(worker)

    refresh_btn.configure(command=do_refresh)
    delete_btn.configure(command=do_delete)
    reboot_btn.configure(command=do_reboot)
    restart_btn.configure(command=do_restart_app)

    # ---------- Tab 4: Settings files (/opt/IC7/screen_configs) ----------
    ttk.Label(tab_cfg, style="Muted.TLabel", wraplength=760, justify="left",
              text=f"Per-dash settings live in {SCREEN_CONFIGS_DIR} — each dash names its own file inside its "
                   ".qml. Download them to keep a backup or tweak by hand, then upload to restore.").grid(
        row=0, column=0, columnspan=7, sticky="w", pady=(0, 8))
    ccols = ("name", "dashes", "size", "mtime")
    cfg_tree = ttk.Treeview(tab_cfg, columns=ccols, show="headings", selectmode="extended", height=8)
    for c, txt, w in (("name", "Settings file", 200), ("dashes", "Used by dash", 160),
                      ("size", "Size", 70), ("mtime", "Modified on unit", 140)):
        cfg_tree.heading(c, text=txt)
        cfg_tree.column(c, width=w, anchor="center", stretch=False)
    cfg_tree.grid(row=1, column=0, columnspan=3, sticky="nsew")
    cfg_sb = ttk.Scrollbar(tab_cfg, command=cfg_tree.yview)
    cfg_sb.grid(row=1, column=3, sticky="ns")
    cfg_tree.configure(yscrollcommand=cfg_sb.set)

    edit_wrap = ttk.Frame(tab_cfg)
    edit_wrap.grid(row=1, column=4, columnspan=3, sticky="nsew", padx=(10, 0))
    gutter = tk.Text(edit_wrap, width=30, wrap="none", state="disabled", font=mono, takefocus=False,
                     bg=P["panel"], fg=P["muted"], relief="flat", bd=0, padx=8, pady=8, highlightthickness=0,
                     cursor="arrow", **_sp)
    gutter.pack(side="left", fill="y")
    gutter.tag_configure("idx", foreground=P["muted"])
    gutter.tag_configure("name", foreground=P["text"])
    gutter.tag_configure("cur", background=P["sel"])
    preview = tk.Text(edit_wrap, width=14, wrap="none", state="disabled", font=mono, undo=True,
                      bg=P["field"], fg=P["text"], insertbackground=P["accent"], relief="flat", bd=0,
                      padx=10, pady=8, highlightthickness=1, highlightbackground=P["line"],
                      highlightcolor=P["accent"], **_sp)
    preview.pack(side="left", fill="both", expand=True)
    preview.tag_configure("cur", background=P["sel"])
    edit_sb = ttk.Scrollbar(edit_wrap, command=lambda *a: (preview.yview(*a), gutter.yview(*a)))
    edit_sb.pack(side="right", fill="y")

    def _sync_scroll(*a):
        edit_sb.set(*a)
        gutter.yview_moveto(a[0])
    preview.configure(yscrollcommand=_sync_scroll)
    gutter.configure(yscrollcommand=lambda *a: preview.yview_moveto(a[0]))
    cfg_map: Dict[str, Dict[int, dict]] = {}        # dash name -> parsed line map (session cache)
    detail_lbl = ttk.Label(tab_cfg, style="Muted.TLabel", wraplength=420, justify="left",
                           text="Select a file: each line is shown with the dash property it feeds.")
    detail_lbl.grid(row=3, column=4, columnspan=3, sticky="w", padx=(10, 0), pady=(4, 0))
    prow = ttk.Frame(tab_cfg)
    prow.grid(row=2, column=4, columnspan=3, sticky="ew", padx=(10, 0), pady=(4, 0))
    preview_title = ttk.Label(prow, style="Muted.TLabel", text="Select a file to edit it")
    preview_title.pack(side="left")
    cfg_save_btn = ttk.Button(prow, text="Save to unit", style="Accent.TButton", state="disabled")
    cfg_save_btn.pack(side="right")
    cfg_revert_btn = ttk.Button(prow, text="Revert", state="disabled")
    cfg_revert_btn.pack(side="right", padx=(0, 6))
    edit_state = {"name": None, "orig": "", "dirty": False}

    crow = ttk.Frame(tab_cfg)
    crow.grid(row=4, column=0, columnspan=7, sticky="ew", pady=(10, 0))
    cfg_refresh_btn = ttk.Button(crow, text="Refresh")
    cfg_refresh_btn.pack(side="left")
    cfg_dl_btn = ttk.Button(crow, text="Download selected…")
    cfg_dl_btn.pack(side="left", padx=(6, 0))
    cfg_dl_all_btn = ttk.Button(crow, text="Download all…")
    cfg_dl_all_btn.pack(side="left", padx=(6, 0))
    cfg_up_btn = ttk.Button(crow, text="Upload files…", style="Accent.TButton")
    cfg_up_btn.pack(side="left", padx=(18, 0))
    cfg_status = ttk.Label(tab_cfg, style="Muted.TLabel", text="Press Refresh to read settings files from the unit.")
    cfg_status.grid(row=5, column=0, columnspan=7, sticky="w", pady=(6, 0))
    tab_cfg.rowconfigure(1, weight=1)
    tab_cfg.columnconfigure(0, weight=2)
    tab_cfg.columnconfigure(4, weight=3)

    def fill_cfg_tree():
        cfg_tree.delete(*cfg_tree.get_children())
        for r in config_rows:
            missing = r["size"] is None
            iid = cfg_tree.insert("", "end", values=(
                r["name"], ", ".join(r["dashes"]) or "—",
                "missing" if missing else f"{r['size']:,} B",
                "" if missing else datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")))
            if missing:
                cfg_tree.item(iid, tags=("missing",))
        cfg_tree.tag_configure("missing", foreground=P["muted"])
        autosize(cfg_tree)
        present = sum(1 for r in config_rows if r["size"] is not None)
        cfg_status.configure(text=f"{present} settings file(s) in {SCREEN_CONFIGS_DIR}   ·   "
                                  f"read {datetime.now().strftime('%H:%M:%S')}")

    def update_edit_buttons():
        dirty = edit_state["dirty"] and edit_state["name"] and not state["busy"]
        cfg_save_btn.configure(state="normal" if dirty else "disabled")
        cfg_revert_btn.configure(state="normal" if dirty else "disabled")
        if edit_state["name"]:
            body = preview.get("1.0", "end-1c")
            lines = len(body.split("\n")) - (1 if body.endswith("\n") else 0)
            preview_title.configure(
                text=f"{edit_state['name']}  ·  {lines} value(s)" + ("  ·  edited" if edit_state["dirty"] else ""),
                style="Warn.TLabel" if edit_state["dirty"] else "Muted.TLabel")

    def current_map() -> Dict[int, dict]:
        dash = edit_state.get("dash")
        return cfg_map.get(dash, {}) if dash else {}

    def render_gutter():
        cmap = current_map()
        n = int(preview.index("end-1c").split(".")[0])
        gutter.configure(state="normal")
        gutter.delete("1.0", "end")
        for i in range(n):
            info = cmap.get(i)
            label = info["name"] if info else ("" if not cmap else "?")
            gutter.insert("end", f"{i:>2}  ", "idx")
            gutter.insert("end", f"{label[:24]}\n", "name")
        gutter.configure(state="disabled")
        highlight_current_line()

    def highlight_current_line(_e=None):
        if not edit_state["name"]:
            return
        line = int(preview.index("insert").split(".")[0])
        for w in (preview, gutter):
            w.tag_remove("cur", "1.0", "end")
            w.tag_add("cur", f"{line}.0", f"{line}.0 lineend+1c")
        cmap = current_map()
        info = cmap.get(line - 1)
        value = preview.get(f"{line}.0", f"{line}.0 lineend").strip()
        if info:
            typ = f" ({info['type']})" if info["type"] else ""
            dflt = f"   default {info['default']}" if info["default"] else ""
            detail_lbl.configure(text=f"line {line - 1}  →  root.{info['name']}{typ}{dflt}   ·   current value: {value or '(empty)'}")
        elif cmap:
            detail_lbl.configure(text=f"line {line - 1}: not read by {edit_state.get('dash')}.qml (reserved / unused)")
        else:
            detail_lbl.configure(text=f"line {line - 1}   ·   value: {value or '(empty)'}   "
                                      "(no dash on the unit reads this file, so lines can't be named)")

    def show_preview(name: str, text: str, dash: Optional[str] = None):
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("end", text)
        preview.edit_reset()
        preview.edit_modified(False)
        edit_state.update(name=name, orig=text, dirty=False, dash=dash)
        render_gutter()
        preview.mark_set("insert", "1.0")
        highlight_current_line()
        update_edit_buttons()

    def on_preview_modified(_evt=None):
        if not preview.edit_modified():
            return
        preview.edit_modified(False)
        if edit_state["name"]:
            edit_state["dirty"] = preview.get("1.0", "end-1c") != edit_state["orig"]
            update_edit_buttons()
            render_gutter()

    def discard_edits_ok() -> bool:
        if not edit_state["dirty"]:
            return True
        return messagebox.askyesno(APP_NAME, f"Discard unsaved changes to {edit_state['name']}?", parent=root)

    def reload_dash_settings():
        """No reboot needed for settings: the dash re-reads its file when the screen changes.
        Flip to the next screen and back with the controller (Right, then Left)."""
        log("Reloading settings on the dash: Right, wait 2 s, Left …")
        time.sleep(0.5)          # let the file settle on the unit after the save
        ctl.tap("right")
        time.sleep(SETTINGS_FLIP_S)   # let the next screen come up fully
        ctl.tap("left")
        log("Dash returned to the screen with the new settings.")

    def do_cfg_save():
        name = edit_state["name"]
        if not name or not edit_state["dirty"]:
            return
        text = preview.get("1.0", "end-1c")
        if edit_state["orig"].endswith("\n") and not text.endswith("\n"):
            text += "\n"  # keep the file's trailing newline; the IC7 reads one value per line

        def worker():
            if not gui_preflight():
                raise RuntimeError(f"GARW device not reachable at {HOST}.")
            with IC7Device(log, confirm, set_progress) as dev:
                dev.check_version()
                dev.write_config(name, text)
                refresh_configs(dev)
            edit_state.update(orig=text, dirty=False)
            ui(update_edit_buttons)
            reload_dash_settings()
            log("Done.")
        start(worker)

    def do_cfg_revert():
        if edit_state["name"]:
            show_preview(edit_state["name"], edit_state["orig"], edit_state.get("dash"))

    def selected_cfg_names() -> List[str]:
        return [cfg_tree.item(i, "values")[0] for i in cfg_tree.selection()
                if cfg_tree.item(i, "values")[2] != "missing"]

    def refresh_configs(dev: IC7Device):
        rows = dev.list_configs()
        config_rows[:] = rows
        ui(fill_cfg_tree)
        return rows

    def do_cfg_refresh():
        def worker():
            if not gui_preflight():
                return
            with IC7Device(log, confirm) as dev:
                dev.check_version()
                rows = refresh_configs(dev)
            log(f"{sum(1 for r in rows if r['size'] is not None)} settings file(s) in {SCREEN_CONFIGS_DIR}")
        start(worker)

    def on_cfg_select(_evt=None):
        names = selected_cfg_names()
        if len(names) != 1 or state["busy"]:
            return
        name = names[0]
        if name == edit_state["name"]:
            return
        if not discard_edits_ok():
            return

        row = next((r for r in config_rows if r["name"] == name), None)
        dash = row["dashes"][0] if row and row["dashes"] else None

        def worker():
            with IC7Device(log, confirm) as dev:
                text = dev.read_config(name)
                if dash and dash not in cfg_map:
                    try:
                        cfg_map[dash] = parse_config_map(dev.read_dash_qml(dash))
                        log(f"  {dash}.qml: {len(cfg_map[dash])} settings lines mapped to properties")
                    except Exception as e:  # annotation is best-effort
                        cfg_map[dash] = {}
                        log(f"  could not read {dash}.qml for annotations: {e}")
            ui(show_preview, name, text, dash)
        # Lightweight: no preflight chatter, no progress reset.
        start(worker, reset_progress=False)

    def do_cfg_download(all_files: bool):
        names = [r["name"] for r in config_rows if r["size"] is not None] if all_files else selected_cfg_names()
        if not names:
            messagebox.showinfo(APP_NAME, "Nothing to download — refresh first, then select files.", parent=root)
            return
        folder = filedialog.askdirectory(title="Save settings files to…", parent=root)
        if not folder:
            return
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        dest = os.path.join(folder, f"IC7_screen_configs_{stamp}") if all_files else folder

        def worker():
            if not gui_preflight():
                raise RuntimeError(f"GARW device not reachable at {HOST}.")
            with IC7Device(log, confirm, set_progress) as dev:
                saved = dev.download_configs(names, dest)
            ui(lambda: messagebox.showinfo(APP_NAME, f"Saved {len(saved)} file(s) to\n{dest}", parent=root))
        start(worker)

    def do_cfg_upload():
        paths = filedialog.askopenfilenames(title="Settings file(s) to upload",
                                            filetypes=[("Settings files", "*.txt *.cfg *.json *.ini"), ("All files", "*.*")])
        if not paths:
            return

        def worker():
            if not gui_preflight():
                raise RuntimeError(f"GARW device not reachable at {HOST}.")
            with IC7Device(log, confirm, set_progress) as dev:
                dev.check_version()
                sent = dev.upload_configs(list(paths))
                refresh_configs(dev)
            if sent:
                reload_dash_settings()
            log("Done.")
        start(worker)

    cfg_refresh_btn.configure(command=do_cfg_refresh)
    cfg_dl_btn.configure(command=lambda: do_cfg_download(False))
    cfg_dl_all_btn.configure(command=lambda: do_cfg_download(True))
    cfg_up_btn.configure(command=do_cfg_upload)
    cfg_tree.bind("<<TreeviewSelect>>", on_cfg_select)
    preview.bind("<<Modified>>", on_preview_modified)
    for ev in ("<KeyRelease>", "<ButtonRelease-1>"):
        preview.bind(ev, highlight_current_line, add="+")
    gutter.bind("<Button-1>", lambda e: (preview.mark_set("insert", f"{gutter.index(f'@{e.x},{e.y}').split('.')[0]}.0"),
                                         preview.focus_set(), highlight_current_line(), "break")[-1])
    preview.bind("<Command-s>", lambda e: (do_cfg_save(), "break")[1])
    preview.bind("<Control-s>", lambda e: (do_cfg_save(), "break")[1])
    cfg_save_btn.configure(command=do_cfg_save)
    cfg_revert_btn.configure(command=do_cfg_revert)

    # ---------- Tab 5: Firmware + system info ----------
    ttk.Label(tab_fw, text="System information", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
    sys_tree = ttk.Treeview(tab_fw, columns=("k", "v"), show="", selectmode="none", height=12)
    sys_tree.column("k", width=150, anchor="w", stretch=False)
    sys_tree.column("v", width=420, anchor="w", stretch=True)
    sys_tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
    sys_sb = ttk.Scrollbar(tab_fw, command=sys_tree.yview)
    sys_sb.grid(row=1, column=0, sticky="nse", pady=(6, 0))
    sys_tree.configure(yscrollcommand=sys_sb.set)
    sys_btn = ttk.Button(tab_fw, text="Read system info")
    sys_btn.grid(row=2, column=0, sticky="w", pady=(10, 0))

    fw_box = ttk.Frame(tab_fw, padding=(16, 0, 0, 0))
    fw_box.grid(row=0, column=1, rowspan=3, sticky="nsew")
    ttk.Label(fw_box, text="Firmware update", style="Muted.TLabel").pack(anchor="w")
    ttk.Label(fw_box, style="Muted.TLabel", wraplength=300, justify="left",
              text=f"Takes a GARW firmware package — a .zip holding <name>.tar.cpt (or the .tar.cpt itself). "
                   f"It is uploaded to {FIRMWARE_SCRATCH} on the unit, decrypted there with ccrypt, unpacked, "
                   "and its own 'run' installer is executed. The unit then reboots. "
                   "Works on v4 units (this is how you get to v5).").pack(anchor="w", pady=(6, 10))
    fw_var = tk.StringVar()
    fw_row = ttk.Frame(fw_box)
    fw_row.pack(fill="x")
    fw_entry = ttk.Entry(fw_row, textvariable=fw_var)
    fw_entry.pack(side="left", fill="x", expand=True)
    fw_browse_btn = ttk.Button(fw_row, text="Browse…")
    fw_browse_btn.pack(side="left", padx=(6, 0))
    fw_info = ttk.Label(fw_box, style="Muted.TLabel", wraplength=300, justify="left",
                        text="Select a firmware package.")
    fw_info.pack(anchor="w", pady=(8, 10))
    fw_step = ttk.Label(fw_box, text="", style="Warn.TLabel")
    fw_step.pack(anchor="w")
    fw_btn_row = ttk.Frame(fw_box)
    fw_btn_row.pack(fill="x", pady=(10, 0))
    fw_install_btn = ttk.Button(fw_btn_row, text="Install firmware…", style="Danger.TButton", state="disabled")
    fw_install_btn.pack(side="left")
    tab_fw.rowconfigure(1, weight=1)
    tab_fw.columnconfigure(0, weight=3)
    tab_fw.columnconfigure(1, weight=2)

    def fill_sys_tree(info: List[Tuple[str, str]]):
        sys_tree.delete(*sys_tree.get_children())
        for k, v in info:
            sys_tree.insert("", "end", values=(k, v))

    def do_sysinfo():
        def worker():
            if not gui_preflight():
                return
            with IC7Device(log, confirm) as dev:
                info = dev.system_info()
            ui(fill_sys_tree, info)
            for k, v in info[:4]:
                log(f"  {k}: {v}")
            log("System info read.")
        start(worker)

    def inspect_firmware(path: str):
        fw_install_btn.configure(state="disabled")
        state["fw"] = None
        if not path or not os.path.isfile(path):
            fw_info.configure(text="Select a firmware package.")
            return
        try:
            size = os.path.getsize(path)
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    cpts = [n for n in zf.namelist() if n.lower().endswith(".tar.cpt") and not n.startswith("__MACOSX")]
                if len(cpts) != 1:
                    raise ValueError(f"expected one .tar.cpt inside, found {len(cpts)}")
                desc = f"✓ {os.path.basename(path)}  ({size / 1024 / 1024:.1f} MB)\n    contains {cpts[0]}"
            elif path.lower().endswith(".tar.cpt"):
                desc = f"✓ {os.path.basename(path)}  ({size / 1024 / 1024:.1f} MB, encrypted archive)"
            else:
                raise ValueError("not a .zip or .tar.cpt")
            local = IC7Device._local_ccrypt()
            desc += "\n    decrypt: on the unit" + (f" (local fallback: {local})" if local else " (no local ccrypt — unit must have it)")
            fw_info.configure(text=desc)
            state["fw"] = path
            fw_install_btn.configure(state="normal" if not state["busy"] else "disabled")
        except (ValueError, zipfile.BadZipFile) as e:
            fw_info.configure(text=f"✗ {os.path.basename(path)}: {e}")

    def fw_browse():
        path = filedialog.askopenfilename(title="Select firmware package",
                                          filetypes=[("Firmware", "*.zip *.cpt"), ("All files", "*.*")])
        if path:
            fw_var.set(path)
            inspect_firmware(path)

    def do_install_firmware():
        path = state.get("fw")
        if not path:
            return
        if not messagebox.askyesno(
                "Install firmware?",
                f"This will replace the GARW firmware on the unit using\n{os.path.basename(path)}\n\n"
                "Do NOT power off the cluster while it runs (a few minutes).\n"
                "Dashes in /opt/IC7/library are kept; stock settings files may be reset.\n\nContinue?",
                icon="warning", parent=root):
            return
        steps = {"upload": "① Uploading package…", "unpack": "② Decrypting and unpacking on the unit…",
                 "install": "③ Running installer — do not power off…", "verify": "④ Verifying…"}

        def worker():
            if not gui_preflight():
                raise RuntimeError(f"GARW device not reachable at {HOST}.")
            log("=" * 60)
            log(f"FIRMWARE UPDATE from {os.path.basename(path)}")
            try:
                with IC7Device(log, confirm, set_progress) as dev:
                    result = dev.install_firmware(path, on_step=lambda st: ui(fw_step.configure, {"text": steps.get(st, st)}))
                    if result["after"] == "both" and confirm(
                            "Remove old v4 folder?",
                            f"The unit now has both {BINARY_PATH} and the legacy {LEGACY_DIR}.\n"
                            "The stock installer leaves the old folder behind, but the tool treats "
                            "'both present' as an unexpected state.\n\nRemove /opt/Garw_IC7 now?"):
                        dev.remove_legacy_dir()
                    info = dev.system_info()
                    ui(fill_sys_tree, info)
                    dev.reboot()
                ui(fw_step.configure, {"text": ""})
                v = f"v{result['display']}" if result.get("display") else "v5+"

                def done():
                    fw_var.set("")            # clear the package path so it can't be run twice by accident
                    inspect_firmware("")
                    messagebox.showinfo(APP_NAME, f"Firmware updated to {v}.\nThe unit is rebooting.", parent=root)
                ui(done)
            except Exception:
                ui(lambda: fw_step.configure(text="✗ Update failed — see log. Do not power off; retry."))
                raise
        start(worker)

    fw_browse_btn.configure(command=fw_browse)
    fw_install_btn.configure(command=do_install_firmware)
    sys_btn.configure(command=do_sysinfo)
    fw_entry.bind("<Return>", lambda e: inspect_firmware(fw_var.get().strip().strip('"')))
    fw_entry.bind("<FocusOut>", lambda e: inspect_firmware(fw_var.get().strip().strip('"')))

    # ---------- Tab 6: Controller ----------
    ctl_log = {"last": None, "count": 0}

    def on_ctl_send(name, data):
        ctl_log["last"] = name
        ctl_log["count"] += 1
    ctl = Controller(on_send=on_ctl_send)
    ctl_state = {"active": False, "release_job": {}}

    ttk.Label(tab_ctl, style="Muted.TLabel", wraplength=420, justify="left",
              text=f"Drives the dash over Wi-Fi exactly like the old phone app: UDP to {HOST}:{CONTROL_PORT}. "
                   "Click the pad or use the keyboard while this tab is showing — arrow keys move, "
                   "S opens GARW settings, L / R hold Left / Right for 3 s to enter the OS main settings, "
                   "Esc releases everything.").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

    pad = ttk.Frame(tab_ctl)
    pad.grid(row=1, column=0, sticky="n", padx=(20, 40))
    pad_btns = {}
    for name, glyph, r, c in (("up", "▲", 0, 1), ("left", "◀", 1, 0), ("right", "▶", 1, 2), ("down", "▼", 2, 1)):
        b = ttk.Button(pad, text=glyph, style="Pad.TButton", takefocus=False)
        b.grid(row=r, column=c, padx=4, pady=4, ipadx=14, ipady=14)
        pad_btns[name] = b
    centre = ttk.Label(pad, text="GARW", style="Muted.TLabel", anchor="center")
    centre.grid(row=1, column=1)

    seq = ttk.Frame(tab_ctl)
    seq.grid(row=2, column=0, sticky="n", padx=(20, 40), pady=(14, 0))
    s_btn = ttk.Button(seq, text="S  ·  GARW settings", takefocus=False)
    s_btn.pack(fill="x", pady=2)
    holdl_btn = ttk.Button(seq, text=f"L  ·  hold ◀ {OS_SETTINGS_HOLD_S:g} s  (OS settings)", takefocus=False)
    holdl_btn.pack(fill="x", pady=2)
    holdr_btn = ttk.Button(seq, text=f"R  ·  hold ▶ {OS_SETTINGS_HOLD_S:g} s  (OS settings)", takefocus=False)
    holdr_btn.pack(fill="x", pady=2)
    cancel_btn = ttk.Button(seq, text="Esc  ·  release", style="Danger.TButton", takefocus=False)
    cancel_btn.pack(fill="x", pady=(8, 2))

    opts = ttk.Frame(tab_ctl)
    opts.grid(row=1, column=1, rowspan=2, sticky="nw")
    ctl_active = tk.BooleanVar(value=False)
    ttk.Checkbutton(opts, text="Controller active (sends heartbeat)", variable=ctl_active).pack(anchor="w")
    ttk.Label(opts, style="Muted.TLabel", text="While a key is held:").pack(anchor="w", pady=(12, 2))
    mode_var = tk.StringVar(value="repeat")
    ttk.Radiobutton(opts, text="Repeat taps  — values step quickly", variable=mode_var, value="repeat").pack(anchor="w")
    ttk.Radiobutton(opts, text="Hold  — one long press", variable=mode_var, value="hold").pack(anchor="w")
    rate_row = ttk.Frame(opts)
    rate_row.pack(anchor="w", pady=(10, 0), fill="x")
    rate_lbl = ttk.Label(rate_row, style="Muted.TLabel", text="Repeat rate: 5 taps/s")
    rate_lbl.pack(anchor="w")
    rate_var = tk.DoubleVar(value=5.0)
    ttk.Scale(rate_row, from_=2, to=20, variable=rate_var, orient="horizontal", length=220).pack(anchor="w")
    readout = ttk.Label(opts, style="Muted.TLabel", text="idle", font=mono)
    readout.pack(anchor="w", pady=(18, 0))
    ttk.Label(opts, style="Muted.TLabel", wraplength=300, justify="left",
              text="The unit keeps the last button state until the next packet, so the tool sends "
                   "'no button' every 200 ms while idle — a lost release can never leave a key stuck.").pack(
        anchor="w", pady=(14, 0))

    def set_pad_visual(name: Optional[str]):
        for n, b in pad_btns.items():
            b.configure(style="PadOn.TButton" if n == name else "Pad.TButton")

    def ctl_press(name):
        if not ctl_active.get():
            ctl_active.set(True)
        job = ctl_state["release_job"].pop(name, None)
        if job:
            root.after_cancel(job)
        if ctl.held != name:
            ctl.press(name)
        set_pad_visual(name)

    def ctl_release(name):
        # Debounce: key auto-repeat on X11/Windows emits Release+Press pairs; wait a moment
        # for a follow-up press before really releasing.
        def do():
            ctl_state["release_job"].pop(name, None)
            ctl.release(name)
            set_pad_visual(None)
        job = ctl_state["release_job"].pop(name, None)
        if job:
            root.after_cancel(job)
        ctl_state["release_job"][name] = root.after(70, do)

    def ctl_tap(name):
        ctl_active.set(True)
        ctl.tap(name)

    def ctl_hold(name, seconds, then=None):
        ctl_active.set(True)
        ctl.hold(name, seconds, then)
        set_pad_visual(name)
        log(f"Controller: holding {name} for {seconds:g} s" + (f", then {then}" if then else ""))

    def ctl_cancel():
        ctl.cancel()
        set_pad_visual(None)

    for name, b in pad_btns.items():
        b.bind("<ButtonPress-1>", lambda e, n=name: (ctl_press(n), "break")[1])
        b.bind("<ButtonRelease-1>", lambda e, n=name: (ctl_release(n), "break")[1])
    s_btn.configure(command=lambda: (ctl_active.set(True), ctl.settings_sequence(), set_pad_visual("left"),
                                     log(f"Controller: GARW settings sequence (Left {SETTINGS_HOLD_S:g} s, then Right)")))
    holdl_btn.configure(command=lambda: ctl_hold("left", OS_SETTINGS_HOLD_S))
    holdr_btn.configure(command=lambda: ctl_hold("right", OS_SETTINGS_HOLD_S))
    cancel_btn.configure(command=ctl_cancel)

    KEYMAP = {"Up": "up", "Down": "down", "Left": "left", "Right": "right"}

    def keys_active() -> bool:
        if not ctl_state["active"]:
            return False
        w = root.focus_get()
        return not isinstance(w, (tk.Text, tk.Entry, ttk.Entry))

    def on_key_press(e):
        if not keys_active():
            return
        if e.keysym in KEYMAP:
            ctl_press(KEYMAP[e.keysym])
            return "break"
        k = e.keysym.lower()
        if k == "s":
            s_btn.invoke()
        elif k == "l":
            holdl_btn.invoke()
        elif k == "r":
            holdr_btn.invoke()
        elif k == "escape":
            ctl_cancel()
        else:
            return
        return "break"

    def on_key_release(e):
        if keys_active() and e.keysym in KEYMAP:
            ctl_release(KEYMAP[e.keysym])
            return "break"

    root.bind_all("<KeyPress>", on_key_press, add="+")
    root.bind_all("<KeyRelease>", on_key_release, add="+")

    def on_tab_changed(_e=None):
        active = nb.select() == str(tab_ctl)
        ctl_state["active"] = active
        if active:
            ctl_active.set(True)
            tab_ctl.focus_set()
        else:
            ctl_active.set(False)
            ctl_cancel()
    nb.bind("<<NotebookTabChanged>>", on_tab_changed, add="+")

    def ctl_tick():
        ctl.enabled = bool(ctl_active.get())
        ctl.mode = mode_var.get()
        ctl.rate = float(rate_var.get())
        rate_lbl.configure(text=f"Repeat rate: {ctl.rate:.0f} taps/s")
        rem = ctl.holding_for()
        if rem:
            txt = f"holding {ctl.hold_button}  {rem:4.1f} s left"
        elif ctl.held:
            txt = f"{ctl.held} ({'repeat' if ctl.mode == 'repeat' else 'hold'})"
        elif not ctl.enabled:
            txt = "controller off"
        else:
            txt = "idle · heartbeat"
        readout.configure(text=f"{txt}\nlast: {ctl_log['last'] or '—'}   packets: {ctl_log['count']}")
        if not rem and not ctl.held and ctl.hold_button is None:
            # timed hold finished: clear the pad highlight
            for n, b in pad_btns.items():
                if str(b.cget("style")) == "PadOn.TButton":
                    set_pad_visual(None)
                    break
        root.after(100, ctl_tick)
    ctl_tick()

    # ---------- wiring ----------
    all_buttons = [browse_btn, add_btn, rm_btn, token_btn, join_btn, check_btn, install_sel_btn,
                   install_all_btn, refresh_btn, delete_btn, reboot_btn, restart_btn,
                   cfg_refresh_btn, cfg_dl_btn, cfg_dl_all_btn, cfg_up_btn, sys_btn, fw_browse_btn]

    def set_buttons():
        busy = state["busy"]
        for b in all_buttons:
            b.configure(state="disabled" if busy else "normal")
        if mon.get("unit"):
            join_btn.configure(state="disabled")   # greyed while the GARW device is connected
        upload_btn.configure(state="normal" if (state["pkgs"] and not busy) else "disabled")
        fw_install_btn.configure(state="normal" if (state.get("fw") and not busy) else "disabled")
        update_edit_buttons()

    after_box.bind("<<ComboboxSelected>>", lambda e: persist(), add="+")
    if paramiko is None:
        log("ERROR: paramiko is not installed — every device tab stays locked. Install it with:  pip install paramiko")
        root.after(600, lambda: messagebox.showerror(
            APP_NAME, "GARW Genie needs the Python package 'paramiko' to talk to the GARW device over SSH, "
                      "and it isn't installed.\n\nOpen a terminal and run:\n\n    pip install paramiko\n\n"
                      "then start GARW Genie again. Until then only the GitHub repos tab works.", parent=root))
    log(f"Settings: {CONFIG_PATH}")
    log(f"Log file: {log_path}  (every action and every SSH command; 30 days kept — 'Logs…' button opens the folder)"
        if log_path else "Log file could not be created.")
    if cfg.get("retro_font") and not fonts["retro"]:
        log("8-bit font requested but Press Start 2P could not be loaded — using the system font.")
    if added_defaults:
        log(f"Added {len(added_defaults)} default repo(s) from {DEFAULT_REPOS_FILE}: " + ", ".join(added_defaults))
    if repos:
        log(f"Tracking {len(repos)} GitHub repo(s): " + ", ".join(r.label for r in repos))
    dr = next((p for p in default_repos_paths() if p.is_file()), None)
    log(f"Default repo list: {dr}" if dr else
        f"No {DEFAULT_REPOS_FILE} found (put one next to the app to bundle default dashes).")
    for r in repos:                     # rules tighten over time; don't let an old cache hide a bad repo
        try:
            r.validate_cache()
        except ValidationError as e:
            r.error = "Not a v5 dash repo: " + str(e).splitlines()[0]
            log(f"{r.label}: {r.error}")
    fill_repo_tree()
    if initial_zip:
        validate(initial_zip)
    pump()
    set_buttons()
    set_device_tabs(False)
    root.after(300, monitor_tick)
    try:
        root.mainloop()
    finally:
        ctl.close()


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def run_cli(args) -> int:
    def log(m):
        print(m, flush=True)
        FILE_LOG.info(m)

    def confirm(title, message):
        if args.yes:
            return True
        return input(f"{message}\n[y/N] ").strip().lower() in ("y", "yes")

    setup_file_log()
    cfg = load_config()
    if merge_default_repos(cfg):
        save_config(cfg)
    token = cfg.get("github_token", "")
    do_reboot = not args.no_reboot

    try:
        cfg_action = (args.list_configs or args.download_configs or args.upload_configs or args.sysinfo
                      or args.install_firmware or args.restart_app)
        if args.reboot and not (args.zip or args.install_repo or args.delete or args.list or cfg_action):
            if not preflight(log):
                return 3
            with IC7Device(log, confirm) as dev:
                dev.reboot()
            return 0

        if args.check_repos:
            repos = [e for e in (RepoEntry.from_dict(d) for d in cfg["repos"]) if e]
            if not repos:
                log("No repos tracked. Add one with --add-repo URL (or in the GUI).")
                return 0
            installed = {}
            if unit_reachable(timeout=1.5):
                with IC7Device(log, confirm) as dev:
                    dev.check_version()
                    installed = {r["name"]: r["source"] for r in dev.list_dashes()}
            for r in repos:
                try:
                    c = r.check(token)
                    if not r.is_downloaded():
                        r.download(token, log)
                    log(f"{r.label} [{r.branch}] {c['short']} {_fmt_date(c['date'])}  ->  {repo_status(r, installed)}")
                except (GitHubError, ValidationError) as e:
                    log(f"{r.label}: {e}")
            cfg["repos"] = [r.to_dict() for r in repos]
            save_config(cfg)
            return 0

        if args.add_repo:
            entry = RepoEntry.from_url(args.add_repo)
            if any(d.get("url") == entry.url for d in cfg["repos"]):
                log(f"{entry.label} already tracked.")
            else:
                entry.check(token)
                cfg["repos"].append(entry.to_dict())
                save_config(cfg)
                log(f"Added {entry.label} [{entry.branch}] @ {entry.latest_sha[:7]}")
            return 0

        pkgs: List[DashPackage] = []
        if args.zip:
            pkgs = validate_zip(args.zip)
        if args.install_repo:
            for url in args.install_repo:
                entry = RepoEntry.from_url(url)
                known = next((d for d in cfg["repos"] if d.get("url") == entry.url), None)
                if known:
                    entry = RepoEntry.from_dict(known)
                pkgs.extend(entry.fetch_packages(token, log, offline=not internet_reachable(2.0)))
                # remember it
                if not any(d.get("url") == entry.url for d in cfg["repos"]):
                    cfg["repos"].append(entry.to_dict())
                    save_config(cfg)
        if pkgs:
            log(f"Validated {len(pkgs)} dash(es): " + ", ".join(f"{p.name} ({len(p.files)} files)" for p in pkgs))
            for p in pkgs:
                for n in p.notes:
                    log(f"  note: {n}")
                for w in p.warnings:
                    log(f"  warning: {w}")

        if not (pkgs or args.delete or args.list or cfg_action):
            log("Nothing to do.")
            return 0
        if not preflight(log):
            return 3
        with IC7Device(log, confirm) as dev:
            if args.install_firmware:
                if not confirm("Install firmware?", f"Apply {args.install_firmware} to the unit at {HOST}? "
                               "Do not power off during the update."):
                    raise UploadAborted("Firmware update cancelled.")
                result = dev.install_firmware(args.install_firmware)
                if result["after"] == "both" and confirm(
                        "Remove legacy folder?", f"Remove the old {LEGACY_DIR} left behind by the installer?"):
                    dev.remove_legacy_dir()
                if do_reboot:
                    dev.reboot()
                log("Done.")
                return 0
            if args.sysinfo:
                for k, v in dev.system_info():
                    log(f"  {k:<18} {v}")
                if not (pkgs or args.delete or args.list or args.list_configs
                        or args.download_configs or args.upload_configs):
                    return 0
            dev.check_version()
            if args.list:
                for r in dev.list_dashes():
                    s = r["source"] or {}
                    src = f"{s.get('owner')}/{s.get('repo')}@{(s.get('sha') or '')[:7]}" if s.get("repo") else "manual"
                    ok = "ok " if (r["has_qml"] and r["has_png"]) else "BAD"
                    log(f"  {ok} {r['name']:<20} {r['files']:>4} files   {src}")
            if args.list_configs:
                for r in dev.list_configs():
                    size = "missing" if r["size"] is None else f"{r['size']:,} B"
                    log(f"  {r['name']:<28} {size:>10}   used by: {', '.join(r['dashes']) or '-'}")
            if args.download_configs:
                names = [r["name"] for r in dev.list_configs() if r["size"] is not None]
                dev.download_configs(names, args.download_configs)
            sent = []
            if args.upload_configs:
                sent = dev.upload_configs(args.upload_configs)
                if sent:   # no reboot needed: the dash re-reads settings on a screen change
                    log("Reloading settings on the dash: Right, wait, Left …")
                    c = Controller()
                    c.tap("right"); time.sleep(SETTINGS_FLIP_S); c.tap("left"); c.close()
            if pkgs:
                dev.upload_all(pkgs)
            if args.delete:
                dev.delete_dashes(args.delete)
            if args.restart_app:
                dev.restart_dash_app()
            elif (pkgs or args.delete) and do_reboot:
                if args.reboot_after:
                    dev.reboot()
                else:
                    dev.restart_dash_app()
    except ValidationError as e:
        log(f"Validation failed: {e}")
        return 2
    except UploadAborted as e:
        log(str(e))
        return 4
    except (GitHubError, RuntimeError) as e:
        log(f"ERROR: {e}")
        return 1
    log("Done.")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="garw_genie", description=f"{APP_NAME} v{APP_VERSION}")
    ap.add_argument("zip", nargs="?", help="dash .zip to upload (optional; pre-fills the GUI)")
    ap.add_argument("--cli", action="store_true", help="run without a window")
    ap.add_argument("--no-reboot", action="store_true", help="do nothing after dash changes (default: restart the dash app)")
    ap.add_argument("-y", "--yes", action="store_true", help="CLI: answer yes to confirmations")
    g = ap.add_argument_group("CLI actions")
    g.add_argument("--list", action="store_true", help="list dashes installed on the device")
    g.add_argument("--delete", metavar="NAME", nargs="+", help="delete dash(es) from the device")
    g.add_argument("--reboot", action="store_true", help="reboot the device")
    g.add_argument("--add-repo", metavar="URL", help="track a GitHub dash repo")
    g.add_argument("--check-repos", action="store_true", help="check tracked repos for updates and cache the downloads")
    g.add_argument("--install-repo", metavar="URL", nargs="+", help="download dash repo(s) and install")
    g.add_argument("--list-configs", action="store_true", help=f"list settings files in {SCREEN_CONFIGS_DIR}")
    g.add_argument("--download-configs", metavar="DIR", help="save all settings files into DIR")
    g.add_argument("--upload-configs", metavar="FILE", nargs="+", help=f"copy local file(s) into {SCREEN_CONFIGS_DIR}")
    g.add_argument("--sysinfo", action="store_true", help="print OS / CPU / RAM / storage info from the unit")
    g.add_argument("--install-firmware", metavar="FILE", help="apply a firmware package (.zip with .tar.cpt, or .tar.cpt)")
    g.add_argument("--restart-app", action="store_true", help="restart the GARW binary (no OS reboot)")
    g.add_argument("--reboot-after", action="store_true", help="after dash changes reboot the unit instead of restarting the app")
    args = ap.parse_args(argv)

    cli_action = any([args.list, args.delete, args.reboot, args.add_repo, args.check_repos, args.install_repo,
                      args.list_configs, args.download_configs, args.upload_configs, args.sysinfo,
                      args.install_firmware, args.restart_app])
    if args.cli or cli_action:
        if args.cli and not (args.zip or cli_action):
            ap.error("--cli needs a zip path or an action (--list, --reboot, --install-repo …)")
        return run_cli(args)
    run_gui(args.zip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
