#!/usr/bin/env python3
"""link-bond - use several internet connections (USB / Wi-Fi tethering, Ethernet ...) at the same time.

A tiny local proxy (SOCKS5 + HTTP, 127.0.0.1 only). Every new connection that goes through it is sent out
over one of your connections, chosen by measured speed and current load. Many connections at once
(browser tabs, parallel downloads, several uploads) add up to the sum of the links.
A single connection still runs at the speed of one link - there is no server that re-assembles packets.

Windows 10/11, Python 3.9+, standard library only.
"""
import argparse
import asyncio
import collections
import ctypes
import fnmatch
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse

__version__ = "1.5.0"
UPDATE_REPO = "baadee55/link-bond"  # a newer release on GitHub is announced (checked once a day, ~5 KB)

HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))  # next to the .exe
STATE_DIR = HERE  # where flag files and usage.json live (the self-test points this at a temp folder)
IP_UNICAST_IF = 31  # Windows: force the outgoing interface (works even when a VPN owns 0.0.0.0/1)
SKIP_WORDS = ("tailscale", "openvpn", "nord", "wireguard", "tap-", "vpn", "hyper-v", "virtual", "vmware", "loopback")
SPEED_HOST = "speed.cloudflare.com"
DOWN_BYTES = 3_000_000   # the full speed test (only when you press "measure"): download per link
UP_BYTES = 200_000       # ...and upload
SMALL_DOWN, SMALL_UP = 300_000, 100_000  # the small first test of a link nobody has measured yet (~0.4 MB)
RESCAN_SEC = 8           # how often to look for links appearing / disappearing
REMEASURE_SEC = 0        # periodic full speed tests: off (they cost ~3 MB per link); --remeasure-hours to turn on
CAP_HALF_LIFE = 3 * 86400  # a link's best real speed ("capacity") halves in 3 days unless it is reached again
PAUSE_SEC = 30           # a link that fails to connect is skipped for this long
DECAY_SEC = 5            # recent-traffic memory: halves in ~20 s
PRIOR_UP, PRIOR_DOWN = 50_000.0, 500_000.0  # assume "mostly downloading" when idle
PROBE_HOST, PROBE_PORT, PROBE_PATH = "cp.cloudflare.com", 80, "/generate_204"  # HTTP check, also spots a
                                                                                # prepaid "out of credit" page
PING_HOST, PING_PORT = "8.8.8.8", 53  # the cheap check: one tiny DNS question to Google Public DNS (~200 bytes)
PROBE_SEC = 15           # cheap check interval per link (~1.2 MB a day per link)
HTTP_CHECK_SEC = 120     # the HTTP check runs this often (~0.5 MB a day per link), and at once when a ping fails
FAILS_TO_DOWN = 3        # consecutive failed checks before a link counts as cut (~45 s)
REMIND_SEC = 1800        # repeat the "still cut" notice this often
labels = {}              # adapter name -> friendly name (labels.local.txt)
no_notify = False
announced = set()        # links already seen once (the first scan is silent)

HISTORY_LEN, HISTORY_SEC = 150, 2   # dashboard speed graph: 150 samples, 2 s apart = 5 minutes
REPORT_HOUR = 9          # the daily usage report is shown at the first scan after this hour

links = {}  # ifindex -> link dict
live = {}  # token -> {"link", "sw", "cw", "t_open", "t_data"} for every open tunnel
BUSY_SEC = 3    # a tunnel that moved data within this many seconds counts as busy
NEW_SEC = 5     # ...and a freshly opened one counts as busy for this long (so a burst of new connections spreads)
history = {}  # label -> deque of Mbps samples carried through the proxy
traffic = {"up": 0.0, "down": 0.0}  # recent bytes, client->server / server->client
pinned_exact, pinned_sub = [], []
vpn_on = False
no_remeasure = False


def log(*a):
    """Write one line to the log. Never fails: a Japanese Windows writes the log in cp932, which cannot hold every
    character (a ✓ in a notice once crashed link-bond at start)."""
    line = " ".join([time.strftime("%H:%M:%S")] + [str(x) for x in a])
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
    except Exception:
        pass


def new_link(idx, name, ip):
    return dict(idx=idx, name=name, ip=ip, down=1.0, up=1.0, active=0, total=0, bytes=0, paused_until=0.0,
                measured_at=0.0, measured=False, healthy=True, fails=0, why="", down_since=0.0, last_notice=0.0,
                ctr=None, gw="", conn_fails=0, qc_at=0.0, b_up=0, b_down=0, now_up=0.0, now_down=0.0,
                http_at=0.0, pings=collections.deque(maxlen=20), rtt=None, loss=0, retry_at=0.0, measuring=False)


# A "link" that means: do not choose, follow Windows routing (used while a VPN holds all traffic).
SYSTEM_LINK = new_link(None, "system-route (VPN on)", None)
vpn_present = False  # a VPN owns the default route
vpn_bypass = False   # ...but link-bond's own connections may still leave over the phones (VPN split tunneling
                     # excludes linkbond.exe): then link-bond bonds all links and the VPN keeps the other apps


def bypass_exe():
    """The file to add to a VPN's split-tunneling exclusion list (a copy of python.exe named linkbond.exe)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.join(os.path.dirname(sys.executable), "linkbond.exe")


async def bypass_ok():
    """Can link-bond's own sockets leave over a phone while the VPN is up? (a 3-second TCP connect, no data)"""
    loop = asyncio.get_running_loop()
    for l in list(links.values()):
        s = None
        try:
            s = mk_sock(l)
            await asyncio.wait_for(loop.sock_connect(s, (PING_HOST, 443)), 3)
            return True
        except Exception:
            pass
        finally:
            if s:
                s.close()
    return False


def load_labels():
    """labels.local.txt lines:  Adapter name = friendly name   (shown in notifications)"""
    labels.clear()
    try:
        with open(os.path.join(HERE, "labels.local.txt"), encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0]
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() and v.strip():
                        labels[k.strip()] = v.strip()
    except OSError:
        pass


def label(l):
    return labels.get(l["name"], l["name"])


def read_kv(name):
    """Lines 'key = value' of a config file next to bond.py (# starts a comment)."""
    out = []
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0]
                if "=" in line:
                    k, v = (x.strip() for x in line.split("=", 1))
                    if k and v:
                        out.append((k, v))
    except OSError:
        pass
    return out


ports_map = {}  # label -> local port that always uses that device          ports.local.txt


def load_ports():
    ports_map.clear()
    for k, v in read_kv("ports.local.txt"):
        try:
            ports_map[k] = int(v)
        except ValueError:
            pass


# ---------- modes (switched by flag files so a desktop button, the dashboard and the PC all agree) ----------
eco_on = True     # ON unless eco.off exists  -> heavy video hosts are slowed down (data saving is the basic mode)
block_on = False  # block.flag exists        -> ad / tracker hosts are refused (opt-in)
power_on = True   # ON unless power.off exists -> the PC's traffic goes through link-bond (the supervisor sets the
                  #                               Windows proxy accordingly; OFF gives the PC its original settings back)
PROXY_PORT = 1080


def state_path(name):
    return os.path.join(STATE_DIR, name)


def refresh_modes():
    global eco_on, block_on, power_on
    e, b, p = not os.path.exists(state_path("eco.off")), os.path.exists(state_path("block.flag")), \
        not os.path.exists(state_path("power.off"))
    for name, old, new in (("eco mode", eco_on, e), ("ad blocking", block_on, b), ("PC-wide switch", power_on, p)):
        if old != new:
            log(f"{name} {'ON' if new else 'OFF'}")
    eco_on, block_on, power_on = e, b, p


