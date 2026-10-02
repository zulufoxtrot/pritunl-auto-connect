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
YELLOW = (241, 196, 15)
ICON_SIZE = 22
DOT = 9  # status dot diameter

POLL = 5

_should_quit = threading.Event()

ASSET_PATH = HERE / "assets" / "pritunl-base.png"


def load_base(size: int = ICON_SIZE):
    """Pritunl logo scaled to the tray size; None if the asset is missing."""
    try:
        from PIL import Image
        img = Image.open(ASSET_PATH).convert("RGBA")
        return img.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def make_overlay_image(base, color, enabled: bool = True):
    """Pritunl logo + coloured status dot (optional slash when disabled).

    Used by both backends: PIL (Linux) and NSImage via a PNG round-trip
    (macOS). Falls back to the old plain circle if Pillow/base is missing.
    """
    from PIL import Image, ImageDraw
    if base is None:
        img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 2, ICON_SIZE - 2, ICON_SIZE - 2], fill=color + (255,))
    else:
        img = base.copy()
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 4 + DOT, 4 + DOT], fill=color + (255,), outline=(255, 255, 255, 255), width=1)
    if not enabled:
        d.line([4, ICON_SIZE - 4, ICON_SIZE - 4, 4], fill=(255, 255, 255, 255), width=2)
    return img


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
    if info["state"] == "active":
        return GREEN
    if info["state"] == "connecting":
        return YELLOW
    return RED


def connect_info() -> None:
    run_vpn("connect")


def disconnect_info() -> None:
    run_vpn("disconnect")


# ---------------------------------------------------------------------------
# Linux backend: pystray + Pillow
# ---------------------------------------------------------------------------


def run_linux() -> int:
    import pystray

    base = load_base()

    def make_icon(info: dict):
        return make_overlay_image(base, color_for(info), info["enabled"])

    def _bg(fn):
        # pystray runs callbacks on the UI thread — never block it.
        threading.Thread(target=fn, daemon=True).start()

    def poll(ic):
        while not _should_quit.is_set():
            info = status()
            try:
                ic.icon = make_icon(info)
                ic.title = tooltip(info)
            except Exception:
                pass
            time.sleep(POLL)

    def build_menu():
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "Disconnect" if status()["state"] == "active" else "Connect",
                lambda icon, item: _bg(lambda: (disconnect_info() if status()["state"] == "active" else connect_info())),
                default=True,
            ),
            pystray.MenuItem(
                lambda item: "Auto-reconnect: {}".format("on" if status()["enabled"] else "off"),
                lambda icon, item: _bg(lambda: run_vpn("disable" if status()["enabled"] else "enable")),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: (_should_quit.set(), icon.stop())),
        )

    info = status()
    icon = pystray.Icon("vpn", title=tooltip(info), icon=make_icon(info), menu=build_menu())
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
        NSMenu,
        NSMenuItem,
        NSStatusBar,
        NSVariableStatusItemLength,
    )
    from Foundation import NSTimer

    class TrayController:
        def __init__(self):
            self.app = NSApplication.sharedApplication()
            self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            self.statusbar = NSStatusBar.systemStatusBar()
            self.item = self.statusbar.statusItemWithLength_(NSVariableStatusItemLength)
            self._base = load_base(44)  # Retina-sized base for crisp scaling
            # Render optimistically (grey "starting") so the icon appears
            # instantly and never blocks on a slow status() call.
            self.info = {"state": "unknown", "enabled": True, "client": "", "server": ""}
            self._render()
            self._refresh_async()
            self._schedule()

        def _nsimage(self, color):
            enabled = bool(self.info.get("enabled", True))
            img = make_overlay_image(self._base, color, enabled)
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = bytes(buf.getvalue())
            image = NSImage.alloc().initWithData_(data)
            image.setSize_((ICON_SIZE, ICON_SIZE))
            return image

        def _render(self):
            # Pure UI: runs on the main thread from cached info, never blocks.
            info = self.info
            self.item.button().setImage_(self._nsimage(color_for(info)))
            self.item.button().setToolTip_(tooltip(info))
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

        def _do_on_main(self, sel):
            self.app.performSelectorOnMainThread_withObject_waitUntilDone_(
                sel, None, False
            )

        def _refresh_async(self):
            def bg():
                self.info = status()  # worker thread does the slow work
                self._do_on_main("_render")
            threading.Thread(target=bg, daemon=True).start()

        def toggleConnect_(self, _sender):
            want_disconnect = self.info["state"] == "active"
            self.info = dict(self.info, state="connecting")  # optimistic feedback
            self._render()

            def bg():
                (disconnect_info() if want_disconnect else connect_info())
                self.info = status()
                self._do_on_main("_render")
            threading.Thread(target=bg, daemon=True).start()

        def toggleAuto_(self, _sender):
            want_off = self.info["enabled"]
            self.info = dict(self.info, enabled=not want_off)
            self._render()

            def bg():
                run_vpn("disable" if want_off else "enable")
                self.info = status()
                self._do_on_main("_render")
            threading.Thread(target=bg, daemon=True).start()

        def quit_(self, _sender):
            _should_quit.set()
            self.app.terminate_(None)

        def _tick(self, _timer):
            if _should_quit.is_set():
                return
            self._render()        # repaint instantly from cache
            self._refresh_async() # then poll in background
            self._schedule()

        def _schedule(self):
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
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
