#!/usr/bin/env python3
"""Live test of the Wi-Fi, VPN, kill switch and power rules on the Pi.

    sudo python3 -I livetest.py [foreign-wifi] [ap] [ap-client]

Default: foreign-wifi and ap. ap-client needs another device that joins the AP.

Runs the installed pinetswitch for real (no dry run) with test configs that
point USB-C at a fake interface name and match no wired NIC, so a management session over
either keeps working while Wi-Fi, VPN, kill switch and power are driven.
The main service is stopped meanwhile; Wi-Fi, power and the service are
restored at the end.

Scenarios:
  foreign-wifi  `Loading` posing as the phone hotspot: joined only after the
                home-only window, then VPN up, kill switch on, low power,
                inbound firewall closes the Wi-Fi (phone set untrusted),
                tunnel works, traffic outside it is blocked, LAN still reachable
  ap-client     AP with a client joined from another device: no rescan for
                two rescan periods while it stays connected
  ap            nothing known in range: AP `Loading Berry` after the search,
                `rpi.local` answered by the AP's DNS, forwarding allowed for
                AP clients, AP rescan and back
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BIN = "/usr/local/bin/pinetswitch"
STATUS = Path("/run/pinetswitch/status.json")
POLICY0 = Path("/sys/devices/system/cpu/cpufreq/policy0")
BASE = {"usb": "none-usb0", "wired": [], "trusted": ["usb0", "eth0"], "interval": 1}  # keep management links open
results: list[tuple[bool, str]] = []


def sh(*cmd: str, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def check(ok: bool, what: str) -> bool:
    results.append((ok, what))
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    return ok


def status() -> dict:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}


def wait_for(pred, timeout: float, seen: list | None = None) -> dict | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = status()
        if st and seen is not None and (not seen or seen[-1] != st["situation"]):
            seen.append(st["situation"])
        if st and pred(st):
            return st
        time.sleep(0.5)
    return None


def toml(cfg: dict) -> str:
    return "".join(f"{k} = {json.dumps(v)}\n" for k, v in cfg.items())


class Daemon:
    def __init__(self, name: str, cfg: dict):
        self.cfgfile = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        self.cfgfile.write(toml({**BASE, **cfg}))
        self.cfgfile.close()
        self.log = open(f"/tmp/livetest-{name}.log", "w")
        STATUS.unlink(missing_ok=True)
        sh("nmcli", "device", "disconnect", "wlan0")
        self.t0 = time.monotonic()
        self.p = subprocess.Popen([BIN, "run", "--config", self.cfgfile.name],
                                  stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(20)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.log.close()
        os.unlink(self.cfgfile.name)


def inbound_trusted() -> str | None:
    """The trusted-interface line of the inbound firewall, None when it is not applied."""
    r = sh("nft", "list", "table", "inet", "pinetswitch_in")
    if r.returncode != 0:
        return None
    return next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("iifname {")), "")


def killswitch_on() -> bool:
    return sh("nft", "list", "table", "inet", "pinetswitch").returncode == 0


def max_freq() -> int:
    return int((POLICY0 / "scaling_max_freq").read_text())


def wg_rx() -> int:
    out = sh("wg", "show", "rpi5", "transfer").stdout.split()
    return int(out[1]) if len(out) >= 3 else 0


def foreign_wifi() -> None:
    d = Daemon("foreign-wifi", {"home_profile": "__no_home__", "phone_profile": "Loading", "trust_phone_inbound": False,
                                "home_search_secs": 3, "total_search_secs": 30})
    try:
        st = wait_for(lambda s: s["state"]["wifi_profile"] == "Loading"
                      and s["state"]["vpn_up"] and s["state"]["killswitch"], 60)
        if not check(st is not None, "foreign-wifi: joined 'Loading' as phone, VPN up, kill switch on"):
            return
        joined = time.monotonic() - d.t0
        check(joined >= 3, f"foreign-wifi: phone not joined inside the home-only window ({joined:.1f}s >= 3s)")
        check(st["situation"] == "roaming/phone", f"foreign-wifi: situation {st['situation']!r}")
        time.sleep(2)
        check(max_freq() == 1500000, f"foreign-wifi: low power (max_freq {max_freq()})")
        rx = wg_rx()
        r = sh("curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "https://www.google.com")
        check(r.stdout == "200" and wg_rx() > rx, f"foreign-wifi: internet through the tunnel (http {r.stdout}, wg rx grew)")
        r = sh("curl", "-s", "-m", "6", "--interface", "wlan0", "-o", "/dev/null", "http://1.1.1.1")
        check(r.returncode != 0, "foreign-wifi: traffic outside the tunnel blocked (curl --interface wlan0)")
        tr = inbound_trusted()
        check(tr is not None and '"wlan0"' not in tr and '"usb0"' in tr,
              f"foreign-wifi: inbound firewall closes wlan0, keeps USB-C open ({tr})")
        ns = [l.split()[1] for l in Path("/etc/resolv.conf").read_text().splitlines() if l.startswith("nameserver")]
        check(ns == ["10.1.0.1"], f"foreign-wifi: DNS only through the VPN (resolv.conf: {', '.join(ns)})")
        gw = json.loads(sh("ip", "-j", "route", "show", "default", "dev", "wlan0").stdout or "[{}]")[0].get("gateway")
        check(bool(gw) and sh("ping", "-c1", "-W2", gw).returncode == 0, f"foreign-wifi: local gateway {gw} reachable")
        if sh("ip", "-6", "route", "show", "default").stdout.strip():
            check(sh("curl", "-6", "-s", "-m", "6", "-o", "/dev/null", "https://ifconfig.co").returncode != 0,
                  "foreign-wifi: IPv6 outside the tunnel blocked")
    finally:
        d.stop()
        check(not killswitch_on() and inbound_trusted() is None, "foreign-wifi: clean stop lifted kill switch and inbound firewall")
        sh("nmcli", "connection", "down", "id", "Home VPN")


def access_point() -> None:
    d = Daemon("ap", {"home_profile": "__no_home__", "phone_profile": "__no_phone__",
                      "home_search_secs": 2, "total_search_secs": 6,
                      "ap_rescan_every": 25, "ap_rescan_secs": 5})
    try:
        seen: list = []
        st = wait_for(lambda s: s["state"]["wifi_mode"] == "ap", 45, seen)
        if not check(st is not None, "ap: 'Loading Berry' started"):
            return
        started = time.monotonic() - d.t0
        check(started >= 6, f"ap: started only after the search window ({started:.1f}s >= 6s)")
        info = sh("iw", "dev", "wlan0", "info").stdout
        check("type AP" in info and "ssid Loading Berry" in info, "ap: wlan0 is an AP with SSID 'Loading Berry'")
        check("10.43.0.1/24" in sh("ip", "-4", "-br", "addr", "show", "wlan0").stdout, "ap: Pi is 10.43.0.1")
        time.sleep(2)
        for name in ("rpi.local", "rpi"):
            ans = sh("dig", "+short", "+time=2", "@10.43.0.1", name, "A").stdout.strip()
            check(ans == "10.43.0.1", f"ap: DNS on the AP answers {name} = {ans or 'nothing'}")
        check(max_freq() == 1500000, "ap: low power")
        check(killswitch_on() and not st["state"]["vpn_up"], "ap: kill switch on, no VPN (no uplink)")
        tr = inbound_trusted() or ""
        check('"wlan0"' in tr, f"ap: inbound firewall trusts the AP ({tr})")
        fwd = ["-i", "wlan0", "-s", "10.43.0.0/24", "-j", "ACCEPT"]
        check(sh("iptables", "-C", "DOCKER-USER", *fwd).returncode == 0, "ap: AP clients may be forwarded (DOCKER-USER)")
        clients = status().get("state", {}).get("ap_clients", 0)
        if clients:
            print(f"note: {clients} AP client(s) connected, the rescan will be skipped", flush=True)
        else:
            seen.clear()
            back = wait_for(lambda s: any("ap-rescan" in x or "search" in x for x in seen)
                            and s["state"]["wifi_mode"] == "ap", 25 + 5 + 30, seen)
            check(back is not None, f"ap: rescanned and came back ({' -> '.join(dict.fromkeys(seen))})")
    finally:
        d.stop()
        check(not killswitch_on() and inbound_trusted() is None, "ap: clean stop lifted kill switch and inbound firewall")


def ap_client() -> None:
    """Needs another device that joins `Loading Berry` (e.g. the laptop) and stays."""
    every = 20
    d = Daemon("ap-client", {"home_profile": "__no_home__", "phone_profile": "__no_phone__",
                             "home_search_secs": 2, "total_search_secs": 6,
                             "ap_rescan_every": every, "ap_rescan_secs": 5})
    try:
        if not check(wait_for(lambda s: s["state"]["wifi_mode"] == "ap", 45) is not None, "ap-client: AP started"):
            return
        print("join 'Loading Berry' from another device now (90 s)", flush=True)
        if not check(wait_for(lambda s: s["state"]["ap_clients"] > 0, 90) is not None, "ap-client: a client joined"):
            return
        seen: list = []
        wait_for(lambda s: False, 2 * every + 5, seen)  # watch two rescan periods
        check(not any("rescan" in x or "search" in x for x in seen),
              f"ap-client: no rescan while a client is connected ({' -> '.join(dict.fromkeys(seen))})")
        check(status().get("state", {}).get("ap_clients", 0) > 0, "ap-client: client still connected")
    finally:
        d.stop()
        check(not killswitch_on() and inbound_trusted() is None, "ap-client: clean stop lifted kill switch and inbound firewall")


SCENARIOS = {"foreign-wifi": foreign_wifi, "ap": access_point, "ap-client": ap_client}


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 -I livetest.py")
    was_active = sh("systemctl", "is-active", "pinetswitch").stdout.strip() == "active"
    wifi_before = next((l.split(":", 1)[1] for l in sh("nmcli", "-t", "-f", "DEVICE,CONNECTION", "device").stdout.splitlines()
                        if l.startswith("wlan0:")), "")
    freq_before = max_freq()
    sh("systemctl", "stop", "pinetswitch")
    sh("nmcli", "radio", "wifi", "on")
    Path("/run/pinetswitch").mkdir(exist_ok=True)
    (Path("/run/pinetswitch") / "power-override").write_text("auto\n")
    try:
        for name in sys.argv[1:] or ["foreign-wifi", "ap"]:
            SCENARIOS[name]()
    finally:
        sh("nmcli", "device", "disconnect", "wlan0")
        if wifi_before:
            sh("nmcli", "-w", "20", "connection", "up", "id", wifi_before)
        for pol in Path("/sys/devices/system/cpu/cpufreq").glob("policy*"):
            (pol / "scaling_max_freq").write_text(str(freq_before))
        for rule in sh("iptables", "-S", "DOCKER-USER").stdout.splitlines():
            if BASE["usb"] in rule:  # forwarding rules the test daemon added for its fake USB name
                sh("iptables", *rule.replace("-A ", "-D ", 1).split())
        if was_active:
            sh("systemctl", "start", "pinetswitch")
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (": FAILED " + "; ".join(failed) if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