def proxy_applied():
    """Is the Windows proxy really pointing at link-bond right now?"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            return bool(winreg.QueryValueEx(k, "ProxyEnable")[0]) and winreg.QueryValueEx(k, "ProxyServer")[0] == f"127.0.0.1:{PROXY_PORT}"
    except Exception:
        return False


def supervised():
    """The crash-safe supervisor (run.ps1) touches supervisor.beat every few seconds."""
    try:
        return time.time() - os.path.getmtime(state_path("supervisor.beat")) < 15
    except OSError:
        return False


def set_mode(name, on):
    fn, present = {"eco": ("eco.off", not on), "block": ("block.flag", on), "power": ("power.off", not on)}[name]
    p = state_path(fn)
    try:
        if present:
            open(p, "a").close()
        elif os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


async def modes_loop():
    while True:
        refresh_modes()
        await asyncio.sleep(3)


# ---------- ad / tracker blocking ----------
blocked_domains = set()
blocked_total = 0
blocked_hosts = collections.Counter()


def load_blocklist():
    """blocklist.txt + blocklist.local.txt: one domain per line; a leading '-' lets a domain through again."""
    blocked_domains.clear()
    for name in ("blocklist.txt", "blocklist.local.txt"):
        try:
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                for line in f:
                    t = line.split("#")[0].strip().lower()
                    if t.startswith("-"):
                        blocked_domains.discard(t[1:].strip())
                    elif t:
                        blocked_domains.add(t)
        except OSError:
            pass


def is_blocked(host):
    if not block_on or not blocked_domains:
        return False
    parts = host.lower().rstrip(".").split(".")
    return any(".".join(parts[i:]) in blocked_domains for i in range(max(len(parts) - 1, 1)))


# ---------- Eco mode: cap the speed of heavy hosts (video) so players drop to a lower quality ----------
# eco.txt / eco.local.txt lines:  host pattern (* allowed) = Mbps for ALL connections to matching hosts together.
# Traffic is encrypted, so we cannot change the quality ourselves; slowing the video server makes the player pick
# a lower quality on its own (YouTube ~2.5 Mbps = about 480p). ON by default; the file eco.off switches it off.
eco_rules = []  # [Rule]


class Rule:
    def __init__(self, pattern, mbps):
        self.pattern, self.mbps = pattern.lower(), mbps
        self.rate = mbps * 1e6 / 8  # bytes per second, shared by every connection that matches
        self.next = 0.0
        self.bytes = 0

    async def take(self, n):
        now = time.monotonic()
        self.next = max(self.next, now) + n / self.rate
        wait = self.next - now - 0.25  # a small burst keeps streaming smooth
        if wait > 0:
            await asyncio.sleep(wait)


def load_eco(paths=None):
    eco_rules.clear()
    for path in paths or [os.path.join(HERE, "eco.txt"), os.path.join(HERE, "eco.local.txt")]:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.split("#")[0]
                    if "=" in line:
                        k, v = (x.strip() for x in line.split("=", 1))
                        try:
                            if k and float(v) > 0:
                                eco_rules.append(Rule(k, float(v)))
                        except ValueError:
                            pass
        except OSError:
            pass


def eco_rule(host):
    h = host.lower()
    return next((r for r in eco_rules if fnmatch.fnmatch(h, r.pattern)), None)


# ---------- data usage per device (counted from the adapter's own byte counter) ----------
# The carrier's real balance cannot be read from the PC. We count what THIS PC used over each device and,
# if you tell us the plan size in budget.local.txt ("Name = 20000" in MB), show the rest and warn at 80/95/100 %.
budgets = {}      # label -> MB
reset_day = 1     # day of month the plan renews
usage = {"cycle": "", "used": {}, "alerted": []}
usage_dirty = False


def cycle_id(now=None):
    t = time.localtime(now or time.time())
    y, m = t.tm_year, t.tm_mon
    if t.tm_mday < reset_day:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return f"{y}-{m:02d}"


def load_budget():
    global reset_day
    budgets.clear()
    try:
        with open(os.path.join(HERE, "budget.local.txt"), encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0]
                if "=" in line:
                    k, v = (x.strip() for x in line.split("=", 1))
                    try:
                        if k.lower() == "reset_day":
                            reset_day = min(max(int(v), 1), 28)
                        elif k:
                            budgets[k] = float(v)
                    except ValueError:
                        pass
    except OSError:
        pass
    try:
        with open(state_path("usage.json"), encoding="utf-8") as f:
            u = json.load(f)
        if u.get("cycle") == cycle_id():
            usage.update(u)
        else:  # a new plan period: keep the day-by-day history, the report marker and the running totals
            usage.update(daily=u.get("daily", {}), last_report=u.get("last_report", ""), total=u.get("total", {}))
    except (OSError, ValueError):
        pass
    usage["cycle"] = cycle_id()
    load_prepaid()


def add_usage(key, n):
    global usage_dirty
    if cycle_id() != usage["cycle"]:  # plan renewed: start counting again
        usage.update(cycle=cycle_id(), used={}, alerted=[], since=0)
    if n > 0:
        usage["used"][key] = usage["used"].get(key, 0) + n
        total = usage.setdefault("total", {})  # never reset: prepaid balances use it
        total[key] = total.get(key, 0) + n
        usage.setdefault("since", time.time())
        if not usage["since"]:
            usage["since"] = time.time()
        day = usage.setdefault("daily", {}).setdefault(time.strftime("%Y-%m-%d"), {})
        day[key] = day.get(key, 0) + n
        for old in sorted(usage["daily"])[:-45]:  # keep 45 days
            del usage["daily"][old]
        usage_dirty = True


def save_usage():
    global usage_dirty
    if usage_dirty:
        usage_dirty = False
        try:
            with open(state_path("usage.json"), "w", encoding="utf-8") as f:
                json.dump(usage, f)
        except OSError:
            pass


def cycle_start(now=None):
    y, m = (int(x) for x in cycle_id(now).split("-"))
    return time.mktime((y, m, reset_day, 0, 0, 0, 0, 0, -1))


def next_cycle_start(now=None):
    y, m = (int(x) for x in cycle_id(now).split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return time.mktime((y, m, reset_day, 0, 0, 0, 0, 0, -1))


# ---------- prepaid balance: you type what is left, link-bond subtracts what this PC uses from then on ----------
# The carrier's balance cannot be read from a PC, so the starting amount is entered by hand (dashboard) and again
# whenever you top up. prepaid.json: {label: {base_MB, at_total, set_at, alerted}}
prepaid = {}


def load_prepaid():
    prepaid.clear()
    try:
        with open(state_path("prepaid.json"), encoding="utf-8") as f:
            prepaid.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_prepaid():
    try:
        with open(state_path("prepaid.json"), "w", encoding="utf-8") as f:
            json.dump(prepaid, f, ensure_ascii=False)
    except OSError:
        pass


def total_bytes(key):
    return usage.get("total", {}).get(key, 0)


def set_prepaid(key, remaining_mb=None, add_mb=None, clear=False):
    """remaining_mb: what the carrier says is left right now.  add_mb: you topped up by this much."""
    if clear:
        prepaid.pop(key, None)
    else:
        now_left = plan_of(key)[0] - plan_of(key)[1] if key in prepaid else 0.0
        base = float(remaining_mb) if remaining_mb is not None else max(now_left, 0.0) + float(add_mb or 0)
        prepaid[key] = {"base_MB": base, "at_total": total_bytes(key), "set_at": time.time(), "alerted": []}
    save_prepaid()


def plan_of(key):
    """(size_MB, used_MB) of this device's plan, or None. A prepaid balance wins over a monthly budget."""
    p = prepaid.get(key)
    if p:
        t = total_bytes(key)
        if t < p["at_total"]:  # usage.json was reset: start counting again from here
            p["at_total"] = t
        return p["base_MB"], (t - p["at_total"]) / 1e6
    if key in budgets:
        return budgets[key], used_mb(key)
    return None


def forecast_days(key, now=None):
    """Days until this device's plan runs out at the pace so far, or None (no plan size / nothing used yet).
    The pace counts only the time since link-bond started counting, so it is a rough guide."""
    now = now or time.time()
    plan = plan_of(key)
    if not plan or plan[1] <= 0:
        return None
    size, used = plan
    start = prepaid[key]["set_at"] if key in prepaid else max(cycle_start(now), usage.get("since") or now)
    rate = used / max((now - start) / 86400, 0.25)  # MB per day
    return max(size - used, 0) / rate


def used_mb(key):
    return usage["used"].get(key, 0) / 1e6


def crossed(key):
    """Newly reached alert level (80/95/100) for this device, or None. Each level fires once per plan / top-up."""
    plan = plan_of(key)
    if not plan or plan[0] <= 0:
        return None
    pct = plan[1] / plan[0] * 100
    alerted = prepaid[key]["alerted"] if key in prepaid else usage["alerted"]
    for lvl in (100, 95, 80):
        if pct >= lvl:
            if f"{key}:{lvl}" in alerted:
                return None
            alerted += [f"{key}:{x}" for x in (80, 95, 100) if x <= lvl]
            if key in prepaid:
                save_prepaid()
            return lvl
    return None


def check_budget(l):
    """Notify only - nothing is switched off automatically (topping up is up to you)."""
    key = label(l)
    lvl = crossed(key)
    if lvl:
        size, used = plan_of(key)
        left = max(size - used, 0)
        topup = "（チャージの目安です）" if key in prepaid and lvl >= 95 else ""
        notify(f"link-bond: {key} データ {lvl}% / data {lvl}% used",
               f"使用 {used:,.0f} MB / {size:,.0f} MB（残り約 {left:,.0f} MB）{topup}\n"
               f"Used {used:,.0f} of {size:,.0f} MB (about {left:,.0f} MB left).")


def daily_report(now=None):
    """(title, text) summarising yesterday's data use per device and the plan pace, or None when there is nothing to say."""
    now = now or time.time()
    yday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    day = usage.get("daily", {}).get(yday)
    if not day:
        return None
    lines = []
    for key, b in sorted(day.items()):
        line = f"{key}: {b / 1e6:,.0f} MB"
        plan = plan_of(key)
        if plan:
            d = forecast_days(key, now)
            left_days = 9999 if key in prepaid else (next_cycle_start(now) - now) / 86400
            line += f"（残り約 {max(plan[0] - plan[1], 0):,.0f} MB）"
            if d is not None and d < left_days and d < 30:
                line += f" ⚠ このペースだと約 {d:.0f} 日で使い切ります / runs out in ~{d:.0f} days"
        lines.append(line)
    return f"link-bond: 昨日の通信量 / yesterday's data ({yday})", "\n".join(lines)


def maybe_report(now=None):
    """Show the daily report once per day, at the first call after REPORT_HOUR."""
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if time.localtime(now).tm_hour < REPORT_HOUR or usage.get("last_report") == today:
        return None
    usage["last_report"] = today
    global usage_dirty
    usage_dirty = True
    rep = daily_report(now)
    if rep:
        notify(*rep)
    return rep


events = collections.deque(maxlen=30)  # what happened, newest last (shown in the widget and the dashboard)


def add_event(text):
    events.append({"t": time.strftime("%H:%M:%S"), "text": text})


def fleet_summary():
    """One line with the state of every device, e.g. 'Phone A: 正常/OK / Phone B: 切断/CUT（1本で継続中）'."""
    ls = sorted(links.values(), key=label)
    names = {l["name"] for l in ls}
    stopped = [labels.get(n, n) for n in sorted(stalled) if n not in names]  # plugged in, tethering stopped
    if not ls and not stopped:
        return "回線なし / no links"
    ok = sum(1 for l in ls if l["healthy"])
    total = len(ls) + len(stopped)
    tail = "" if ok == total else (f"（{ok}本で継続中 / carrying on with {ok}）" if ok else "（使える回線がありません / none usable）")
    parts = [f"{label(l)}: {'正常/OK' if l['healthy'] else '切断/CUT'}" for l in ls] + [f"{s}: テザリング停止/STOPPED" for s in stopped]
    return " / ".join(parts) + tail


def notify_link(title, text):
    """A notification about one device that also tells the state of all of them."""
    notify(title, text + "\n" + fleet_summary())


def drop_link_tunnels(link):
    """A link just died: close the connections that were stuck on it so the apps reconnect at once through a
    working link. Does nothing when there is no other working link (nothing better to move them to)."""
    if not any(l is not link and l["healthy"] for l in links.values()):
        return 0
    n = 0
    for rec in list(live.values()):
        if rec["link"] is link:
            for w in (rec["sw"], rec["cw"]):
                try:
                    w.close()
                except Exception:
                    pass
            n += 1
    if n:
        add_event(f"{label(link)} の {n} 本の通信を他の回線へ移しました / moved {n} connection(s) to another link")
        log(f"moved {n} connection(s) off {link['name']}")
    return n


