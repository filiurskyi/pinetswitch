# pinetswitch

Network and power policy for a Raspberry Pi 5: decides which link the Pi
should use (USB-C gadget, Ethernet, iPhone USB tethering, Wi-Fi client, Wi-Fi
access point) and which power profile to run, and tells NetworkManager and
cpufreq to do it.

Status: **live**. Use `--dry-run` (logs decisions, changes nothing) to try changes.

## Principles

- **NetworkManager does the networking.** pinetswitch only observes state and
  issues `nmcli` commands (up/down a profile, radio on/off, rescan). It never
  creates or deletes profiles.
- **pinetswitch is the only one that joins Wi-Fi.** All Wi-Fi profiles have
  `autoconnect=no`; other known networks are never joined automatically.
  Ethernet and USB-C keep NetworkManager's normal autoconnect.
- **Never cut the management path.** Ethernet and USB-C are never brought down
  by a rule.
- **Rules are ordered and independent.** Each rule looks at one state snapshot
  and either returns actions or passes. First match wins.
- **Dry-run first.** `--dry-run` logs decisions without acting.

## Use cases

| # | Situation | Network | Power |
|---|---|---|---|
| 1 | **Home, Ethernet** | Wi-Fi radio off. If Ethernet drops: radio on and join `Loading` immediately | full |
| 2 | **Home, Wi-Fi** | On `Loading`. When Ethernet comes up (and is home): Wi-Fi off, i.e. case 1 | full |
| 3 | **On the go, USB-C** (power + LAN from laptop/tablet) | `usb0` gadget, reachable as `rpi.local`. Wi-Fi left alone (joined manually) | low |
| 4 | **On the go, iPhone USB tethering** (iPhone on USB-A) | Internet via iPhone; start AP `Loading Berry` and share it | low |
| 5a | **On the go, Wi-Fi search 0–5 s** | Look for `Loading` only (are we home?) | — |
| 5b | **On the go, Wi-Fi search 5–15 s** | Look for `iPhone 12 Pro Max` hotspot, join it (internet via LTE) | low |
| 5c | **On the go, nothing found at 15 s** (wall power or powerbank) | Start AP `Loading Berry`; every 3 min drop it ~10 s to rescan for `Loading` / iPhone, skipped while clients are connected | low |
| 6 | **Ethernet, not home** | Wi-Fi follows the search (5a–5c) | full |

Precedence (first match wins): **3 USB-C → 4 tethering → 1 home Ethernet →
2 home Wi-Fi → 6 foreign Ethernet → 5 search**. USB-C wins over Ethernet.

**VPN applies on top of every case**: whenever the Pi's internet uplink is not
home, the WireGuard tunnel is up and enforced (see below).

## States

```
                     usb0 host attached
   (any) ------------------------------------------------> USB_HOST     wlan untouched, low power
                     iPhone USB tether up
   (any) ------------------------------------------------> TETHER_AP    AP shares tether, low power
                     wired uplink up + home
   (any) ------------------------------------------------> HOME_WIRED   radio off, full power

   none of the above:
   SEARCH_HOME (0-5 s) --Loading--> HOME_WIFI (full power)
        |
        v 5 s
   SEARCH_PHONE (5-15 s) --iPhone--> PHONE_WIFI (power ?)
        |
        v 15 s
   AP (Loading Berry, low power) --rescan finds Loading/iPhone?--> HOME_WIFI / PHONE_WIFI
```

Leaving `HOME_WIRED` (cable pulled) goes straight to `SEARCH_HOME` with the
radio switched back on.

## Power profiles

| Profile | Default | Mechanism (runtime, no reboot) |
|---|---|---|
| full | CPU up to 2.4 GHz, `ondemand` | cpufreq `scaling_max_freq` / governor |
| low | target ~8-12 W: CPU capped (1.5 GHz, the lowest cpufreq step), `powersave` | same, configurable |

Note: an idle Pi 5 draws ~3 W and ~8-10 W under full CPU load, so "low" mostly
limits the peaks. Cap values stay in config so they can be tuned after
measuring. Power source is visible as the USB-PD current limit
(`/proc/device-tree/chosen/power/max_current`: 5000 on the official 27 W
supply, usually 3000 or less on laptops and powerbanks), but wall vs powerbank
cannot be told apart reliably, so the policy keys on the link, not the supply.

