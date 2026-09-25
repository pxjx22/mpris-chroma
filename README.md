# mpris-chroma

## Overview & Problem Statement
`mpris-chroma` is an event-driven background service that dynamically synchronizes desktop aesthetics to active media playback without relying on CPU-intensive polling. Written in Rust, it leverages a single event loop over a bounded channel, D-Bus event listening, and an asynchronous actor model: it coordinates real-time album art extraction and applies perceptual color transformations over low-level IPC. It was built to solve the performance and lifecycle issues typical of polling-based desktop customization scripts, ensuring strict zero-polling operation and seamless state reversion when media players exit.

## Architecture & System Design

```mermaid
flowchart TD
    subgraph External[External Processes]
        P1[jellyfin-tui / Spotify] -- MPRIS --> PC[playerctl --follow]
        DBus[D-Bus Session Bus]
    end

    subgraph Service[mpris-chroma Daemon]
        Coord[Coordinator<br/>Event Loop]
        Worker[Background Worker Thread<br/>Async Actor]
        Memo[PaletteMemo<br/>Cache & Rate Limit]
    end

    subgraph Output[IPC]
        WL[wlchroma-ctl → wlchroma]
    end

    PC -- Track/Status Updates --> Coord
    DBus -- NameOwnerChanged\n(Player Exit) --> Coord
    Coord -- Schedule (Non-blocking) --> Worker
    Worker -- Extract & Tone --> Memo
    Memo -- AF_UNIX IPC --> WL
```

### Core Design Decisions
* **Zero-Polling & Event-Driven**: The coordinator operates purely on external events. Media metadata is streamed via `playerctl --follow`, while process lifecycle events are trapped via D-Bus `NameOwnerChanged`.
* **Asynchronous Actor Model**: Heavy operations like I/O, network requests, and image processing run on an isolated worker thread. The event loop is strictly non-blocking and handles state transitions only.
* **Bounded Caching & Coalescing**: Requests are coalesced so only the newest state is processed. The `PaletteMemo` cache is LRU-bounded (128 MiB / 512 entries), preventing unbounded memory growth over prolonged uptime.
* **Low-Level IPC**: Recoloring instructions are sent directly to the `wlchroma` UNIX domain socket via lightweight IPC, bypassing shell invocation overhead and preventing zombie processes.

## Key Features & Technical Hardening

* **Oklab Perceptual Toning**: Employs the Oklab color space to map extracted hues into predefined lightness and chroma envelopes. This guarantees accessible contrast and vibrancy ratios across both dynamically detected light and dark modes, avoiding muddy outputs or blown-out highlights.
* **Systemd Security Sandboxing**: Shipped with a highly restricted user unit relying on `ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateDevices=yes`, and `ProtectHome=read-only`. Write access is exclusively limited to a dedicated cache directory.
* **Stream Protection & Network Isolation**: Downloading remote artwork utilizes strict bounds. The HTTP client implements fixed timeouts, size ceilings, and stream validation to mitigate SSRF (Server-Side Request Forgery) and DNS rebinding attacks.
* **Fail-Safe Transitions & Fault Tolerance**: Network failures trigger a capped exponential backoff retry cycle. If a process exits abruptly, D-Bus listeners guarantee the service reverts gracefully to the base desktop palette, preventing stale UI states.

## Testing & Verification
The test suite validates pure logic states, thread coordination, cache eviction, edge-case file mutations and adversarial bounds checks without a live Wayland session. Golden tests pin the colour pipeline to exact values recorded from the original Python implementation (see [docs/rust-port.md](docs/rust-port.md)), and end-to-end tests run the real binary against a fake `playerctl` and `wlchroma-ctl` on a private D-Bus.

```bash
cargo test
```

## Quickstart / Installation

### Prerequisites
* A Rust toolchain (1.85 or newer) to build. The binary links only glibc: no GLib, libdbus or Python at runtime.
* `playerctl` and an active `wlchroma` instance running.

### Installation
The unit assumes `wlchroma` is cloned at `~/wlchroma`; adjust `WLCHROMA_CTL` in `systemd/mpris-chroma.service` if yours differs.

```bash
# Clone the repository
git clone https://github.com/dhonus/mpris-chroma.git ~/mpris-chroma
cd ~/mpris-chroma

# Build, install the binary to ~/.local/bin, and enable the systemd user service
./install.sh

# Verify the service is running
systemctl --user status mpris-chroma
journalctl --user -u mpris-chroma -f
```

Rerun `./install.sh` after pulling to rebuild and restart. To remove the service and revert to the default palette, run `./uninstall.sh`.

### Configuration
Set these in the unit (`Environment=`) or the shell:

* `WLCHROMA_CTL`: path to `wlchroma-ctl` (default: found on `PATH`).
* `MPRIS_CHROMA_MODE=light|dark`: force the palette mode; otherwise the desktop colour scheme is followed live.
* `MPRIS_CHROMA_ART_DOMAINS`: extra allowlisted artwork domains.
* `MPRIS_CHROMA_LOG=debug|info|warn|error`: log level (default `info`, one line per palette change).