def notify(title, text):
    """Windows balloon notification (also written to the log and kept in the event list)."""
    log("NOTICE:", title, "-", text)
    add_event(title.replace("link-bond: ", "", 1) + " — " + text.splitlines()[0])
    if no_notify:
        return
    ps = ("Add-Type -AssemblyName System.Windows.Forms;Add-Type -AssemblyName System.Drawing;"
          "$n=New-Object Windows.Forms.NotifyIcon;$n.Icon=[Drawing.SystemIcons]::Warning;$n.Visible=$true;"
          "$n.ShowBalloonTip(20000,$env:LB_T,$env:LB_X,'Warning');Start-Sleep 22;$n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         env=dict(os.environ, LB_T=title, LB_X=text), creationflags=0x08000000)
    except Exception:
        pass


# ---------- pinned hosts ----------
def load_pinned(paths=None):
    pinned_exact.clear()
    pinned_sub.clear()
    for path in paths or [os.path.join(HERE, "pinned.txt"), os.path.join(HERE, "pinned.local.txt")]:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    t = line.split("#")[0].strip().lower()
                    if t:
                        (pinned_sub if t.startswith("~") else pinned_exact).append(t.lstrip("~"))
        except OSError:
            pass


def is_pinned(host):
    """Pinned hosts always leave over the same (fastest) link, so a login session keeps one public IP."""
    h = host.lower()
    return any(h == t or h.endswith("." + t) for t in pinned_exact) or any(t in h for t in pinned_sub)


# ---------- finding the links ----------
PS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "$r=Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0 | ForEach-Object {"
    "$a=Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue;"
    "$ip=(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue|Select-Object -First 1).IPAddress;"
    "$s=Get-NetAdapterStatistics -Name $a.Name -ErrorAction SilentlyContinue;"
    "[pscustomobject]@{idx=[int]$_.InterfaceIndex;name=[string]$a.Name;desc=[string]$a.InterfaceDescription;ip=$ip;"
    "metric=[int]($_.RouteMetric+$_.InterfaceMetric);gw=[string]$_.NextHop;bytes=[long]($s.SentBytes+$s.ReceivedBytes)}};"
    "$v=@(Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.DestinationPrefix -like '*/1'}).Count -gt 0;"
    "$ap=@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.IPAddress -like '169.254.*'}|ForEach-Object {"
    "$x=Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue;"
    "if($x -and $x.Status -eq 'Up'){[string]$x.Name+'|'+[string]$x.InterfaceDescription}});"
    "ConvertTo-Json -InputObject @{vpn=$v;links=@($r);stalled=@($ap)} -Compress -Depth 4"
)


def discover_sync():
    """Return ({ifindex: (name, ip, adapter_byte_counter, gateway)}, vpn_holds_all_traffic, stalled_adapter_names)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
        capture_output=True, timeout=20, creationflags=0x08000000,
    ).stdout.decode("utf-8", "replace").strip()
    data = json.loads(out or "{}")
    found, vpn = classify(data.get("links") or [], bool(data.get("vpn")))
    return found, vpn, stalled_names(data.get("stalled") or [])


stalled = set()  # adapters that are plugged in but got no address (169.254.x): tethering stopped on the phone


def stalled_names(rows):
    """Connected adapters stuck on a 169.254.x address ('name|description'), real ones only - not VPN, virtual,
    Wi-Fi Direct or Bluetooth adapters, which sit on such addresses all the time."""
    out = set()
    for row in rows:
        name, _, desc = str(row).partition("|")
        text = (name + " " + desc).lower()
        if name and not any(w in text for w in SKIP_WORDS + ("bluetooth", "wi-fi direct")):
            out.add(name)
    return out


def gone_message(name):
    """(title, text) when a device disappears: a stopped tethering is told apart from an unplugged phone."""
    lab = labels.get(name, name)
    if name in stalled:
        return (f"link-bond: {lab} のテザリングが止まりました / tethering stopped",
                "ケーブルはつながっていますが、スマホからアドレスが来ていません。スマホで USB テザリングを OFF→ON してください。\n"
                "The cable is connected but the phone gives no address - switch USB tethering off and on.")
    return (f"link-bond: {lab} が外れました / disconnected",
            "USB が抜けた・ホットスポットが切れた可能性があります。\nUnplugged or hotspot turned off?")


def classify(rows, slash1=False):
    """Split default-route adapters into real links and VPN-like adapters.
    A VPN holds the traffic when it owns 0.0.0.0/0 with a better (lower) metric than every real link - that is how
    NordLynx/WireGuard works - or when the two half-internet "/1" routes exist (OpenVPN style, slash1)."""
    found, real_metrics, vpn_metrics = {}, [], []
    for r in rows:
        text = (r["name"] + " " + r["desc"]).lower()
        if any(w in text for w in SKIP_WORDS):
            vpn_metrics.append(int(r.get("metric") or 0))
            continue
        ip = r.get("ip") or ""
        if not ip or ip.startswith("169.254."):
            continue
        found[r["idx"]] = (r["name"], ip, int(r.get("bytes") or 0), r.get("gw") or "")
        real_metrics.append(int(r.get("metric") or 0))
    best = min(real_metrics) if real_metrics else 1 << 30
    return found, slash1 or any(m < best for m in vpn_metrics)


def mk_sock(link):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setblocking(False)
    if link["idx"] is not None:
        s.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", link["idx"]))
        s.bind((link["ip"], 0))
    return s


def best_if(ip):
    """Interface index Windows itself would use for this destination (0 if unknown)."""
    try:
        out = ctypes.c_ulong(0)
        if ctypes.windll.iphlpapi.GetBestInterface(struct.unpack("<I", socket.inet_aton(ip))[0], ctypes.byref(out)) == 0:
            return out.value
    except Exception:
        pass
    return 0


def drop_bonded_tunnels():
    """A VPN just took over: close every tunnel we pinned to a physical link, so the apps reconnect through the VPN."""
    n = 0
    for rec in list(live.values()):
        if rec["link"] is not SYSTEM_LINK:
            for w in (rec["sw"], rec["cw"]):
                try:
                    w.close()
                except Exception:
                    pass
            n += 1
    return n


def busy(link, now=None):
    """How many of this link's connections are really working right now. Idle keep-alive connections (a browser
    keeps dozens open) do not count - otherwise a fast but idle link would look 'full' and work would go to a
    slow one."""
    now = now or time.time()
    return sum(1 for r in live.values()
               if r["link"] is link and (now - r["t_data"] < BUSY_SEC or now - r["t_open"] < NEW_SEC))


# ---------- speed tests (small, per link) ----------
async def connect_tls(link, host):
    loop = asyncio.get_running_loop()
    s = mk_sock(link)
    try:
        ip = (await loop.getaddrinfo(host, 443, family=socket.AF_INET))[0][4][0]
        await asyncio.wait_for(loop.sock_connect(s, (ip, 443)), 10)
        return await asyncio.open_connection(sock=s, ssl=ssl.create_default_context(), server_hostname=host)
    except BaseException:
        s.close()
        raise


async def measure_down(link, nbytes=DOWN_BYTES):
    """Download speed in Mbps, 0 on failure."""
    try:
        r, w = await connect_tls(link, SPEED_HOST)
        w.write(f"GET /__down?bytes={nbytes} HTTP/1.1\r\nHost: {SPEED_HOST}\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        first, n = None, 0
        while True:
            b = await asyncio.wait_for(r.read(65536), 15)
            if not b:
                break
            first = first or time.time()
            n += len(b)
        w.close()
        return n * 8 / max(time.time() - first, 0.05) / 1e6 if first and n > min(100_000, nbytes // 3) else 0.0
    except Exception as e:
        log("download test failed:", link["name"], type(e).__name__)
        return 0.0


async def measure_up(link, nbytes=UP_BYTES):
    """Upload speed in Mbps. A stalled uplink counts as 0.05."""
    try:
        r, w = await connect_tls(link, SPEED_HOST)
        t0 = time.time()
        w.write(f"POST /__up HTTP/1.1\r\nHost: {SPEED_HOST}\r\nContent-Length: {nbytes}\r\nConnection: close\r\n\r\n".encode()
                + b"\0" * nbytes)
        await w.drain()
        await asyncio.wait_for(r.readline(), 12)
        w.close()
        return nbytes * 8 / max(time.time() - t0, 0.05) / 1e6
    except Exception:
        return 0.05


# ---------- capacity: what a link can really do, learned from real traffic (costs no data) ----------
# Every 2 s the adapter's real speed is compared with the best seen; the best slowly fades (CAP_HALF_LIFE) so a link
# that got slower is not overrated for ever. Kept per device AND network (a phone that fell back to another Wi-Fi is
# another network). Saved in capacity.json, so a restart does not need a speed test.
capacity = {}  # "label|gateway" -> {"down": Mbps, "up": Mbps, "at": time the best download was seen}


def cap_key(l):
    return f"{label(l)}|{l.get('gw') or ''}"


def load_capacity():
    capacity.clear()
    try:
        with open(state_path("capacity.json"), encoding="utf-8") as f:
            capacity.update({k: v for k, v in json.load(f).items() if isinstance(v, dict)})
    except (OSError, ValueError):
        pass


def save_capacity():
    try:
        with open(state_path("capacity.json"), "w", encoding="utf-8") as f:
            json.dump(capacity, f, ensure_ascii=False)
    except OSError:
        pass


def learn_capacity(l, down, up, dt, now=None):
    """Fold one real-speed sample into the link's capacity. Returns the entry."""
    now = now or time.time()
    c = capacity.setdefault(cap_key(l), {"down": 0.0, "up": 0.0, "at": 0.0})
    fade = 0.5 ** (dt / CAP_HALF_LIFE)
    c["down"], c["up"] = c["down"] * fade, c["up"] * fade
    if down > c["down"]:
        c["down"], c["at"] = down, now
    c["up"] = max(c["up"], up)
    if c["down"] >= 0.5:  # something real is known: it drives the link choice
        l["down"], l["up"], l["measured"] = max(c["down"], 0.5), max(c["up"], 0.05), True
    return c


def adopt_capacity(l):
    """A link (re)appears: use what is known about it, so no speed test is needed. True when something was known."""
    c = capacity.get(cap_key(l))
    if c and c.get("down", 0) >= 0.5:
        l["down"], l["up"], l["measured"] = c["down"], max(c.get("up", 0.05), 0.05), True
        return True
    return False


