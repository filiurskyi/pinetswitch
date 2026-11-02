# pinetswitch

Network policy for a Raspberry Pi 5: decides which link the Pi should use
(USB-C gadget, Ethernet, Wi-Fi client, Wi-Fi access point) and tells
NetworkManager to do it.

Status: **design draft** — use cases and the state machine are being refined
here before any logic is written.

## Principles

- **NetworkManager does the networking.** pinetswitch only observes state and
  issues `nmcli` commands (up/down a profile, radio on/off, rescan). It never
  creates or deletes profiles.
- **Never cut the management path.** Ethernet and USB-C are never brought down
  by a rule.
- **Rules are ordered and independent.** Each rule looks at one state snapshot
  and either returns an action or passes. First match wins.
- **Dry-run first.** `--dry-run` logs decisions without acting.

## Inputs (state snapshot)

| Signal | Source |
|---|---|
| USB-C gadget link up (host attached) | `usb0` carrier |
| Ethernet link up | `eth0` carrier / NM device state |
| Ethernet is home | TBD (gateway MAC? subnet?) |
| Wi-Fi radio on/off | `nmcli radio wifi` |
| Wi-Fi mode: off / client / AP | active NM connection on `wlan0` |
| Connected SSID | active NM connection on `wlan0` |
| Visible SSIDs | `nmcli dev wifi list` |
| AP clients connected | TBD (`iw dev wlan0 station dump`) |
| Time in current state | internal timer |

## Actions

| Action | nmcli |
|---|---|
| Connect to known Wi-Fi | `nmcli con up <profile>` |
| Start AP | `nmcli con up "Loading Berry"` |
| Stop AP / disconnect Wi-Fi | `nmcli dev disconnect wlan0` |
| Wi-Fi radio off / on | `nmcli radio wifi off/on` |
| Rescan | `nmcli dev wifi rescan` |

## Use cases (draft)

| # | Situation | Wanted behaviour |
|---|---|---|
| 1 | LAN on USB-C | Reachable as `rpi.local` over USB-C; Wi-Fi stays a client to any known network |
| 2 | LAN on Ethernet, at home (`Loading`) | Wi-Fi off |
| 3 | No LAN, `Loading` visible | Connect to `Loading` immediately |
| 4 | No LAN, no known network for 15 s | Start AP `Loading Berry` (WPA2, password in config) |
| … | *more to be added* | |

## States (draft)

```
          +--------------+
          |    BOOT      |
          +------+-------+
                 v
  +------------------------------+
  |  WIFI_CLIENT_SEARCHING       |---- known SSID found ---> WIFI_CLIENT
  +------------------------------+
        | 15 s, nothing                       ^
        v                                     | periodic rescan finds known SSID
  +--------------+                            |
  |   AP         |----------------------------+
  +--------------+

  WIRED_HOME  (eth0 at home) : Wi-Fi radio off; leaving it -> WIFI_CLIENT_SEARCHING
  USB_HOST    (usb0 up)      : Wi-Fi client only, never AP
```

## Open questions

- How is "Ethernet is home" detected once Wi-Fi is off (can't see `Loading`)?
- Ethernet but *not* home: Wi-Fi client, off, or AP?
- USB-C + no known Wi-Fi: stay disconnected, or AP?
- While AP is up: how often to drop it and rescan; skip while clients are connected?
- Precedence when USB-C and Ethernet are both up.

## Layout

```
pinetswitch.py        # state snapshot -> ordered rules -> nmcli actions
pinetswitch.service   # systemd unit
```

## Install (later)

```sh
sudo install -m 755 pinetswitch.py /usr/local/bin/pinetswitch
sudo install -m 644 pinetswitch.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pinetswitch
```
