# pritunl-auto-reconnect

A simple `vpn` command for macOS and Linux. It connects your Pritunl profile
with auto-generated TOTP codes. A watchdog keeps the tunnel up after sleep or
drops. It also offers diagnostics and a menu-bar tray controller.

## What it does

- Connects your Pritunl profile using your PIN plus a TOTP code.
  If the server rejects the code, it retries with a fresh one.
- Reconnects within about 15 seconds of an unexpected drop.
  It stays quiet while the machine sleeps or the tunnel is already working.
  It checks internet reachability before dialing.
- Lets you turn auto-reconnect on and off with one command or a tray toggle.
- Honors manual disconnect. When you click Disconnect, the watchdog stays
  down. It only reconnects on unexpected drops.
- Shows a menu-bar or system-tray icon. The menu has Connect, Disconnect,
  Auto-reconnect, About, and Quit. About shows the author and repo link.
- Can optionally power-cycle Wi-Fi if the tunnel fails repeatedly after wake.
  This is off by default and every attempt is logged.

## Supported platforms

- macOS. Uses launchd LaunchAgents and a native NSStatusItem icon.
- Linux and Ubuntu LTS. Uses systemd user units plus an autostart entry.
  The tray uses pystray with GTK or AppIndicator.

## Setup

1. Install the Pritunl client and import your profile.
   Confirm it with `pritunl-client list`.

2. Create the config file with your base32 TOTP secret.
   You get this secret from Pritunl OTP enrollment. It is the value encoded
   in the QR code.

   ```sh
   mkdir -p ~/.config/vpn-connector
   cp config.example.json ~/.config/vpn-connector/config.json
   $EDITOR ~/.config/vpn-connector/config.json
   chmod 600 ~/.config/vpn-connector/config.json
   ```

   Only `totp_secret` is required. `profile_id` is found automatically when
   one profile is registered. `pritunl_client` is found automatically.
   `pin` is optional. `auto_wifi_reset` defaults to false.

3. Install the tray dependencies. You only need this for the menu-bar UI.

   ```sh
   python3 -m pip install -r requirements.txt
   ```

4. Install the service. Add `--tray` to also install the tray app.

   ```sh
   ./vpn install --tray
   ```

## Commands

| Command            | Effect                                         |
|------------------|------------------------------------------------|
| `vpn connect`    | Connect using PIN plus TOTP                    |
| `vpn disconnect` | Disconnect                                     |
| `vpn status`     | Show state and auto-reconnect. Exit 0 if connected. |
| `vpn totp`       | Print the current TOTP code. `--copy` sends it to the clipboard. |
| `vpn enable`     | Enable auto-reconnect and connect now        |
| `vpn disable`    | Disable auto-reconnect and disconnect now    |
| `vpn enabled`    | Print whether auto-reconnect is on or off    |
| `vpn ensure`     | Connect if not connected and enabled         |
| `vpn daemon`     | Run the watchdog loop. Used by the service manager. |
| `vpn diag`       | Show routing, DNS, and reachability diagnostics |
| `vpn install`    | Install the service unit. `--tray` adds the tray. |
| `vpn uninstall`  | Remove the service, tray, and symlink        |

## Files

- Config: `~/.config/vpn-connector/config.json`. Mode 0600.
  See `config.example.json`.
- Runtime state: `~/.config/vpn-connector/state.json`
- macOS units: `~/Library/LaunchAgents/com.local.vpn-daemon.plist` and
  `com.local.vpn-tray.plist`
- Linux units: `~/.config/systemd/user/pritunl-auto-reconnect.service` and
  `pritunl-auto-reconnect-tray.service`
- Linux autostart: `~/.config/autostart/pritunl-auto-reconnect-tray.desktop`
- Log: macOS `~/Library/Logs/vpn-connector.log`. Linux
  `~/.local/state/vpn-connector.log`.

## Notes on the old no-internet-after-wake bug

The old watchdog read the wrong column from `pritunl-client list`. It read
TYPE as STATE, so it saw System instead of Active. It then re-ran start every
5 seconds even when the tunnel was already up. That left the default route and
DNS stale after wake. Parsing now maps columns by header name. The watchdog
waits while the state is active, connecting, or system. It also verifies
reachability before dialing. Run `vpn diag` after a wake to confirm routing and
DNS. Only enable `auto_wifi_reset` once diagnostics confirm the tunnel is at
fault.

## Uninstall

```sh
vpn uninstall
```
