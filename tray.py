#!/usr/bin/env python3
"""tray — cross-platform system-tray controller for the Pritunl watchdog.

Menu-bar (macOS) / system-tray (Linux) item showing VPN state with
Connect / Disconnect / Enable-auto-reconnect / Quit. All VPN actions shell out
to the sibling ``vpn`` CLI so there is a single source of truth for logic.

Install with ``./vpn install --tray`` (launchd job on macOS, systemd user unit
+ autostart entry on Linux), or run manually with ``python3 tray.py``.

Backends:
  * macOS — native NSStatusItem via PyObjC (runs on the main thread with the
    accessory activation policy; the pystray Cocoa backend crashes under
    launchd, so we do not use it here).
  * Linux — pystray (GTK AppIndicator / XEmbed), plus Pillow for icons.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

IS_MAC = sys.platform == "darwin"

HERE = Path(__file__).resolve().parent
VPN = str(HERE / "vpn")

GREEN = (46, 204, 113)
RED = (231, 76, 60)
GREY = (127, 140, 141)
ICON_SIZE = 22

POLL = 5

_should_quit = threading.Event()


# ---------------------------------------------------------------------------
# Shared CLI helpers (single source of truth = the ``vpn`` script)
# ---------------------------------------------------------------------------


def run_vpn(*args: str) -> str:
    try:
        r = subprocess.run([sys.executable, VPN, *args], capture_output=True, text=True, timeout=30)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as exc:
        return "error: {!r}".format(exc)


def status() -> dict:
    """Return {'state': str, 'enabled': bool, 'client': str, 'server': str}."""
    out = run_vpn("status")
    info = {"state": "unknown", "enabled": True, "client": "", "server": ""}
    for line in out.splitlines():
        low = line.lower()
        if low.startswith("state:"):
            info["state"] = line.split(":", 1)[1].strip().lower()
        elif low.startswith("auto-reconnect:"):
            info["enabled"] = line.split(":", 1)[1].strip().lower() == "on"
        elif low.startswith("client:"):
            info["client"] = line.split(":", 1)[1].strip()
        elif low.startswith("server:"):
            info["server"] = line.split(":", 1)[1].strip()
    return info


def tooltip(info: dict) -> str:
    if not info["enabled"]:
        return "VPN: disabled (auto-reconnect off)"
    if info["state"] == "active":
        return "VPN: connected — {} via {}".format(info["client"], info["server"])
    if info["state"] == "connecting":
        return "VPN: connecting…"
    return "VPN: disconnected"


def color_for(info: dict):
    if not info["enabled"]:
        return GREY
    return GREEN if info["state"] == "active" else RED


def connect_info() -> None:
    run_vpn("connect")


def disconnect_info() -> None:
    run_vpn("disconnect")


# ---------------------------------------------------------------------------
# Linux backend: pystray + Pillow
# ---------------------------------------------------------------------------


def run_linux() -> int:
    import pystray
    from PIL import Image, ImageDraw

    def make_icon(info: dict):
        color = color_for(info) + (255,)
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 2, ICON_SIZE - 2, ICON_SIZE - 2], fill=color)
        if not info["enabled"]:
            d.line([3, ICON_SIZE - 3, ICON_SIZE - 3, 3], fill=(255, 255, 255, 255), width=2)
        return img

    def poll(ic):
        while not _should_quit.is_set():
            info = status()
            try:
                ic.icon = make_icon(info)
                ic.title = tooltip(info)
                ic.menu = build_menu(ic)
            except Exception:
                pass
            time.sleep(POLL)

    def build_menu(ic):
        info = status()
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "Disconnect" if info["state"] == "active" else "Connect",
                lambda icon, item: (disconnect_info() if status()["state"] == "active" else connect_info()),
                default=True,
            ),
            pystray.MenuItem(
                lambda item: "Auto-reconnect: {}".format("on" if info["enabled"] else "off"),
                lambda icon, item: run_vpn("disable" if status()["enabled"] else "enable"),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: (_should_quit.set(), icon.stop())),
        )

    info = status()
    icon = pystray.Icon("vpn", title=tooltip(info), icon=make_icon(info), menu=build_menu(None))
    threading.Thread(target=poll, args=(icon,), daemon=True).start()
    icon.run()
    return 0


# ---------------------------------------------------------------------------
# macOS backend: NSStatusItem via PyObjC (main thread)
# ---------------------------------------------------------------------------


def run_macos() -> int:
    import objc  # noqa: F401  (ensures PyObjC bridges are installed)
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSImage,
        NSMakeRect,
        NSMenu,
        NSMenuItem,
        NSStatusBar,
        NSVariableStatusItemLength,
        NSBezierPath,
        NSColor,
    )
    from Foundation import NSTimer

    class TrayController:
        def __init__(self):
            self.app = NSApplication.sharedApplication()
            self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            self.statusbar = NSStatusBar.systemStatusBar()
            self.item = self.statusbar.statusItemWithLength_(NSVariableStatusItemLength)
            self._refresh()
            self._schedule()

        def _icon_for(self, color):
            size = ICON_SIZE
            # Draw a coloured circle by locking focus on a fresh NSImage.
            image = NSImage.alloc().initWithSize_((size, size))
            image.lockFocus()
            col = NSColor.colorWithSRGBRed_green_blue_alpha_(
                color[0] / 255.0, color[1] / 255.0, color[2] / 255.0, 1.0
            )
            col.setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2, 2, size - 4, size - 4)).fill()
            image.unlockFocus()
            return image

        def _refresh(self):
            info = status()
            self.item.button().setImage_(self._icon_for(color_for(info)))
            self.item.button().setToolTip_(tooltip(info))
            self._info = info
            menu = NSMenu.alloc().init()
            active = info["state"] == "active"
            connect = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Disconnect" if active else "Connect", "toggleConnect:", ""
            )
            connect.setTarget_(self)
            menu.addItem_(connect)
            auto = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Auto-reconnect: {}".format("on" if info["enabled"] else "off"),
                "toggleAuto:", "",
            )
            auto.setTarget_(self)
            menu.addItem_(auto)
            menu.addItem_(NSMenuItem.separatorItem())
            quit_ = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "quit:", "q")
            quit_.setTarget_(self)
            menu.addItem_(quit_)
            self.item.setMenu_(menu)

        def toggleConnect_(self, _sender):
            if status()["state"] == "active":
                disconnect_info()
            else:
                connect_info()
            self._refresh()

        def toggleAuto_(self, _sender):
            run_vpn("disable" if status()["enabled"] else "enable")
            self._refresh()

        def quit_(self, _sender):
            _should_quit.set()
            self.app.terminate_(None)

        def _tick(self, _timer):
            if _should_quit.is_set():
                return
            self._refresh()
            self._schedule()

        def _schedule(self):
            t = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                POLL, self, "_tick:", None, False
            )

    controller = TrayController()
    controller.app.run()
    return 0


def main() -> int:
    if IS_MAC:
        return run_macos()
    try:
        return run_linux()
    except ImportError as exc:
        print("tray needs pystray + Pillow ({}): pip install -r requirements.txt".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
