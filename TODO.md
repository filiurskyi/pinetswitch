# TODO (handover, not committed)

Context for continuing with Claude Code on the Pi. Design, use cases and tests
are in README.md.

## Current state

- **Service:** `pinetswitch.service` is installed and running live.
  - Config: `/etc/pinetswitch.toml`. Binaries: `/usr/local/bin/pinetswitch` and `/usr/local/bin/pinetswitch-tray`.
- **Tests:** unit tests pass (36). `sudo python3 -I livetest.py` passes 18/18.
- **Tray:** runs from `~/.config/autostart`. Its menu sets `/run/pinetswitch/power-override`.
- **Commit style:** argos-git-workflow.
  - Subject-only conventional commits (`type(scope): …`), no body, no Co-Authored-By line.
  - Commit only when the user asks; never amend or rebase.
  - Remote: `git@github.com:filiurskyi/pinetswitch.git`.

## pinetswitch

1. [ ] **Tests that need the user:**
   - How: start it by hand while on USB-C (`sudo nmcli con up "Loading Berry"`; the usb-host rule leaves it alone), join from the Pad, VNC to `rpi.local`.
   - [ ] **Pad on `Loading Berry`**: it gets 10.43.0.x, `rpi.local` resolves, and VNC works.
   - [ ] **iPhone on a USB-A port** (needs USB-C unplugged and the Pi on its own power supply: usb-host takes priority over tethering; reach the Pi via its hotspot at 10.43.0.1): tap "Trust" on the iPhone.
     - Expect the `tether-ap` situation: the hotspot starts, the VPN comes up and the kill switch is on.
     - Check that hotspot clients get internet through the tunnel.
   - [ ] **Real iPhone Wi-Fi hotspot,** with `Loading` out of range or its Wi-Fi off: the Pi should join the phone at 5 s, with VPN and low power.
   - [x] **Rescan skipped while a client is on the hotspot:** `livetest.py ap-client` with the laptop as client, 5/5 passed.
   - [ ] **IPv6 blocked outside the tunnel:** only checkable on an uplink with an IPv6 default route.
   - [x] **Reboot over USB-C:** check that `usb0` comes up managed by itself. It relies on the udev rule and the fixed MACs in `/etc/modprobe.d/g_ether.conf`, which were applied at runtime but are untested on a fresh boot.
2. [ ] **Workstation / dock case** (new use case). USB-C goes to a dock with power, monitor, LAN and USB.
   - The Pi must then be a client there, not create the USB-C network.
   - The user still has to name the dock and test with it.
   - Hardware facts:
     - The Pi 5's USB-C port carries power plus USB 2.0 only (dwc2). There is no video over USB-C, so the monitor needs micro-HDMI unless the dock is DisplayLink.
     - The dock's LAN and USB ports need host mode on that port. That works with some docks only, at USB 2.0 speed.
   - Idea: USB-C is the power input, so replugging means a reboot, and the role is decided once per boot:
     1. Start in gadget mode.
     2. If no host enumerates within ~10 s (`/sys/class/udc/*/state` != `configured`), switch dwc2 to host mode at runtime.
     3. Treat the dock's NIC as a wired uplink: home check by router MAC, full power.
   - Currently `dtoverlay=dwc2` gives `dr_mode=otg`, and dwc2 is built into the kernel, not a module.
   - [x] Wired rules cover any NM ethernet NIC (`wired` globs, default `*`, minus `usb0` and the tether). Committed, installed, livetest 18/18.
   - [ ] Runtime gadget -> host switch of dwc2 (needs the dock).
3. [ ] **`rpi.lan` for Android at home:** add a DHCP reservation for the Pi in the GL.iNet router (its DNS serves `.lan`). `rpi.lan` currently points at a stale lease.
4. [ ] **Research:** sharing the iPhone hotspot's internet with all travel devices.

## Security (done 2026-11-03)

- [x] Inbound firewall (`inet pinetswitch_in`): untrusted networks get replies, DHCP, WireGuard (fixed port 51820) and IPv6 ND only; no mDNS announcements there.
- [x] SSH keys only (`/etc/ssh/sshd_config.d/10-keys-only.conf`), root login off. Termux on the tablet now needs a key.
- [x] Cockpit disabled.
- [x] Docker FORWARD DROP worked around for own networks (DOCKER-USER rules via housekeeping); VPN DNS exclusive (`ipv4.dns-priority -50`).
- [x] Boot window closed: `pinetswitch-boot-firewall.service` loads closed defaults before NetworkManager (verified on reboot: 3.9 s vs NM 5.2 s, daemon took over at 6.4 s).
- [x] Laptop: firewalld zones (default `public`, dock NIC + `Loading` = `home`, `rpi usb-c` = `trusted`), verified by probing from the Pi. Not yet seen on a real foreign network.

## Pi housekeeping

- [ ] **VNC discovery for AVNC:** now permanent (`/etc/avahi/services/rfb.service`, docker0 excluded), verified from the laptop over Wi-Fi. Left: check that AVNC on the Pad lists "rpi VNC".
  - Ask whether AVNC found it.
  - If yes, make it permanent with `/etc/avahi/services/rfb.service`, excluding `docker0`.
- [ ] **VNC resolution:** switchable from the tray's Screen submenu (PC / Tablet / Low-res). Now 1500x1000 (3:2 for the Pad), persisted in `~/.config/kanshi/config` and `config.init` with `mode --custom` (plain `mode` is rejected on the headless output). Backups: `*.bak`. Confirm it survives a reboot.
- [x] Bluetooth disabled (`sudo systemctl enable --now bluetooth` brings it back). PackageKit left alone: it only starts on demand and the panel's update notifier needs it.
- [x] USB-C + Wi-Fi: the usb-host rule now joins `Loading` when Wi-Fi is idle and home is in range (verified: rejoined 8 s after a disconnect).
- Power measured (PMIC rails, board only): idle full 1.82 W / low 1.66 W; 4-core load full 5.05 W @ 2.4 GHz / low 3.20 W @ 1.5 GHz.
- [ ] **Claude Code login:** it was copied from the laptop with `claude_profiles copy rpi` and shares the refresh token with the laptop. Consider a separate `claude auth login` on the Pi.

- [x] Password hash removed from `/boot/firmware/user-data` (boot partition is world-readable); `/root` Wi-Fi backups deleted.
- [x] `unattended-upgrades` on, Debian security only (`20auto-upgrades`, label=Debian line commented in `50unattended-upgrades`).

## Laptop side (not doable from the Pi)

- [x] Plain-text WireGuard key: `~/Downloads/rpi5.conf` deleted from the laptop (the config lives in the Pi's NM profile).
- [ ] **Expired logins:** `claude_profiles` profiles `datacollect_claude1` and `metalove` need a new login.
- [x] Duplicate GitHub repo `filiurskyi/claude_profiles` deleted.
