#!/usr/bin/env python3
"""Policy tests on fake states: python3 -m unittest -v test_pinetswitch"""
import unittest
from dataclasses import replace

from pinetswitch import (AP, DISCONNECT, JOIN, LEAVE, OFF, SCAN, Config, Policy, State,
                         killswitch_ruleset, reconcile, split_terse)

HOME, PHONE, BERRY = "Loading", "iPhone 12 Pro Max", "Loading Berry"


def acts_of(s, p, pol, now):
    return {(a.kind, a.detail, a.profile) for a in reconcile(s, p, pol.cfg, pol, now)}


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(home_router_mac="94:83:c4:e3:d1:4a")
        self.pol = Policy(self.cfg, {HOME: HOME, PHONE: PHONE, BERRY: BERRY})

    def step(self, s, now):
        p = self.pol.plan(s, now)
        return p, acts_of(s, p, self.pol, now)


class Home(Base):
    def test_home_ethernet_turns_wifi_off_full_power_no_vpn(self):
        s = State(eth_up=True, eth_home=True, wifi_mode="client", wifi_profile=HOME, power="low")
        p, a = self.step(s, 0)
        self.assertEqual((p.situation, p.wifi), ("home-wired", OFF))
        self.assertEqual(a, {("wifi", "radio off", None), ("power", "full", None)})

    def test_home_ethernet_settled_does_nothing(self):
        s = State(eth_up=True, eth_home=True, wifi_radio=False, power="full")
        self.assertEqual(self.step(s, 0)[1], set())

    def test_cable_pulled_radio_on_then_joins_home_asap(self):
        s = State(wifi_radio=False, power="full", killswitch=False)
        p, a = self.step(s, 0)
        self.assertEqual(p.wifi, SCAN)
        self.assertIn(("wifi", "radio on", None), a)
        self.assertIn(("killswitch", "on", None), a)  # not home yet: fail closed
        s = State(visible=frozenset({HOME}), power="full", killswitch=True)
        p, a = self.step(s, 1)
        self.assertEqual(p.wifi, JOIN(HOME))
        self.assertIn(("wifi", "up", HOME), a)

    def test_home_wifi_full_power_no_killswitch(self):
        s = State(wifi_mode="client", wifi_profile=HOME, power="low", killswitch=True, vpn_up=True)
        p, a = self.step(s, 0)
        self.assertEqual(p.situation, "home-wifi")
        self.assertEqual(a, {("power", "full", None), ("killswitch", "off", None),
                             ("vpn", "down", "Home VPN")})

    def test_home_wifi_then_cable_goes_wired(self):
        s = State(eth_up=True, eth_home=True, wifi_mode="client", wifi_profile=HOME, power="full")
        p, a = self.step(s, 0)
        self.assertEqual(p.situation, "home-wired")
        self.assertEqual(a, {("wifi", "radio off", None)})

    def test_power_override_wins(self):
        s = State(eth_up=True, eth_home=True, wifi_radio=False, power="full", override="low")
        self.assertEqual(self.step(s, 0)[1], {("power", "low", None)})


class Search(Base):
    def test_phone_ignored_for_first_5s_then_joined(self):
        s = State(visible=frozenset({PHONE, "Cafe"}), killswitch=True)
        self.assertEqual(self.step(s, 100)[0].wifi, SCAN)
        self.assertEqual(self.step(s, 104.9)[0].wifi, SCAN)
        p, a = self.step(s, 105)
        self.assertEqual(p.wifi, JOIN(PHONE))
        self.assertIn(("wifi", "up", PHONE), a)

    def test_other_known_networks_never_joined(self):
        s = State(visible=frozenset({"Cafe"}), killswitch=True)
        for t in range(0, 15):
            self.assertEqual(self.step(s, t)[0].wifi, SCAN)

    def test_nothing_found_ap_after_15s_low_power(self):
        s = State(killswitch=True, power="full")
        self.step(s, 0)
        self.assertEqual(self.step(s, 14.9)[0].wifi, SCAN)
        p, a = self.step(s, 15)
        self.assertEqual((p.wifi, p.power), (AP, "low"))
        self.assertIn(("wifi", "up", BERRY), a)
        self.assertIn(("power", "low", None), a)

    def test_on_phone_low_power_vpn_enforced(self):
        s = State(wifi_mode="client", wifi_profile=PHONE, power="full")
        p, a = self.step(s, 0)
        self.assertEqual((p.situation, p.wifi), ("roaming/phone", LEAVE))
        self.assertEqual(a, {("power", "low", None), ("vpn", "up", "Home VPN"),
                             ("killswitch", "on", None)})

    def test_on_phone_switches_to_home_when_seen(self):
        s = State(wifi_mode="client", wifi_profile=PHONE, visible=frozenset({HOME, PHONE}))
        self.assertEqual(self.step(s, 0)[0].wifi, JOIN(HOME))

    def test_failed_join_backs_off_then_ap(self):
        s = State(visible=frozenset({HOME}), killswitch=True)
        self.assertEqual(self.step(s, 0)[0].wifi, JOIN(HOME))
        self.pol.mark_failed(HOME, 0)
        self.assertEqual(self.step(s, 1)[0].wifi, SCAN)
        self.assertEqual(self.step(s, 15)[0].wifi, AP)

    def test_manually_joined_other_network_left_alone_vpn_enforced(self):
        s = State(wifi_mode="client", wifi_profile="Cafe", power="low", killswitch=True)
        p, a = self.step(s, 0)
        self.assertEqual((p.situation, p.wifi), ("roaming/other-wifi", LEAVE))
        self.assertEqual(a, {("vpn", "up", "Home VPN")})


