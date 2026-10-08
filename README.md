# GARW Genie

![GARW Genie](assets/garw_genie_logo_dark.png)

GARW Genie is a desktop app for your GARW cluster. It puts dashes on the device, keeps them up to
date from GitHub, lets you edit each dash's settings, installs firmware, and doubles as a Wi-Fi
controller — all from your laptop, over the device's own Wi-Fi.

## Getting it running

Download the build for your machine (`GARW Genie.app` on Mac, `GARW Genie.exe` on Windows) and open
it. The first launch shows a security warning because the app isn't signed with a developer certificate:
on Mac, right-click → Open (and Open again in the dialog), or if macOS calls a downloaded copy "damaged",
open Terminal and run `xattr -dr com.apple.quarantine "/path/to/GARW Genie.app"`; on Windows, More info →
Run anyway.

If you'd rather run it from source, you need Python 3.8 or newer:

```
pip install -r requirements.txt
python garw_genie.py
```

Don't skip the first line: without the `paramiko` package the app can't talk to the device at all, so
it opens with every device tab locked and tells you to install it.

## Your first session

When the window opens, most of it is greyed out. That's expected — the device isn't connected yet.
The two pills at the top tell you where you stand: one for the internet, one for the GARW device.

GARW Genie works in two steps, because the device's Wi-Fi has no internet:

**1. While you still have internet** — go to the **GitHub repos** tab. It already lists a few
dashes. Press **Check & download updates** and the latest version of each one is saved on your
laptop. Want another dash? **Add repo…** and paste its GitHub URL; the app checks that it really is
a GARW dash before accepting it.

**2. Join the GARW Wi-Fi** — press **Join** in the header (the network name and password are already
filled in), or pick "GARW" from your system Wi-Fi menu. Recent versions of macOS sometimes refuse to
let apps switch networks; if that happens, Join opens the Wi-Fi settings for you with the password on
your clipboard, and the app carries on the moment you pick the network by hand. A few seconds later the device pill turns
green, every tab unlocks, and each one fills itself in. From here on nothing needs the internet.

Once connected, the login and Wi-Fi boxes in the header grey out — there's nothing to change there
while things are working.

## The tabs

