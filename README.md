# mpris-chroma

Recolors the desktop to match the album art playing in
[jellyfin-tui](https://github.com/dhonus/jellyfin-tui) or Spotify: the **wlchroma**
background shader follows the current cover and reverts to the configured palette when
playback pauses or stops, or all players exit.

It runs as a single systemd **user** service on a GLib main loop (event-driven, no
polling, single instance): `playerctl --follow` drives track/status changes, and D-Bus
`NameOwnerChanged` drives reverts when a player process exits — `playerctl --follow`
emits no line for a vanished player, so it can't notice one closing on its own. On each
track change it resolves the current cover, extracts its three most prominent colors,
and applies them with a smooth fade.

## How it works

```
playerctl --follow (jellyfin-tui, spotify MPRIS)
        │  status + mpris:artUrl
        ▼
coordinator: decide(players) ──► desired palette (newest Playing wins)
        │   parses + schedules only — never blocks the GLib loop
        ▼
worker thread ──► resolve_cover ──► extract_colors (Pillow, 3 prominent colors)
        │   (bounded, coalesced to the newest cover; stale results rejected;
        │    transient fetch failures retried with capped backoff)
        └─► wlchroma: wlchroma-ctl set-colors <c1> <c2> <c3> <fade_ms>
```

- **Multi-player:** The daemon watches both `jellyfin-tui` and `spotify`. The most
  recent Playing player's cover is applied (most recent play event takes precedence);
  its download and decode run on a background worker, so the daemon stays responsive
  and only the newest cover is ever applied. A transient artwork failure (network
  blip, cover file not written yet) retries automatically with capped backoff; if
  the newest player's cover can't be resolved, the most recent player whose cover
  can is shown instead, and the current palette is held — never replaced with a
  wrong one — while that plays out. When nothing is Playing — paused, stopped,
  or closed — the desktop reverts to the configured wlchroma palette.
- **wlchroma:** all three palette slots are set to the three most apparent,
  visibly-distinct colors in the cover. Ranking is vibrancy-weighted (coverage
  plus a chroma bonus), so a small vivid accent — a logo, a face — can take a
  slot from a large drab background instead of the palette being all backdrop.
  Colors are never invented: hues come from the cover, and a grayscale cover
  stays grey rather than being tinted. Colors cross-fade over `FADE_MS`
  (see `mpris_chroma/apply.py`) instead of snapping.
- **Light/dark aware:** hue always comes from the cover; the theme decides where
  the palette sits in *lightness*. Each cover is toned into a per-mode Oklab
  envelope — compressed relative to other covers, so a bright cover cannot wash
  out the desktop, but keeping that cover's own contrast, so a flat cover stays
  flat and a contrasty one stays contrasty. Chroma is then set as a fraction of
  what sRGB can actually show at that lightness and hue, which is what keeps
  dark palettes saturated instead of muddy. The daemon reads `color-scheme` from
  the freedesktop settings portal and re-tones the current palette live when you
  flip themes (same hues, different lightness). Set `MPRIS_CHROMA_MODE=light` or
  `dark` to force a mode (skips the portal); unset follows the system, defaulting
  to dark when no portal answers or no preference is set.
- **Revert:** Only Playing holds the album colors. When every player is Paused, Stopped,
  **or has exited**, the desktop fades back to the palette in wlchroma's config
  (`[effect.settings] palette` in `~/.config/wlchroma/config.toml`), falling back to the
  named `witch_hour` palette if that config can't be read. It is also restored on service
  start/stop, so the desktop can never get stuck on an album.

> Waybar was intentionally left out: it has no live-recolor IPC, so each accent change
> needs a full `SIGUSR2` stylesheet reload — that flickers the whole bar every track and
> leaks unreaped child processes. The background alone gives the effect without the cost.

## Requirements

- `playerctl` — jellyfin-tui exposes MPRIS with `mpris:artUrl` as a `file://` path to
  the cached cover in `~/.local/share/jellyfin-tui/covers/` (confirmed on jellyfin-tui
  1.5.0). A newest-file-in-covers fallback covers other cases.
- Spotify (optional) — the official client (via `spotify-launcher`) exposes MPRIS
  as player `spotify` with an `http(s)` `mpris:artUrl`. Its art is downloaded (HTTPS
  to an allowlisted provider domain only, size- and deadline-bounded) and cached
  under `~/.cache/mpris-chroma/covers/`. The cache is bounded — least-recently-used
  entries are evicted past 30 days or a 128 MiB / 512-entry budget.
- [wlchroma](../wlchroma) built with the `set-colors` IPC command, running. The service
  assumes this repo is at `~/mpris-chroma` and wlchroma at `~/wlchroma` (siblings);
  override `WLCHROMA_CTL` in a systemd drop-in if your layout differs.
- Python 3.11+ with **PyGObject** (`gi`) and **dbus-python** (`dbus`) — the GLib loop
  and D-Bus vanish detection — and **Pillow** (`PIL`) for in-process cover decoding.
  On Arch: `python-gobject`, `python-dbus`, `python-pillow`. Cover art is decoded
  in-process (JPEG/PNG/WebP only, validated by signature); there is no ImageMagick
  dependency.

## Install / uninstall

```bash
./install.sh      # links and enables the systemd user service
./uninstall.sh    # disables/removes the service and restores the default palette
```

## Operate

```bash
systemctl --user status mpris-chroma
journalctl --user -u mpris-chroma -f      # live logs
```

## Theme switching

The daemon subscribes to `SettingChanged` on `org.freedesktop.portal.Settings`,
so anything that implements the portal's Settings interface drives it live — no
restart, no configuration on this side. Check what your desktop currently
reports with:

```bash
gdbus call --session --dest org.freedesktop.portal.Desktop \
  --object-path /org/freedesktop/portal/desktop \
  --method org.freedesktop.portal.Settings.ReadOne \
  org.freedesktop.appearance color-scheme
```

`uint32 1` is prefer-dark, `2` is prefer-light, `0` is no preference (treated as
dark). Out of the box on most setups this is served by
`xdg-desktop-portal-gtk`, which proxies the `org.gnome.desktop.interface
color-scheme` gsettings key.

To drive it from [darkman](https://gitlab.com/WhyNotHugo/darkman), either
register darkman as the Settings backend:

```ini
# ~/.config/xdg-desktop-portal/portals.conf
[preferred]
org.freedesktop.impl.portal.Settings=darkman
```

or, to leave the portal backend alone, have darkman set the gsettings key that
the gtk portal already republishes — a script in `~/.local/share/dark-mode.d/`
and `~/.local/share/light-mode.d/` running:

```bash
gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'   # or 'prefer-light'
```

Neither approach needs any change on this daemon's side. Either way it
re-tones the current cover in place: same hues, different lightness envelope.

The two differ in latency, which is worth knowing if a flip feels sluggish: the
portal-backend route reaches the daemon directly over `SettingChanged`, while
the gsettings route depends on `xdg-desktop-portal-gtk` noticing the key and
republishing it.

## Tuning

Color feel is controlled by constants in `mpris_chroma/tone.py` (toning and
separation) and `mpris_chroma/colors.py` (ranking and selection). Lightness and
chroma are in [Oklab](https://bottosson.github.io/posts/oklab/), so a lightness
target means apparent brightness rather than HSV's `value`.

| Constant | Meaning | Dark | Light |
|----------|---------|------|-------|
| `ENVELOPES` | Oklab lightness envelope per mode | `0.15`–`0.55` | `0.55`–`0.92` |
| `GAMMA` | compression exponent across covers (`<1` pulls bright covers down) | `0.85` | `1.18` |
| `SPREAD_GAIN` | how much of a cover's own lightness spread survives (`1.0` = all) | `1.0` | `1.0` |
| `CHROMA_FRAC` | target chroma as a fraction of the in-gamut ceiling | `0.85` | `0.85` |
| `NEUTRAL_C` | chroma at or below which a slot stays grey (never tinted) | `0.02` | `0.02` |
| `MIN_DE` | minimum perceptual distance between two slots | `0.10` | `0.10` |
| `MAX_SEPARATION_SHIFT` | most one slot may be moved to resolve a collision | `0.04` | `0.04` |
| `MAX_SEPARATION_PASSES` | separation attempts before giving up — a real stopping point, not just a backstop: a clamped move can be smaller than a full step, so a run can burn through all its passes with displacement budget still unspent | `8` | `8` |
| `SELECT_MIN_DE` | minimum distance between two *source* picks (`colors.py`) | `0.08` | `0.08` |
| `VIBRANCY_WEIGHT` | chroma bonus vs. pixel coverage in ranking (`0.0` = most-pixels-wins) | `0.5` | `0.5` |
| `VIBRANCY_MIN_POP` | coverage below this gets no vibrancy boost (noise guard) | `0.01` | `0.01` |

The dark envelope and gamma are fitted against `witch_hour`, the palette
actually configured in this setup, and validated across a corpus of roughly
193 real covers: mean screen luminance through wlchroma's real 12-cell ramp
comes out to a median of **0.063**, against **0.223** for the pre-rework HSV
behavior and **0.075** for `witch_hour` itself — dark output now lands near
the reference instead of well past it. The light envelope and gamma have no
equivalent reference palette to fit against; they're a provisional seed
reasoned from symmetry with the dark side and should be expected to move once
someone reviews them by eye, unlike the dark constants above.

`tools/palette_lab.py` walks a corpus of real covers and A/Bs candidate
parameter sets live through `wlchroma-ctl`, which is how these were chosen.
Candidates: `legacy` (the pre-rework HSV algorithm, reproduced lab-only for
before/after comparison), `oklab-v1`, `oklab-darker`, `oklab-vivid`,
`oklab-yellowlift`. Run `tools/palette_lab.py --list` to see them, `--replay`
to re-score recorded verdicts against a change, and `--holdout N` to reserve
covers that tuning never sees.

## Tests

```bash
python -m unittest discover -s tests -v
```

Pure logic (cover resolution, color extraction, state transitions) is separated from
subprocess I/O, so the suite runs without a live Wayland session. The
color-extraction tests generate their image fixtures in-process with Pillow, so no
external image tooling is required.
