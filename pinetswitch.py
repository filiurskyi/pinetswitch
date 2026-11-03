#!/usr/bin/env python3
"""pinetswitch: network and power policy for the Pi, executed through NetworkManager.

Each tick: System.snapshot() reads the system into a State, Policy.plan() picks
the first matching rule and returns a Plan (what Wi-Fi and power should be),
reconcile() turns the difference between State and Plan into Actions (VPN and
kill switch included), and System.execute() runs them. Only System touches the
machine, so the policy is unit-testable with fake states (test_pinetswitch.py).
"""
import argparse
import fnmatch
import grp
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("pinetswitch")

CONFIG_PATH = Path("/etc/pinetswitch.toml")
RUN_DIR = Path("/run/pinetswitch")
OVERRIDE_FILE = RUN_DIR / "power-override"  # auto | full | low, written by the user / tray
STATUS_FILE = RUN_DIR / "status.json"
CPUFREQ = Path("/sys/devices/system/cpu/cpufreq")
NFT_TABLE = "pinetswitch"
NFT_INBOUND = "pinetswitch_in"


# --- config ------------------------------------------------------------------

@dataclass
class Config:
    wired: list = field(default_factory=lambda: ["*"])  # glob patterns over NM ethernet devices
    usb: str = "usb0"
    wifi: str = "wlan0"
    tether_driver: str = "ipheth"
    home_profile: str = "Loading"
    home_router_mac: str = ""
    phone_profile: str = "iPhone 12 Pro Max"
    ap_profile: str = "Loading Berry"
    home_search_secs: float = 5
    total_search_secs: float = 15
    ap_rescan_every: float = 180
    ap_rescan_secs: float = 10
    vpn_profile: str = "Home VPN"
    vpn_iface: str = "rpi5"
    vpn_endpoint_port: int = 64894
    vpn_enforce: bool = True
    trust_phone_inbound: bool = True  # accept connections from devices on the phone hotspot
    trusted: list = field(default_factory=list)  # interfaces always trusted for inbound
    vpn_listen_port: int = 51820  # fixed WireGuard port, the only one open on untrusted networks
    power: dict = field(default_factory=lambda: {
        "full": {"max_freq": 2400000, "governor": "ondemand"},
        "low": {"max_freq": 1500000, "governor": "ondemand"},
    })
    usb_subnet: str = "10.42.0.0/24"  # NetworkManager shared network on USB-C
    ap_subnet: str = "10.43.0.0/24"
    retry_secs: float = 30
    housekeeping_secs: float = 30
    interval: float = 2


def load_config(path: Path = CONFIG_PATH) -> Config:
    cfg = Config()
    if path.exists():
        for k, v in tomllib.loads(path.read_text()).items():
            if not hasattr(cfg, k):
                raise SystemExit(f"{path}: unknown key {k!r}")
            setattr(cfg, k, v)
    cfg.home_router_mac = cfg.home_router_mac.lower()
    return cfg


# --- state, plan, actions ----------------------------------------------------

@dataclass
class State:
    usb_up: bool = False
    wired_up: bool = False  # any wired uplink (eth0, a dock or USB NIC) connected
    wired_home: bool = False  # ... and every connected one is behind the home router
    tether_up: bool = False
    wired_ifaces: tuple = ()  # connected wired uplinks
    tether_ifaces: tuple = ()
    wifi_radio: bool = True
    wifi_mode: str = "off"  # off | connecting | client | ap
    wifi_profile: str | None = None  # active NM profile on the Wi-Fi device
    visible: frozenset = frozenset()  # visible SSIDs
    ap_clients: int = 0
    vpn_up: bool = False
    killswitch: bool = False
    inbound: tuple | None = None  # trusted interfaces the inbound firewall allows, None = not applied
    power: str | None = None  # full | low | None (matches neither profile)
    override: str = "auto"


# Wi-Fi targets; joining a client network is "join:<profile>"
OFF, LEAVE, SCAN, AP, DISCONNECT = "off", "leave", "scan", "ap", "disconnect"