async def measure_and_set(l, full=False):
    """A speed test of one link: small (~0.4 MB) for a link nobody knows yet, full (~3.2 MB) when you ask for it."""
    l["measuring"] = True
    try:
        d = await measure_down(l, DOWN_BYTES if full else SMALL_DOWN)
        if d <= 0:
            l["paused_until"] = time.time() + PAUSE_SEC
            l["retry_at"] = time.time() + 120  # try again in two minutes
            log(f"speed test {l['name']}: failed")
            return
        u = await measure_up(l, UP_BYTES if full else SMALL_UP)
        c = capacity.setdefault(cap_key(l), {"down": 0.0, "up": 0.0, "at": 0.0})
        if full:  # a full test is the truth now, also when the link got slower
            c.update(down=d, up=u, at=time.time())
        else:
            c.update(down=max(c["down"], d), up=max(c["up"], u), at=c["at"] or time.time())
        l["down"], l["up"], l["paused_until"], l["measured_at"], l["measured"] = c["down"], c["up"], 0.0, time.time(), True
        save_capacity()
        log(f"speed test {l['name']} ({'full' if full else 'small'}): down {d:.1f} / up {u:.1f} Mbps")
    finally:
        l["measuring"] = False


async def refresh_loop():
    global vpn_on, vpn_present, vpn_bypass
    last = None
    while True:
        try:
            found, v, st = await asyncio.to_thread(discover_sync)
            for n in st - stalled:
                log("tethering stopped (no address):", n)
            stalled.clear()
            stalled.update(st)
            bypass = bool(v and links and await bypass_ok())
            if (v, bypass) != (vpn_present, vpn_bypass):
                if v and bypass:
                    log("VPN detected, but link-bond is excluded from it (split tunneling): bonding all links")
                    add_event("VPN と束ねを両立中（VPN は他のアプリ用） / VPN + bonding")
                elif v:
                    log("VPN detected: following the system route (add linkbond.exe to the VPN's split tunneling to bond)")
                    notify("link-bond: VPN がつながったので束ねを止めました / VPN on: not bonding",
                           "PC 版の VPN（NordVPN など）が全部の通信を 1 本にまとめています。VPN を切ると、全回線の束ねに戻ります。\n"
                           "A desktop VPN is carrying all traffic through one tunnel. Turn it off to bond all links again.")
                else:
                    log("VPN gone: bonding all links")
            vpn_present, vpn_bypass = v, bypass
            on = v and not bypass
            if on != vpn_on:
                vpn_on = on
                if on:
                    log(f"closed {drop_bonded_tunnels()} open connection(s) so they reconnect through the VPN")
            for idx in list(links):
                if idx not in found:
                    log("link gone:", links[idx]["name"])
                    gone = links.pop(idx)
                    drop_link_tunnels(gone)
                    notify_link(*gone_message(gone["name"]))
                    note_flap(label(gone))
            for idx, (name, ip, ctr, gw) in found.items():
                l = links.get(idx)
                if l is None:
                    l = links[idx] = new_link(idx, name, ip)
                    l["gw"] = gw
                    known = adopt_capacity(l)
                    log("link added:", name, ip, f"(known: down {l['down']:.1f} / up {l['up']:.1f} Mbps)" if known else "")
                    if name in announced:
                        notify_link(f"link-bond: {label(l)} つながりました / connected", f"{ip}")
                    announced.add(name)
                elif apply_network(l, ip, gw):
                    log("network changed:", name, "->", ip, "via", gw)
                    adopt_capacity(l)
                    drop_link_tunnels(l)
                    notify_link(f"link-bond: {label(l)} が別のネットワークにつながりました / switched network",
                                f"ゲートウェイが {gw} に変わりました。スマホが落ちて、Windows が保存済みの別の Wi-Fi に自動でつなぎ替えた可能性があります。\n"
                                f"The gateway is now {gw}: Windows may have auto-joined another saved Wi-Fi after the phone dropped.")
                    note_flap(label(l))
                elif l["ip"] != ip:
                    log("link address changed:", name, l["ip"], "->", ip)
                    l["ip"], l["measured_at"] = ip, 0.0
                prev, l["ctr"] = l["ctr"], ctr
                if prev is not None:  # a counter that went down means the adapter was reset: count from 0
                    add_usage(label(l), ctr - prev if ctr >= prev else ctr)
                check_budget(l)
                # A speed test costs data, so it only runs for a link nobody knows yet (small test), or periodically
                # if --remeasure-hours asks for it. Everything else is learned from real traffic (learn_capacity).
                # Never while a VPN holds the traffic: tests bypass it and a kill switch would fail them.
                now = time.time()
                first = not l["measured"] and now >= l["retry_at"]
                periodic = REMEASURE_SEC > 0 and not no_remeasure and now - l["measured_at"] > REMEASURE_SEC
                if (first or periodic) and not vpn_on and not l["measuring"]:
                    l["measured_at"], l["retry_at"] = now, now + 120
                    asyncio.create_task(measure_and_set(l, full=periodic and not first))
            maybe_report()
        except Exception as e:
            log("link scan failed:", type(e).__name__)
        save_usage()
        save_capacity()
        sig = sorted((l["name"], round(l["down"]), round(l["up"])) for l in links.values())
        if sig != last:
            last = sig
            log("links:", "; ".join(f"{n} down {d} / up {u} Mbps" for n, d, u in sig) or "none")
        await asyncio.sleep(RESCAN_SEC)


# ---------- health check: tell the user when a device loses the internet ----------
async def probe(link):
    """(ok, why). why: 'redirect' = answers but is not the internet (captive portal / prepaid credit or data
    used up), 'no-response' = nothing comes back (no signal, tethering off, data cut)."""
    try:
        loop = asyncio.get_running_loop()
        s = mk_sock(link)
        try:
            ip = (await loop.getaddrinfo(PROBE_HOST, PROBE_PORT, family=socket.AF_INET))[0][4][0]
            await asyncio.wait_for(loop.sock_connect(s, (ip, PROBE_PORT)), 5)
            r, w = await asyncio.open_connection(sock=s)
        except BaseException:
            s.close()
            raise
        w.write(f"GET {PROBE_PATH} HTTP/1.1\r\nHost: {PROBE_HOST}\r\nConnection: close\r\n\r\n".encode())
        line = await asyncio.wait_for(r.readline(), 6)
        w.close()
        parts = line.split()
        if len(parts) >= 2 and parts[1] == b"204":
            return True, ""
        return False, "redirect" if parts else "no-response"
    except Exception:
        return False, "no-response"


flap_log = {}  # label -> times (s) the device dropped, newest last


def note_flap(key, now=None):
    """Remember that a device dropped. Returns True when it just dropped 3+ times in 30 minutes (and tells the user)."""
    now = now or time.time()
    fl = [t for t in flap_log.get(key, []) if now - t < 1800] + [now]
    flap_log[key] = fl
    if len(fl) >= 3 and now - flap_log.get(key + "#notice", [0])[-1] > 3600:
        flap_log[key + "#notice"] = [now]
        notify(f"link-bond: {key} が不安定です / unstable",
               "30 分に 3 回以上切れています。Wi-Fi のホットスポットより、USB ケーブルでのテザリングの方が安定することが多いです。\n"
               "Dropped 3+ times in 30 minutes - USB tethering is usually more stable than a Wi-Fi hotspot.")
        return True
    return False


def apply_network(l, ip, gw):
    """True when this device is now on a different network - e.g. Windows auto-joined another saved Wi-Fi after the
    phone's hotspot dropped. Speed and usage then no longer describe the phone, so it is measured again."""
    if gw and l.get("gw") and l["gw"] != gw:
        l.update(gw=gw, ip=ip, measured_at=0.0, measured=False, down=1.0, up=1.0, fails=0)
        return True
    if gw:
        l["gw"] = gw
    return False


def apply_probe(l, ok, why, now=None):
    """Update a link's health. Returns 'down' | 'up' | 'remind' | None (what to tell the user)."""
    now = now or time.time()
    if ok:
        l["fails"] = 0
        if not l["healthy"]:
            l["healthy"], l["paused_until"] = True, 0.0
            return "up"
        return None
    l["fails"] += 1
    l["why"] = why
    if l["healthy"] and l["fails"] >= FAILS_TO_DOWN:
        l["healthy"], l["down_since"], l["last_notice"] = False, now, now
        return "down"
    if not l["healthy"] and now - l["last_notice"] >= REMIND_SEC:
        l["last_notice"] = now
        return "remind"
    return None


def tell(l, event):
    name = label(l)
    if event == "up":
        notify_link(f"link-bond: {name} 復旧 / back online", f"{name} はまたネットに出られます。 / {name} has internet again.")
        return
    mins = int((time.time() - l["down_since"]) / 60)
    if event == "down":
        note_flap(name)
    if l["why"] == "redirect":
        jp = "つながっているのにネットに出られません。プリペイドの残高・データ切れ、または認証待ちの可能性があります。"
        en = "Connected but no internet - prepaid credit or data may have run out."
    else:
        jp = "応答がありません。電波なし・テザリングOFF・データ切れの可能性があります。"
        en = "No response - weak signal, tethering off, or data used up."
    head = "切れました / lost internet" if event == "down" else f"まだ切れています({mins}分) / still cut"
    if event == "down":
        drop_link_tunnels(l)
    notify_link(f"link-bond: {name} {head}", f"{jp}\n{en}")


async def quick_check(l):
    """Connections to this link keep failing: check it right now instead of waiting for the next regular check."""
    now = time.time()
    if now - l["qc_at"] < 15 or vpn_on:
        return
    l["qc_at"] = now
    ok, why = await probe(l)
    if ok:
        l["conn_fails"] = 0
        return
    l["fails"] = max(l["fails"], FAILS_TO_DOWN - 1)  # one more miss = cut
    event = apply_probe(l, False, why)
    if event:
        tell(l, event)


def _dns_question():
    name = b"".join(bytes([len(p)]) + p.encode() for p in "one.one.one.one".split(".")) + b"\0"
    return struct.pack("!HHHHHH", 0x4C42, 0x0100, 1, 0, 0, 0) + name + struct.pack("!HH", 1, 1)


DNS_Q = _dns_question()


class _OneDatagram(asyncio.DatagramProtocol):
    def __init__(self):
        self.answer = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, addr):
        if not self.answer.done():
            self.answer.set_result(data)

    def error_received(self, exc):
        if not self.answer.done():
            self.answer.set_exception(exc)


async def ping(link):
    """(ok, response time in ms) - one tiny DNS question over this link (~200 bytes with the answer)."""
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    tr = None
    try:
        if link["idx"] is not None:
            s.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", link["idx"]))
            s.bind((link["ip"], 0))
        tr, pr = await loop.create_datagram_endpoint(_OneDatagram, sock=s)
        t0 = time.monotonic()
        tr.sendto(DNS_Q, (PING_HOST, PING_PORT))
        data = await asyncio.wait_for(pr.answer, 3)
        return len(data) >= 12 and data[:2] == DNS_Q[:2], round((time.monotonic() - t0) * 1000)
    except Exception:
        return False, None
    finally:
        if tr:
            tr.close()
        else:
            s.close()


