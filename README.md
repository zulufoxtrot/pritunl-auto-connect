# pritunl-auto-reconnect

`vpn` — lightweight, **cross-platform** (macOS + Linux) Pritunl client wrapper
with automatic TOTP, a sleep-safe watchdog, an enable/disable switch,
diagnostics, and a menu-bar / system-tray controller.

## What it does

- Connects your Pritunl profile using PIN + TOTP (RFC 6238, auto-generated,
  retried with a fresh code if rejected mid-flight)
- **Sleep-safe watchdog** — reconnects within ~15s of a drop, but does **not**
  fight the OS while asleep or mid-transition (fixes the old
  `state=system -> connecting` / no-internet-after-wake loop), verifies
  internet reachability before dialing, and cleans up half-open tunnels
- **Enable / disable auto-reconnect** with one command or a tray toggle
- **Menu-bar / system-tray** item with status icon + Connect / Disconnect /
  Auto-reconnect / Quit
- Optional, gated, logged **Wi-Fi power-cycle** if the tunnel cannot come up
  after repeated post-wake failures (off by default)

## Supported platforms

- **macOS** — launchd LaunchAgents (`daemon` + optional `tray`), native
  `NSStatusItem` menu-bar icon
- **Linux / Ubuntu LTS** — systemd user units (`daemon` + optional `tray`)
  plus an autostart `.desktop` entry, tray via `pystray` (GTK/AppIndicator)

## Setup

1. Install the [Pritunl client](https://client.pritunl.com/) and import your
   profile (verify with `pritunl-client list`).

2. Create the config with your base32 TOTP secret (from Pritunl's OTP/2FA
   enrollment — the secret encoded in the QR code):

   ```sh
   mkdir -p ~/.config/vpn-connector
   cp config.example.json ~/.config/vpn-connector/config.json
   $EDITOR ~/.config/vpn-connector/config.json
   chmod 600 ~/.config/vpn-connector/config.json
   ```

   Only `totp_secret` is required. `profile_id` is auto-detected when exactly
   one profile is registered; `pritunl_client` is auto-detected via
   `shutil.which`; `pin` is optional; `auto_wifi_reset` defaults to `false`.

3. Install the tray dependencies (optional, only needed for the menu-bar UI):

   ```sh
   python3 -m pip install -r requirements.txt
   ```

4. Install the service (add `--tray` for the menu-bar / tray app):

   ```sh
   ./vpn install --tray
   ```

## Commands

| Command             | Effect                                                     |
|-------------------|------------------------------------------------------------|
| `vpn connect`     | Connect using PIN + TOTP                                    |
| `vpn disconnect`  | Disconnect                                                  |
| `vpn status`      | Show profile state + auto-reconnect (exit 0 = connected)    |
| `vpn totp`        | Print current TOTP code (`--copy` → clipboard)              |
| `vpn enable`      | Enable auto-reconnect and connect now                       |
| `vpn disable`     | Disable auto-reconnect and disconnect now                   |
| `vpn enabled`     | Print whether auto-reconnect is on/off                      |
| `vpn ensure`      | One-shot: connect if not connected and enabled              |
| `vpn daemon`      | Watchdog loop (used by the service manager)                 |
| `vpn diag`        | Routing / DNS / reachability diagnostics                    |
| `vpn install`     | Install service unit (+ `--tray`)                           |
| `vpn uninstall`   | Remove service, tray, and symlink                           |

## Files

- Config: `~/.config/vpn-connector/config.json` (0600) — see `config.example.json`
- Runtime state: `~/.config/vpn-connector/state.json` (`{"enabled": true}`)
- macOS units: `~/Library/LaunchAgents/com.local.vpn-{daemon,tray}.plist`
- Linux units: `~/.config/systemd/user/pritunl-auto-reconnect{,-tray}.service`
  + autostart `~/.config/autostart/pritunl-auto-reconnect-tray.desktop`
- Log: macOS `~/Library/Logs/vpn-connector.log` · Linux `~/.local/state/vpn-connector.log`

## Notes on the post-sleep "no internet" issue

The old watchdog misread `pritunl-client list` columns (it used fixed indices
and read **TYPE** as **STATE**), so it saw `System` instead of `Active` and
re-issued `start` every 5s — even while the tunnel was already up, which can
leave the default route/DNS stale after wake. Parsing now maps columns by
header name, and the watchdog defers on `active`/`connecting`/`system` and
verifies reachability before dialing. Run `vpn diag` after a wake to confirm
routing/DNS; only enable `auto_wifi_reset` once diagnostics confirm the
tunnel is at fault.

## Uninstall

```sh
vpn uninstall
```
