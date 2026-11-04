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
    check("links: a Wi-Fi that dropped (Windows keeps its route for a while) is not a link",
          sorted(bond.classify([usb, dict(wifi, status="Disconnected")])[0]) == [48]
          and not bond.classify([dict(nord, status="Disconnected"), usb, wifi])[1])
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

    # self-update: a published release is downloaded and put in place by itself (only the program files)
    import io as _io
    import zipfile as _zip
    check("update: the web module is loaded (1.5.0 failed every check on this)", hasattr(bond.urllib, "request"))

    def release_zip(version="9.9.9", bond_src=None, extra=None):
        b = _io.BytesIO()
        with _zip.ZipFile(b, "w") as z:
            top = "baadee55-link-bond-abc123/"
            z.writestr(top, "")
            z.writestr(top + "bond.py", bond_src if bond_src is not None else f'__version__ = "{version}"\nx = 1\n')
            z.writestr(top + "widget.pyw", "y = 2\n")
            z.writestr(top + "eco.txt", "new eco\n")
            z.writestr(top + "browser-extension/background.js", "// new\n")
            z.writestr(top + ".github/workflows/ci.yml", "evil: 1\n")
            z.writestr(top + "usage.json", "{}")
            z.writestr(top + "eco.local.txt", "mine\n")
            z.writestr(top + "docs/../../evil.py", "boom\n")
            for k, v in (extra or {}).items():
                z.writestr(top + k, v)
        return b.getvalue()

    m = bond.update_members(["r-1/bond.py", "r-1/.github/workflows/ci.yml", "r-1/usage.json", "r-1/eco.local.txt",
                             "r-1/docs/../../x.py", "r-1/browser-extension/a.js", "r-1/browser-extension/x/y.js",
                             "r-1/", "bond.py", "r-1/C:/x.py"])
    check("update: only program files are taken (no settings, no CI files, no paths that climb out)",
          sorted(m.values()) == ["bond.py", "browser-extension/a.js"])
    upd = tempfile.mkdtemp(prefix="linkbond-upd-")
    for name, text in (("bond.py", '__version__ = "1.0.0"\n'), ("eco.txt", "old eco\n"), ("usage.json", '{"keep": 1}'),
                       ("eco.local.txt", "my own\n")):
        with open(os.path.join(upd, name), "w", encoding="utf-8") as f:
            f.write(text)
    written = bond.apply_update(release_zip(), "9.9.9", here=upd)
    rd = lambda *p: open(os.path.join(upd, *p), encoding="utf-8").read()
    check("update: the new files are put in place, bond.py last (it makes link-bond restart)",
          written[-1] == "bond.py" and '"9.9.9"' in rd("bond.py") and rd("eco.txt") == "new eco\n"
          and rd("browser-extension", "background.js") == "// new\n")
    check("update: your settings and own lists are not touched",
          rd("usage.json") == '{"keep": 1}' and rd("eco.local.txt") == "my own\n"
          and not os.path.exists(os.path.join(upd, ".github")) and not os.path.exists(os.path.join(upd, "evil.py")))
    check("update: the old files are kept in update-backup", rd("update-backup", "bond.py") == '__version__ = "1.0.0"\n')

    def refused(blob, version="9.9.9"):
        before = rd("bond.py")
        try:
            bond.apply_update(blob, version, here=upd)
            return False
        except ValueError:
            return rd("bond.py") == before
    check("update: a download of another version is refused (nothing written)", refused(release_zip("9.9.8")))
    check("update: a broken bond.py is refused (nothing written)", refused(release_zip(bond_src='__version__ = "9.9.9"\ndef (:\n')))
    check("update: something that is not a zip is refused", refused(b"<html>not found</html>"))
    check("update: a zip without link-bond in it is refused", refused(_zip_bytes := b"PK\x05\x06" + b"\x00" * 18))
    check("update: a developer's git copy is never overwritten",
          bond.update_blocker() == ("git" if os.path.isdir(os.path.join(bond.HERE, ".git")) else ""))

    class _Resp(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=0):
            return _Resp(json.dumps({"tag_name": "v9.9.9", "html_url": "https://example.com/r",
                                     "zipball_url": "https://example.com/z",
                                     "assets": [{"name": "link-bond-windows-exe.zip"}] if tested else []}).encode())
    real_build = bond.urllib.request.build_opener
    bond.urllib.request.build_opener = lambda *a: _Opener()
    try:
        tested = True
        rel = bond.check_release()
        tested = False
        rel2 = bond.check_release()
    finally:
        bond.urllib.request.build_opener = real_build
    check("update: the newest release is read from GitHub (version, page, zip, tests passed)",
          rel == ("9.9.9", "https://example.com/r", "https://example.com/z", True) and rel2[3] is False)
    check("update: Windows device names in a release are not written",
          bond.update_members(["r/docs/NUL", "r/docs/con.md", "r/docs/a.", "r/docs/ok.md"]) == {"r/docs/ok.md": "docs/ok.md"})
    # one install at a time, and never the same one twice
    saved = dict(bond.update_info)
    bond.update_info.clear()
    bond.update_info.update(version="9.9.9", zip="https://example.com/z", state="installed")
    check("update: an install that is done is not repeated", bond.install_update() is True)
    bond._install_lock.acquire()
    bond.update_info["state"] = None
    check("update: a second install while one runs is refused", bond.install_update() is False)
    bond._install_lock.release()
    bond.update_info.clear()
    bond.update_info.update(saved)
    check("update: a marker is left so that a new version that keeps failing can be rolled back",
          os.path.exists(os.path.join(upd, "update-pending.json")))

    # Wi-Fi rejoin: only the network that was a link, a few seconds after it dropped, then every 30 s
    bond.wifi_last.clear()
    bond.adapter_desc.clear()
    bond.net_profile.clear()
    bond.adapter_desc.update({"Wi-Fi": "Intel Wi-Fi"})
    bond.net_profile.update({"Wi-Fi": "my iPhone"})
    t0 = 1000.0
    check("wifi: a connected Wi-Fi link is remembered", bond.wifi_plan([("Intel Wi-Fi", 1)], {"Wi-Fi"}, t0) == []
          and bond.wifi_last["Intel Wi-Fi"]["profile"] == "my iPhone")
    first = bond.wifi_plan([("Intel Wi-Fi", 4)], {"Wi-Fi"}, t0 + 1)
    again = bond.wifi_plan([("Intel Wi-Fi", 4)], {"Wi-Fi"}, t0 + 7)
    soon = bond.wifi_plan([("Intel Wi-Fi", 4)], {"Wi-Fi"}, t0 + 20)
    later = bond.wifi_plan([("Intel Wi-Fi", 4)], {"Wi-Fi"}, t0 + 40)
    check("wifi: a dropped Wi-Fi link is joined again (after 5 s, then every 30 s)",
          first == [] and again == [("Intel Wi-Fi", "my iPhone")] and soon == [] and later == again)
    bond.net_profile["Wi-Fi"] = "Cafe"
    bond.wifi_plan([("Intel Wi-Fi", 1)], set(), t0 + 50)
    check("wifi: when you join another network, it is left alone",
          "Intel Wi-Fi" not in bond.wifi_last and bond.wifi_plan([("Intel Wi-Fi", 4)], set(), t0 + 99) == [])
    check("wifi: Windows' Wi-Fi state can be read without location permission", isinstance(bond.wlan_call(), list))

    # DNS replies: only the answer to OUR question counts (id, name), CNAME chains and compressed names are read
    qid, pkt = bond.dns_packet("www.Example.com")
    q = pkt[12:]
    cname = b"\x03cdn\xc0\x10"  # cdn.example.com, compressed (example.com sits at offset 16)
    ans = (b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 300, len(cname)) + cname
           + b"\xc0\x2d" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("1.2.3.4"))
    reply = struct.pack("!HHHHHH", qid, 0x8180, 1, 2, 0, 0) + q + ans
    check("dns: a reply with a CNAME chain and compressed names is read", bond.dns_answer(reply, qid, "www.example.com") == (["1.2.3.4"], 60))
    check("dns: a reply with another id is ignored", bond.dns_answer(reply, qid ^ 1, "www.example.com") is None)
    check("dns: a reply about another name is ignored", bond.dns_answer(reply, qid, "evil.example.com") is None)
    tc = struct.pack("!HHHHHH", qid, 0x8380, 1, 2, 0, 0) + q + ans
    check("dns: a truncated reply is left to Windows", bond.dns_answer(tc, qid, "www.example.com") is None)
    check("dns: garbage does not crash", bond.dns_answer(b"\x00" * 40, qid, "www.example.com") is None
          and bond.dns_answer(reply[:30], qid, "www.example.com") is None)

    # notices on a flapping link: a blip is recorded, not notified; a real cut is notified once, then "back"
    told = []
    real_notify, real_wait = bond.notify, bond.NOTIFY_DOWN_AFTER
    bond.notify = lambda title, text: told.append(title)
    fl = bond.new_link(77, "flappy", "10.9.9.9")
    bond.links[77] = fl

    async def flaps():
        bond.NOTIFY_DOWN_AFTER = 0.2
        for _ in range(5):  # five blips, each over before NOTIFY_DOWN_AFTER
            fl["fails"] = bond.FAILS_TO_DOWN - 1
            bond.tell(fl, bond.apply_probe(fl, False, "no-response"))
            await asyncio.sleep(0.05)
            bond.tell(fl, bond.apply_probe(fl, True, ""))
            await asyncio.sleep(0.3)
        blips = [t for t in told if "不安定" not in t]
        fl["fails"] = bond.FAILS_TO_DOWN - 1
        bond.tell(fl, bond.apply_probe(fl, False, "no-response"))  # a cut that lasts
        await asyncio.sleep(0.4)
        w_probe = bond.weight(fl)
        bond.tell(fl, bond.apply_probe(fl, True, ""))
        return blips, w_probe
    try:
        blips, _ = asyncio.run(flaps())
    finally:
        bond.notify, bond.NOTIFY_DOWN_AFTER = real_notify, real_wait
        bond.links.pop(77, None)
    check(f"notices: five short blips are recorded but not notified ({len(blips)} notices)", len(blips) == 0)
    cut_back = [t for t in told if "不安定" not in t]
    check(f"notices: a cut that lasts is notified once, and its end too ({cut_back})", len(cut_back) == 2)
    check("notices: a link that keeps dropping is called unstable once", len([t for t in told if "不安定" in t]) == 1)
    d, up = bond.stability_today(fl)
    check(f"stability: today's drops are counted ({d})", d == 6)
    w_back = bond.weight(fl)
    fl["probation_until"] = 0
    check("stability: a link that just came back gets a smaller share", w_back < bond.weight(fl))
    bond.wifi_last.clear()
    bond.save_wifi("Intel Wi-Fi", "my iPhone")
    bond.load_wifi()
    check("wifi: the network to rejoin is remembered across a restart",
          bond.wifi_last.get("Intel Wi-Fi", {}).get("profile") == "my iPhone")
    with open(os.path.join(bond.STATE_DIR, "update-skip.txt"), "w", encoding="utf-8") as f:
        f.write("9.9.9")
    check("update: a version that was rolled back is remembered (not installed again)", bond.skipped_version() == "9.9.9")
    os.remove(os.path.join(bond.STATE_DIR, "update-skip.txt"))
    # the roll-back marker is removed only by the version it is about
    with open(os.path.join(upd, "update-pending.json"), "w", encoding="utf-8") as f:
        json.dump({"to": "9.9.9"}, f)
    check("update: an older process does not confirm a newer version's install", not bond.confirm_pending(upd)
          and os.path.exists(os.path.join(upd, "update-pending.json")))
    with open(os.path.join(upd, "update-pending.json"), "w", encoding="utf-8") as f:
        json.dump({"to": bond.__version__}, f)
    check("update: the running version confirms its own install", bond.confirm_pending(upd)
          and not os.path.exists(os.path.join(upd, "update-pending.json")))
    # a failed write puts everything back; unchanged files are not rewritten
    upd2 = tempfile.mkdtemp(prefix="linkbond-upd2-")
    for name, text in (("bond.py", '__version__ = "1.0.0"\n'), ("eco.txt", "new eco\n"), ("widget.pyw", "old\n")):
        with open(os.path.join(upd2, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    real_replace = bond.os.replace
    calls = []

    def flaky_replace(a, b):
        calls.append(b)
        if b.endswith("bond.py"):
            raise PermissionError("held")
        return real_replace(a, b)
    bond.os.replace = flaky_replace
    real_sleep, bond.time.sleep = bond.time.sleep, lambda x: None
    try:
        try:
            bond.apply_update(release_zip(), "9.9.9", here=upd2)
            failed_ok = False
        except PermissionError:
            failed_ok = True
    finally:
        bond.os.replace, bond.time.sleep = real_replace, real_sleep
    rd2 = lambda n: open(os.path.join(upd2, n), encoding="utf-8").read()
    check("update: a write that fails puts the old files back (no mix of versions, no marker left)",
          failed_ok and rd2("widget.pyw") == "old\n" and '"1.0.0"' in rd2("bond.py")
          and not os.path.exists(os.path.join(upd2, "update-pending.json"))
          and not os.path.exists(os.path.join(upd2, "browser-extension", "background.js")))
    check("update: a file that did not change is not rewritten", not any(c.endswith("eco.txt") for c in calls))
    check("capacity: a damaged saved value does not stop link-bond", not bond.sane_capacity({"down": None, "up": 1})
          and not bond.sane_capacity({"down": "9", "up": 1}) and bond.sane_capacity({"down": 20.5, "up": 6.1, "at": 1.0}))
    here = os.path.dirname(os.path.abspath(__file__))
    bad = [n for n in os.listdir(here) if n.endswith(".ps1") and any(b > 127 for b in open(os.path.join(here, n), "rb").read())]
    check("scripts: every .ps1 is plain ASCII (Windows PowerShell 5.1 misreads anything else)", not bad)
    bond.add_event("test event")
    day = time.strftime("%Y-%m-%d")
    rec = [json.loads(x) for x in open(os.path.join(bond.STATE_DIR, "connlog", day + ".jsonl"), encoding="utf-8")]
    # the history page: minute-by-minute state per device, drops, % up, the page that answered instead
    hd = os.path.join(bond.STATE_DIR, "connlog")
    with open(os.path.join(hd, "2026-01-02.jsonl"), "w", encoding="utf-8") as f:
        for i, (ok, q, page) in enumerate([(True, "good", ""), (True, "good", ""), (False, "bad", "302 http://top-up.example/"),
                                            (False, "bad", ""), (True, "slow", ""), (True, "good", "")]):
            f.write(json.dumps({"t": f"2026-01-02 10:0{i}:00", "kind": "link", "link": "Phone A", "ok": ok,
                                "quality": q, "rtt_ms": 100 + i, "page": page}) + "\n")
        f.write(json.dumps({"t": "2026-01-02 10:02:30", "kind": "event", "text": "Phone A: 切断 / cut"}) + "\n")
        f.write(json.dumps({"t": "2026-01-02 10:03:30", "kind": "none"}) + "\n")
    h = bond.history_data("2026-01-02")
    dev = h["devices"][0] if h and h["devices"] else {}
    check("history: a day is read per device and minute (drops, % up, the answering page, events)",
          dev.get("drops") == 1 and dev.get("up_pct") == 67 and dev.get("states", {}).get("602") == "cut"
          and "302 http://top-up.example/" in dev.get("pages", {}) and len(h["events"]) == 1 and h["nolink"] == [603])
    check("history: a bad day name is refused", bond.history_data("../../secret") is None)
    # automatic repair: which adapter to restart, and when not
    saved_links = dict(bond.links)
    bond.links.clear()
    bond.repair_last.clear()
    bond.stalled_since.clear()
    bond.adapter_idx.clear()
    now = time.time()
    dead = bond.new_link(41, "Ethernet 9", "10.7.0.2")
    dead.update(healthy=False, why="no-response", down_since=now - 120)
    paid = bond.new_link(42, "Ethernet 8", "10.8.0.2")
    paid.update(healthy=False, why="redirect", down_since=now - 600)  # an "out of data" page: a restart cannot help
    fresh = bond.new_link(43, "Ethernet 7", "10.9.0.2")
    fresh.update(healthy=False, why="no-response", down_since=now - 20)  # cut 20 s ago: wait a bit first
    bond.links.update({41: dead, 42: paid, 43: fresh})
    bond.adapter_idx["Ethernet 6"] = 44
    bond.stalled_since["Ethernet 6"] = now - 90  # plugged in, no address for 90 s
    plan = bond.repair_plan(now)
    check("repair: a cut adapter (no answer, 90 s+) and one stuck without an address are restarted",
          sorted(i for i, _, _ in plan) == [41, 44])
    for i, _, _ in plan:
        bond.repair_last[i] = now
    check("repair: not again within 10 minutes", bond.repair_plan(now + 60) == [])
    check("repair: an 'out of data' page is not a reason to restart", all(i != 42 for i, _, _ in bond.repair_plan(now + 700)))
    bond.links.clear()
    bond.links.update(saved_links)
    bond.stalled_since.clear()
    bond.repair_last.clear()
    check("record: events and log lines go to the connection record (connlog/day.jsonl)",
          any(r["kind"] == "event" and r["text"] == "test event" for r in rec) and any(r["kind"] == "log" for r in rec))
    check("changelog: what changed in this version is found", len(bond.changelog_head(bond.__version__)) > 5)
    check("changelog: the notice has no markdown marks", "**" not in bond.changelog_head(bond.__version__))
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
    bond.capacity["bad|10.9.9.9"] = {"down": 17145.5, "up": 17145.5, "at": 1.0}  # saved by the old counting error
    bond.save_capacity()
    bond.load_capacity()
    check("capacity: an impossible saved speed is thrown away on load", "bad|10.9.9.9" not in bond.capacity
          and bond.cap_key(c) in bond.capacity)
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
    # a reconnected adapter counts from 0 again: that is not 4 GB of traffic (it once showed 17 000 Mbps)
    before = (good["down"], good["up"])
    counters[11] = [3_000_000, 1_000, 433_000_000]  # counter reset; the adapter says it runs at 433 Mbps
    bond.sample_history(2)
    check("speeds: an adapter whose counters start again from 0 is not read as a huge speed",
          good["now_down"] < 100 and good["now_up"] < 100 and good["down"] <= before[0] + 0.01 and good["up"] <= before[1] + 0.01)
    counters[11] = [3_000_000 + 50_000_000, 1_000, 433_000_000]
    bond.sample_history(2)
    check("speeds: after the reset it counts normally again", good["now_down"] == 200.0)
    counters[11] = [3_600_000_000, 2_000, 0, True]  # 64-bit counters (GetIfEntry2), 3.6 GB so far
    bond.sample_history(2)
    counters[11] = [5_000, 100, 0, True]            # the phone reconnected: counters start again
    before64 = good["down"]
    bond.sample_history(2)
    check("speeds: a 64-bit counter that goes down is a reset, never a wrap (no 3000 Mbps ghost)",
          good["now_down"] < 100 and good["down"] <= before64 + 0.01)
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
          code == 0 and got == b"failover" and took < 3 and bad["conn_fails"] >= 1)
    del bond.links[9]

    # ---------- built for unstable links: measured with faults injected on purpose ----------
    loop_ = asyncio.get_running_loop()
    good = bond.links[1]
    real_connect = bond.link_connect

    async def connect_hole(l, sk, addr):  # a link that never answers (SYN into a black hole)
        if l["name"] == "hole":
            await asyncio.sleep(60)
        await real_connect(l, sk, addr)
    bond.link_connect = connect_hole
    hole = bond.new_link(1, "hole", "127.0.0.1")
    hole.update(down=100.0, up=50.0, rtt=150)  # looks like the best link, and answers checks in 150 ms
    bond.links[8] = hole
    t0 = loop_.time()
    code, got = await socks_echo(pport, eport, b"hole")
    took = loop_.time() - t0
    check(f"unstable: a link that never answers costs well under a second, not 4 s ({took:.2f} s)",
          code == 0 and got == b"hole" and took < 1.0)
    hole["suspect"] = False
    hole["conn_fails"] = 0

    # the slower attempt that lost the race is not a failure of its link
    async def connect_slow(l, sk, addr):
        if l["name"] == "hole":
            await asyncio.sleep(0.6)
        await real_connect(l, sk, addr)
    bond.link_connect = connect_slow
    hole["rtt"] = 100  # the 2nd link starts after 0.25 s and wins; the 1st is cancelled
    code, got = await socks_echo(pport, eport, b"race")
    check("unstable: the attempt that lost the race does not count against its link",
          code == 0 and not hole["suspect"] and hole["conn_fails"] == 0)
    bond.link_connect = real_connect
    del bond.links[8]

    # all links cut for a moment: a new connection waits for one to come back instead of failing at once
    good["healthy"] = False
    good["data_at"] = 0  # no traffic arriving on it (that alone would prove it works)

    async def back_soon():
        await asyncio.sleep(1.5)
        good["healthy"] = True
        bond.link_back.set()
    real_c = bond.link_connect

    async def connect_when_back(l, sk, addr):
        if not good["healthy"]:
            raise OSError("network is unreachable")
        await real_c(l, sk, addr)
    bond.link_connect = connect_when_back
    asyncio.ensure_future(back_soon())
    t0 = loop_.time()
    code, got = await socks_echo(pport, eport, b"held")
    took = loop_.time() - t0
    bond.link_connect = real_c
    good["suspect"] = False
    check(f"unstable: with every link cut for 1.5 s, the connection waits and succeeds ({took:.1f} s)",
          code == 0 and got == b"held" and 1.0 < took < 6)

    # prepaid data used up: DNS still answers, the web does not -> the link must be cut (it once stayed "fine" 18 min)
    dataless = bond.new_link(7, "dataless", "127.0.0.1")
    real_ping, real_probe = bond.ping, bond.probe

    async def ping_ok(l):
        return True, 120

    async def probe_dead(l):
        return (False, "redirect") if l["name"] == "dataless" else await real_probe(l)
    bond.ping, bond.probe = ping_ok, probe_dead
    real_fast = bond.FAST_SEC
    bond.FAST_SEC = 0.05
    bond.links[7] = dataless
    try:
        ok, why = await bond.check_link(dataless, time.time())
        check("data cut: a link whose DNS answers but whose web does not fails its check", not ok and why == "redirect")
        bond.suspect(dataless, "check")
        for _ in range(60):
            if not dataless["healthy"]:
                break
            await asyncio.sleep(0.05)
        check("data cut: such a link is cut within seconds, not left 'fine'", not dataless["healthy"])
        ok2, _ = await bond.check_link(dataless, time.time(), quick=True)
        check("data cut: a DNS answer alone does not bring it back while the web is dead", not ok2)
    finally:
        bond.ping, bond.probe, bond.FAST_SEC = real_ping, real_probe, real_fast
        bond.links.pop(7, None)

    # one host that never answers (a tracker blocked by the network) does not make a good link suspect
    th_link = bond.new_link(1, "two-hosts", "127.0.0.1")
    bond.links[6] = th_link

    async def connect_slowhost(l, sk, addr):
        await asyncio.sleep(60)
    real_c2 = bond.link_connect
    bond.link_connect = connect_slowhost
    real_hold = bond.HOLD_SEC
    bond.HOLD_SEC = 0
    try:
        try:
            await bond.race_connect([th_link], "1.2.3.4", 443, "tracker.example")
        except Exception:
            pass
    finally:
        bond.link_connect, bond.HOLD_SEC = real_c2, real_hold
    one_host = th_link["suspect"]
    bond.links.pop(6, None)
    check("connect: a single host timing out does not make a working link suspect", not one_host)

    # a server that refuses is not a link problem: no waiting, no suspicion
    dead_port = free_port()
    t0 = loop_.time()
    code, _ = await socks_echo(pport, dead_port, b"x")
    took = loop_.time() - t0
    check(f"unstable: a server that refuses fails at once and does not make the link suspect ({took:.2f} s)",
          code != 0 and took < 3.5 and not good["suspect"])  # Windows itself takes ~2 s to report a refused local port

    # every adapter gone (the commonest total outage): a new connection waits for one to appear
    saved = dict(bond.links)
    bond.links.clear()

    async def appears():
        await asyncio.sleep(1.0)
        bond.links.update(saved)
        bond.wake_waiters()
    asyncio.ensure_future(appears())
    t0 = loop_.time()
    code, got = await socks_echo(pport, eport, b"appeared")
    took = loop_.time() - t0
    check(f"unstable: with no link at all, a new connection waits for one to appear ({took:.1f} s)",
          code == 0 and got == b"appeared" and 0.8 < took < 6)

    # names: Windows' resolver hangs (its phone is cut) -> answered over a working link; nothing answers -> last known
    real_sys, real_over = bond._system_lookup, bond.dns_over

    async def sys_hangs(name, port):
        await asyncio.sleep(30)

    async def over_ok(link, name, server, timeout=2.0):
        return (["8.8.8.8"], 120)
    bond._system_lookup, bond.dns_over = sys_hangs, over_ok
    bond.dns_cache.clear()
    t0 = loop_.time()
    ip = await bond.resolve("www.example-unstable.com")
    took = loop_.time() - t0
    check(f"names: Windows' resolver hangs -> answered over a working link ({took:.2f} s)", ip == "8.8.8.8" and took < 1)

    async def over_dead(link, name, server, timeout=2.0):
        return None
    bond.dns_over = over_dead
    bond.dns_cache["www.example-unstable.com"][1] = time.time() - 1  # expired, but known
    real_as = bond.asyncio.as_completed

    def quick_as_completed(fs, timeout=None):
        return real_as(fs, timeout=0.5)
    bond.asyncio.as_completed = quick_as_completed
    try:
        ip2 = await bond.resolve("www.example-unstable.com")
    finally:
        bond.asyncio.as_completed = real_as
    check("names: when nothing answers, the last known address is used", ip2 == "8.8.8.8")
    bond._system_lookup, bond.dns_over = real_sys, real_over
    check("names: an address is used as it is", await bond.resolve("10.1.2.3") == "10.1.2.3")
    bond.dns_cache.clear()

    async def sys_portal(name, port):
        return (["10.10.10.10"], 300, "sys")  # an operator's "out of data" page answers every name

    async def over_public(link, name, server, timeout=2.0):
        await asyncio.sleep(0.1)
        return (["1.2.3.4"], 300)
    bond._system_lookup, bond.dns_over = sys_portal, over_public
    ip3 = await bond.resolve("news.example-unstable.com")
    check("names: a private address for a public name loses to the real one from another link", ip3 == "1.2.3.4")

    async def over_portal(link, name, server, timeout=2.0):
        return (["192.168.0.1"], 3600)
    bond.dns_over = over_portal
    bond.dns_cache.clear()
    check("names: a private answer from the public DNS over a link is never used",
          await bond._link_lookup(bond.links[1], "x.example-unstable.com", "8.8.8.8") is None)
    calls = []

    async def sys_counted(name, port):
        calls.append(name)
        await asyncio.sleep(0.2)
        return (["8.8.8.8"], 300, "sys")

    async def over_none(link, name, server, timeout=2.0):
        return None
    bond._system_lookup, bond.dns_over = sys_counted, over_none
    got_many = await asyncio.gather(*(bond.resolve("same.example-unstable.com") for _ in range(20)))
    check(f"names: 20 connections to one new name cause one lookup ({len(calls)})",
          len(calls) == 1 and set(got_many) == {"8.8.8.8"})
    bond._system_lookup, bond.dns_over = real_sys, real_over

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


# ---------- one connection over all links (relay): checks that need no relay ----------
class FakeWS:
    """Stands in for a WebSocket: remembers what was sent."""

    def __init__(self):
        self.sent, self.closed, self.close_code = [], False, None

    def send(self, payload, ctl=False, opcode=2):
        self.sent.append((ctl, bytes(payload)))

    def close(self, code=1000):
        self.closed = True

    def abort(self):
        self.closed = True

    def frames(self, kind=None):
        out = [bond.decode_frame(m) for _, m in self.sent]
        return [f for f in out if f and (kind is None or f[0] == kind)]


def fake_flow(n=2, rtt_ms=40):
    cfg = dict(host="127.0.0.1", port=1, tls=False, base="", secret=b"\x01" * 32)
    f = bond.BondFlow(cfg, "example.test", 443)
    bl = []
    for i in range(n):
        link = bond.new_link(1, "L%d" % i, "127.0.0.1")
        link["rtt"] = rtt_ms
        b = bond.BLink(i, link)
        f.lk[i], f.nums[id(link)] = b, i
        b.ws, b.state, b.last_heard = FakeWS(), "up", time.monotonic()
        bl.append(b)
    return f, bl


async def ws_server_side(r, w, on_path):
    """Tiny WebSocket server for the tests (handshake written out by hand, independent of bond.py)."""
    import base64
    import hashlib
    head = (await r.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
    hdr = {k.strip().lower(): v.strip() for k, _, v in (x.partition(":") for x in head[1:] if x)}
    on_path(head[0].split(" ")[1])
    acc = base64.b64encode(hashlib.sha1(hdr["sec-websocket-key"].encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
    w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")
    await w.drain()
    return bond.WS(r, w, mask=False)


def bond_unit():
    import hashlib
    import hmac as hmac_
    # frames
    fr = bond.f_data(7, b"abc")
    check("relay frames: DATA = type 1, u32 seq, payload", fr == b"\x01\x00\x00\x00\x07abc" and bond.decode_frame(fr) == (1, 7, b"abc"))
    a = bond.f_ack(300, [(0, 5), (3, 2 ** 40)])
    check("relay frames: ACK = u32 cum, u8 n, n x (u8 link, u64 bytes)",
          a == b"\x02\x00\x00\x01\x2c\x02" + b"\x00" + (5).to_bytes(8, "big") + b"\x03" + (2 ** 40).to_bytes(8, "big")
          and bond.decode_frame(a) == (2, 300, {0: 5, 3: 2 ** 40}))
    check("relay frames: FIN / ERR / OK / PING / PONG",
          bond.f_fin(9) == b"\x03\x00\x00\x00\x09" and bond.decode_frame(bond.f_fin(9)) == (3, 9)
          and bond.decode_frame(bond.f_err("connect: refused")) == (4, "connect: refused")
          and bond.decode_frame(bond.f_ok()) == (5,) and bond.f_ping(1)[:1] == b"\x06" and len(bond.f_ping(1)) == 9
          and bond.decode_frame(bond.f_pong(123456789012)) == (7, 123456789012))
    check("relay frames: garbage is refused, not crashed on",
          bond.decode_frame(b"") is None and bond.decode_frame(b"\x09") is None and bond.decode_frame(b"\x02\x00\x00\x00\x01\x05") is None
          and bond.decode_frame(b"\x01\x00\x00\x00\x01") is None and bond.decode_frame(b"\x06\x01") is None)
    # signed address
    secret = bytes(range(32))
    cfg = dict(base="", secret=secret)
    flow = "0123456789abcdef0123456789abcdef"
    path = bond.relay_path(cfg, flow, 3, 1700000000, "video.example.com", 443)
    d = "dmlkZW8uZXhhbXBsZS5jb206NDQz"
    want = hmac_.new(secret, ("%s|3|1700000000|%s" % (flow, d)).encode(), hashlib.sha256).hexdigest()
    check("relay address: /f/<flow>?l=&t=&d=<base64url host:port>&s=<hmac-sha256 hex>",
          path == "/f/%s?l=3&t=1700000000&d=%s&s=%s" % (flow, d, want))
    check("relay address: no '=' padding, url-safe alphabet", "=" not in bond.relay_dst("a", 1) and bond.relay_dst("??>", 1).replace("-", "").replace("_", "").isalnum())
    # WebSocket frames (examples from RFC 6455 section 5.7)
    rfc = bytes([0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51, 0x58])
    check("websocket: masking (RFC 6455 example 'Hello')", bond.ws_xor(rfc[4:], rfc[:4]) == b"Hello" and bond.ws_xor(b"", b"abcd") == b"")
    big = bytes(range(256)) * 256  # 64 KiB: the 8-byte length form
    f64 = bond.ws_frame(2, big, mask=False)
    check("websocket: lengths 125 / 126 / 65536 use the 1 / 2 / 8-byte forms",
          bond.ws_frame(2, b"x" * 125, False)[:2] == b"\x82\x7d" and bond.ws_frame(2, b"x" * 126, False)[:4] == b"\x82\x7e\x00\x7e"
          and f64[:10] == b"\x82\x7f" + (65536).to_bytes(8, "big") and len(f64) == 10 + 65536)
    mk = bond.ws_frame(2, b"hi", mask=True)
    check("websocket: a client frame is masked", mk[1] == 0x82 and mk[2:6] != b"" and bond.ws_xor(mk[6:], mk[2:6]) == b"hi")
    # sizes and the rule
    check("scheduler: window 64 KiB..16 MiB, chunk 4..64 KiB",
          bond.l_window(1, 0.1) == 65536 and bond.l_window(1e9, 1) == 16 << 20 and abs(bond.l_window(1 << 20, 0.4) - (1 << 20)) < 1
          and bond.l_chunk(1) == 4096 and bond.l_chunk(1e9) == 65536 and bond.l_chunk(256 * 1024) == 13107)
    fast, slow = bond.BLink(0), bond.BLink(1)
    fast.rate, fast.rtt, fast.state = 7.5e6, 0.04, "up"
    slow.rate, slow.rtt, slow.state = 75e3, 1.7, "up"
    p = bond.pick_link([slow, fast], 1 << 20)
    check("scheduler: the fast link gets the piece while it has room", p[0] is fast and p[2])
    fast.sent = int(bond.l_window(fast.rate, fast.rtt))  # its window is full
    p = bond.pick_link([slow, fast], 1 << 20)
    check("scheduler: window full -> WAIT for an ACK, the slow link gets nothing", p[0] is fast and not p[2])
    fast.sent = 12_000_000  # a long queue: the slow link would deliver first now
    p = bond.pick_link([slow, fast], 1 << 20)
    check("scheduler: once the fast link's queue is long the slow link gets a piece", p[0] is slow and p[2])
    fast.sent = 0
    a_, b_ = bond.BLink(0), bond.BLink(1)
    a_.state = b_.state = "up"
    a_.sent = 50_000
    check("scheduler: equal links - the emptier one wins", bond.pick_link([a_, b_], 100_000)[0] is b_ and bond.pick_link([], 5) is None)
    # rate estimate: idle windows say nothing, a faster sample counts at once, a slower one by 1/4
    b = bond.BLink(0)
    fl = bond.BondFlow.__new__(bond.BondFlow)
    b.rate, b.rt_t, b.rt_acked, b.rt_sent = 1e6, 10.0, 0, 100
    fl.rate_window(b, 10.25)
    check("rate: a window with nothing sent keeps the rate", b.rate == 1e6)
    b.sent, b.acked, b.peak, b.rt_t, b.rt_acked, b.rt_sent = 700_000, 600_000, 800_000, 20.0, 0, 0
    fl.rate_window(b, 20.25)
    check("rate: a full window with a faster sample takes it at once (2.4 MB/s)", abs(b.rate - 2.4e6) < 1)
    b.sent, b.acked, b.peak, b.rt_t, b.rt_acked, b.rt_sent = 900_000, 600_000 + 100_000, 3_000_000, 30.0, 600_000, 700_000
    fl.rate_window(b, 30.25)
    check("rate: a slower sample moves it by 1/4 only, never below 8 KiB/s", abs(b.rate - (2.4e6 * 0.75 + 0.4e6 * 0.25)) < 1)
    b.sent, b.acked, b.peak, b.rt_t, b.rt_acked, b.rt_sent = 1, 1, 10 ** 9, 40.0, 1, 0
    b.rate = 9000
    fl.rate_window(b, 40.25)
    check("rate: floor", b.rate == 8192)
    # which hosts
    check("registered domain: last two labels, three for co.jp / com.au",
          bond.reg_domain("a.b.example.com") == "example.com" and bond.reg_domain("x.example.co.jp") == "example.co.jp"
          and bond.reg_domain("foo.com.au") == "foo.com.au" and bond.reg_domain("example.com.") == "example.com")


def bond_hosts():
    """bondable(): every rule on its own."""
    import ipaddress
    saved = (dict(bond.links), bond.cf_nets, bond.relay_off_until, dict(bond.host_bad), bond.vpn_on, list(bond.pinned_exact),
             list(bond.pinned_sub))
    d = bond.STATE_DIR
    rf, bf, lf = (os.path.join(d, n) for n in (bond.RELAY_FILE, bond.BULK_FILE, bond.LEARNED_FILE))
    for p in (rf, bf, lf):
        if os.path.exists(p):
            os.remove(p)
    bond.learned_bulk.clear()
    bond._learned_loaded[0] = True
    bond.links.clear()
    bond.pinned_exact[:], bond.pinned_sub[:] = ["bank.example"], []
    for i in (1, 2):
        bond.links[i] = bond.new_link(1, "ph%d" % i, "127.0.0.1")
    bond.cf_nets = [ipaddress.ip_network("10.16.0.0/13")]
    bond.relay_off_until, bond.vpn_on = 0.0, False
    bond.host_bad.clear()
    check("bondable: no relay.local.txt -> off, and it says why", not bond.bondable("r1.gvt1.com", 443, "8.8.8.8")
          and bond.bond_off_reason() == "no relay set up" and bond.relay_cfg() is None)
    with open(rf, "w", encoding="utf-8") as f:
        f.write("ws://127.0.0.1:8787\n" + "ab" * 32 + "\n")
    cfg = bond.relay_cfg()
    check("relay.local.txt: address and secret are read", cfg and cfg["host"] == "127.0.0.1" and cfg["port"] == 8787 and not cfg["tls"]
          and cfg["secret"] == bytes.fromhex("ab" * 32))
    with open(rf, "w", encoding="utf-8") as f:
        f.write("wss://relay.example.com/x\n" + "cd" * 32 + "\n")
    cfg = bond.relay_cfg()
    check("relay.local.txt: wss defaults to 443, the file is read again when it changes",
          cfg and cfg["tls"] and cfg["port"] == 443 and cfg["base"] == "/x")
    with open(rf, "w", encoding="utf-8") as f:
        f.write("http://nope\nzz\n")
    check("relay.local.txt: a broken file means off", bond.relay_cfg() is None)
    with open(rf, "w", encoding="utf-8") as f:
        f.write("ws://127.0.0.1:8787\n" + "ab" * 32 + "\n")
    ok = bond.bondable("r1.gvt1.com", 443, "8.8.8.8")
    check("bondable: a built-in bulk host with two links", ok and bond.bond_status()["on"] and bond.bond_status()["why_off"] is None)
    check("bondable: a host that is not bulk is not", not bond.bondable("example.com", 443, "8.8.8.8"))
    check("bondable: an address behind Cloudflare is not", not bond.bondable("r1.gvt1.com", 443, "10.16.1.1"))
    check("bondable: mail ports are not", not bond.bondable("r1.gvt1.com", 25, "8.8.8.8") and not bond.bondable("r1.gvt1.com", 587, "8.8.8.8"))
    bond.pinned_exact[:] = ["gvt1.com"]
    check("bondable: a named download host is bonded even under a pinned domain (it has no login)",
          bond.bondable("r1.gvt1.com", 443, "8.8.8.8"))
    bond.learned_bulk["files.pinme.example"] = time.time()
    bond.pinned_exact[:] = ["pinme.example"]
    check("bondable: a pinned host (stays on one IP) that was only learned is not",
          not bond.bondable("files.pinme.example", 443, "8.8.8.8"))
    bond.learned_bulk.pop("files.pinme.example", None)
    bond.pinned_exact[:] = []
    del bond.links[2]
    check("bondable: one link only -> off (nothing to bond)", not bond.bondable("r1.gvt1.com", 443, "8.8.8.8")
          and bond.bond_off_reason() == "fewer than 2 links")
    bond.links[2] = bond.new_link(1, "ph2", "127.0.0.1")
    bond.links[2]["suspect"] = True
    check("bondable: a suspect link does not count", not bond.bondable("r1.gvt1.com", 443, "8.8.8.8"))
    bond.links[2]["suspect"] = False
    bond.cf_nets = None
    check("bondable: Cloudflare ranges not known yet -> off", not bond.bondable("r1.gvt1.com", 443, "8.8.8.8")
          and bond.bond_off_reason() == "Cloudflare ranges not known yet")
    with open(os.path.join(d, bond.CF_FILE), "w", encoding="utf-8") as f:
        json.dump({"at": time.time(), "v4": ["10.16.0.0/13", "1.2.3.4/32"]}, f)
    bond.cf_state["tried"] = 0.0
    check("bondable: the ranges come from cf-ips.json", bond.bondable("r1.gvt1.com", 443, "8.8.8.8")
          and not bond.bondable("r1.gvt1.com", 443, "1.2.3.4"))
    bond.host_bad["r1.gvt1.com"] = time.time() + 100
    check("bondable: a host the relay could not reach is skipped for a while", not bond.bondable("r1.gvt1.com", 443, "8.8.8.8"))
    bond.host_bad["r1.gvt1.com"] = time.time() - 1
    bond.relay_off_until, bond.relay_why = time.time() + 100, "relay refused (HTTP 403)"
    check("bondable: the relay refusing us pauses everything", not bond.bondable("r1.gvt1.com", 443, "8.8.8.8")
          and bond.bond_status()["why_off"] == "relay refused (HTTP 403)")
    bond.relay_off_until = 0.0
    bond.vpn_on = True
    check("bondable: VPN on -> off", not bond.bondable("r1.gvt1.com", 443, "8.8.8.8"))
    bond.vpn_on = False
    # bulk.local.txt and learning
    with open(bf, "w", encoding="utf-8") as f:
        f.write("# mine\ncdn.mysite.example  # comment\n*.big.example\n")
    check("bulk.local.txt: your own hosts (suffix match, comments, *.)", bond.bondable("a.cdn.mysite.example", 443, "8.8.8.8")
          and bond.bondable("big.example", 443, "8.8.8.8") and not bond.bondable("mysite.example", 443, "8.8.8.8"))
    rec = {"n_up": 100, "n_down": bond.LEARN_BYTES - 1}
    bond.learn_from(rec, bond.links[1], "files.learnme.co.jp")
    check("learning: below 8 MB nothing is learned", not bond.bondable("x.learnme.co.jp", 443, "8.8.8.8"))
    rec["n_down"] = bond.LEARN_BYTES
    bond.learn_from(rec, bond.BOND_LINK, "files.learnme.co.jp")
    bond.learn_from(rec, bond.SYSTEM_LINK, "files.learnme.co.jp")
    bond.learn_from(rec, bond.links[1], "10.1.2.3")
    check("learning: a bonded tunnel, the system route and an IP address do not teach anything", not bond.learned_bulk)
    bond.learn_from(rec, bond.links[1], "files.learnme.co.jp")
    saved_file = json.load(open(lf, encoding="utf-8"))
    check("learning: 8 MB in one direction -> that exact host is bulk and kept in bulk-learned.json",
          bond.bondable("files.learnme.co.jp", 443, "8.8.8.8") and "files.learnme.co.jp" in saved_file)
    check("learning: other hosts of the same domain (login pages ...) are not", not bond.bondable("other.learnme.co.jp", 443, "8.8.8.8"))
    bond.learned_bulk["files.learnme.co.jp"] = time.time() - bond.LEARN_KEEP_SEC - 5
    bond.save_learned()
    bond.load_learned()
    check("learning: forgotten after 30 days", "files.learnme.co.jp" not in bond.learned_bulk)
    for i in range(bond.LEARN_MAX + 20):
        bond.learn_bulk("h%d.site%d.example" % (i, i))
    check("learning: at most 500 hosts", len(bond.learned_bulk) <= bond.LEARN_MAX)
    for p in (rf, bf, lf, os.path.join(d, bond.CF_FILE)):
        if os.path.exists(p):
            os.remove(p)
    bond.learned_bulk.clear()
    bond.links.clear()
    bond.links.update(saved[0])
    bond.cf_nets, bond.relay_off_until, bond.vpn_on = saved[1], saved[2], saved[4]
    bond.host_bad.clear()
    bond.host_bad.update(saved[3])
    bond.pinned_exact[:], bond.pinned_sub[:] = saved[5], saved[6]


async def bond_flow_logic():
    """The flow without any relay: pieces handed to fake sockets, answers fed in by hand."""
    now = time.monotonic()
    # upstream: split over two equal links, kept until confirmed
    f, (b0, b1) = fake_flow()
    b0.rate = b1.rate = 256 << 10  # small windows, so 50 KB must use both links
    data = bytes(range(256)) * 200  # 51200 bytes
    f.app_write(data)
    f.pump()
    got = {}
    for b in (b0, b1):
        for fr in b.ws.frames(bond.T_DATA):
            got[fr[1]] = fr[2]
    check("flow up: everything written is sent over the two links with consecutive numbers",
          sorted(got) == list(range(len(got))) and b"".join(got[s] for s in sorted(got)) == data
          and b0.ws.frames(bond.T_DATA) and b1.ws.frames(bond.T_DATA))
    check("flow up: per-link bytes are counted (also on the link dict)",
          b0.sent + b1.sent == len(data) and b0.link["b_up"] == b0.sent and b1.link["bytes"] == b1.sent)
    seqs = sorted(got)
    f.on_ack(len(seqs) // 2, {0: b0.sent // 2, 1: b1.sent // 2})
    check("flow up: an ACK frees the confirmed pieces and the link's bytes",
          f.cum == len(seqs) // 2 and len(f.chunks) == len(seqs) - len(seqs) // 2 and b0.inflight == b0.sent - b0.sent // 2)
    # a link goes silent: its unconfirmed pieces go to the other one
    f, (b0, b1) = fake_flow()
    b0.rate = b1.rate = 256 << 10
    f.app_write(data[:30000])
    f.pump()
    on1 = [fr[1] for fr in b1.ws.frames(bond.T_DATA)]
    n0 = len(b0.ws.frames(bond.T_DATA))
    f.link_dead(b1, now)
    f.pump()
    again = [fr[1] for fr in b0.ws.frames(bond.T_DATA)][n0:]
    check("flow up: pieces on a silent link are sent again on the live one (nothing is lost)",
          on1 and sorted(again) == sorted(on1) and b1.state == "dead")
    f.on_msg(b1, bond.f_pong(int(time.monotonic() * 1000)))
    check("flow up: a silent link that is heard again is live again", b1.state == "up")
    # downstream: out of order, duplicate, FIN
    f, (b0, b1) = fake_flow(rtt_ms=40)
    b1.rtt = 0.02
    f.on_msg(b0, bond.f_data(1, b"BB"))
    f.on_msg(b1, bond.f_data(0, b"AA"))
    f.on_msg(b0, bond.f_data(0, b"AA"))  # a duplicate
    got = await asyncio.wait_for(f.reader.read(10), 2)
    check("flow down: pieces from two links come out in order, a duplicate is dropped", got == b"AABB" and f.cum_rx == 2)
    check("flow down: bytes are counted per link, duplicates included", b0.rx == 4 and b1.rx == 2 and b0.link["b_down"] == 4)
    f.send_ack(time.monotonic())
    ack = (b1.ws.frames(bond.T_ACK) or [None])[-1]
    check("flow down: the ACK goes out on the link with the shortest answer time and carries cum + per-link totals",
          ack == (bond.T_ACK, 2, {0: 4, 1: 2}) and not b0.ws.frames(bond.T_ACK))
    f.on_msg(b0, bond.f_fin(3))
    check("flow down: FIN before the last piece waits for it", not f.eof)
    f.on_msg(b1, bond.f_data(2, b"C"))
    got = await asyncio.wait_for(f.reader.read(10), 2)
    tail = await asyncio.wait_for(f.reader.read(10), 2)
    check("flow down: FIN + all pieces -> end of stream", got == b"C" and tail == b"" and f.eof)
    # the app side
    f, (b0, b1) = fake_flow()
    w = f.writer
    w.write(b"x" * 10)
    w.close()
    w.close()
    check("flow: the second close() with the answer unfinished means the app is gone -> flow ends",
          f.dead and w.is_closing() and w.get_extra_info("peername") is None)
    try:
        await f.reader.read(1)
        failed_ok = False
    except ConnectionError:
        failed_ok = True
    check("flow: ...and the app's read fails (not a clean end)", failed_ok)
    f, (b0, b1) = fake_flow()
    f.writer.write(b"hello")
    f.writer.close()
    f.pump()
    fins = [fr for fr in b0.ws.frames(bond.T_FIN) + b1.ws.frames(bond.T_FIN)]
    check("flow: close() sends FIN(total) on the live links once the data is out", fins and all(x == (bond.T_FIN, 1) for x in fins))
    f.on_msg(b0, bond.f_err("connect: refused"))
    check("flow: ERR from the relay ends the flow", f.dead)
    f, (b0, b1) = fake_flow()
    f.on_data(b0, 0, b"x" * 10, now)
    f.rx_buf_bytes = bond.BOND_REORDER_MAX
    f.on_data(b0, 5, b"y", now)
    check("flow down: more than 32 MiB out of order ends the flow", f.dead and f.why == "reorder overflow")
    bond.bond_flows.clear()


async def bond_ws_and_fallback():
    # the WebSocket client against a server written here
    got_paths = []
    srv_msgs = []

    async def handler(r, w):
        ws = await ws_server_side(r, w, got_paths.append)
        w.write(bond.ws_frame(9, b"hi", mask=False))  # a ping from the server
        w.write(bond.ws_frame(2, b"AAA", mask=False, fin=False) + bond.ws_frame(10, b"", mask=False)  # a pong in the middle
                + bond.ws_frame(0, b"BBB", mask=False, fin=True))  # ...of a fragmented message
        w.write(bond.ws_frame(1, b"text is ignored", mask=False))
        while True:
            m = await ws.recv()
            if m is None:
                break
            srv_msgs.append(len(m))
            ws.send(m)  # echo
        w.close()

    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    r, w = await asyncio.open_connection("127.0.0.1", port)
    ws = await bond.ws_handshake(r, w, "127.0.0.1:%d" % port, "/f/abc?x=1")
    m1 = await asyncio.wait_for(ws.recv(), 2)
    check("websocket: a fragmented message with a pong in the middle arrives whole (text and ping are skipped)", m1 == b"AAABBB")
    big = os.urandom(70000)
    ws.send(big)
    ws.send(b"")
    ws.send(b"x" * 300)
    e1, e2, e3 = [await asyncio.wait_for(ws.recv(), 5) for _ in range(3)]
    check("websocket: masked client frames of 70000 / 0 / 300 bytes are echoed back intact", e1 == big and e2 == b"" and e3 == b"x" * 300
          and srv_msgs == [70000, 0, 300])
    check("websocket: the handshake sent the path", got_paths == ["/f/abc?x=1"])
    ws.close(1000)
    end = await asyncio.wait_for(ws.recv(), 5)
    check("websocket: close ends recv() with None", end is None)
    srv.close()
    # a server that answers 403
    async def deny(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()
    srv = await asyncio.start_server(deny, "127.0.0.1", 0)
    r, w = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1])
    try:
        await bond.ws_handshake(r, w, "x", "/")
        st = None
    except bond.WSHandshakeError as e:
        st = e.status
    check("websocket: HTTP 403 instead of 101 is reported with its code", st == 403)
    srv.close()

    # open_via with and without a relay
    import ipaddress
    saved = (dict(bond.links), bond.cf_nets, bond.relay_off_until, bond.relay_why, bond.vpn_on)
    d = bond.STATE_DIR
    rf, bf = os.path.join(d, bond.RELAY_FILE), os.path.join(d, bond.BULK_FILE)
    eport = free_port()
    e = await asyncio.start_server(echo, "127.0.0.1", eport)
    bond.links.clear()
    for i in (1, 2):
        l = bond.new_link(1, "ph%d" % i, "127.0.0.1")
        l["rtt"] = 20
        bond.links[i] = l
    # the numbers in links must be real interface numbers for best_if(): the loopback is 1, both fake links use it
    bond.links[1]["idx"] = bond.links[2]["idx"] = 1
    bond.cf_nets = [ipaddress.ip_network("10.16.0.0/13")]
    bond.relay_off_until, bond.vpn_on = 0.0, False
    with open(bf, "w", encoding="utf-8") as f:
        f.write("127.0.0.1\n")
    if os.path.exists(rf):
        os.remove(rf)

    async def via_echo(payload):
        l, r, w = await bond.open_via("127.0.0.1", eport)
        w.write(payload)
        await w.drain()
        got = await asyncio.wait_for(r.readexactly(len(payload)), 5)
        w.close()
        return l, got

    l, got = await via_echo(b"direct")
    check("fallback: relay.local.txt missing -> the normal path, nothing changes", l is not bond.BOND_LINK and got == b"direct")
    with open(rf, "w", encoding="utf-8") as f:
        f.write("ws://127.0.0.1:%d\n" % free_port() + "ab" * 32 + "\n")  # nothing listens there
    t0 = time.time()
    l, got = await via_echo(b"fallback")
    took = time.time() - t0
    check("fallback: relay unreachable -> the normal path at once (no 6 s wait; took %.1f s)" % took, l is not bond.BOND_LINK and got == b"fallback" and took < 5)
    check("fallback: ...and the relay is left alone for a minute (every later connection is direct at once)",
          bond.relay_off_until > time.time() and bond.bond_status()["why_off"] == "relay not reachable")
    t0 = time.time()
    l, got = await via_echo(b"again")
    check("fallback: the next one does not try the relay", l is not bond.BOND_LINK and got == b"again" and time.time() - t0 < 1)

    # a small relay written here: signature checked, OK sent, DATA echoed
    secret = bytes(range(32))
    seen = []

    async def fake_relay(r, w):
        import hashlib
        import hmac as hmac_
        import urllib.parse
        path = []
        ws = await ws_server_side(r, w, path.append)
        u = urllib.parse.urlsplit(path[0])
        q = dict(urllib.parse.parse_qsl(u.query))
        flow = u.path.rsplit("/", 1)[1]
        sig = hmac_.new(secret, ("%s|%s|%s|%s" % (flow, q["l"], q["t"], q["d"])).encode(), hashlib.sha256).hexdigest()
        seen.append((hmac_.compare_digest(sig, q["s"]), abs(time.time() - int(q["t"])) < 60, len(flow), q["l"]))
        dst = base64_decode(q["d"])
        seen.append(dst)
        ws.send(bond.f_ok(), ctl=True)
        down = up = got_b = 0
        while True:
            m = await ws.recv()
            if m is None:
                break
            fr = bond.decode_frame(m)
            if fr and fr[0] == bond.T_DATA:
                ws.send(bond.f_data(down, fr[2]))
                down += 1
                up, got_b = max(up, fr[1] + 1), got_b + len(fr[2])
                ws.send(bond.f_ack(up, [(int(q["l"]), got_b)]), ctl=True)
            elif fr and fr[0] == bond.T_FIN:
                ws.send(bond.f_fin(down), ctl=True)
        w.close()

    def base64_decode(s):
        import base64
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()

    rs = await asyncio.start_server(fake_relay, "127.0.0.1", 0)
    rport = rs.sockets[0].getsockname()[1]
    with open(rf, "w", encoding="utf-8") as f:
        f.write("ws://127.0.0.1:%d\n" % rport + secret.hex() + "\n")
    bond.relay_off_until = 0.0
    b_before = bond.BOND_LINK["active"]
    l, r, w = await bond.open_via("127.0.0.1", eport)
    check("bonded open: open_via returns the 'bonded' pseudo link, the signature is right, the destination is host:port",
          l is bond.BOND_LINK and l["name"] == "bonded" and bond.BOND_LINK["active"] == b_before + 1
          and seen and seen[0][0] and seen[0][1] and seen[0][2] == 32 and seen[1] == "127.0.0.1:%d" % eport)
    for _ in range(50):
        if len(seen) >= 4:
            break
        await asyncio.sleep(0.02)
    w.write(b"through the relay")
    await w.drain()
    got = await asyncio.wait_for(r.readexactly(17), 5)
    w.close()
    tail = await asyncio.wait_for(r.read(10), 5)
    check("bonded open: bytes go out and come back through the relay protocol, FIN gives end of stream", got == b"through the relay" and tail == b"")
    ups = sum(x["b_up"] for x in bond.links.values())
    check("bonded open: the bytes are counted on the real links", ups >= 17 and sum(x["b_down"] for x in bond.links.values()) >= 17
          and bond.bond_status()["flows"] in (0, 1) and bond.bond_status()["bytes"] >= 34)
    link_n = len({s[3] for s in seen if isinstance(s, tuple)})
    check("bonded open: one WebSocket per link (two links -> two link numbers)", link_n == 2)
    await asyncio.sleep(0.3)
    rs.close()
    e.close()
    for p in (rf, bf):
        if os.path.exists(p):
            os.remove(p)
    bond.links.clear()
    bond.links.update(saved[0])
    bond.cf_nets, bond.relay_off_until, bond.relay_why, bond.vpn_on = saved[1], saved[2], saved[3], saved[4]


async def bond_tests():
    bond_unit()
    bond_hosts()
    await bond_flow_logic()
    await bond_ws_and_fallback()


if __name__ == "__main__":
    unit()
    asyncio.run(e2e())
    asyncio.run(bond_tests())
    print("\nALL PASSED" if not failed else f"\n{len(failed)} FAILED: {failed}")
    sys.exit(1 if failed else 0)