def JOIN(profile: str) -> str:
    return "join:" + profile


@dataclass
class Plan:
    situation: str
    wifi: str
    power: str | None  # full | low | None = keep current


@dataclass
class Action:
    kind: str  # wifi | power | vpn | killswitch | inbound
    detail: str
    profile: str | None = None  # NM profile being brought up, for failure backoff


# --- policy ------------------------------------------------------------------

class Policy:
    """Ordered rules plus the Wi-Fi search timers. First rule that matches wins.

    To add a rule: write a method (State, now) -> Plan | None and put it in
    self.rules at the right priority.
    """

    def __init__(self, cfg: Config, ssids: dict[str, str]):
        self.cfg = cfg
        self.ssids = ssids  # profile -> SSID
        self.search_since: float | None = None
        self.rescanning = False  # search started by an AP rescan (short window)
        self.ap_since: float | None = None
        self.failed: dict[str, float] = {}  # profile -> time of last failed activation
        self.rules = [self.usb_host, self.tether, self.home_wired, self.home_wifi,
                      self.foreign_wired, self.roaming]

    def plan(self, s: State, now: float) -> Plan:
        for rule in self.rules:
            if (p := rule(s, now)) is not None:
                if rule not in (self.foreign_wired, self.roaming):
                    self.reset_search()
                if s.wifi_mode != AP:
                    self.ap_since = None
                return p
        raise AssertionError("roaming always matches")

    def mark_failed(self, profile: str, now: float) -> None:
        self.failed[profile] = now

    def recently_failed(self, profile: str, now: float) -> bool:
        t = self.failed.get(profile)
        return t is not None and now - t < self.cfg.retry_secs

    def reset_search(self) -> None:
        self.search_since, self.rescanning = None, False

    # rules, in priority order

    def usb_host(self, s, now):
        if s.usb_up:
            return Plan("usb-host", LEAVE, "low")

    def tether(self, s, now):
        if s.tether_up:
            return Plan("tether-ap", AP, "low")

    def home_wired(self, s, now):
        if s.wired_up and s.wired_home:
            return Plan("home-wired", OFF, "full")

    def home_wifi(self, s, now):
        if s.wifi_mode == "client" and s.wifi_profile == self.cfg.home_profile:
            return Plan("home-wifi", LEAVE, "full")

    def foreign_wired(self, s, now):
        if s.wired_up:
            sub, wifi, _ = self.search(s, now)
            return Plan("foreign-wired/" + sub, wifi, "full")

    def roaming(self, s, now):
        sub, wifi, power = self.search(s, now)
        return Plan("roaming/" + sub, wifi, power)

    # Wi-Fi search: home only for the first seconds, then the phone too, then AP

    def joinable(self, profile: str, s: State, now: float) -> bool:
        return self.ssids.get(profile) in s.visible and not self.recently_failed(profile, now)

    def search(self, s: State, now: float) -> tuple[str, str, str | None]:
        cfg = self.cfg
        home_ok = self.joinable(cfg.home_profile, s, now)
        phone_ok = self.joinable(cfg.phone_profile, s, now)

        if s.wifi_mode == "client" and s.wifi_profile == cfg.phone_profile:
            self.reset_search()
            if home_ok:
                return "phone->home", JOIN(cfg.home_profile), "low"
            return "phone", LEAVE, "low"

        if s.wifi_mode == AP:
            self.reset_search()
            if self.ap_since is None:
                self.ap_since = now
            if now - self.ap_since >= cfg.ap_rescan_every:
                if s.ap_clients:
                    self.ap_since = now  # someone is connected: skip this rescan
                else:
                    self.search_since, self.rescanning = now, True
                    return "ap-rescan", DISCONNECT, "low"
            return "ap", LEAVE, "low"

        if s.wifi_mode == "client":  # another network, joined by hand
            self.reset_search()
            return "other-wifi", LEAVE, "low"

        if s.wifi_mode == "connecting":
            return "connecting", LEAVE, None

        if self.search_since is None:
            self.search_since = now
        t = now - self.search_since
        if home_ok:
            return "search", JOIN(cfg.home_profile), None
        if phone_ok and (self.rescanning or t >= cfg.home_search_secs):
            return "search", JOIN(cfg.phone_profile), None
        if t >= (cfg.ap_rescan_secs if self.rescanning else cfg.total_search_secs):
            self.reset_search()
            return "search->ap", AP, "low"
        return f"search {t:.0f}s", SCAN, None


