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

import io
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_MAC = sys.platform == "darwin"

HERE = Path(__file__).resolve().parent
VPN = str(HERE / "vpn")

APP_NAME = "Pritunl Auto-Reconnect"
AUTHOR = "zulufoxtrot"
REPO_URL = "https://github.com/zulufoxtrot/pritunl-auto-connect"
ABOUT_TEXT = "{}\n\nAuthor: {}\nRepo: {}".format(APP_NAME, AUTHOR, REPO_URL)

GREEN = (46, 204, 113)
RED = (231, 76, 60)
GREY = (127, 140, 141)
YELLOW = (241, 196, 15)
ICON_SIZE = 22
DOT = 11  # status dot diameter

POLL = 5

_should_quit = threading.Event()
_CONTROLLER = None

ASSET_PATH = HERE / "assets" / "pritunl-base.png"


RENDER = ICON_SIZE * 2  # render at 2x for crisp Retina menu-bar icons


def load_base():
    """Pritunl logo as a PIL image; None if Pillow/asset is missing."""
    try:
        from PIL import Image
        return Image.open(ASSET_PATH).convert("RGBA")
    except Exception:
        return None


def make_overlay_image(base, color, enabled: bool = True):
    """Pritunl logo + coloured status dot (optional slash when disabled).

    Rendered at ``RENDER`` px (2x) and scaled by the backend. Used by both
    the Linux (pystray/Pillow) and macOS (Pillow -> NSImage) backends.
    """
    from PIL import Image, ImageDraw
    if base is None:
        img = Image.new("RGBA", (RENDER, RENDER), (0, 0, 0, 0))
    else:
        img = base.resize((RENDER, RENDER), Image.LANCZOS).copy()
    d = ImageDraw.Draw(img)
    scale = RENDER / float(ICON_SIZE)
    dot = int(DOT * scale)
    m = int(1 * scale)                      # small margin from the edge
    x1 = RENDER - dot - m                   # top-right placement
    y1 = m
    d.ellipse([x1, y1, x1 + dot, y1 + dot], fill=color + (255,),
              outline=(255, 255, 255, 255), width=1)
    if not enabled:
        d.line([m, RENDER - m, RENDER - m, m], fill=(255, 255, 255, 255),
               width=max(1, int(2 * scale)))
    return img


def render_icon_png(color, enabled: bool = True) -> bytes:
    """Status icon as PNG bytes (logo + dot); ObjC-circle fallback if Pillow
    is unavailable. Must be called on the main thread on macOS (NSImage)."""
    try:
        buf = io.BytesIO()
        make_overlay_image(load_base(), color, enabled).save(buf, format="PNG")
        return bytes(buf.getvalue())
    except Exception:
        from AppKit import NSBezierPath, NSColor, NSMakePoint, NSMakeRect, NSImage
        image = NSImage.alloc().initWithSize_((ICON_SIZE, ICON_SIZE))
        image.lockFocus()
        # NSImage origin is bottom-left; top-right = high x, high y.
        rect = NSMakeRect(ICON_SIZE - DOT - 1, ICON_SIZE - DOT - 1, DOT, DOT)
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            color[0] / 255.0, color[1] / 255.0, color[2] / 255.0, 1.0
        ).setFill()
        dot_path = NSBezierPath.bezierPathWithOvalInRect_(rect)
        dot_path.fill()
        NSColor.whiteColor().setStroke()
        dot_path.setLineWidth_(1.0)
        dot_path.stroke()
        if not enabled:
            NSColor.whiteColor().setStroke()
            p = NSBezierPath.bezierPath()
            p.moveToPoint_(NSMakePoint(1, 1))
            p.lineToPoint_(NSMakePoint(ICON_SIZE - 1, ICON_SIZE - 1))
            p.setLineWidth_(2.0)
            p.stroke()
        image.unlockFocus()
        return bytes(image.TIFFRepresentation().bytes().tobytes())


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