**GitHub repos** — the dashes you're tracking, with their status against what's on the device.
Select one and its preview image appears on the right (once it's been downloaded), so you can see
what a dash looks like before putting it on the device. The Device Dashes tab shows the same preview
for whatever is installed. Statuses are:
*Not installed*, *Up to date*, or *Update ready*. Select one and press **Install / update selected**
(or just double-click it). **Install all updates** does the lot. Right-click a row to copy its URL or
commit, open it on GitHub, or check, install or remove just that repo.

**Device Dashes** — what's on the device right now, where each came from, and when. The list is in the
device's own screen order — dash folders sorted by name, then the encrypted add-ons sorted by name, both case-insensitive (so `LapTimer` sits before `LFA`, as Qt lists them) — which is the order the screen indices follow, and the **Active**
column shows each dash's screen index plus, for the ones the cluster actually cycles through, their slot position, and a line under
the table lists all the active slots including the built-in screens. Encrypted GARW add-ons such as the LapTimer
(`LapTimer.enc` beside the dash folders) appear in the list too, marked *✓ encrypted*, and can be made active like
any dash; they just have no preview and can't be backed up as a zip. **Active screens…** lets you change
what's in each slot (the number of slots is what the device has now, up to six) and restarts the GARW
binary so it takes effect. **Install from
.zip…** is the manual route for a dash you didn't get from GitHub: pick a zip holding one or more
dashes (a dash is a folder called `Name` containing `Name.qml` and `Name.qml.png`; anything else in the
folder comes along), check the list it shows you, and confirm. **Download selected…** and
**Backup all…** copy dashes off the device into exactly that zip layout, so a backup goes straight
back on with Install from .zip (the app checks it passes before reporting success). **Delete selected…**
removes dashes. **Restart GARW Binary** and **Reboot device** are here too.

**Dash Settings** — every dash has a small settings file (redline, warning temperatures, units, and
so on). Pick a file, and the editor shows each value with the name of the setting it controls next
to it, plus the default for the line you're on. Change what you like and press **Save to unit** —
the dash picks the new values up immediately, no reboot. **Backup all to .zip…** saves every settings file
in one archive and **Restore from .zip…** puts them back (after a confirmation, then the dash reloads
them); you can also download or upload individual files.

**Boot & Logo Screens** — three optional files that personalise the device. The **ignition-on
welcome** is either a **boot logo** (`bootlogo.png`) or a **welcome video** (`welcome.mp4`) — the device shows one or the
other, and the **ignition-off
screen** (`logo.png`) is shown when the ignition goes off. For the two images you can pick any picture —
JPEG, PNG, BMP, WebP, whatever size — and the app converts it to a PNG and fits it to the device's
800×480 screen — letterboxed on black by default, or switch the dropdown to *Fill & crop* or *Stretch*
if you'd rather fill the screen — showing you the result before you upload. The welcome video is handled the
same way: the device only plays H.264 at 800×480 (a VP9 or HEVC file just logs "No decoder available"),
so any video you pick is checked — codec, size, length — and re-encoded to exactly that, silent, and squeezed
under the 2 MB limit; a file that's already right is sent untouched. Everything is uploaded under the name the device expects and the GARW binary is restarted
so it picks the file up. It also shows what's on the device already: **View current** shows an image in
the preview, and **Save current…** copies any of the three files back to your computer. One thing to
remember: uploading a file doesn't switch it on. On the device, open Main OS settings (hold L or R
about 2 seconds — the Controller tab's L/R buttons do it), go to **Startup**, and pick it under
*Ignition on welcome* or *Ignition off screen*; the app reminds you after every upload. This tab works
on v4 devices too — the files go to `/opt/Garw_IC7` there instead of `/opt/IC7`.

**Firmware / System Info** — **Read system info** shows firmware version, OS, CPU, memory, storage,
temperature and more. **Install firmware…** takes an official GARW update package (the `.zip` you
would otherwise put on a USB stick) and installs it the same way the stick would. The package is
encrypted; the key to open it is read from the GARW software already on the device, so there's nothing
to type and nothing stored in the app. This is also how a
v4 device gets to v5. When it finishes, the device's scratch area is tidied up and the package
path is cleared so it can't be run twice by accident.

**Controller** — the old phone app's D-pad. Click the arrows or use your keyboard: arrow keys move
(hold ◀ or ▶ for a couple of seconds to open the device's own menu; ▲/▼ repeat so values step quickly),
**S** opens the dash's settings screen, **L** and **R** hold left or right to reach the device's own
menu, **Esc** lets go of everything. It only listens while this tab is showing.

## The header

- **After changes** decides what happens after you install, delete or update a dash: restart the
  dash software (default, a couple of seconds), reboot the whole device, or nothing.
- **8-bit font** switches the app to a pixel font, just for fun. Off by default.
- **SSH login** and **GARW Wi-Fi** are pre-filled with the factory values (`root` / `root` and
  `GARW` / `garwicxX`). Only change them if your device is set up differently.
- **Logs…** (next to the progress bar) opens the folder with a full record of everything the app has
  done, handy if something goes wrong.

## Good to know

- The device tabs lock themselves whenever the device isn't answering and unlock the moment it's
  back — there's no connect button to press.
- Need to look around the device tabs without a device? Click the GARW Genie logo four times quickly
  to unlock them (and four more to lock again). Anything that actually talks to the device still
  needs it connected.
- The repo list remembers what it last saw on the device, so statuses still make sense while you're
  away from GARW — they're marked *device as of <time>* until the device is checked again.
- The default dash list comes from `default_repos.txt`. A copy is built into the app, but one placed
  next to the app (or in `~/.garw_genie/`) takes precedence — add a GitHub URL per line to change what
  new installs start with. Repos you've already got are remembered in your settings either way.
- GitHub lets you check about 60 times an hour without signing in, which is plenty. If you ever hit
  the limit, **GitHub token…** on the repos tab takes a personal access token and raises it to 5,000.
- Dashes still in the old v4 layout (a `Name_main.qml` file) are refused with a clear message until
  they've been updated for v5.
- A device still on v4 firmware connects fine, but only the Firmware / System Info, Boot & Logo
  Screens and Controller tabs do anything useful on it — the status pill tells you so. Install the v4→v5 package there
  and the rest unlocks after the reboot.

---

### For developers

The app is `garw_genie.py` plus `laptimer.py` (the TrackList/UserTracks formats and the local web server behind the
map editor; Leaflet is bundled under `assets/leaflet/`, BSD-2); the dependencies are `paramiko` (SSH), `certifi`
(CA bundle, so HTTPS to GitHub verifies inside the frozen app — `SSL_CERT_FILE` overrides it behind a
corporate proxy) `pillow` (image conversion for the Boot & Logo Screens tab and smooth preview scaling;
without it images must already be exact-size PNGs) and `imageio-ffmpeg` (a bundled ffmpeg for the welcome
video; an ffmpeg on PATH is used first). The firmware version
comes from `/opt/IC7/version.txt` (firmware 5.5+); older v5 units fall back to the float literal in the binary.  Settings, the
dash cache and logs live in `~/.garw_genie/`. Dashes installed from GitHub carry a hidden
`.garw_source.json` on the device so any laptop sees the same install state. Uploads go to a staging
folder and are renamed into place, so a dropped connection never leaves a half-written dash.

A command-line mode covers the same ground (`python garw_genie.py --help`): `--list`, `--delete`,
`--add-repo`, `--check-repos`, `--install-repo`, `--list-configs`, `--download-configs`,
`--upload-configs`, `--sysinfo`, `--install-firmware`, `--restart-app`, `--reboot`.

`build.sh` (Mac) and `build.bat` (Windows) make the standalone apps with PyInstaller;
`.github/workflows/build.yml` builds both on every push and attaches them to a release on tags.
The logo and icons are generated by `assets/make_logo.py` from the bundled Press Start 2P font
(SIL OFL, see `assets/PressStart2P-LICENSE.txt`).

### Action Shots
<img width="1715" height="1013" alt="Screenshot 2026-09-30 at 23 19 32" src="https://github.com/user-attachments/assets/8410158d-ca21-4e7d-9f83-d17fbb8d4a3a" />