# --- reconcile: plan vs state -> actions ---------------------------------------

def is_home(s: State, cfg: Config) -> bool:
    """True only when there is an uplink and every uplink is home."""
    uplinks = []
    if s.wired_up:
        uplinks.append(s.wired_home)
    if s.wifi_mode == "client":
        uplinks.append(s.wifi_profile == cfg.home_profile)
    if s.tether_up:
        uplinks.append(False)
    return bool(uplinks) and all(uplinks)


def has_foreign_uplink(s: State, cfg: Config) -> bool:
    return ((s.wired_up and not s.wired_home)
            or (s.wifi_mode == "client" and s.wifi_profile != cfg.home_profile)
            or s.tether_up)


def reconcile(s: State, p: Plan, cfg: Config, policy: Policy, now: float) -> list[Action]:
    acts: list[Action] = []

    def up(kind, profile):
        if not policy.recently_failed(profile, now):
            acts.append(Action(kind, "up", profile))

    w = p.wifi
    if w == OFF:
        if s.wifi_radio:
            acts.append(Action("wifi", "radio off"))
    else:
        if not s.wifi_radio:
            acts.append(Action("wifi", "radio on"))
        if w == SCAN:
            acts.append(Action("wifi", "rescan"))
        elif w == DISCONNECT and s.wifi_mode != "off":
            acts.append(Action("wifi", "disconnect"))
        elif w == AP and s.wifi_profile != cfg.ap_profile:
            up("wifi", cfg.ap_profile)
        elif w.startswith("join:") and s.wifi_profile != w[5:]:
            up("wifi", w[5:])

    power = p.power if s.override == "auto" else s.override
    if power and s.power != power:
        acts.append(Action("power", power))

    want_vpn = has_foreign_uplink(s, cfg)
    if want_vpn and not s.vpn_up:
        up("vpn", cfg.vpn_profile)
    elif not want_vpn and s.vpn_up:
        acts.append(Action("vpn", "down", cfg.vpn_profile))

    # fail closed: block non-VPN internet unless every uplink is known to be home
    want_ks = cfg.vpn_enforce and not is_home(s, cfg)
    if want_ks != s.killswitch:
        acts.append(Action("killswitch", "on" if want_ks else "off"))

    trusted = trusted_ifaces(s, cfg)
    if s.inbound != trusted:
        acts.append(Action("inbound", "trust " + ",".join(trusted)))
    return acts


def trusted_ifaces(s: State, cfg: Config) -> tuple:
    """Interfaces that may open connections to the Pi; everything else gets replies only.

    Trusted: loopback, USB-C, the VPN, Docker, the iPhone USB tether, the Pi's
    own AP, home uplinks, and the phone hotspot (cfg.trust_phone_inbound).
    """
    t = {"lo", cfg.usb, cfg.vpn_iface, "docker0", *s.tether_ifaces, *cfg.trusted}
    if s.wired_home:
        t.update(s.wired_ifaces)
    profiles = {cfg.home_profile} | ({cfg.phone_profile} if cfg.trust_phone_inbound else set())
    if s.wifi_mode == AP or (s.wifi_mode == "client" and s.wifi_profile in profiles):
        t.add(cfg.wifi)
    return tuple(sorted(t))


