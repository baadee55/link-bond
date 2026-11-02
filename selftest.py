"""Self-test: needs no tethering. Uses the loopback adapter as a fake "link" and checks the whole path
(SOCKS5, HTTP CONNECT, plain HTTP, link choice, eco mode, ad blocking, dashboard + API, balances ...).
It works in a temporary folder, so it never touches your real settings.   Run:  python selftest.py"""
import asyncio
import json
import os
import socket
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bond  # noqa: E402

bond.STATE_DIR = tempfile.mkdtemp(prefix="linkbond-test-")
bond.no_notify = True
try:
    sys.stdout.reconfigure(errors="replace")  # a Japanese Windows console cannot show every symbol (e.g. a warning sign)
except Exception:
    pass

failed = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed.append(name)


def load(link, n, idle=False):
    """Pretend `link` carries n connections: working ones (data just now) or idle keep-alive ones (silent for long)."""
    t = time.time() - (100 if idle else 0)
    for _ in range(n):
        bond.live[object()] = {"link": link, "sw": None, "cw": None, "t_open": t, "t_data": t}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def widget_helpers():
    """The small floating box: its text helpers (no window is opened)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lb_widget", os.path.join(os.path.dirname(os.path.abspath(__file__)), "widget.pyw"))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    check("widget: sizes are shown in MB below 1 GB and GB above", w.fmt_mb(250) == "250 MB" and w.fmt_mb(1500) == "1.5 GB" and w.fmt_mb(None) == "")
    link = {"label": "Phone A", "healthy": True, "measured": True, "down_mbps": 12.3, "up_mbps": 2.0, "remaining_MB": 3200,
            "used_MB": 800, "rtt_ms": 47, "loss_pct": 0, "quality": "good", "checked_ago": 12}
    txt, tone = w.link_status(link)
    check("widget: a link shows it is alive - response time and when it was checked (%s)" % txt,
          "47ms" in txt and w.t("ago").format(12) in txt and tone == "ok")
    check("widget: no speed-right-now number (it would mostly say 0)", "↓" not in txt)
    check("widget: what it can do and what is left (short)", "↓12" in w.link_detail(link) and "↑2" in w.link_detail(link)
          and "3.2 GB" in w.link_detail(link) and "Mbps" not in w.link_detail(link))
    check("widget: a slow link is yellow, an unstable one red with its loss",
          w.link_status(dict(link, quality="slow"))[1] == "slow" and w.link_status(dict(link, quality="bad", loss_pct=30))[1] == "bad"
          and "30%" in w.link_status(dict(link, quality="bad", loss_pct=30))[0])
    check("widget: a cut link is red", w.link_status(dict(link, healthy=False))[1] == "bad")
    check("widget: a link not checked yet / being measured says so",
          w.link_status(dict(link, rtt_ms=None))[0] == w.t("checking") and w.link_status(dict(link, measuring=True))[0] == w.t("measuring"))
    check("widget: an unmeasured link with no balance shows what it used",
          w.t("unm") in w.link_detail(dict(link, measured=False, remaining_MB=None)) and "800 MB" in w.link_detail(dict(link, measured=False, remaining_MB=None))
          and "↓12" not in w.link_detail(dict(link, measured=False, remaining_MB=None)))
    st = {"modes": {"power": True, "eco": True, "block": False}, "problems": [],
          "links": [link, dict(link, label="B", down_mbps=30.0, up_mbps=5.0, rtt_ms=31)]}
    check("widget: the maximum of all links together", w.max_total(st) == (42.3, 7.0) and "42" in w.max_text(st))
    check("widget: a cut link does not count toward the maximum",
          w.max_total(dict(st, links=[link, dict(link, label="B", down_mbps=30.0, healthy=False)])) == (12.3, 2.0))
    title, sub, tone = w.hero(st)
    check("widget: all fine = how many links are connected and their response times",
          tone == "ok" and "2" in title and "Phone A 47ms" in sub and "B 31ms" in sub)
    p = w.pill_text(st)
    check("widget: the pill says OK with the best response time (%s)" % p, "OK" in p and "31ms" in p and "↓" not in p)
    check("widget: no problem = the pill is not red", w.problem_list(st) == [])
    st["problems"] = ["Phone A: 切断 / cut", "VPN 中のため束ねていません / VPN on: not bonding"]
    p = w.pill_text(st)
    title, sub, tone = w.hero(st)
    check("widget: with a problem the pill and the top name it, in red (%s)" % p,
          p.startswith("⚠ Phone A: 切断") and "/ cut" not in p and tone == "bad" and title.startswith("⚠ Phone A: 切断"))
    del st["problems"]
    st["stalled"] = [{"name": "Ethernet 2", "label": "Phone C"}]
    check("widget: a phone with stopped tethering is a problem in the pill", "Phone C" in w.pill_text(st))
    st["modes"]["power"] = False
    check("widget: the pill says OFF when the PC-wide switch is off", "OFF" in w.pill_text(st))
    check("widget: the pill copes with no connection to link-bond", "link-bond" in w.pill_text(None))
    evs = [{"t": f"23:0{i}:00", "text": f"event {i}"} for i in range(6)]
    lines = w.event_lines(evs).split("\n")
    check("widget: the event list shows the newest four", len(lines) == 4 and lines[0].endswith("event 0") and w.event_lines([]) == "")


def unit():
    widget_helpers()
    bond.pinned_exact[:] = ["example.com"]
    bond.pinned_sub[:] = ["bank"]
    check("pinned: exact + sub-domain", bond.is_pinned("example.com") and bond.is_pinned("a.example.com"))
    check("pinned: substring rule", bond.is_pinned("mybank.co.jp"))
    check("pinned: not pinned", not bond.is_pinned("example.org") and not bond.is_pinned("notexample.com"))

    check("best_if: loopback destination is interface 1", bond.best_if("127.0.0.1") == 1)
    check("best_if: a public destination has a real interface", bond.best_if("8.8.8.8") > 0)

    usb = dict(idx=48, name="Ethernet 2", desc="Remote NDIS Compatible Device", ip="10.0.0.2", metric=25, bytes=0)
    wifi = dict(idx=10, name="Wi-Fi", desc="Intel Wi-Fi 6", ip="192.168.1.5", metric=50, bytes=0)
    nord = dict(idx=37, name="NordLynx", desc="NordLynx Tunnel", ip="10.5.0.2", metric=6, bytes=0)
    found, vpn = bond.classify([usb, wifi])
    check("vpn detect: two real links, no VPN", sorted(found) == [10, 48] and not vpn)
    found, vpn = bond.classify([nord, usb, wifi])
    check("vpn detect: WireGuard/NordLynx default route with the best metric = VPN on", sorted(found) == [10, 48] and vpn)
    check("vpn detect: a VPN adapter that is not preferred = VPN off",
          not bond.classify([dict(nord, metric=100), usb, wifi])[1])
    check("vpn detect: OpenVPN-style /1 routes = VPN on", bond.classify([usb, wifi], slash1=True)[1])
    many = [dict(usb, idx=100 + i, name=f"Phone {i}", ip=f"10.1.{i}.2", metric=25 + i) for i in range(6)]
    check("many links: six phones are all found", len(bond.classify(many)[0]) == 6)
    check("classify: the gateway is reported", bond.classify([dict(usb, gw="10.0.0.1")])[0][48][3] == "10.0.0.1")

    # Windows auto-joins another saved Wi-Fi after the phone's hotspot drops: the link is on a different network
    ph = bond.new_link(10, "Wi-Fi", "192.168.43.7")
    ph["gw"] = "192.168.43.1"
    ph.update(down=9.0, up=3.0, measured=True, measured_at=123.0)
    check("network: same gateway = no change", not bond.apply_network(ph, "192.168.43.8", "192.168.43.1") and ph["measured"])
    check("network: a new gateway is detected and the link is measured again",
          bond.apply_network(ph, "192.168.100.103", "192.168.100.1") and not ph["measured"] and ph["measured_at"] == 0.0
          and ph["ip"] == "192.168.100.103" and ph["gw"] == "192.168.100.1")
    check("network: an unknown gateway does not count as a change", not bond.apply_network(ph, "10.1.1.1", ""))
    bond.flap_log.clear()
    t0 = time.time()
    r1, r2, r3 = (bond.note_flap("phone", t0 + i * 60) for i in range(3))
    check("flap: the 3rd drop within 30 minutes warns once", (r1, r2, r3) == (False, False, True))
    check("flap: no repeat warning right away", bond.note_flap("phone", t0 + 4 * 60) is False)
    check("flap: old drops are forgotten", bond.note_flap("other", t0) is False and bond.note_flap("other", t0 + 7200) is False)

    t = bond.new_link(9, "t", "10.9.9.9")
    ev = [bond.apply_probe(t, False, "no-response", now=1000.0) for _ in range(3)]
    check("health: 3 failed checks mark the link cut", ev == [None, None, "down"] and not t["healthy"])
    check("health: reminder after 30 min", bond.apply_probe(t, False, "no-response", now=3000.0) == "remind")
    check("health: recovery is reported", bond.apply_probe(t, True, "", now=5000.0) == "up" and t["healthy"])

    # one device cut: say so, keep the rest going, move the stuck connections to a working link
    class FakeW:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    bond.links.clear()
    la, lb = bond.new_link(1, "A", "10.1.1.1"), bond.new_link(2, "B", "10.2.2.2")
    bond.links.update({1: la, 2: lb})
    check("summary: every device and its state", bond.fleet_summary() == "A: 正常/OK / B: 正常/OK")
    lb["healthy"] = False
    s = bond.fleet_summary()
    check("summary: a cut device is named and the others carry on", "B: 切断/CUT" in s and "1本で継続中" in s and "A: 正常/OK" in s)
    la["healthy"] = False
    check("summary: nothing usable is said plainly", "使える回線がありません" in bond.fleet_summary())
    la["healthy"], lb["healthy"] = True, True
    ws = [FakeW() for _ in range(4)]
    bond.live.clear()
    bond.live["t1"] = {"link": la, "sw": ws[0], "cw": ws[1], "t_open": 0.0, "t_data": 0.0}
    bond.live["t2"] = {"link": lb, "sw": ws[2], "cw": ws[3], "t_open": 0.0, "t_data": 0.0}
    lb["healthy"] = False
    n = bond.drop_link_tunnels(lb)
    check("failover: connections stuck on the cut link are closed, the other link's are untouched",
          n == 1 and ws[2].closed and ws[3].closed and not ws[0].closed and not ws[1].closed)
    la["healthy"] = False
    ws2 = [FakeW(), FakeW()]
    bond.live["t3"] = {"link": lb, "sw": ws2[0], "cw": ws2[1], "t_open": 0.0, "t_data": 0.0}
    check("failover: with no other working link nothing is dropped", bond.drop_link_tunnels(lb) == 0 and not ws2[0].closed)
    bond.live.clear()
    # a phone still plugged in but with tethering stopped (169.254.x address) is told apart from an unplugged one
    check("stalled: VPN / virtual / Wi-Fi Direct / Bluetooth adapters are ignored",
          bond.stalled_names(["Ethernet 2|Remote NDIS Compatible Device", "Tailscale|Tailscale Tunnel", "Bluetooth Network|Bluetooth Device",
                              "vEthernet (Hyper-V)|Hyper-V Virtual Ethernet", "Local Area Connection 2|TAP-NordVPN Windows Adapter V9",
                              "Local Area Connection* 1|Microsoft Wi-Fi Direct Virtual Adapter", ""]) == {"Ethernet 2"})
    bond.stalled.clear()
    t_unplug = bond.gone_message("Ethernet 2")
    bond.stalled.add("Ethernet 2")
    t_stop = bond.gone_message("Ethernet 2")
    check("stalled: the notice says what to do (switch tethering off and on)",
          "外れました" in t_unplug[0] and "テザリングが止まりました" in t_stop[0] and "OFF→ON" in t_stop[1])
    la["healthy"] = lb["healthy"] = True
    s = bond.fleet_summary()
    check("stalled: the summary lists it as stopped and says how many still work",
          "Ethernet 2: テザリング停止/STOPPED" in s and "2本で継続中" in s)
    bond.stalled.clear()
    bond.events.clear()
    bond.notify("link-bond: X 切れました / lost internet", "line1\nline2")
    check("events: every notification is kept in the list", len(bond.events) == 1 and bond.events[-1]["text"].startswith("X 切れました")
          and "line1" in bond.events[-1]["text"])

    # the log never crashes link-bond, whatever characters a notice contains (cp932 = a Japanese Windows log file)
    import io
    real_out = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp932")
    try:
        bond.log("NOTICE:", "✓ 応答 32ms · ⚡ 🆕")
        ok = True
    except Exception:
        ok = False
    finally:
        sys.stdout = real_out
    check("log: characters the log file cannot hold do not crash link-bond", ok)

    # updates: the version is read from the file, compared as numbers, and announced once with what changed
    check("version: read from a bond.py file", bond.file_version(bond.__file__) == bond.__version__)
    check("version: compared as numbers", bond.vtuple("1.10.0") > bond.vtuple("1.9.3") and bond.vtuple("v1.4") == (1, 4))
    check("changelog: what changed in this version is found", len(bond.changelog_head(bond.__version__)) > 5)
    vf = os.path.join(bond.STATE_DIR, "version.txt")
    if os.path.exists(vf):
        os.remove(vf)
    bond.events.clear()
    check("update notice: the very first run is silent", bond.announce_version() is None and not bond.events)
    with open(vf, "w", encoding="utf-8") as f:
        f.write("1.0.0")
    old = bond.announce_version()
    check("update notice: after an update it says so once, with the changes",
          old == "1.0.0" and len(bond.events) == 1 and "1.0.0" in bond.events[-1]["text"] and bond.announce_version() is None)
    bond.events.clear()

    # cheap checks: typical response time, loss, and the condition they mean
    q = bond.new_link(30, "q", "10.3.0.1")
    for r in (40, 50, 60, None, 45):
        bond.note_ping(q, r is not None, r)
    check("ping: median response time and loss", q["rtt"] == 50 and q["loss"] == 20)
    check("quality: 20% lost = bad", bond.quality(q) == "bad")
    q["pings"].clear()
    for _ in range(5):
        bond.note_ping(q, True, 300)
    check("quality: 300 ms = slow", bond.quality(q) == "slow")
    q["pings"].clear()
    for _ in range(5):
        bond.note_ping(q, True, 40)
    check("quality: 40 ms, nothing lost = good", bond.quality(q) == "good")

    # capacity: learned from real traffic (no data spent), fading slowly, per device AND network, kept on disk
    bond.capacity.clear()
    c = bond.new_link(31, "cap", "10.3.0.2")
    c["gw"] = "10.0.0.1"
    bond.learn_capacity(c, 40.0, 5.0, 2)
    check("capacity: a real 40 Mbps becomes what the link can do", c["down"] == 40.0 and c["up"] == 5.0 and c["measured"])
    bond.learn_capacity(c, 1.0, 0.1, bond.CAP_HALF_LIFE)
    check("capacity: an old best fades to half in CAP_HALF_LIFE", abs(c["down"] - 20.0) < 0.01)
    back = bond.new_link(32, "cap", "10.3.0.3")
    back["gw"] = "10.0.0.1"
    check("capacity: a device that comes back knows its speed without a test", bond.adopt_capacity(back) and abs(back["down"] - 20.0) < 0.01)
    other = bond.new_link(33, "cap", "10.3.0.4")
    other["gw"] = "192.168.9.1"
    check("capacity: on another network it is unknown again", not bond.adopt_capacity(other) and not other["measured"])
    bond.save_capacity()
    bond.capacity.clear()
    bond.load_capacity()
    check("capacity: kept on disk across a restart", bond.cap_key(c) in bond.capacity)
    check("speed tests: no periodic tests by default (they cost data)", bond.REMEASURE_SEC == 0)

    # problems: everything that is wrong, in one list (the widget turns red on any of them)
    beat = os.path.join(bond.STATE_DIR, "supervisor.beat")
    if os.path.exists(beat):
        os.remove(beat)
    bond.links.clear()
    bond.stalled.clear()
    p1, p2 = bond.new_link(40, "P1", "10.4.0.1"), bond.new_link(41, "P2", "10.4.0.2")
    bond.links.update({40: p1, 41: p2})
    check("problems: none when all is fine", bond.problems() == [])
    p2["healthy"] = False
    check("problems: a cut link is named", any(x.startswith("P2: 切断") for x in bond.problems()))
    p2["healthy"] = True
    for _ in range(5):
        bond.note_ping(p1, True, 900)
    check("problems: a very slow link is named as unstable", any(x.startswith("P1: 不安定") for x in bond.problems()))
    bond.links.clear()

    # counting usage from a clean (or old-format) usage.json must never fail
    bond.usage.clear()
    bond.usage.update({"cycle": bond.cycle_id(), "used": {}, "alerted": []})  # no total / daily / since: the old format
    bond.add_usage("k", 1000)
    bond.add_usage("k", 500)
    day = bond.usage["daily"][time.strftime("%Y-%m-%d")]
    check("usage: counts into used, total and today's bucket from an old-format file",
          bond.usage["used"]["k"] == 1500 and bond.usage["total"]["k"] == 1500 and day["k"] == 1500 and bond.usage["since"])
    bond.usage.update(cycle="1999-01")  # the plan period changed: used resets, the running total stays
    bond.add_usage("k", 10)
    check("usage: a new plan period resets 'used' but keeps the running total",
          bond.usage["used"]["k"] == 10 and bond.usage["total"]["k"] == 1510)
    bond.usage.update({"cycle": bond.cycle_id(), "used": {}, "alerted": [], "total": {}, "daily": {}})

    # monthly plan (budget.local.txt)
    bond.prepaid.clear()
    bond.budgets.clear()
    bond.budgets["dev"] = 1000.0
    bond.usage.update(cycle=bond.cycle_id(), used={"dev": 810e6}, alerted=[])
    check("budget: 80% fires once", bond.crossed("dev") == 80 and bond.crossed("dev") is None)
    bond.usage["used"]["dev"] = 1000e6
    check("budget: 100% fires once, 80/95 not repeated", bond.crossed("dev") == 100 and bond.crossed("dev") is None)
    bond.reset_day = 15
    check("budget: plan cycle follows reset_day",
          bond.cycle_id(time.mktime((2026, 11, 10, 12, 0, 0, 0, 0, -1))) == "2026-10"
          and bond.cycle_id(time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))) == "2026-11"
          and bond.cycle_id(time.mktime((2026, 1, 3, 12, 0, 0, 0, 0, -1))) == "2025-12")
    bond.reset_day = 1
    bond.budgets.clear()

    # prepaid balance: typed in by hand, this PC's usage is subtracted, a top-up adds
    bond.usage["total"] = {"dev": 0}
    bond.set_prepaid("dev", remaining_mb=1000)
    check("prepaid: starts at what you typed", bond.plan_of("dev") == (1000.0, 0.0))
    bond.usage["total"]["dev"] = 300e6
    size, used = bond.plan_of("dev")
    check("prepaid: usage is subtracted", size == 1000.0 and abs(used - 300.0) < 0.01)
    bond.set_prepaid("dev", add_mb=500)
    size, used = bond.plan_of("dev")
    check("prepaid: a top-up adds to what is left (700 + 500)", size == 1200.0 and abs(used) < 0.01)
    bond.usage["total"]["dev"] = 300e6 + 970e6
    check("prepaid: 80% alert fires once", bond.crossed("dev") == 80 and bond.crossed("dev") is None)
    prepaid_file = os.path.join(bond.STATE_DIR, "prepaid.json")
    check("prepaid: saved to disk", os.path.exists(prepaid_file) and "dev" in json.load(open(prepaid_file, encoding="utf-8")))
    bond.usage["total"]["dev"] = 300e6 + 200e6
    bond.prepaid["dev"]["set_at"] = time.time() - 2 * 86400
    bond.prepaid["dev"]["at_total"] = 300e6
    fd = bond.forecast_days("dev")
    check("forecast: 200 MB in 2 days of a 1200 MB plan -> about 10 days left", fd is not None and 9.5 < fd < 10.5)
    now = time.mktime((2026, 11, 10, 10, 0, 0, 0, 0, -1))
    bond.usage["daily"] = {"2026-11-09": {"dev": 250e6}}
    bond.usage["last_report"] = ""
    rep = bond.maybe_report(now)
    check("report: yesterday's usage and what is left", rep is not None and "dev: 250 MB" in rep[1] and "残り" in rep[1])
    check("report: only once a day", bond.maybe_report(now) is None)
    check("report: not before the report hour",
          bond.maybe_report(time.mktime((2026, 11, 11, 6, 0, 0, 0, 0, -1))) is None)
    bond.set_prepaid("dev", clear=True)
    check("prepaid: can be cleared", bond.plan_of("dev") is None)

    # modes (flag files in the temp folder)
    bond.refresh_modes()
    check("modes: eco is ON by default, ad blocking OFF", bond.eco_on and not bond.block_on)
    bond.set_mode("eco", False)
    bond.set_mode("block", True)
    bond.refresh_modes()
    check("modes: switched by flag files", not bond.eco_on and bond.block_on)
    bond.set_mode("eco", True)
    bond.set_mode("block", False)
    bond.refresh_modes()
    check("modes: back to default", bond.eco_on and not bond.block_on)
    check("modes: the PC-wide switch is ON by default", bond.power_on)
    bond.set_mode("power", False)
    bond.refresh_modes()
    off = not bond.power_on and os.path.exists(os.path.join(bond.STATE_DIR, "power.off"))
    bond.set_mode("power", True)
    bond.refresh_modes()
    check("modes: the PC-wide switch is a flag file the supervisor follows", off and bond.power_on)
    check("supervisor: no heartbeat = not supervised", not bond.supervised())
    open(os.path.join(bond.STATE_DIR, "supervisor.beat"), "w").close()
    check("supervisor: a fresh heartbeat = supervised", bond.supervised())
    check("file names: unsafe characters and paths are removed",
          bond.safe_filename('..\\..\\evil<>:"|?*name.exe') == "evilname.exe" and bond.safe_filename("") == "download.bin"
          and "/" not in bond.safe_filename("a/../../b.txt") and bond.safe_filename("..") == "download.bin"
          and bond.filename_from({"content-disposition": 'attachment; filename="a b.zip"'}, "/x/y.bin") == "a b.zip"
          and bond.filename_from({}, "/dir/some%20file.iso?token=1") == "some file.iso")
    for bad in ("ftp://x.test/a", "file:///c:/x", "http://192.168.1.1/a", "http://127.0.0.1/a", "not a url"):
        try:
            bond.parse_url(bad)
            ok = False
        except ValueError:
            ok = True
        if not ok:
            check("download: refuses " + bad, False)
    check("download: only public http(s) URLs are accepted", bond.parse_url("https://example.com/a?b=1")[:3] == ("https", "example.com", 443))

    # ad / tracker blocking
    bond.blocked_domains.clear()
    bond.blocked_domains.add("doubleclick.net")
    check("block: off = nothing blocked", not bond.is_blocked("ad.doubleclick.net"))
    bond.block_on = True
    check("block: domain and its sub-domains", bond.is_blocked("ad.doubleclick.net") and bond.is_blocked("doubleclick.net"))
    check("block: look-alikes and bare TLDs pass", not bond.is_blocked("notdoubleclick.net") and not bond.is_blocked("net"))
    bond.load_blocklist()
    check("block: shipped list loads", bond.is_blocked("pagead2.googlesyndication.com") and not bond.is_blocked("www.google.com"))
    bond.block_on = False

    # links, priority tiers, dedicated device
    bond.links.clear()
    bond.vpn_on = False
    bond.traffic["up"] = bond.traffic["down"] = 0.0
    fast, slow = bond.new_link(1, "fast", "10.1.1.1"), bond.new_link(2, "slow", "10.2.2.2")
    fast.update(down=50.0, up=10.0)
    slow.update(down=25.0, up=0.1)
    bond.links.update({1: fast, 2: slow})
    bond.live.clear()
    check("choice: idle goes to the faster link", bond.candidates("x.test")[0] is fast)
    load(fast, 10)
    check("choice: a busy fast link shares work with a decent second link", bond.candidates("x.test")[0] is slow)
    bond.live.clear()
    load(fast, 40, idle=True)  # a browser's idle keep-alive connections: they must not make the fast link look full
    check("choice: idle connections do not count - the fast link still gets the work", bond.candidates("x.test")[0] is fast)
    bond.live.clear()
    weak = bond.new_link(3, "weak", "10.3.3.3")
    weak.update(down=5.0, up=0.1)  # 10% of the best - it is still used, just for fewer connections
    bond.links[3] = weak
    for _ in range(80):  # open 80 connections one after another: every link must get work in proportion to its speed
        load(bond.candidates("x.test")[0], 1)
    share = {l["name"]: bond.busy(l) for l in (fast, slow, weak)}
    check("choice: ALL links are used at once, in proportion to their speed (%s)" % share,
          share == {"fast": 50, "slow": 25, "weak": 5})
    bond.live.clear()
    del bond.links[3]
    check("choice: pinned host ignores load", bond.candidates("example.com")[0] is fast)
    bond.traffic["up"] = 1e9  # heavy uploading: the slow link's dead uplink must not get work
    check("choice: uploading avoids a dead uplink", bond.weight(fast) > 5 * bond.weight(slow))
    bond.traffic["up"] = 0.0

    # every working link is used - also one with a thin uplink - and when one drops the rest carry on
    good, thin = bond.new_link(11, "good", "10.1.1.1"), bond.new_link(12, "thin", "10.2.2.2")
    good.update(down=40.0, up=8.0, measured=True)
    thin.update(down=30.0, up=0.1, measured=True)
    bond.links.clear()
    bond.links.update({11: good, 12: thin})
    load(good, 2)
    check("all links: a link with a thin uplink still gets work", bond.candidates("x.test")[0] is thin)
    check("all links: fast downloads use every working link", set(map(id, bond.dl_links())) == {id(good), id(thin)})
    thin["healthy"] = False
    check("all links: when one drops, the rest carry everything",
          bond.candidates("x.test") == [good] and bond.dl_links() == [good])
    thin["healthy"] = True
    bond.live.clear()
    # speeds come from the adapters' own counters (all of the PC's traffic), not only from what link-bond carried
    real_read = bond.read_octets
    counters = {11: [4_294_000_000, 1000], 12: [0, 0]}  # adapter 11's receive counter is about to wrap (32-bit)
    bond.read_octets = lambda idx: tuple(counters[idx]) if idx in counters else None
    bond._prev_bytes.clear()
    bond._prev_octets.clear()
    bond.total_down.clear()
    bond.total_up.clear()
    bond.sample_history(2)
    counters[11][0] = (counters[11][0] + 25_000_000) % (1 << 32)  # 100 Mbps down for 2 s - across the wrap
    counters[11][1] += 2_500_000                                   # 10 Mbps up
    counters[12][0] += 5_000_000                                   # the other adapter: 20 Mbps down
    good["b_down"] += 6_000_000                                    # of which link-bond carried 24 Mbps on 'good'
    bond.sample_history(2)
    check("speeds: each link's real speed (adapter counters), down and up separately, across a counter wrap",
          good["now_down"] == 100.0 and good["now_up"] == 10.0 and thin["now_down"] == 20.0 and thin["now_up"] == 0.0)
    check("speeds: the part that went through link-bond is shown too", good["via_down"] == 24.0)
    check("speeds: the total of all links, down and up", bond.total_down[-1] == 120.0 and bond.total_up[-1] == 10.0)
    check("speeds: real throughput above the speed test raises the link's speed", good["down"] == 100.0 and good["up"] == 10.0)
    bond.read_octets = lambda idx: None  # no adapter counters (e.g. not Windows): fall back to what link-bond carried
    bond._prev_octets.clear()
    good["b_down"] += 2_500_000
    bond.sample_history(2)
    check("speeds: without adapter counters, link-bond's own count is used", good["now_down"] == 10.0)
    bond.read_octets = real_read
    check("speeds: Windows' adapter counters can be read", real_read(1) is not None)

    c1, c2, c3 = (bond.new_link(5 + i, f"c{i + 1}", f"10.5.5.{i}") for i in range(3))
    for c in (c1, c2, c3):
        c.update(down=30.0, up=10.0)
    bond.links.clear()
    bond.links.update({5: c1, 6: c2, 7: c3})
    load(c1, 4)
    load(c2, 1)
    load(c3, 2)
    check("three links: new work goes to the least loaded one first",
          [l["name"] for l in bond.candidates("x.test")] == ["c2", "c3", "c1"])
    check("dedicated port: only that device, no fallback",
          bond.candidates("x.test", only="c2") == [c2] and bond.candidates("x.test", only="nope") == [])
    c2["healthy"] = False
    check("a cut device is skipped, also for its dedicated port",
          bond.candidates("x.test", only="c2") == [] and bond.candidates("x.test")[0] is c3)
    c2["healthy"] = True
    bond.live.clear()

    bond.links.clear()
    a, b = bond.new_link(7, "a", "10.7.7.7"), bond.new_link(8, "b", "8.8.8.8")
    bond.links.update({7: a, 8: b})
    real_read = bond.read_octets
    bond.read_octets = lambda idx: None  # fake links: never read a real adapter that happens to have the same number
    bond._prev_bytes.clear()
    bond.sample_history(2)
    a["bytes"] += 250_000
    bond.sample_history(2)
    bond.read_octets = real_read
    check("history: Mbps carried per device", list(bond.history["a"]) == [0.0, 1.0] and list(bond.history["b"]) == [0.0, 0.0])

    bond.links.clear()
    bond.links.update({1: fast, 2: slow})
    bond.vpn_on = True
    check("vpn: follows the system route", bond.candidates("x.test") == [bond.SYSTEM_LINK])
    bond.vpn_on = False


async def echo(r, w):
    try:
        while True:
            b = await r.read(4096)
            if not b:
                break
            w.write(b)
            await w.drain()
    finally:
        w.close()


async def web(r, w):
    await r.readuntil(b"\r\n\r\n")
    w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    await w.drain()
    w.close()


async def socks_echo(port, eport, payload=b"hello"):
    """Open a tunnel to the echo server through a proxy port; returns (reply_code, echoed_bytes)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"\x05\x01\x00")
    await r.readexactly(2)
    w.write(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", eport))
    code = (await r.readexactly(10))[1]
    got = b""
    if code == 0:
        w.write(payload)
        got = await asyncio.wait_for(r.readexactly(len(payload)), 5)
    w.close()
    return code, got


async def http_req(port, raw):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw)
    out = await asyncio.wait_for(r.read(), 5)
    w.close()
    return out