## Inputs (state snapshot)

| Signal | Source |
|---|---|
| USB-C host attached | `usb0` carrier / NM device state |
| Wired uplink up | any connected NM ethernet device matching `wired` globs (default `*`), except `usb0` and the iPhone tether, e.g. `eth0` or a dock/USB NIC |
| Wired uplink is home | every connected wired uplink's default gateway has the home router MAC |
| iPhone tether up | `ipheth` interface present and connected (needs `usbmuxd`) |
| Wi-Fi radio on/off | `nmcli radio wifi` |
| Wi-Fi mode: off / client / AP | active NM connection on `wlan0` |
| Connected SSID | active NM connection on `wlan0` |
| Visible SSIDs | `nmcli dev wifi list` |
| AP clients connected | `iw dev wlan0 station dump` |
| Time in current state | internal timer |

## Actions

| Action | Command |
|---|---|
| Join Wi-Fi | `nmcli con up <profile>` |
| Start AP | `nmcli con up "Loading Berry"` |
| Stop AP / leave Wi-Fi | `nmcli dev disconnect wlan0` |
| Wi-Fi radio off / on | `nmcli radio wifi off/on` |
| Rescan | `nmcli dev wifi rescan` |
| VPN up / down | `nmcli con up/down rpi5` |
| Kill switch on / off | `nft` table `inet pinetswitch` |
| Power profile | write `/sys/devices/system/cpu/cpufreq/policy0/scaling_{max_freq,governor}` |

## Home detection

- **Wired** (`eth0`, a dock or USB NIC): the default gateway's MAC on each wired
  uplink matches the home router MAC from config (works with Wi-Fi off).
- **Wi-Fi**: connected to the `Loading` profile (WPA2-PSK, so a look-alike SSID
  without the password cannot pass).

## VPN (WireGuard)

- NM profile `rpi5` (imported from `rpi5.conf`, `autoconnect=no`): full tunnel
  (`0.0.0.0/0`) to the home router (`kn3d148.glddns.com:64894`), address
  `10.1.0.5`, DNS `10.1.0.1`. NM installs policy routing (fwmark table +
  `suppress_prefixlength 0`), so local subnets (LAN, iPhone hotspot, `usb0`,
  AP) stay reachable directly: SSH/VNC to the Pi keep working.
- **Auto**: pinetswitch brings `rpi5` up when the uplink is not home and down
  when it is home (the endpoint *is* the home router).
- **Enforced (kill switch)**: while not home, an nftables table owned by
  pinetswitch drops traffic leaving through the uplink unless it goes through
  the tunnel. Allowed outside the tunnel: WireGuard to the endpoint, DHCP, DNS
  to the uplink gateway (to resolve the endpoint), and local subnets. Traffic
  forwarded from AP / `usb0` clients is covered too: it leaves through the
  tunnel or not at all.
- Tunnel down on a foreign network = no internet, by design.

## Decisions

- Ethernet is home: gateway MAC check. Ethernet not home: Wi-Fi search, full power, VPN enforced.
- iPhone Wi-Fi hotspot (5b): low power.
- AP rescan: every 3 min for ~10 s, skipped while AP clients are connected.
- USB-C and Ethernet both up: USB-C wins.

## Open questions

- iPhone hotspot internet for all travel devices: to be researched.

## Tray widget

`pinetswitch-tray` puts an icon in the panel tray (`wf-panel-pi` hosts
StatusNotifierItems; the app uses `gir1.2-ayatanaappindicator3-0.1`):

- icon: power profile (performance / power saver / balanced when neither
  matches), warning icon when pinetswitch is not running
- menu: where the Pi is (`Home · Ethernet`, `On the go`, ...), a detail line
  (`Wi-Fi: … · VPN on · kill switch · power low`), and *Power: Auto / Full /
  Low*, which writes `/run/pinetswitch/power-override`
