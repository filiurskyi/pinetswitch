#!/usr/bin/env python3
"""pinetswitch: network policy for the Pi, executed through NetworkManager.

Skeleton only: the rules are defined in README.md first.
"""
import argparse
import logging
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger("pinetswitch")


@dataclass
class State:
    usb_up: bool = False
    eth_up: bool = False
    eth_home: bool = False
    wifi_radio: bool = True
    wifi_mode: str = "off"  # off | client | ap
    wifi_ssid: str | None = None
    visible: set[str] = field(default_factory=set)
    ap_clients: int = 0
    since: float = 0.0  # monotonic time the current wifi_mode was entered


@dataclass
class Action:
    reason: str
    cmd: list[str]


def nmcli(*args: str) -> str:
    return subprocess.run(["nmcli", *args], capture_output=True, text=True, check=True).stdout


def snapshot() -> State:
    # TODO: fill from nmcli / sysfs
    return State()


# Rules: State -> Action | None, evaluated in order, first match wins.
RULES = []


def decide(s: State) -> Action | None:
    for rule in RULES:
        if (a := rule(s)) is not None:
            return a
    return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="log decisions, change nothing")
    p.add_argument("--interval", type=float, default=5.0)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    while True:
        s = snapshot()
        if a := decide(s):
            log.info("%s -> %s", a.reason, " ".join(a.cmd))
            if not args.dry_run:
                subprocess.run(a.cmd, check=False)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