def boot_rulesets(cfg: Config) -> str:
    """Closed defaults loaded before the network comes up (pinetswitch-boot-firewall.service).

    Inbound: only the always-trusted interfaces (loopback, USB-C, VPN, Docker,
    cfg.trusted). Outbound: the kill switch when enforced, since home is not
    known yet. The daemon replaces both on its first ticks.
    """
    rules = inbound_ruleset(trusted_ifaces(State(), cfg), cfg)
    if cfg.vpn_enforce:
        rules += f"table inet {NFT_TABLE}\ndelete table inet {NFT_TABLE}\n" + killswitch_ruleset(cfg)
    return rules


def inbound_ruleset(trusted: tuple, cfg: Config) -> str:
    """Untrusted interfaces get replies, DHCP, WireGuard and IPv6 neighbour discovery
    only, and the Pi does not announce itself there over mDNS."""
    ifs = ", ".join(f'"{i}"' for i in trusted)
    return f"""
table inet {NFT_INBOUND}
delete table inet {NFT_INBOUND}
table inet {NFT_INBOUND} {{
  chain input {{
    type filter hook input priority 0; policy accept;
    ct state established,related accept
    iifname {{ {ifs} }} accept
    iifname "br-*" accept
    udp dport {{ 68, 546, {cfg.vpn_listen_port} }} accept
    icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, packet-too-big }} accept
    counter drop
  }}
  chain output {{
    type filter hook output priority 0; policy accept;
    udp dport 5353 oifname != {{ {ifs} }} drop
  }}
}}
"""


def killswitch_ruleset(cfg: Config) -> str:
    """Allowed outside the tunnel: the tunnel itself, DNS/DHCP, local and link-local nets."""
    return f"""
table inet {NFT_TABLE} {{
  chain guard {{
    oifname {{ "lo", "{cfg.vpn_iface}" }} accept
    udp dport {{ 53, 67, 68, {cfg.vpn_endpoint_port} }} accept
    tcp dport 53 accept
    ip daddr {{ 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 224.0.0.0/4, 255.255.255.255 }} accept
    ip6 daddr {{ fe80::/10, fc00::/7, ff00::/8 }} accept
    counter reject
  }}
  chain output {{ type filter hook output priority 0; policy accept; jump guard; }}
  chain forward {{ type filter hook forward priority 0; policy accept; jump guard; }}
}}
"""


# --- system access -------------------------------------------------------------