- *Screen* submenu for the headless VNC output: *PC (1920×1080)*, *Tablet
  (1500×1000, 3:2 like the Xiaomi Pad)*, *Low-res (1200×800)*. Applied at once
  with `wlr-randr --custom-mode` and saved to `~/.config/kanshi/config` (and
  `config.init`) as `mode --custom`, so it holds after a reboot

Starts with the desktop session via `~/.config/autostart/pinetswitch-tray.desktop`.

## Layout

```
pinetswitch.py            # snapshot -> Policy.plan (ordered rules) -> reconcile -> nmcli / nft / cpufreq
pinetswitch_tray.py       # tray icon, power and screen menu, installed as /usr/local/bin/pinetswitch-tray
pinetswitch-tray.desktop  # autostart entry for the tray
test_pinetswitch.py       # unit tests: policy on fake states, config file, tray labels
livetest.py               # live test on the Pi: Wi-Fi search, phone join, VPN, kill switch, AP, power
pinetswitch.toml          # config, installed as /etc/pinetswitch.toml
pinetswitch.service       # systemd unit (ExecStart has --dry-run until switched live)
```

Adding a rule: a `Policy` method `(State, now) -> Plan | None`, inserted into
`Policy.rules` at its priority, plus a test.

## Usage

```sh
pinetswitch status                 # what the daemon sees and wants (JSON)
pinetswitch power low|full|auto    # power override, no sudo (group netdev)
journalctl -u pinetswitch -f       # decisions and actions
pinetswitch run --config X.toml    # run with another config (used by livetest.py)
```

## Tests

```sh
python3 -m unittest test_pinetswitch   # anywhere, no hardware: policy, config, tray labels
sudo python3 -I livetest.py            # on the Pi, from a session over USB-C or Ethernet
```

`livetest.py` stops the service, runs pinetswitch for real with test configs
that point USB-C and Ethernet at fake interface names (so the management link
survives), and restores Wi-Fi, power and the service afterwards. It covers:
the home-only search window, joining the phone profile (played by `Loading`),
VPN up, kill switch on, tunnel works and bypass is blocked, LAN still
reachable, low power, the AP after the search window, `rpi.local` / `rpi`
answered by the AP's DNS, the AP rescan cycle, and the kill switch lifted on a
clean stop. Not covered: a real foreign network, iPhone USB tethering, skipping
the rescan while AP clients are connected, IPv6 (checked only when the uplink
has an IPv6 default route).

Runtime files: `/run/pinetswitch/status.json`, `/run/pinetswitch/power-override`.
A clean stop (`systemctl stop`) lifts the kill switch; a crash leaves it in place.

## Install

```sh
python3 -m unittest test_pinetswitch
sudo install -m 755 pinetswitch.py /usr/local/bin/pinetswitch
sudo install -m 644 pinetswitch.toml /etc/pinetswitch.toml
sudo install -m 644 pinetswitch.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pinetswitch
sudo install -m 755 pinetswitch_tray.py /usr/local/bin/pinetswitch-tray
install -D -m 644 pinetswitch-tray.desktop ~/.config/autostart/pinetswitch-tray.desktop
```

Prerequisites (done once): Wi-Fi profiles `Loading` and `iPhone 12 Pro Max`
with `autoconnect=no`; AP profile `Loading Berry` (mode ap, WPA2 CCMP, shared,
`10.43.0.1/24`, `autoconnect=no`); WireGuard profile `Home VPN` (interface
`rpi5`, `autoconnect=no`); cloud-init network config disabled; packages
`usbmuxd wireguard-tools iw nftables gir1.2-ayatanaappindicator3-0.1 bind9-dnsutils`. `/etc/NetworkManager/dnsmasq-shared.d/rpi-local.conf` makes the DNS of the
Pi's own networks (USB-C, AP) answer `rpi.local` / `rpi`, for Android clients
that cannot do mDNS. USB-C gadget: `/etc/udev/rules.d/86-usb-gadget-managed.rules`
(`ENV{DEVTYPE}=="gadget", ENV{NM_UNMANAGED}="0"`, NetworkManager ignores gadgets
by default) and `/etc/modprobe.d/g_ether.conf` with fixed `dev_addr` /
`host_addr` so the host sees the same device every boot.