def note_ping(l, ok, rtt):
    """Keep the last 20 cheap checks: the typical response time (median) and how many got lost (%)."""
    l["ping_at"], l["ping_ok"] = time.time(), ok
    l["pings"].append(rtt if ok else None)
    good = sorted(x for x in l["pings"] if x is not None)
    l["rtt"] = good[len(good) // 2] if good else None
    l["loss"] = round(100 * (len(l["pings"]) - len(good)) / len(l["pings"]))


def quality(l):
    """'good' | 'slow' | 'bad' | 'unknown' - the link's condition, from the cheap checks."""
    if not l["healthy"]:
        return "bad"
    if l["rtt"] is None:
        return "unknown"
    if l["loss"] >= 20 or l["rtt"] >= 800:
        return "bad"
    if l["loss"] >= 5 or l["rtt"] >= 250:
        return "slow"
    return "good"


async def check_link(l, now):
    """One regular check: the cheap DNS question; the HTTP check only every HTTP_CHECK_SEC or when that fails
    (it tells 'no answer at all' from 'a prepaid / login page answers instead of the internet')."""
    ok, rtt = await ping(l)
    note_ping(l, ok, rtt)
    why = ""
    if not ok or now - l["http_at"] >= HTTP_CHECK_SEC:
        l["http_at"] = now
        hok, why = await probe(l)
        if ok and not hok and why == "redirect":
            ok = False            # DNS answers, but the web is an "out of credit" page
        elif not ok and hok:
            ok, why = True, ""    # DNS is blocked on this network, but the internet works
    return ok, why


async def health_tick():
    ls = list(links.values())
    if vpn_on:
        # A VPN (its kill switch in particular) blocks checks that bypass it: pause instead of crying wolf.
        for l in ls:
            l["fails"] = 0
        return
    if not ls:
        return
    now = time.time()
    for l, (ok, why) in zip(ls, await asyncio.gather(*(check_link(l, now) for l in ls))):
        event = apply_probe(l, ok, why)
        if event:
            tell(l, event)


async def health_loop():
    while True:
        await asyncio.sleep(PROBE_SEC)
        await health_tick()


async def decay_loop():
    while True:
        await asyncio.sleep(DECAY_SEC)
        traffic["up"] *= 0.85
        traffic["down"] *= 0.85


# ---------- choosing a link ----------
def upload_share():
    u, d = traffic["up"] + PRIOR_UP, traffic["down"] + PRIOR_DOWN
    return u / (u + d)


def weight(l):
    """Expected usefulness (Mbps) given what the PC is doing right now: mostly uploading -> uplink counts more."""
    s = upload_share()
    return max(l["down"] * (1 - s) + l["up"] * s, 0.2)


def candidates(host, only=None):
    """Links to try, best first. `only` = a device label: the dedicated port of that device (no fallback)."""
    if vpn_on:
        return [SYSTEM_LINK]
    now = time.time()
    if only is not None:
        return [l for l in links.values() if label(l) == only and l["healthy"]]
    u = [l for l in links.values() if l["paused_until"] <= now and l["healthy"]] or list(links.values())
    if is_pinned(host):
        return sorted(u, key=lambda l: -weight(l))
    # ALL links are used at the same time, each in proportion to its speed: the next connection goes to the link
    # with the least WORKING connections per Mbps. A link that drops simply leaves this list; the rest carry it all.
    return sorted(u, key=lambda l: (busy(l, now) + 1) / weight(l))


async def open_via(host, port, only=None):
    global blocked_total
    if is_blocked(host):
        blocked_total += 1
        blocked_hosts[host.lower()] += 1
        raise PermissionError("blocked: " + host)
    loop = asyncio.get_running_loop()
    try:
        ip = (await loop.getaddrinfo(host, port, family=socket.AF_INET))[0][4][0]
    except Exception:
        raise ConnectionError("cannot resolve " + host)
    last = None
    cands = candidates(host, only)
    if not cands:
        raise ConnectionError(f"link {only!r} is not available")
    if cands != [SYSTEM_LINK] and not vpn_bypass and best_if(ip) not in links:
        cands = [SYSTEM_LINK]  # Windows routes this destination somewhere else (VPN just started, ...): follow it
    for k, l in enumerate(cands):
        s = None
        try:
            s = mk_sock(l)
            # a dead link must not hold a page up: give up on it after 4 s and try the next one
            await asyncio.wait_for(loop.sock_connect(s, (ip, port)), 4 if k < len(cands) - 1 else 8)
            r, w = await asyncio.open_connection(sock=s)
            l["active"] += 1
            l["total"] += 1
            l["conn_fails"] = 0
            return l, r, w
        except Exception as e:
            last = e
            if s:
                s.close()
            l["paused_until"] = time.time() + PAUSE_SEC
            l["conn_fails"] += 1
            if l["conn_fails"] >= 2 and l is not SYSTEM_LINK:
                asyncio.create_task(quick_check(l))
            log("connect failed, pausing link:", l["name"], host, type(e).__name__)
    raise ConnectionError(f"no link could reach {host} ({last})")


async def pipe(r, w, link, kind, rule=None, rec=None):
    try:
        while True:
            b = await r.read(16384 if rule else 65536)
            if not b:
                break
            w.write(b)
            if rec is not None:
                rec["t_data"] = time.time()
            link["bytes"] += len(b)
            link["b_" + kind] += len(b)
            traffic[kind] += len(b)
            if rule:
                rule.bytes += len(b)
                if eco_on:
                    await rule.take(len(b))
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def tunnel(cr, cw, link, sr, sw, first_data=b"", host=""):
    token = object()
    rec = live[token] = {"link": link, "sw": sw, "cw": cw, "t_open": time.time(), "t_data": 0.0}
    rule = eco_rule(host) if host else None
    try:
        if first_data:
            sw.write(first_data)
        await asyncio.gather(pipe(cr, sw, link, "up", rec=rec), pipe(sr, cw, link, "down", rule, rec))
    finally:
        live.pop(token, None)
        link["active"] -= 1
        for w in (sw, cw):
            try:
                w.close()
            except Exception:
                pass


# ---------- SOCKS5 / HTTP front ends ----------
async def handle(cr, cw, only=None):
    try:
        head = await asyncio.wait_for(cr.readexactly(1), 30)
        if head == b"\x05":
            await socks5(cr, cw, only)
        else:
            await http_proxy(cr, cw, head, only)
    except Exception:
        pass
    finally:
        try:
            cw.close()
        except Exception:
            pass


async def socks5(cr, cw, only=None):
    n = (await cr.readexactly(1))[0]
    await cr.readexactly(n)
    cw.write(b"\x05\x00")
    _, cmd, _, atyp = await cr.readexactly(4)
    if cmd != 1:
        cw.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    if atyp == 1:
        host = socket.inet_ntoa(await cr.readexactly(4))
    elif atyp == 3:
        host = (await cr.readexactly((await cr.readexactly(1))[0])).decode()
    else:  # IPv6 not supported
        cw.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    port = struct.unpack("!H", await cr.readexactly(2))[0]
    try:
        link, sr, sw = await open_via(host, port, only)
    except PermissionError:  # ad / tracker host: "connection not allowed by ruleset"
        cw.write(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    except Exception:
        cw.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    cw.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    await cw.drain()
    await tunnel(cr, cw, link, sr, sw, host=host)


async def http_proxy(cr, cw, head, only=None):
    data = head + await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 30)
    first, _, rest = data.partition(b"\r\n")
    method, target, ver = first.decode("latin-1").split(" ", 2)
    if method.upper() == "CONNECT":
        host, _, p = target.rpartition(":")
        try:
            link, sr, sw = await open_via(host.strip("[]"), int(p or 443), only)
        except PermissionError:
            cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        except Exception:
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cw.drain()
        await tunnel(cr, cw, link, sr, sw, host=host.strip("[]"))
        return
    # plain http: turn the absolute URI into a path and relay a single request
    if not target.lower().startswith("http://"):
        cw.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        return
    hostport, _, path = target[7:].partition("/")
    host, _, p = hostport.partition(":")
    lines = [x for x in rest.split(b"\r\n") if x and not x.lower().startswith((b"proxy-connection:", b"connection:"))]
    req = f"{method} /{path} {ver}\r\n".encode() + b"\r\n".join(lines) + b"\r\nConnection: close\r\n\r\n"
    try:
        link, sr, sw = await open_via(host, int(p or 80), only)
    except PermissionError:
        cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        return
    except Exception:
        cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        return
    await tunnel(cr, cw, link, sr, sw, req, host=host)


# ---------- fast download: ONE file over ALL links at once ----------
# The file is cut into 1 MB pieces. Every link has workers that keep taking the next piece, so a fast link simply
# carries more pieces and a slow one fewer - nobody waits for the slowest (work-stealing). Near the end, idle workers
# repeat pieces that are still running on slower links. This is the one case where a single file really gets the
# SUM of the links, with no server in between. Needs a server that supports HTTP Range (most do); otherwise the
# file is fetched over the best single link.
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
CHUNK = 1 << 20
WORKERS_PER_LINK = 3
ALLOW_LOCAL_DOWNLOADS = False  # tests only
downloads = {}                 # id -> job dict
_dl_seq = 0


class Aborted(Exception):
    pass


def parse_url(url):
    u = urllib.parse.urlsplit(str(url).strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("http:// か https:// のURLを入れてください / need an http(s) URL")
    try:
        ip = ipaddress.ip_address(u.hostname)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local) and not ALLOW_LOCAL_DOWNLOADS:
        raise ValueError("local addresses are not allowed")
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    return u.scheme, u.hostname, port, path


def safe_filename(name):
    name = urllib.parse.unquote(name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c >= " " and c not in '<>:"|?*').strip(" .")
    return name[:120] or "download.bin"


def filename_from(headers, path):
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I) or re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    return safe_filename(m.group(1) if m else urllib.parse.urlsplit(path).path)


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    p, i = os.path.join(folder, name), 1
    while os.path.exists(p) or os.path.exists(p + ".part"):
        p = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return p