def run(*cmd: str, input: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def nmcli(*args: str) -> str:
    return run("nmcli", *args).stdout


def split_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped ':' and unescape '\\:' and '\\\\'."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def carrier(dev: str) -> bool:
    try:
        return Path(f"/sys/class/net/{dev}/carrier").read_text().strip() == "1"
    except OSError:
        return False


def driver(dev: str) -> str:
    try:
        return os.path.basename(os.readlink(f"/sys/class/net/{dev}/device/driver"))
    except OSError:
        return ""


def read_override() -> str:
    try:
        v = OVERRIDE_FILE.read_text().strip()
    except OSError:
        return "auto"
    return v if v in ("auto", "full", "low") else "auto"


class System:
    def __init__(self, cfg: Config, dry_run: bool):
        self.cfg, self.dry_run = cfg, dry_run
        self.gw_mac: dict[tuple[str, str], str] = {}  # (wired dev, gateway IP) -> MAC, cached while up
        self.last_rescan = 0.0
        self.inbound: tuple | None = None  # trusted interfaces last applied

    def ssids(self) -> dict[str, str]:
        cfg = self.cfg
        return {p: nmcli("-g", "802-11-wireless.ssid", "connection", "show", p).strip()
                for p in (cfg.home_profile, cfg.phone_profile, cfg.ap_profile)}

    # snapshot

    def snapshot(self) -> State:
        cfg = self.cfg
        devs = {}
        for line in nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device").splitlines():
            f = split_terse(line)
            if len(f) == 4:
                devs[f[0]] = (f[1], f[2], f[3])

        def connected(dev):
            return dev in devs and devs[dev][1].startswith("connected")

        s = State()
        s.usb_up = connected(cfg.usb) and carrier(cfg.usb)
        tethers = {d for d in devs if driver(d) == cfg.tether_driver}
        s.tether_up = any(connected(d) for d in tethers)
        s.tether_ifaces = tuple(sorted(d for d in tethers if connected(d)))
        wired = [d for d, (typ, _, _) in devs.items()
                 if typ == "ethernet" and d != cfg.usb and d not in tethers and connected(d)
                 and any(fnmatch.fnmatch(d, pat) for pat in cfg.wired)]
        s.wired_up = bool(wired)
        s.wired_ifaces = tuple(sorted(wired))
        s.wired_home = s.wired_up and all(self.wired_is_home(d) for d in wired)
        self.gw_mac = {k: v for k, v in self.gw_mac.items() if k[0] in wired}
        s.wifi_radio = nmcli("radio", "wifi").strip() == "enabled"
        if cfg.wifi in devs:
            _, st, con = devs[cfg.wifi]
            if st.startswith("connected"):
                s.wifi_mode = AP if con == cfg.ap_profile else "client"
                s.wifi_profile = con
            elif st.startswith("connecting"):
                s.wifi_mode, s.wifi_profile = "connecting", con or None
        ssids = nmcli("-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "no")
        s.visible = frozenset(x for x in (split_terse(l)[0] for l in ssids.splitlines()) if x)
        if s.wifi_mode == AP:
            s.ap_clients = run("iw", "dev", cfg.wifi, "station", "dump").stdout.count("Station ")
        s.vpn_up = connected(cfg.vpn_iface)
        s.killswitch = run("nft", "list", "table", "inet", NFT_TABLE).returncode == 0
        if run("nft", "list", "table", "inet", NFT_INBOUND).returncode != 0:
            self.inbound = None
        s.inbound = self.inbound
        s.power = self.read_power()
        s.override = read_override()
        return s

    def wired_is_home(self, dev: str) -> bool:
        """Home = the default gateway on dev has the home router's MAC."""
        cfg = self.cfg
        if not cfg.home_router_mac:
            return False
        routes = json.loads(run("ip", "-j", "route", "show", "default", "dev", dev).stdout or "[]")
        gw = next((r["gateway"] for r in routes if "gateway" in r), None)
        if not gw:
            return False
        if (dev, gw) not in self.gw_mac:
            mac = None
            for attempt in range(2):
                neigh = json.loads(run("ip", "-j", "neigh", "show", gw, "dev", dev).stdout or "[]")
                mac = next((n["lladdr"] for n in neigh if "lladdr" in n), None)
                if mac or attempt:
                    break
                run("ping", "-c1", "-W1", "-I", dev, gw)  # fill the ARP cache
            if not mac:
                return False
            self.gw_mac[(dev, gw)] = mac.lower()
        return self.gw_mac[(dev, gw)] == cfg.home_router_mac

    def read_power(self) -> str | None:
        p0 = CPUFREQ / "policy0"
        try:
            cur = (int((p0 / "scaling_max_freq").read_text()), (p0 / "scaling_governor").read_text().strip())
        except OSError:
            return None
        for name, prof in self.cfg.power.items():
            if cur == (prof["max_freq"], prof["governor"]):
                return name
        return None

    # execute

    def execute(self, a: Action) -> bool:
        if self.dry_run:
            return True
        cfg = self.cfg
        if a.kind == "power":
            return self.set_power(a.detail)
        if a.kind == "wifi" and a.detail == "rescan":
            if time.monotonic() - self.last_rescan >= 3:
                self.last_rescan = time.monotonic()
                run("nmcli", "device", "wifi", "rescan", "ifname", cfg.wifi)  # NM may refuse; harmless
            return True
        if a.kind == "wifi" and a.detail.startswith("radio"):
            r = run("nmcli", "radio", "wifi", a.detail.split()[1])
        elif a.kind == "wifi" and a.detail == "disconnect":
            r = run("nmcli", "device", "disconnect", cfg.wifi)
        elif a.kind in ("wifi", "vpn"):
            r = run("nmcli", "-w", "15", "connection", a.detail, "id", a.profile)
        elif a.kind == "killswitch" and a.detail == "on":
            r = run("nft", "-f", "-", input=killswitch_ruleset(cfg))
        elif a.kind == "killswitch":
            r = run("nft", "delete", "table", "inet", NFT_TABLE)
        elif a.kind == "inbound" and a.detail.startswith("trust "):
            trusted = tuple(a.detail[6:].split(","))
            r = run("nft", "-f", "-", input=inbound_ruleset(trusted, cfg))
            if r.returncode == 0:
                self.inbound = trusted
        elif a.kind == "inbound":
            r = run("nft", "delete", "table", "inet", NFT_INBOUND)
            self.inbound = None
        else:
            raise ValueError(f"unknown action {a}")
        if r.returncode != 0:
            log.warning("%s %s failed: %s", a.kind, a.detail, (r.stderr or r.stdout).strip())
        return r.returncode == 0

    def set_power(self, name: str) -> bool:
        prof = self.cfg.power[name]
        try:
            for pol in CPUFREQ.glob("policy*"):
                (pol / "scaling_governor").write_text(prof["governor"])
                (pol / "scaling_max_freq").write_text(str(prof["max_freq"]))
        except OSError as e:
            log.warning("power %s failed: %s", name, e)
            return False
        return True

    # housekeeping: invariants that NetworkManager or Docker can undo, checked periodically

    def housekeeping(self) -> None:
        verb = "would" if self.dry_run else "do"
        listing = nmcli("-t", "-f", "UUID,TYPE,AUTOCONNECT,NAME", "connection")
        for uuid, name in wifi_autoconnect_on(listing):
            log.info("%s autoconnect off for Wi-Fi profile %r", verb, name)
            if not self.dry_run:
                run("nmcli", "connection", "modify", uuid, "connection.autoconnect", "no")
        if run("iptables", "-n", "-L", "DOCKER-USER").returncode != 0:
            return  # no Docker chain: nothing drops forwarded traffic
        for rule in forward_rules(self.cfg):
            if run("iptables", "-C", "DOCKER-USER", *rule).returncode != 0:
                log.info("%s allow forwarding %s", verb, " ".join(rule))
                if not self.dry_run:
                    run("iptables", "-I", "DOCKER-USER", *rule)


def wifi_autoconnect_on(listing: str) -> list[tuple[str, str]]:
    """(uuid, name) of Wi-Fi profiles with autoconnect on, from `nmcli -t -f UUID,TYPE,AUTOCONNECT,NAME c`."""
    found = []
    for line in listing.splitlines():
        f = split_terse(line)
        if len(f) == 4 and f[1] == "802-11-wireless" and f[2] == "yes":
            found.append((f[0], f[3]))
    return found


def forward_rules(cfg: Config) -> list[list[str]]:
    """DOCKER-USER rules that let clients of the Pi's own networks be routed.

    Docker sets the FORWARD policy to DROP, which would cut USB-C and AP clients
    off from the uplink. Only their own subnets are let through, so hosts on a
    foreign uplink still cannot route through the Pi.
    """
    rules = []
    for iface, net in ((cfg.usb, cfg.usb_subnet), (cfg.wifi, cfg.ap_subnet)):
        rules.append(["-i", iface, "-s", net, "-j", "ACCEPT"])
        rules.append(["-o", iface, "-d", net, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
    return rules


# --- daemon --------------------------------------------------------------------

def setup_run_dir() -> None:
    """/run/pinetswitch: root-owned, group netdev may write the power override."""
    RUN_DIR.mkdir(exist_ok=True)
    try:
        os.chown(RUN_DIR, 0, grp.getgrnam("netdev").gr_gid)
        os.chmod(RUN_DIR, 0o2775)
    except (KeyError, PermissionError):
        pass


def watch_nm(wake: threading.Event) -> None:
    """Wake the loop on any NetworkManager event instead of waiting for the interval."""
    while True:
        try:
            with subprocess.Popen(["nmcli", "monitor"], stdout=subprocess.PIPE, text=True) as p:
                for _ in p.stdout:
                    wake.set()
        except OSError:
            pass
        time.sleep(5)


def write_status(s: State, p: Plan, dry_run: bool) -> None:
    st = asdict(s)
    st["visible"] = sorted(s.visible)
    data = {"situation": p.situation, "wifi_target": p.wifi, "power_target": p.power,
            "dry_run": dry_run, "state": st, "time": time.time()}
    try:
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n")
        os.chmod(tmp, 0o644)
        tmp.replace(STATUS_FILE)
    except OSError:
        pass


def daemon(cfg: Config, dry_run: bool) -> None:
    sysm = System(cfg, dry_run)
    policy = Policy(cfg, sysm.ssids())
    setup_run_dir()
    wake, stop = threading.Event(), threading.Event()
    threading.Thread(target=watch_nm, args=(wake,), daemon=True).start()

    def on_term(*_):
        stop.set()
        wake.set()
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    log.info("started%s", " (dry run)" if dry_run else "")
    last_situation, last_shown, last_housekeeping = None, None, None
    while not stop.is_set():
        now = time.monotonic()
        if last_housekeeping is None or now - last_housekeeping >= cfg.housekeeping_secs:
            sysm.housekeeping()
            last_housekeeping = now
        s = sysm.snapshot()
        p = policy.plan(s, now)
        acts = reconcile(s, p, cfg, policy, now)
        situation = p.situation.split(" ")[0]  # ignore the seconds counter of "search Ns"
        if situation != last_situation:
            log.info("situation: %s", p.situation)
            last_situation = situation
        shown = [a for a in acts if a.detail != "rescan"]
        if shown != last_shown or not dry_run:  # dry run would repeat the same actions every tick
            for a in shown:
                log.info("%s %s %s%s", "would" if dry_run else "do", a.kind, a.detail,
                         f" {a.profile!r}" if a.profile else "")
        last_shown = shown
        for a in acts:
            if not sysm.execute(a) and a.profile:
                policy.mark_failed(a.profile, now)
        write_status(s, p, dry_run)
        wake.wait(cfg.interval)
        wake.clear()

    if not dry_run:  # a clean stop lifts both firewalls; a crash keeps them
        s = sysm.snapshot()
        if s.killswitch:
            sysm.execute(Action("killswitch", "off"))
        if s.inbound is not None:
            sysm.execute(Action("inbound", "off"))
    log.info("stopped")


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(prog="pinetswitch", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the policy loop (systemd)")
    r.add_argument("--dry-run", action="store_true", help="log decisions, change nothing")
    r.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"default {CONFIG_PATH}")
    sub.add_parser("status", help="show what the running daemon sees and wants")
    bf = sub.add_parser("boot-firewall", help="load closed firewall defaults (early boot)")
    bf.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"default {CONFIG_PATH}")
    pw = sub.add_parser("power", help="override the power profile")
    pw.add_argument("mode", choices=["auto", "full", "low"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "run":
        daemon(load_config(args.config), args.dry_run)
    elif args.cmd == "boot-firewall":
        r = run("nft", "-f", "-", input=boot_rulesets(load_config(args.config)))
        if r.returncode != 0:
            sys.exit(f"nft failed: {r.stderr.strip()}")
    elif args.cmd == "status":
        try:
            print(STATUS_FILE.read_text(), end="")
        except OSError:
            sys.exit("pinetswitch is not running")
    elif args.cmd == "power":
        try:
            OVERRIDE_FILE.write_text(args.mode + "\n")
        except OSError as e:
            sys.exit(f"cannot write {OVERRIDE_FILE}: {e}")


if __name__ == "__main__":
    main()
