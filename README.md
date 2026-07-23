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
        │   (bounded, coalesced to the newest cover; stale results rejected)
        └─► wlchroma: wlchroma-ctl set-colors <c1> <c2> <c3> <fade_ms>
```

- **Multi-player:** The daemon watches both `jellyfin-tui` and `spotify`. The most
  recent Playing player's cover is applied (most recent play event takes precedence);
  its download and decode run on a background worker, so the daemon stays responsive
  and only the newest cover is ever applied. When nothing is Playing — paused, stopped,
  or closed — the desktop reverts to the configured wlchroma palette.
- **wlchroma:** all three palette slots are set to the three most apparent,
  visibly-distinct colors in the cover. Ranking is vibrancy-weighted (coverage
  plus a chroma bonus), so a small vivid accent — a logo, a face — can take a
  slot from a large drab background instead of the palette being all backdrop. Colors are only lifted for visibility, never invented — a grayscale cover
  stays neutral rather than being tinted. Colors cross-fade over `FADE_MS`
  (see `mpris_chroma/apply.py`) instead of snapping.
- **Light/dark aware:** hue and saturation always come from the cover; only the
  *brightness band* follows your desktop theme. The daemon reads `color-scheme`
  from the freedesktop settings portal and re-tones the current palette live when
  you flip themes (same hues, shifted values). Set `MPRIS_CHROMA_MODE=light` or
  `dark` to force a band (skips the portal); unset follows the system, defaulting
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

## Tuning

Color feel is controlled by constants at the top of
`mpris_chroma/colors.py`:

| Constant | Meaning | Default |
|----------|---------|---------|
| `S_MIN` | minimum saturation (lifts drab covers) | `0.45` |
| `V_MIN` / `V_MAX` | value band (visible, not blown out) | `0.45` / `0.85` |
| `BANDS` | value band per theme mode (`dark` = `V_MIN`/`V_MAX`) | light: `0.70` / `0.97` |
| `NEUTRAL_S` | saturation at or below which grayscale stays untinted | `0.12` |
| `COLOR_MIN_DIST` | minimum RGB distance between selected colors | `0.12` |
| `VIBRANCY_WEIGHT` | chroma bonus vs. pixel coverage in ranking (`0.0` = most-pixels-wins) | `0.5` |
| `VIBRANCY_MIN_POP` | coverage below this gets no vibrancy boost (noise guard) | `0.01` |

## Tests

```bash
python -m unittest discover -s tests -v
```

Pure logic (cover resolution, color extraction, state transitions) is separated from
subprocess I/O, so the suite runs without a live Wayland session. The
color-extraction tests generate their image fixtures in-process with Pillow, so no
external image tooling is required.