class AccessPoint(Base):
    def ap(self, **kw):
        return State(wifi_mode=AP, wifi_profile=BERRY, power="low", killswitch=True, **kw)

    def test_rescan_every_3_min(self):
        self.assertEqual(self.step(self.ap(), 0)[0].wifi, LEAVE)
        self.assertEqual(self.step(self.ap(), 179)[0].wifi, LEAVE)
        p, a = self.step(self.ap(), 180)
        self.assertEqual(p.wifi, DISCONNECT)
        self.assertIn(("wifi", "disconnect", None), a)
        # rescan window: phone counts immediately, AP back after 10 s
        off = State(power="low", killswitch=True)
        self.assertEqual(self.step(off, 181)[0].wifi, SCAN)
        self.assertEqual(self.step(replace(off, visible=frozenset({PHONE})), 182)[0].wifi, JOIN(PHONE))
        self.assertEqual(self.step(off, 190)[0].wifi, AP)

    def test_rescan_skipped_while_clients_connected(self):
        self.step(self.ap(), 0)
        self.assertEqual(self.step(self.ap(ap_clients=1), 180)[0].wifi, LEAVE)
        self.assertEqual(self.step(self.ap(), 359)[0].wifi, LEAVE)  # timer restarted at 180
        self.assertEqual(self.step(self.ap(), 360)[0].wifi, DISCONNECT)


class Links(Base):
    def test_usb_host_low_power_wifi_untouched_radio_on(self):
        s = State(usb_up=True, eth_up=True, eth_home=True, wifi_radio=False, power="full")
        p, a = self.step(s, 0)
        self.assertEqual((p.situation, p.wifi), ("usb-host", LEAVE))
        self.assertEqual(a, {("wifi", "radio on", None), ("power", "low", None)})

    def test_usb_host_with_manual_foreign_wifi_gets_vpn(self):
        s = State(usb_up=True, wifi_mode="client", wifi_profile="Cafe", power="low", killswitch=True)
        self.assertEqual(self.step(s, 0)[1], {("vpn", "up", "Home VPN")})

    def test_iphone_usb_tether_starts_ap_shares_through_vpn(self):
        s = State(tether_up=True, power="full")
        p, a = self.step(s, 0)
        self.assertEqual((p.situation, p.wifi), ("tether-ap", AP))
        self.assertEqual(a, {("wifi", "up", BERRY), ("power", "low", None),
                             ("vpn", "up", "Home VPN"), ("killswitch", "on", None)})

    def test_foreign_ethernet_full_power_vpn_wifi_searches(self):
        s = State(eth_up=True, eth_home=False, power="full")
        p, a = self.step(s, 0)
        self.assertEqual((p.situation, p.wifi, p.power), ("foreign-wired/search 0s", SCAN, "full"))
        self.assertEqual(a, {("wifi", "rescan", None), ("vpn", "up", "Home VPN"),
                             ("killswitch", "on", None)})
        self.assertEqual(self.step(s, 15)[0].wifi, AP)
        self.assertEqual(self.step(s, 15)[0].power, "full")

    def test_vpn_retry_backoff(self):
        s = State(eth_up=True, power="full", killswitch=True, visible=frozenset())
        self.pol.mark_failed("Home VPN", 0)
        self.assertNotIn(("vpn", "up", "Home VPN"), self.step(s, 29)[1])
        self.assertIn(("vpn", "up", "Home VPN"), self.step(s, 30)[1])

    def test_not_enforced_means_no_killswitch(self):
        self.pol.cfg.vpn_enforce = False
        s = State(eth_up=True, power="full")
        self.assertNotIn(("killswitch", "on", None), self.step(s, 0)[1])


class Helpers(unittest.TestCase):
    def test_split_terse(self):
        self.assertEqual(split_terse(r"wlan0:wifi:connected:a\:b\\c"),
                         ["wlan0", "wifi", "connected", "a:b\\c"])

    def test_ruleset(self):
        r = killswitch_ruleset(Config())
        self.assertIn('"rpi5"', r)
        self.assertIn("64894", r)


if __name__ == "__main__":
    unittest.main()