def show_about_dialog() -> None:
    """Linux about dialog via zenity/kdialog (falls back to stdout)."""
    if shutil.which("zenity"):
        subprocess.run(["zenity", "--info", "--title", APP_NAME, "--text", ABOUT_TEXT], check=False)
        return
    if shutil.which("kdialog"):
        subprocess.run(["kdialog", "--title", APP_NAME, "--msgbox", ABOUT_TEXT], check=False)
        return
    print(ABOUT_TEXT)


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
            pystray.MenuItem("About…", lambda icon, item: _bg(show_about_dialog)),
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
    import queue
    from Foundation import NSObject, NSAutoreleasePool
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSDate,
        NSDefaultRunLoopMode,
        NSImage,
        NSMenu,
        NSMenuItem,
        NSStatusBar,
        NSVariableStatusItemLength,
        NSAlert,
        NSWorkspace,
        NSURL,
    )

    # This Python/PyObjC build never delivers NSTimer callbacks nor
    # performSelectorOnMainThread:withObject: — so we run the run loop by hand
    # with nextEventMatchingMask:untilDate:inMode:dequeue: and marshal worker
    # results through a plain queue. All AppKit access stays on the main
    # thread; the worker thread only does blocking subprocess calls.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    statusbar = NSStatusBar.systemStatusBar()
    item = statusbar.statusItemWithLength_(NSVariableStatusItemLength)
    item.setHighlightMode_(1)
    button = item.button()  # create ON the main thread (layout engine)
    updates = queue.Queue()
    stop = threading.Event()

    initial = {"state": "unknown", "enabled": True, "client": "", "server": ""}
    state = {"info": initial}

    def apply_info(info):
        if os.environ.get('TRAY_DEBUG'):
            print('apply_info state=%s enabled=%s' % (info['state'], info['enabled']), flush=True)
        try:
            image = NSImage.alloc().initWithData_(
                render_icon_png(color_for(info), bool(info["enabled"]))
            )
            image.setSize_((ICON_SIZE, ICON_SIZE))
            button.setImage_(image)
            button.setToolTip_(tooltip(info))
        except Exception:
            pass
        item.setMenu_(build_menu(info))

    class Controller(NSObject):
        def toggleConnect_(self, _sender):
            want_disconnect = state["info"]["state"] == "active"
            state["info"] = dict(state["info"], state="connecting")
            apply_info(state["info"])

            def job():
                disconnect_info() if want_disconnect else connect_info()
                updates.put(status())
            threading.Thread(target=job, daemon=True).start()

        def toggleAuto_(self, _sender):
            want_off = bool(state["info"].get("enabled", True))
            state["info"] = dict(state["info"], enabled=not want_off)
            apply_info(state["info"])

            def job():
                run_vpn("disable" if want_off else "enable")
                updates.put(status())
            threading.Thread(target=job, daemon=True).start()

        def about_(self, _sender):
            alert = NSAlert.alloc().init()
            alert.setMessageText_(APP_NAME)
            alert.setInformativeText_("Author: {}\nRepo: {}".format(AUTHOR, REPO_URL))
            alert.addButtonWithTitle_("Open Repo")
            alert.addButtonWithTitle_("Close")
            if alert.runModal() == 1000:  # NSAlertFirstButtonReturn
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(REPO_URL))

        def quit_(self, _sender):
            stop.set()

    controller = Controller.alloc().init()
    global _CONTROLLER
    _CONTROLLER = controller  # keep the ObjC target alive

    def build_menu(info):
        active = info["state"] == "active"
        menu = NSMenu.alloc().init()
        connect = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Disconnect" if active else "Connect", "toggleConnect:", ""
        )
        connect.setTarget_(controller)
        menu.addItem_(connect)
        auto = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Auto-reconnect: {}".format("on" if info["enabled"] else "off"),
            "toggleAuto:", "",
        )
        auto.setTarget_(controller)
        menu.addItem_(auto)
        menu.addItem_(NSMenuItem.separatorItem())
        about = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "About…", "about:", ""
        )
        about.setTarget_(controller)
        menu.addItem_(about)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_ = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "quit:", "q")
        quit_.setTarget_(controller)
        menu.addItem_(quit_)
        return menu

    apply_info(initial)

    def poll_worker():
        while not stop.is_set() and not _should_quit.is_set():
            try:
                updates.put(status())
            except Exception:
                pass
            stop.wait(POLL)

    threading.Thread(target=poll_worker, daemon=True).start()

    # Main-thread event pump: apply queued updates, then process AppKit
    # events. Each iteration gets its own autorelease pool so the AppKit
    # objects created per tick (NSDate/NSEvent/NSImage/NSMenu) are drained
    # instead of accumulating over days of runtime.
    while not stop.is_set() and not _should_quit.is_set():
        pool = NSAutoreleasePool.alloc().init()
        try:
            try:
                while True:
                    state["info"] = updates.get_nowait()
                    apply_info(state["info"])
            except queue.Empty:
                pass
            event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                0xFFFFFFFF, NSDate.dateWithTimeIntervalSinceNow_(0.2),
                NSDefaultRunLoopMode, True,
            )
            if event is not None:
                app.sendEvent_(event)
        finally:
            pool.drain()
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