class Conn:
    """One keep-alive HTTP(S) connection to a host over a chosen link."""

    def __init__(self, link, scheme, host, port):
        self.link, self.scheme, self.host, self.port = link, scheme, host, port
        self.r = self.w = None

    async def open(self):
        loop = asyncio.get_running_loop()
        s = mk_sock(self.link)
        try:
            ip = (await loop.getaddrinfo(self.host, self.port, family=socket.AF_INET))[0][4][0]
            await asyncio.wait_for(loop.sock_connect(s, (ip, self.port)), 10)
            kw = dict(ssl=ssl.create_default_context(), server_hostname=self.host) if self.scheme == "https" else {}
            self.r, self.w = await asyncio.open_connection(sock=s, **kw)
        except BaseException:
            s.close()
            raise

    def close(self):
        if self.w:
            try:
                self.w.close()
            except Exception:
                pass
        self.r = self.w = None

    async def request(self, path, rng=None, extra=None):
        """extra: {"Cookie", "Referer", "User-Agent"} from the browser that started the download."""
        if self.w is None or self.w.is_closing():
            await self.open()
        hdrs = {"User-Agent": "link-bond"}
        hdrs.update({k: v for k, v in (extra or {}).items() if v})
        more = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        req = (f"GET {path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n{more}Accept: */*\r\n"
               "Accept-Encoding: identity\r\nConnection: keep-alive\r\n" + (f"Range: bytes={rng[0]}-{rng[1]}\r\n" if rng else "") + "\r\n")
        self.w.write(req.encode())
        await self.w.drain()
        head = await asyncio.wait_for(self.r.readuntil(b"\r\n\r\n"), 30)
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
        return int(lines[0].split()[1]), headers

    async def read_body(self, headers, on_data):
        """Stream the body to on_data(bytes). Closes the connection when it cannot be reused."""
        async def take(n):
            while n:
                b = await asyncio.wait_for(self.r.read(min(65536, n)), 30)
                if not b:
                    raise ConnectionError("connection closed")
                on_data(b)
                n -= len(b)

        reusable = headers.get("connection", "").lower() != "close"
        if "chunked" in headers.get("transfer-encoding", "").lower():
            while True:
                size = int((await self.r.readline()).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await self.r.readuntil(b"\r\n")
                    break
                await take(size)
                await self.r.readexactly(2)
        elif "content-length" in headers:
            await take(int(headers["content-length"]))
        else:
            reusable = False
            while True:
                b = await asyncio.wait_for(self.r.read(65536), 30)
                if not b:
                    break
                on_data(b)
        if not reusable:
            self.close()


def dl_links():
    """Links a download uses: every working one (slow ones too - work-stealing gives them fewer pieces)."""
    if vpn_on:
        return [SYSTEM_LINK]
    return [l for l in links.values() if l["healthy"]] or list(links.values())


async def run_download(job):
    try:
        scheme, host, port, path = parse_url(job["url"])
        first_host = host
        extra = dict(job.get("headers") or {})

        def hdrs():  # the browser's cookies only go to the host they belong to (not after a redirect elsewhere)
            return extra if host == first_host else {k: v for k, v in extra.items() if k != "Cookie"}

        ls = dl_links()
        if not ls:
            raise ConnectionError("つながっている回線がありません / no connection")
        best = max(ls, key=weight)
        # 1. ask for the first byte: follows redirects, learns the size and whether Range works
        for _ in range(6):
            c = Conn(best, scheme, host, port)
            status, hdr = await c.request(path, (0, 0), hdrs())
            if status in (301, 302, 303, 307, 308) and hdr.get("location"):
                c.close()
                nu = urllib.parse.urljoin(f"{scheme}://{host}:{port}{path}", hdr["location"])
                scheme, host, port, path = parse_url(nu)
                continue
            break
        else:
            raise ConnectionError("too many redirects")
        if status >= 400:
            c.close()
            raise ConnectionError(f"サーバーがエラーを返しました (HTTP {status}) / server error")
        total = None
        m = re.match(r"bytes\s+\d+-\d+/(\d+)", hdr.get("content-range", ""))
        ranged = status == 206 and m is not None
        if ranged:
            total = int(m.group(1))
        elif hdr.get("content-length", "").isdigit():
            total = int(hdr["content-length"])
        c.close()
        name = safe_filename(job["filename"]) if job.get("filename") else filename_from(hdr, path)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        final = unique_path(DOWNLOAD_DIR, name)
        job.update(name=os.path.basename(final), path=final, size=total, ranged=ranged, t0=time.time())
        part = final + ".part"
        by = job["by_link"]

        def count(l, n):
            by[label(l)] = by.get(label(l), 0) + n

        if not ranged or not total or total <= CHUNK or len(ls) == 0:
            # single stream over the best link (no Range support, unknown or small size)
            job["ranged"] = False
            with open(part, "wb") as f:
                c = Conn(best, scheme, host, port)
                status, hdr = await c.request(path, None, hdrs())
                if status >= 400:
                    raise ConnectionError(f"HTTP {status}")

                def put(b):
                    if job["state"] != "running":
                        raise Aborted()
                    f.write(b)
                    job["done"] += len(b)
                    count(best, len(b))

                await c.read_body(hdr, put)
                c.close()
        else:
            n = (total + CHUNK - 1) // CHUNK
            queue = collections.deque(range(n))
            done_set, inflight, got = set(), {}, [0] * n
            tries = collections.Counter()
            with open(part, "wb") as f:
                f.truncate(total)

                def pick(mine):
                    if queue:
                        i = queue.popleft()
                        inflight[i] = [time.time(), 1]
                        return i
                    now = time.time()  # endgame: repeat a piece that a slower link is still working on
                    cand = [(t, i) for i, (t, k) in inflight.items() if i not in done_set and i != mine and now - t > 2 and k < 2]
                    if cand:
                        i = min(cand)[1]
                        inflight[i][1] += 1
                        return i
                    return None

                async def piece(conn, l, i):
                    start, end = i * CHUNK, min((i + 1) * CHUNK, total) - 1
                    st, h = await conn.request(path, (start, end), hdrs())
                    if st != 206 or not h.get("content-range", "").startswith(f"bytes {start}-"):
                        conn.close()
                        raise ConnectionError("range not honoured")
                    pos = [start]

                    def put(b):
                        if job["state"] != "running" or i in done_set:
                            raise Aborted()
                        f.seek(pos[0])
                        f.write(b)
                        pos[0] += len(b)
                        k = pos[0] - start
                        if k > got[i]:
                            job["done"] += k - got[i]
                            got[i] = k
                        count(l, len(b))

                    await conn.read_body(h, put)
                    if pos[0] != end + 1:
                        raise ConnectionError("short piece")

                async def worker(l):
                    conn, fails = Conn(l, scheme, host, port), 0
                    try:
                        while len(done_set) < n and job["state"] == "running":
                            i = pick(-1)
                            if i is None:
                                await asyncio.sleep(0.2)
                                continue
                            try:
                                await piece(conn, l, i)
                                done_set.add(i)
                                inflight.pop(i, None)
                                fails = 0
                            except Aborted:
                                conn.close()  # someone else finished this piece first
                            except Exception:
                                conn.close()
                                fails += 1
                                if i not in done_set:
                                    tries[i] += 1
                                    if inflight.get(i, [0, 1])[1] <= 1:
                                        inflight.pop(i, None)
                                        queue.appendleft(i)
                                    else:
                                        inflight[i][1] -= 1
                                if tries[i] > 8 or fails >= 4:
                                    break  # this link is not working for this file: the others carry on
                                await asyncio.sleep(1)
                    finally:
                        conn.close()

                await asyncio.gather(*(worker(l) for l in ls for _ in range(WORKERS_PER_LINK)))
            if len(done_set) < n and job["state"] == "running":
                raise ConnectionError("途中で失敗しました / download failed part-way")
        if job["state"] != "running":
            raise Aborted()
        os.replace(part, final)
        job.update(state="done", t1=time.time(), done=job["size"] or job["done"])
    except Aborted:
        job["state"] = "cancelled" if job["state"] == "cancel" else job["state"]
    except Exception as e:
        job.update(state="error", error=str(e) or type(e).__name__, t1=time.time())
    finally:
        if job["state"] in ("cancel", "cancelled", "error"):
            try:
                os.remove((job.get("path") or "") + ".part")
            except OSError:
                pass
            if job["state"] == "cancel":
                job["state"] = "cancelled"


def clean_header(v, limit=8000):
    """A header value from outside: one line, bounded (no header injection)."""
    return str(v or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def start_download(url, cookie="", referer="", user_agent="", filename="", source="manual"):
    """source: 'manual' (pasted URL) or 'browser' (handed over by the browser extension)."""
    global _dl_seq
    parse_url(url)  # raises ValueError for a bad / local URL
    _dl_seq += 1
    headers = {"Cookie": clean_header(cookie), "Referer": clean_header(referer, 2000),
               "User-Agent": clean_header(user_agent, 400) or "link-bond"}
    job = dict(id=_dl_seq, url=str(url).strip(), name="", path="", size=None, done=0, state="running", error="",
               ranged=False, by_link={}, t0=time.time(), t1=0.0, headers=headers, filename=clean_header(filename, 200),
               source=source)
    downloads[job["id"]] = job
    job["task"] = asyncio.create_task(run_download(job))
    for old in sorted(downloads)[:-8]:  # keep the last 8 on the list
        if downloads[old]["state"] != "running":
            downloads.pop(old, None)
    return job


class _MIB_IFROW(ctypes.Structure):
    _fields_ = [("wszName", ctypes.c_wchar * 256), ("dwIndex", ctypes.c_ulong), ("dwType", ctypes.c_ulong),
                ("dwMtu", ctypes.c_ulong), ("dwSpeed", ctypes.c_ulong), ("dwPhysAddrLen", ctypes.c_ulong),
                ("bPhysAddr", ctypes.c_ubyte * 8), ("dwAdminStatus", ctypes.c_ulong), ("dwOperStatus", ctypes.c_ulong),
                ("dwLastChange", ctypes.c_ulong), ("dwInOctets", ctypes.c_ulong), ("dwInUcastPkts", ctypes.c_ulong),
                ("dwInNUcastPkts", ctypes.c_ulong), ("dwInDiscards", ctypes.c_ulong), ("dwInErrors", ctypes.c_ulong),
                ("dwInUnknownProtos", ctypes.c_ulong), ("dwOutOctets", ctypes.c_ulong), ("dwOutUcastPkts", ctypes.c_ulong),
                ("dwOutNUcastPkts", ctypes.c_ulong), ("dwOutDiscards", ctypes.c_ulong), ("dwOutErrors", ctypes.c_ulong),
                ("dwOutQLen", ctypes.c_ulong), ("dwDescrLen", ctypes.c_ulong), ("bDescr", ctypes.c_ubyte * 256)]


def read_octets(idx):
    """(received, sent) byte counters of a network adapter as Windows counts them - ALL of the PC's traffic on it,
    not only what goes through link-bond. 32-bit counters (they wrap; see sample_history). None if unknown."""
    try:
        row = _MIB_IFROW()
        row.dwIndex = idx
        if ctypes.windll.iphlpapi.GetIfEntry(ctypes.byref(row)) == 0:
            return row.dwInOctets, row.dwOutOctets
    except Exception:
        pass
    return None


_prev_bytes = {}
_prev_octets = {}
hist_down, hist_up = {}, {}  # label -> deque of Mbps, downloads / uploads separately
total_down = collections.deque(maxlen=HISTORY_LEN)  # all links together
total_up = collections.deque(maxlen=HISTORY_LEN)


def sample_history(dt=HISTORY_SEC):
    """Every few seconds: how fast each device is going right now, up and down separately, and all together.
    The speed comes from the adapter's own counters (everything the PC sends and receives on that device);
    'via_down / via_up' is the part that went through link-bond."""
    sd = su = 0.0
    for l in list(links.values()) + ([SYSTEM_LINK] if vpn_on else []):
        k = label(l)
        rates = []
        for kind in ("bytes", "b_down", "b_up"):
            b = l[kind]
            d = b - _prev_bytes.get((k, kind), b)
            _prev_bytes[(k, kind)] = b
            rates.append(round(max(d, 0) * 8 / dt / 1e6, 2))
        both, down, up = rates
        l["via_down"], l["via_up"] = down, up
        oc = read_octets(l["idx"]) if l["idx"] is not None else None
        if oc is not None:
            prev = _prev_octets.get(l["idx"])
            _prev_octets[l["idx"]] = oc
            if prev is not None:  # counters are 32-bit: a wrap-around is handled by the modulo
                down = round(((oc[0] - prev[0]) % (1 << 32)) * 8 / dt / 1e6, 2)
                up = round(((oc[1] - prev[1]) % (1 << 32)) * 8 / dt / 1e6, 2)
                both = round(down + up, 2)
        l["now_down"], l["now_up"] = down, up
        history.setdefault(k, collections.deque(maxlen=HISTORY_LEN)).append(both)
        hist_down.setdefault(k, collections.deque(maxlen=HISTORY_LEN)).append(down)
        hist_up.setdefault(k, collections.deque(maxlen=HISTORY_LEN)).append(up)
        sd, su = sd + down, su + up
        if l is not SYSTEM_LINK:  # learn what the link can really do from what it really did (costs no data)
            learn_capacity(l, down, up, dt)
    total_down.append(round(sd, 2))
    total_up.append(round(su, 2))


async def history_loop():
    while True:
        await asyncio.sleep(HISTORY_SEC)
        sample_history()


def problems():
    """Short texts for everything that is wrong right now (empty = all fine). The widget turns red on any."""
    out = []
    for l in sorted(links.values(), key=label):
        q = quality(l)
        if not l["healthy"]:
            out.append(f"{label(l)}: 切断 / cut")
        elif q == "bad":
            out.append(f"{label(l)}: 不安定（応答 {l['rtt']}ms・取りこぼし {l['loss']}%） / unstable")
    names = {l["name"] for l in links.values()}
    out += [f"{labels.get(n, n)}: テザリング停止 / tethering stopped" for n in sorted(stalled) if n not in names]
    if vpn_on:
        out.append("VPN 中のため束ねていません / VPN on: not bonding")
    if power_on and links and not proxy_applied() and supervised():
        out.append("PC に適用されていません / not applied to the PC")
    if not links and not stalled:
        out.append("回線がありません / no connection")
    return out


def status_dict():
    now = time.time()
    out = []
    for l in links.values():
        k = label(l)
        fd = forecast_days(k)
        out.append({
            "name": l["name"], "label": k, "healthy": l["healthy"], "ip": l["ip"], "measured": l["measured"],
            "down_mbps": round(l["down"], 1), "up_mbps": round(l["up"], 1), "weight": round(weight(l), 1),
            "port": ports_map.get(k), "active": l["active"], "busy": busy(l, now), "connections": l["total"],
            "why": l["why"], "down_since": l["down_since"],
            "MB": round(l["bytes"] / 1e6, 1), "paused": l["paused_until"] > now,
            "used_MB": round(plan_of(k)[1] if plan_of(k) else used_mb(k)),
            "budget_MB": round(plan_of(k)[0]) if plan_of(k) else None,
            "remaining_MB": round(max(plan_of(k)[0] - plan_of(k)[1], 0)) if plan_of(k) else None,
            "prepaid": k in prepaid,
            "forecast_days": round(fd, 1) if fd is not None else None,
            "history": list(history.get(k, [])),
            "now_down": l["now_down"], "now_up": l["now_up"],
            "via_down": l.get("via_down", 0.0), "via_up": l.get("via_up", 0.0),
            "rtt_ms": l["rtt"], "loss_pct": l["loss"], "quality": quality(l), "measuring": l["measuring"],
            "checked_ago": round(now - l["ping_at"]) if l.get("ping_at") else None, "last_ok": l.get("ping_ok"),
            "best_at": (capacity.get(cap_key(l)) or {}).get("at") or None,
            "peak_down": max(hist_down.get(k) or [0]), "peak_up": max(hist_up.get(k) or [0]),
            "hist_down": list(hist_down.get(k, [])), "hist_up": list(hist_up.get(k, [])),
        })
    return {
        "version": __version__, "update": dict(update_info) or None, "news": news(),
        "vpn_on": vpn_on, "vpn_present": vpn_present, "vpn_bypass": vpn_bypass,
        "bypass_exe": bypass_exe(), "upload_share": round(upload_share(), 2),
        "modes": {"eco": eco_on, "block": block_on, "power": power_on},
        "proxy_applied": proxy_applied(), "supervised": supervised(), "proxy_port": PROXY_PORT,
        "eco": {"on": eco_on, "rules": [{"host": r.pattern, "cap_mbps": r.mbps, "MB": round(r.bytes / 1e6, 1)} for r in eco_rules]},
        "blocked": {"on": block_on, "total": blocked_total, "top": blocked_hosts.most_common(8)},
        "links": out,
        "problems": problems(),
        "total": {"now_down": total_down[-1] if total_down else 0.0, "now_up": total_up[-1] if total_up else 0.0,
                  "peak_down": max(total_down or [0]), "peak_up": max(total_up or [0]),
                  "hist_down": list(total_down), "hist_up": list(total_up),
                  "via_vpn": vpn_on, "vpn_down": SYSTEM_LINK["now_down"] if vpn_on else 0.0,
                  "vpn_up": SYSTEM_LINK["now_up"] if vpn_on else 0.0},
        "stalled": [{"name": n, "label": labels.get(n, n)} for n in sorted(stalled) if n not in {l["name"] for l in links.values()}],
        "summary": fleet_summary(),
        "events": list(events)[-12:][::-1],
        "downloads": [_dl_view(j) for j in sorted(downloads.values(), key=lambda j: -j["id"])],
        "download_dir": DOWNLOAD_DIR,
    }


def _dl_view(j):
    end = j["t1"] or time.time()
    secs = max(end - j["t0"], 0.5)
    tot = sum(j["by_link"].values()) or 1
    return {"id": j["id"], "name": j["name"] or j["url"], "state": j["state"], "error": j["error"], "size": j["size"],
            "source": j.get("source", "manual"),
            "done": j["done"], "ranged": j["ranged"], "mbps": round(j["done"] * 8 / secs / 1e6, 1),
            "by_link": {k: round(v / tot * 100) for k, v in j["by_link"].items()}}


def status_json():
    return json.dumps(status_dict(), ensure_ascii=False, indent=1)


def http_resp(status, ctype, body):
    if isinstance(body, str):
        body = body.encode()
    return (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
            "Cache-Control: no-store\r\nX-Frame-Options: DENY\r\nX-Content-Type-Options: nosniff\r\n"
            "Content-Security-Policy: default-src 'self' 'unsafe-inline'; frame-ancestors 'none'\r\n\r\n").encode() + body


async def stats_server(cr, cw):
    """Dashboard on 127.0.0.1: GET / (page), GET /status.json, POST /api/mode {"eco":bool,"block":bool}.
    The POST needs the header X-LinkBond: 1 (a web page on another site cannot send it without our permission)
    and a Host of 127.0.0.1 / localhost (blocks DNS-rebinding)."""
    try:
        head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        hdr = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
        if hdr.get("host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            out = http_resp("403 Forbidden", "text/plain", "forbidden")
        elif method == "GET" and path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
                    out = http_resp("200 OK", "text/html; charset=utf-8", f.read())
            except OSError:
                out = http_resp("200 OK", "text/plain; charset=utf-8", status_json())
        elif method == "GET" and path == "/status.json":
            out = http_resp("200 OK", "application/json; charset=utf-8", status_json())
        elif method == "POST" and path == "/api/budget":
            n = int(hdr.get("content-length") or 0)
            if hdr.get("x-linkbond") != "1" or not 0 < n <= 1000:
                out = http_resp("403 Forbidden", "text/plain", "forbidden")
            else:
                d = json.loads(await asyncio.wait_for(cr.readexactly(n), 5))
                lab = d.get("label")
                known = {label(l) for l in links.values()} | set(prepaid) | set(budgets)
                num = lambda k: d.get(k) if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool) \
                    and 0 <= d.get(k) <= 10_000_000 else None  # MB; 10 TB is already absurd
                if lab not in known:
                    out = http_resp("400 Bad Request", "text/plain", "unknown device")
                elif d.get("clear") is True:
                    set_prepaid(lab, clear=True)
                    out = http_resp("200 OK", "application/json; charset=utf-8", status_json())
                elif num("remaining_MB") is not None or num("add_MB") is not None:
                    set_prepaid(lab, remaining_mb=num("remaining_MB"), add_mb=num("add_MB"))
                    out = http_resp("200 OK", "application/json; charset=utf-8", status_json())
                else:
                    out = http_resp("400 Bad Request", "text/plain", "bad number")
        elif method == "POST" and path in ("/api/download", "/api/download/cancel", "/api/download/open"):
            n = int(hdr.get("content-length") or 0)
            if hdr.get("x-linkbond") != "1" or not 0 < n <= 16000:
                out = http_resp("403 Forbidden", "text/plain", "forbidden")
            else:
                d = json.loads(await asyncio.wait_for(cr.readexactly(n), 5))
                code = "200 OK"
                job_id = None
                try:
                    if path == "/api/download":
                        job_id = start_download(d.get("url", ""), d.get("cookie", ""), d.get("referer", ""),
                                                d.get("user_agent", ""), d.get("filename", ""),
                                                "browser" if d.get("from_browser") else "manual")["id"]
                    else:
                        job = downloads.get(d.get("id"))
                        if not job:
                            code = "400 Bad Request"
                        elif path == "/api/download/cancel" and job["state"] == "running":
                            job["state"] = "cancel"
                        elif path == "/api/download/open":
                            p = job["path"] if job["state"] == "done" and os.path.exists(job["path"]) else DOWNLOAD_DIR
                            subprocess.Popen(["explorer", "/select," + p] if p != DOWNLOAD_DIR else ["explorer", p])
                except ValueError as e:
                    code = "400 Bad Request"
                    out = http_resp(code, "text/plain; charset=utf-8", str(e))
                else:
                    body = status_dict()
                    body["job"] = job_id  # the browser extension follows its download with this
                    out = http_resp(code, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False))
        elif method == "POST" and path == "/api/measure":
            # the full speed test (~3.2 MB per link), only when asked for: {"label": "..."} or {} = every link
            n = int(hdr.get("content-length") or 0)
            if hdr.get("x-linkbond") != "1" or not 0 <= n <= 1000:
                out = http_resp("403 Forbidden", "text/plain", "forbidden")
            else:
                d = json.loads(await asyncio.wait_for(cr.readexactly(n), 5)) if n else {}
                lab = d.get("label")
                for l in list(links.values()):
                    if (lab is None or label(l) == lab) and l["healthy"] and not l["measuring"] and not vpn_on:
                        asyncio.create_task(measure_and_set(l, full=True))
                        l["measuring"] = True
                out = http_resp("200 OK", "application/json; charset=utf-8", status_json())
        elif method == "POST" and path in ("/api/mode", "/api/quit"):
            n = int(hdr.get("content-length") or 0)
            if hdr.get("x-linkbond") != "1" or not 0 <= n <= 1000:
                out = http_resp("403 Forbidden", "text/plain", "forbidden")
            else:
                data = json.loads(await asyncio.wait_for(cr.readexactly(n), 5)) if n else {}
                code = "200 OK"
                if path == "/api/mode":
                    for k in ("eco", "block", "power"):
                        if isinstance(data.get(k), bool):
                            set_mode(k, data[k])
                    if data.get("news_seen") is True:  # "what's new" was read in the box
                        mark_news_seen()
                    refresh_modes()
                elif path == "/api/quit":  # stop everything and give the PC its original settings back
                    stop = os.path.join(HERE, "stop.ps1")
                    if os.path.exists(stop):
                        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", stop],
                                         creationflags=0x08000000 | 0x00000008 | 0x00000200)
                out = http_resp(code, "application/json; charset=utf-8", status_json())
        else:
            out = http_resp("404 Not Found", "text/plain", "not found")
        cw.write(out)
        await cw.drain()
    except Exception:
        pass
    finally:
        cw.close()


