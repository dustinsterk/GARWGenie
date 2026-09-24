# GARW Genie

![GARW Genie](assets/garw_genie_logo_dark.png)

GARW Genie is a desktop app for your GARW cluster. It puts dashes on the device, keeps them up to
date from GitHub, lets you edit each dash's settings, installs firmware, and doubles as a Wi-Fi
controller — all from your laptop, over the device's own Wi-Fi.

## Getting it running

Download the build for your machine (`GARW Genie.app` on Mac, `GARW Genie.exe` on Windows) and open
it. The first launch shows a security warning because the app isn't signed: on Mac, right-click →
Open; on Windows, More info → Run anyway.

If you'd rather run it from source, you need Python 3.8 or newer:

```
pip install -r requirements.txt
python garw_genie.py
```

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

**GitHub repos** — the dashes you're tracking, with their status against what's on the device:
*Not installed*, *Up to date*, or *Update ready*. Select one and press **Install / update selected**
(or just double-click it). **Install all updates** does the lot.

**Device Dashes** — what's on the device right now, where each came from, and when. Select and
**Delete selected…** to remove dashes. **Restart GARW Binary** and **Reboot device** are here too.

**Dash Settings** — every dash has a small settings file (redline, warning temperatures, units, and
so on). Pick a file, and the editor shows each value with the name of the setting it controls next
to it, plus the default for the line you're on. Change what you like and press **Save to unit** —
the dash picks the new values up immediately, no reboot. You can also download the files as a
backup or upload ones you edited elsewhere.

**Upload .zip** — the manual route. Point it at a zip containing one or more dashes and press
upload. A dash is a folder holding `Name.qml` and `Name.qml.png` (the folder must be called `Name`
too); anything else in the folder comes along for the ride.

**Firmware / System Info** — **Read system info** shows firmware version, OS, CPU, memory, storage,
temperature and more. **Install firmware…** takes an official GARW update package (the `.zip` you
would otherwise put on a USB stick) and installs it the same way the stick would. This is also how a
v4 device gets to v5.

**Controller** — the old phone app's D-pad. Click the arrows or use your keyboard: arrow keys move,
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
- The default dash list comes from `default_repos.txt`, a plain text file next to the app. Add a
  GitHub URL per line to change what new installs start with.
- GitHub lets you check about 60 times an hour without signing in, which is plenty. If you ever hit
  the limit, **GitHub token…** on the repos tab takes a personal access token and raises it to 5,000.
- Dashes still in the old v4 layout (a `Name_main.qml` file) are refused with a clear message until
  they've been updated for v5.

---

### For developers

Everything is one Python file, `garw_genie.py`; the only dependency is `paramiko`. Settings, the
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