async def e2e():
    bond.links.clear()
    bond.links[1] = bond.new_link(1, "loopback", "127.0.0.1")  # ifindex 1 = loopback adapter
    eport, wport, pport, sport, dport = free_port(), free_port(), free_port(), free_port(), free_port()
    e = await asyncio.start_server(echo, "127.0.0.1", eport)
    wsrv = await asyncio.start_server(web, "127.0.0.1", wport)
    bond.ports_map.clear()
    bond.ports_map["loopback"] = dport
    srv, st = await bond.serve(pport, sport, discover=False)

    # SOCKS5
    code, got = await socks_echo(pport, eport)
    check("socks5 connect + echo through the proxy", code == 0 and got == b"hello")

    # a dedicated per-device port
    code, got = await socks_echo(dport, eport, b"dedicated")
    check("dedicated port: works for an existing device", code == 0 and got == b"dedicated")
    bond.links[1]["healthy"] = False
    code, _ = await socks_echo(dport, eport)
    bond.links[1]["healthy"] = True
    check("dedicated port: refuses when that device is cut", code != 0)

    # a dead link that Windows still lists must not hold a connection up: it is skipped at once
    bad = bond.new_link(1, "bad", "10.255.255.1")  # an address that does not exist on this PC: connecting over it fails
    bad.update(down=100.0, up=50.0)                # ...and it looks like the best link, so it is tried first
    bond.links[9] = bad
    t0 = asyncio.get_running_loop().time()
    code, got = await socks_echo(pport, eport, b"failover")
    took = asyncio.get_running_loop().time() - t0
    check("failover: the dead link is skipped and the connection still succeeds quickly",
          code == 0 and got == b"failover" and took < 3 and bad["paused_until"] > time.time() and bad["conn_fails"] >= 1)
    del bond.links[9]

    # VPN takes over -> open tunnels on a physical link are closed so apps reconnect through the VPN
    r, w = await asyncio.open_connection("127.0.0.1", pport)
    w.write(b"\x05\x01\x00")
    await r.readexactly(2)
    w.write(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", eport))
    await r.readexactly(10)
    w.write(b"x")
    await asyncio.wait_for(r.readexactly(1), 5)
    n = bond.drop_bonded_tunnels()
    eof = await asyncio.wait_for(r.read(10), 5) == b""
    w.close()
    check("vpn switch-over closes open tunnels", n >= 1 and eof)

    # HTTP CONNECT
    r, w = await asyncio.open_connection("127.0.0.1", pport)
    w.write(f"CONNECT 127.0.0.1:{eport} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    head = await r.readuntil(b"\r\n\r\n")
    w.write(b"world")
    got = await asyncio.wait_for(r.readexactly(5), 5)
    w.close()
    check("http CONNECT + echo through the proxy", b" 200 " in head and got == b"world")

    # plain HTTP
    body = await http_req(pport, f"GET http://127.0.0.1:{wport}/a HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    check("plain http through the proxy", body.endswith(b"ok"))

    # ad blocking
    bond.blocked_domains.add("127.0.0.1")
    bond.set_mode("block", True)
    bond.refresh_modes()
    code, _ = await socks_echo(pport, eport)
    out = await http_req(pport, f"CONNECT 127.0.0.1:{eport} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    bond.set_mode("block", False)
    bond.refresh_modes()
    bond.blocked_domains.discard("127.0.0.1")
    code2, got2 = await socks_echo(pport, eport)
    check("ad blocking: a listed host is refused (socks5 + http), counted", code == 2 and b" 403 " in out and bond.blocked_total >= 2)
    check("ad blocking: off again = the same host works", code2 == 0 and got2 == b"hello")

    # Eco mode: a 600 KB download from a capped host must be slowed to ~1.6 Mbps; uncapped it is instant
    async def big(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 600000\r\nConnection: close\r\n\r\n" + b"x" * 600000)
        await w.drain()
        w.close()

    bport = free_port()
    bsrv = await asyncio.start_server(big, "127.0.0.1", bport)

    async def timed_get():
        t0 = asyncio.get_running_loop().time()
        got = len(await http_req(pport, f"GET http://127.0.0.1:{bport}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode()))
        return got, asyncio.get_running_loop().time() - t0

    bond.eco_rules[:] = [bond.Rule("127.0.0.1", 1.6)]  # 1.6 Mbps = 200 KB/s
    bond.set_mode("eco", False)
    bond.refresh_modes()
    g0, t_off = await timed_get()
    bond.set_mode("eco", True)
    bond.refresh_modes()
    g1, t_on = await timed_get()
    check("eco: off = full speed", g0 > 600000 and t_off < 1.5)
    check("eco: on = capped to about 200 KB/s (took %.1fs)" % t_on, g1 > 600000 and 2.0 < t_on < 6.0)
    check("eco: rule counts the bytes", bond.eco_rules[0].bytes >= 1200000)
    check("eco: unmatched host is not capped", bond.eco_rule("example.com") is None)
    bond.load_eco()
    check("eco: shipped rules load and match video hosts",
          bond.eco_rule("rr3---sn-abc.googlevideo.com") is not None and bond.eco_rule("video-sjc3-1.xx.fbcdn.net") is not None
          and bond.eco_rule("www.youtube.com") is None)
    bsrv.close()

    # unreachable target -> clean error, proxy keeps working
    r, w = await asyncio.open_connection("127.0.0.1", pport)
    w.write(f"CONNECT 127.0.0.1:{free_port()} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 15)
    w.close()
    check("unreachable target gives 502, not a crash", b" 502 " in head)
    bond.links[1]["paused_until"] = 0.0

    # while a VPN is on, per-link checks are paused (a kill switch would make them fail: false "cut" alarms)
    bond.PROBE_HOST, bond.PROBE_PORT = "127.0.0.1", free_port()  # nothing listens there: every check would fail
    bond.links[1]["fails"], bond.links[1]["healthy"] = 2, True
    bond.vpn_on = True
    await bond.health_tick()
    paused_ok = bond.links[1]["fails"] == 0 and bond.links[1]["healthy"]
    bond.vpn_on = False
    check("health: checks are paused while a VPN is on (no false alarm)", paused_ok)
    bond.links[1]["fails"] = 0

    # the cheap check: a tiny DNS question over the link, with the response time
    class DnsEcho(asyncio.DatagramProtocol):
        def connection_made(self, tr):
            self.tr = tr

        def datagram_received(self, data, addr):
            self.tr.sendto(data[:2] + b"\x81\x80" + data[4:], addr)

    dtr, _ = await asyncio.get_running_loop().create_datagram_endpoint(DnsEcho, local_addr=("127.0.0.1", 0))
    bond.PING_HOST, bond.PING_PORT = "127.0.0.1", dtr.get_extra_info("sockname")[1]
    ok, rtt = await bond.ping(bond.links[1])
    check("ping: a tiny DNS question gets its answer, with the time", ok and rtt is not None and rtt < 1000)
    dtr.close()
    ok2, rtt2 = await bond.ping(bond.links[1])
    check("ping: nobody answering = not ok", not ok2 and rtt2 is None)

    # health probe: real internet / captive portal (prepaid out of credit) / nothing answering
    async def probe_srv(reply):
        async def h(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(reply)
            await w.drain()
            w.close()
        return await asyncio.start_server(h, "127.0.0.1", bond.PROBE_PORT)

    bond.PROBE_HOST, bond.PROBE_PORT = "127.0.0.1", free_port()
    ps = await probe_srv(b"HTTP/1.1 204 No Content\r\n\r\n")
    ok_result = await bond.probe(bond.links[1])
    ps.close()
    await ps.wait_closed()
    ps = await probe_srv(b"HTTP/1.1 302 Found\r\nLocation: http://portal/\r\n\r\n")
    redirect_result = await bond.probe(bond.links[1])
    ps.close()
    await ps.wait_closed()
    none_result = await bond.probe(bond.links[1])
    check("probe: 204 = internet ok", ok_result == (True, ""))
    check("probe: redirect = connected but no internet (prepaid/portal)", redirect_result == (False, "redirect"))
    check("probe: no answer = no-response", none_result == (False, "no-response"))

    # fast download: one file over several links at once
    payload = os.urandom(5 * 1024 * 1024 + 12345)

    async def file_server(honour_range=True, delay=0.05):
        async def h(r, w):
            try:
                while True:
                    head = await r.readuntil(b"\r\n\r\n")
                    lines = head.decode().split("\r\n")
                    path = lines[0].split()[1]
                    hd = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
                    await asyncio.sleep(delay)
                    if path == "/redir":
                        w.write(b"HTTP/1.1 302 Found\r\nLocation: /file.bin\r\nContent-Length: 0\r\n\r\n")
                    elif path == "/missing":
                        w.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    elif hd.get("range") and honour_range:
                        a, b = hd["range"].split("=")[1].split("-")
                        a, b = int(a), min(int(b), len(payload) - 1)
                        body = payload[a:b + 1]
                        w.write((f"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {a}-{b}/{len(payload)}\r\n"
                                 f"Content-Length: {len(body)}\r\nContent-Disposition: attachment; filename=\"test file.bin\"\r\n\r\n").encode() + body)
                    else:
                        w.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n\r\n".encode() + payload)
                    await w.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass
            finally:
                w.close()
        return await asyncio.start_server(h, "127.0.0.1", 0)

    bond.DOWNLOAD_DIR = tempfile.mkdtemp(prefix="linkbond-dl-")
    bond.ALLOW_LOCAL_DOWNLOADS = True
    bond.links[2] = bond.new_link(1, "loopback2", "127.0.0.1")  # a second "link" on the same adapter
    fs = await file_server()
    base = f"http://127.0.0.1:{fs.sockets[0].getsockname()[1]}"
    job = bond.start_download(base + "/redir")  # redirect -> /file.bin, Range supported, 2 links
    await asyncio.wait_for(job["task"], 60)
    got = open(job["path"], "rb").read() if os.path.exists(job["path"]) else b""
    check("download: a file arrives complete and identical (redirect + file name from the server)",
          job["state"] == "done" and got == payload and job["name"] == "test file.bin")
    check("download: the same file used BOTH links", set(job["by_link"]) == {"loopback", "loopback2"})
    check("download: no .part file is left behind", not any(f.endswith(".part") for f in os.listdir(bond.DOWNLOAD_DIR)))
    job2 = bond.start_download(base + "/file.bin")
    await asyncio.wait_for(job2["task"], 60)
    check("download: a second download never overwrites the first",
          job2["state"] == "done" and job2["path"] != job["path"] and open(job2["path"], "rb").read() == payload)
    fs.close()
    fs2 = await file_server(honour_range=False, delay=0)
    base2 = f"http://127.0.0.1:{fs2.sockets[0].getsockname()[1]}"
    job3 = bond.start_download(base2 + "/file.bin")  # a server without Range: one stream over the best link
    await asyncio.wait_for(job3["task"], 60)
    check("download: a server without Range still works (single stream)",
          job3["state"] == "done" and not job3["ranged"] and open(job3["path"], "rb").read() == payload)
    # a download handed over by the browser extension carries the browser's cookie / referer / user agent
    seen = []

    async def hdr_srv(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        seen.append(head.decode("latin-1"))
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc")
        await w.drain()
        w.close()

    hs = await asyncio.start_server(hdr_srv, "127.0.0.1", 0)
    hurl = f"http://127.0.0.1:{hs.sockets[0].getsockname()[1]}/f.bin"
    jobh = bond.start_download(hurl, cookie="sid=42\r\nX-Evil: 1", referer="https://site.example/page",
                               user_agent="TestBrowser/1", filename="from-browser.bin", source="browser")
    await asyncio.wait_for(jobh["task"], 30)
    first = seen[0] if seen else ""
    check("browser hand-over: cookie, referer and user agent reach the server, the browser's file name is kept",
          "Cookie: sid=42" in first and "Referer: https://site.example/page" in first and "User-Agent: TestBrowser/1" in first
          and jobh["name"] == "from-browser.bin" and jobh["state"] == "done")
    check("browser hand-over: no header injection through the cookie", "\r\nX-Evil: 1" not in first)
    hs.close()
    job4 = bond.start_download(base2 + "/missing")
    await asyncio.wait_for(job4["task"], 30)
    check("download: a 404 is reported as an error", job4["state"] == "error" and "404" in job4["error"])
    fs2.close()
    del bond.links[2]
    bond.ALLOW_LOCAL_DOWNLOADS = False

    # dashboard + status + API
    html = await http_req(sport, f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{sport}\r\n\r\n".encode())
    check("dashboard: the page is served with safe headers",
          b"text/html" in html and b"link-bond" in html and b"X-Frame-Options: DENY" in html)
    raw = await http_req(sport, f"GET /status.json HTTP/1.1\r\nHost: 127.0.0.1:{sport}\r\n\r\n".encode())
    data = json.loads(raw.split(b"\r\n\r\n", 1)[1])
    check("status.json reports links, modes and history", data["links"][0]["connections"] >= 3 and "eco" in data["modes"]
          and "history" in data["links"][0])

    def post(path, obj, host=None, header=True):
        body = json.dumps(obj).encode()
        h = f"POST {path} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{sport}'}\r\n" + ("X-LinkBond: 1\r\n" if header else "")
        return (h + f"Content-Length: {len(body)}\r\n\r\n").encode() + body

    out = await http_req(sport, post("/api/mode", {"eco": False}, header=False))
    check("api: a request without the X-LinkBond header is refused", b" 403 " in out and bond.eco_on)
    out = await http_req(sport, post("/api/mode", {"eco": False}, host="evil.example.com"))
    check("api: a request for another Host (DNS rebinding) is refused", b" 403 " in out and bond.eco_on)
    out = await http_req(sport, post("/api/mode", {"eco": False}))
    check("api: switch eco off from the dashboard", b" 200 " in out and not bond.eco_on)
    await http_req(sport, post("/api/mode", {"eco": True}))
    out = await http_req(sport, post("/api/mode", {"power": False}))
    off = b" 200 " in out and not bond.power_on
    await http_req(sport, post("/api/mode", {"power": True}))
    check("api: the PC-wide switch can be turned off and on from the screen", off and bond.power_on)
    out = await http_req(sport, post("/api/download", {"url": "http://127.0.0.1/a"}))
    check("api: a download of a local address is refused", b" 400 " in out)
    out = await http_req(sport, post("/api/download", {"url": "http://example.com/a"}, header=False))
    check("api: a download request without the header is refused", b" 403 " in out)
    out = await http_req(sport, post("/api/budget", {"label": "loopback", "remaining_MB": 5000}))
    d = json.loads(out.split(b"\r\n\r\n", 1)[1])
    check("api: enter the remaining balance of a device",
          b" 200 " in out and d["links"][0]["remaining_MB"] == 5000 and d["links"][0]["prepaid"])
    out = await http_req(sport, post("/api/budget", {"label": "loopback", "add_MB": 1000}))
    d = json.loads(out.split(b"\r\n\r\n", 1)[1])
    check("api: top up adds to what is left", d["links"][0]["remaining_MB"] == 6000)
    out = await http_req(sport, post("/api/budget", {"label": "no-such-device", "remaining_MB": 1}))
    check("api: unknown device is refused", b" 400 " in out)
    out = await http_req(sport, post("/api/budget", {"label": "loopback", "remaining_MB": -5}))
    check("api: negative numbers are refused", b" 400 " in out)
    await http_req(sport, post("/api/budget", {"label": "loopback", "clear": True}))

    for s in [srv, st, e, wsrv] + bond.port_servers:
        s.close()


if __name__ == "__main__":
    unit()
    asyncio.run(e2e())
    print("\nALL PASSED" if not failed else f"\n{len(failed)} FAILED: {failed}")
    sys.exit(1 if failed else 0)
