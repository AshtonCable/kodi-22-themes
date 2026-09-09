# kodi-22-themes

Custom skins for **Kodi 22 "Piers"**, built for a remote control at ten feet and
tuned to stay smooth on low-power boxes (Fire TV Stick, Raspberry Pi) as well as
desktop.

| Skin | Add-on id | Status |
|---|---|---|
| **Cable TV** — ten-foot launcher: rows of artwork cards, focused row pinned to the top | `skin.cable.tv` | in progress |
| **Cable Retro** — retro console dashboard, switchable between two console styles | `skin.cable.retro` | planned |
| **Cable Stream** — hero billboard and poster carousels | `skin.cable.stream` | planned |

All three are derivative works of **Estuary**, Kodi's default skin. See
[`NOTICE.md`](NOTICE.md) for the licence and attribution chain.

## Install

Download a zip from `dist/` (or build one with `bash tools/package.sh`), then on
the device: **Settings → Add-ons → Install from zip file**. You will need
*Settings → System → Add-ons → Unknown sources* enabled first.

Paths, if you would rather copy the directory in directly:

| Platform | Skin directory |
|---|---|
| Linux / Raspberry Pi OS | `~/.kodi/addons/` |
| LibreELEC | `/storage/.kodi/addons/` |
| Windows | `%APPDATA%\Kodi\addons\` |
| Android / Fire TV | `/sdcard/Android/data/org.xbmc.kodi/files/.kodi/addons/` |

Fire TV, fastest route:

```bash
adb connect <device-ip>:5555
adb push skin.cable.tv /sdcard/Android/data/org.xbmc.kodi/files/.kodi/addons/
adb shell am force-stop org.xbmc.kodi && adb shell monkey -p org.xbmc.kodi 1
```

## Low-power devices

On a Fire TV Stick or a Raspberry Pi, set these in Kodi itself — no skin setting
can substitute, and it is the single biggest win available:

```xml
<!-- userdata/advancedsettings.xml -->
<advancedsettings>
  <imageres>540</imageres>
  <fanartres>720</fanartres>
</advancedsettings>
```

## Development

Kodi reads a skin *directory* live, so the fast loop is a symlink plus a reload
rather than a rebuild:

```bash
ln -s "$PWD/skin.cable.tv" ~/.kodi/addons/skin.cable.tv
kodi-send --host=127.0.0.1 --action="ReloadSkin()"   # re-reads all XML in ~1s
```

`ReloadSkin()` needs `kodi-eventclients-kodi-send` installed and *Settings →
Services → Control → Allow remote control from other systems* enabled. Bind it
to a key in `userdata/keymaps/` to reload from the sofa.

Useful while working: `Skin.ToggleDebug` overlays control ids and focus state,
and *Settings → System → Logging → debug logging overlay* gives an on-screen FPS
readout, which is the performance meter that matters on a Stick.

### Tooling

| Command | What it does |
|---|---|
| `python3 tools/kodilint.py skin.cable.tv --strict` | Static validation (see below) |
| `python3 tools/gen_textures.py --skin skin.cable.tv` | Regenerate every texture and the add-on artwork |
| `python3 tools/fetch_fonts.py --skin skin.cable.tv` | Fetch fonts and bake static instances |
| `python3 tools/gen_reference_data.py --xbmc-src <path>` | Refresh Kodi-version reference data |
| `bash tools/package.sh` | Build installable zips into `dist/` |

Every binary asset in a skin is generated, not hand-drawn: the two commands above
rebuild `media/cable/`, `resources/` and `fonts/` from scratch, and CI checks
that the committed output is byte-identical to a fresh run.

### Why there is a linter

Kodi cannot be run in CI here, and — more to the point — a whole class of
skinning mistakes produces **no runtime diagnostic at all**:

- An unknown colour name falls through `CGUIColorManager::GetColor` to
  `sscanf("%x")`, so it renders **fully transparent with nothing in the log**.
  Shipping Estuary 4.1.0 contains two live instances of this bug.
- Duplicate `<include>` / `<constant>` / `<variable>` / `<default>` names
  resolve first-wins via `try_emplace`, silently.
- `<include file=… condition=…>` honours the condition inside `Includes.xml`
  but **ignores it inside a window file**.
- `$VAR[]` in `<font>`, or in a texture tag other than `<texture>`/`<imagepath>`,
  resolves to nothing.

So `tools/kodilint.py` is the primary verification, not a nicety. It is
calibrated against the vendored copy of Estuary at
`tools/calibration.json`: linting a real, shipping, known-good skin must report
exactly the known-real defects and nothing else. That runs in CI, and it is what
stops the linter decaying into noise.

Real visual sign-off still happens on hardware — Kodi 22 cannot be installed in
CI (distro repos carry Kodi 20, and the Kodi PPA is unreachable from the build
environment), and software rendering could not answer the question that actually
matters, which is whether it is smooth on a Fire Stick.