# ---------- updates: a new version put on disk is picked up by itself and announced ----------
SELF_FILE = None if getattr(sys, "frozen", False) else os.path.abspath(__file__)
update_info = {}  # {"version", "url"} when a newer published release exists


def file_version(path):
    """The __version__ written in a bond.py file, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def changelog_head(version):
    """The (short) list of changes for that version from CHANGELOG.md."""
    try:
        with open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r"^## " + re.escape(version) + r"[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    items = [x.strip().lstrip("-* ").split(" / ")[0] for x in (m.group(1).splitlines() if m else []) if x.strip()]
    return "\n".join(items[:4])


def announce_version():
    """After an update, say so once (with what changed). The very first run is silent."""
    p = state_path("version.txt")
    try:
        with open(p, encoding="utf-8") as f:
            old = f.read().strip()
    except OSError:
        old = ""
    if old == __version__:
        return None
    if old:
        notify(f"link-bond を新しい版にしました / updated: {old} → {__version__}",
               changelog_head(__version__) or f"version {__version__}")
        try:  # the box shows "updated" (with the changes) until you have seen it, for a day at most
            with open(state_path("last-update.json"), "w", encoding="utf-8") as f:
                json.dump({"from": old, "to": __version__, "at": time.time(), "seen": False}, f)
        except OSError:
            pass
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(__version__)
    except OSError:
        pass
    return old or None


def news():
    """{"from", "to", "changes"} right after an update (until seen, at most a day), else None."""
    try:
        with open(state_path("last-update.json"), encoding="utf-8") as f:
            n = json.load(f)
    except (OSError, ValueError):
        return None
    if n.get("seen") or time.time() - n.get("at", 0) > 86400:
        return None
    return {"from": n.get("from"), "to": n.get("to"), "changes": changelog_head(n.get("to", ""))}


def mark_news_seen():
    try:
        with open(state_path("last-update.json"), encoding="utf-8") as f:
            n = json.load(f)
        n["seen"] = True
        with open(state_path("last-update.json"), "w", encoding="utf-8") as f:
            json.dump(n, f)
    except (OSError, ValueError):
        pass


async def self_update_loop():
    """bond.py replaced on disk (a new version): restart into it - the supervisor (run.ps1) starts it again within
    seconds; meanwhile the PC simply uses its normal connection. A file that does not compile is left alone."""
    if not SELF_FILE:
        return
    try:
        seen = os.path.getmtime(SELF_FILE)
    except OSError:
        return
    while True:
        await asyncio.sleep(10)
        try:
            m = os.path.getmtime(SELF_FILE)
        except OSError:
            continue
        if m == seen:
            continue
        await asyncio.sleep(3)  # let the copy finish
        seen = os.path.getmtime(SELF_FILE)
        try:
            with open(SELF_FILE, encoding="utf-8") as f:
                compile(f.read(), SELF_FILE, "exec")
        except (OSError, SyntaxError) as e:
            log("new bond.py is not usable - staying on the running version:", e)
            continue
        if not supervised():
            log("new bond.py found, but nothing would restart link-bond - restart it yourself to update")
            continue
        log(f"new version on disk ({__version__} -> {file_version(SELF_FILE)}): restarting into it")
        save_usage()
        save_capacity()
        os._exit(0)


def check_release():
    """The newest published release of UPDATE_REPO: (version, url) or None. Plain HTTPS, no proxy."""
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": "link-bond", "Accept": "application/vnd.github+json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    tag = str(data.get("tag_name", "")).lstrip("vV")
    return (tag, data.get("html_url", "")) if tag else None


async def release_loop():
    """Once a day: is there a newer published version? Tell once per version."""
    if not UPDATE_REPO:
        return
    while True:
        try:
            rel = await asyncio.to_thread(check_release)
            if rel and vtuple(rel[0]) > vtuple(__version__):
                update_info.update(version=rel[0], url=rel[1])
                p = state_path("update-notified.txt")
                try:
                    told = open(p, encoding="utf-8").read().strip()
                except OSError:
                    told = ""
                if told != rel[0]:
                    notify(f"link-bond の新しい版 {rel[0]} があります / update available",
                           f"いまは {__version__}。小さな箱の「新しい版」から入れられます。\n{rel[1]}")
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(rel[0])
        except Exception as e:
            log("update check failed:", type(e).__name__)
        await asyncio.sleep(86400)


port_servers = []


async def serve(port=1080, stats_port=1081, discover=True):
    """Start listening (127.0.0.1 only). Returns the main proxy server and the dashboard server."""
    refresh_modes()
    asyncio.create_task(decay_loop())
    asyncio.create_task(modes_loop())
    asyncio.create_task(history_loop())
    if discover:
        asyncio.create_task(refresh_loop())
        asyncio.create_task(health_loop())
        asyncio.create_task(self_update_loop())
        asyncio.create_task(release_loop())
    srv = await asyncio.start_server(handle, "127.0.0.1", port)
    st = await asyncio.start_server(stats_server, "127.0.0.1", stats_port) if stats_port else None
    port_servers.clear()
    for lab, p in ports_map.items():  # one extra port per device that always uses exactly that device
        port_servers.append(await asyncio.start_server(lambda r, w, lab=lab: handle(r, w, lab), "127.0.0.1", p))
    return srv, st


async def main(args):
    global no_remeasure, REMEASURE_SEC, no_notify, PROXY_PORT
    no_remeasure = args.no_remeasure
    no_notify = args.no_notify
    PROXY_PORT = args.port
    REMEASURE_SEC = args.remeasure_hours * 3600
    load_pinned([args.pinned] if args.pinned else None)
    load_labels()
    load_budget()
    load_capacity()
    load_eco()
    load_blocklist()
    load_ports()
    announce_version()
    srv, _ = await serve(args.port, args.stats_port)
    extra = f", per-device ports: {dict(ports_map)}" if ports_map else ""
    log(f"link-bond {__version__} listening on 127.0.0.1:{args.port} (SOCKS5 + HTTP), dashboard: http://127.0.0.1:{args.stats_port}{extra}")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("link-bond currently supports Windows only.")
    ap = argparse.ArgumentParser(description="Bond several internet connections through a local proxy.")
    ap.add_argument("--port", type=int, default=1080, help="proxy port on 127.0.0.1 (default 1080)")
    ap.add_argument("--stats-port", type=int, default=1081, help="status page port on 127.0.0.1 (default 1081)")
    ap.add_argument("--pinned", help="file with hosts that must always use the same link (default pinned.txt + pinned.local.txt)")
    ap.add_argument("--no-remeasure", action="store_true", help="measure link speeds once at start only (saves mobile data)")
    ap.add_argument("--remeasure-hours", type=float, default=0,
                    help="hours between automatic full speed tests, ~3 MB per link each (default 0 = never; "
                         "the speed is learned from real traffic and you can press 'measure')")
    ap.add_argument("--no-notify", action="store_true", help="do not show Windows notifications when a link is cut")
    ap.add_argument("--version", action="version", version=__version__)
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass
