#!/usr/bin/env python3
"""pinetswitch-tray: panel icon showing the pinetswitch situation and power profile.

The menu shows where the Pi is connected, sets the power override
(Auto / Full / Low) by writing /run/pinetswitch/power-override, and switches
the screen size (VNC on the headless output) between PC and tablet, saved in
the kanshi config so it holds after a reboot.
describe(), current_mode() and kanshi_config() are pure, so they are
unit-tested without GTK (test_pinetswitch.py).
"""
import json
import os
import subprocess
import time
from pathlib import Path

STATUS_FILE = Path("/run/pinetswitch/status.json")
OVERRIDE_FILE = Path("/run/pinetswitch/power-override")
STALE_SECS = 15
KANSHI_DIR = Path.home() / ".config/kanshi"
SCREENS = [  # mode -> menu label
    ("1920x1080", "PC (1920×1080)"),
    ("1500x1000", "Tablet (1500×1000)"),  # 3:2 like the Xiaomi Pad
    ("1200x800", "Low-res (1200×800)"),  # 3:2 fallback for a slow link
]

ICONS = {
    "full": "power-profile-performance-symbolic",
    "low": "power-profile-power-saver-symbolic",
    None: "power-profile-balanced-symbolic",
    "error": "dialog-warning-symbolic",
}

PLACES = [  # situation prefix -> label; first match wins
    ("home-wired", "Home · Ethernet"),
    ("home-wifi", "Home · Wi-Fi"),
    ("usb-host", "USB-C to host"),
    ("tether-ap", "iPhone USB tether · hotspot"),
    ("foreign-wired", "Foreign Ethernet"),
    ("roaming", "On the go"),
]
WIFI = [  # situation suffix -> Wi-Fi label
    ("phone->home", "joining home"),
    ("phone", "iPhone hotspot"),
    ("ap-rescan", "hotspot, rescanning"),
    ("ap", "hotspot Loading Berry"),
    ("search->ap", "starting hotspot"),
    ("search", "searching"),
    ("other-wifi", "joined by hand"),
    ("connecting", "connecting"),
]


def describe(status: dict | None, now: float) -> dict:
    """Status JSON -> icon name, two menu lines and the current override."""
    if not status or now - status.get("time", 0) > STALE_SECS:
        return {"icon": ICONS["error"], "place": "pinetswitch not running", "detail": "", "override": "auto"}
    st = status["state"]
    sit = status["situation"]
    place = next((label for prefix, label in PLACES if sit.startswith(prefix)), sit)
    wifi = None
    if "/" in sit:
        sub = sit.split("/", 1)[1]
        wifi = next((label for prefix, label in WIFI if sub.startswith(prefix)), sub)
    elif not st["wifi_radio"]:
        wifi = "off"
    elif st["wifi_mode"] == "client":
        wifi = st["wifi_profile"]
    elif st["wifi_mode"] == "ap":
        wifi = "hotspot Loading Berry"
    else:
        wifi = "not connected"
    parts = [f"Wi-Fi: {wifi}", "VPN on" if st["vpn_up"] else "VPN off"]
    if st["killswitch"]:
        parts.append("kill switch")
    parts.append(f"power {st['power'] or '?'}")
    if status.get("dry_run"):
        parts.append("dry run")
    return {"icon": ICONS.get(st["power"], ICONS[None]), "place": place,
            "detail": " · ".join(parts), "override": st.get("override", "auto")}


def current_mode(outputs: list) -> tuple[str, str] | None:
    """`wlr-randr --json` -> (output name, "WxH") of the first enabled output."""
    for out in outputs:
        if out.get("enabled"):
            for m in out.get("modes", []):
                if m.get("current"):
                    return out["name"], f"{m['width']}x{m['height']}"
    return None


def kanshi_config(output: str, mode: str) -> str:
    # --custom: the headless output lists no modes, so a plain `mode` is rejected
    return (f"profile {{\n\t\toutput {output} enable scale 1.000000 mode --custom {mode}"
            " position 0,0 transform normal\n}\n")


def read_screen() -> tuple[str, str] | None:
    try:
        r = subprocess.run(["wlr-randr", "--json"], capture_output=True, text=True, timeout=5)
        return current_mode(json.loads(r.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def set_screen(output: str, mode: str) -> str | None:
    """Apply mode now and save it for kanshi (next login). Returns an error or None."""
    r = subprocess.run(["wlr-randr", "--output", output, "--custom-mode", mode],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip() or "wlr-randr failed"
    try:
        KANSHI_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("config", "config.init"):  # config.init: the Pi's screen tool keeps one too
            tmp = KANSHI_DIR / (name + ".tmp")
            tmp.write_text(kanshi_config(output, mode))
            os.replace(tmp, KANSHI_DIR / name)
    except OSError as e:
        return f"applied, not saved: {e.strerror}"
    return None


def read_status() -> dict | None:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return None


def main() -> None:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AI, GLib, Gtk

    ind = AI.Indicator.new("pinetswitch", ICONS[None], AI.IndicatorCategory.SYSTEM_SERVICES)
    ind.set_status(AI.IndicatorStatus.ACTIVE)
    ind.set_title("pinetswitch")

    menu = Gtk.Menu()
    place, detail = Gtk.MenuItem(label=""), Gtk.MenuItem(label="")
    place.set_sensitive(False)
    detail.set_sensitive(False)
    menu.append(place)
    menu.append(detail)
    menu.append(Gtk.SeparatorMenuItem())
    radios, group, updating = {}, None, [False]

    def on_mode(item, mode):
        if updating[0] or not item.get_active():
            return
        try:
            OVERRIDE_FILE.write_text(mode + "\n")
        except OSError as e:
            detail.set_label(f"cannot set power: {e.strerror}")

    for mode, label in (("auto", "Power: Auto"), ("full", "Power: Full"), ("low", "Power: Low")):
        item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
        group = item
        item.connect("toggled", on_mode, mode)
        radios[mode] = item
        menu.append(item)

    menu.append(Gtk.SeparatorMenuItem())
    screen_items, sgroup, screen = {}, None, [read_screen()]
    screen_menu = Gtk.Menu()
    screen_root = Gtk.MenuItem(label="Screen")
    screen_root.set_submenu(screen_menu)
    menu.append(screen_root)

    def on_screen(item, mode):
        if updating[0] or not item.get_active() or not screen[0] or screen[0][1] == mode:
            return
        if err := set_screen(screen[0][0], mode):
            detail.set_label(f"screen: {err}")
        screen[0] = read_screen()

    for mode, label in SCREENS:
        item = Gtk.RadioMenuItem.new_with_label_from_widget(sgroup, label)
        sgroup = item
        item.connect("toggled", on_screen, mode)
        screen_items[mode] = item
        screen_menu.append(item)
    menu.show_all()
    ind.set_menu(menu)

    def refresh():
        d = describe(read_status(), time.time())
        ind.set_icon_full(d["icon"], d["place"])
        place.set_label(d["place"])
        detail.set_label(d["detail"])
        detail.set_visible(bool(d["detail"]))
        updating[0] = True
        radios.get(d["override"], radios["auto"]).set_active(True)
        if screen[0] and screen[0][1] in screen_items:
            screen_items[screen[0][1]].set_active(True)
        updating[0] = False
        return True

    def refresh_screen():  # catches changes made outside the tray
        screen[0] = read_screen()
        return True

    refresh()
    GLib.timeout_add_seconds(2, refresh)
    GLib.timeout_add_seconds(10, refresh_screen)
    Gtk.main()


if __name__ == "__main__":
    main()
