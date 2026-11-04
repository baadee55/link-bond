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
import base64
import collections
import concurrent.futures
import ctypes
import fnmatch
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile

__version__ = "1.6.0"
UPDATE_REPO = "baadee55/link-bond"  # newer releases on GitHub are installed by themselves (checked every 6 h, ~5 KB)

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
CAP_SANE_MBPS = 5000  # no phone, Wi-Fi or USB link does more; a bigger number is a counting error
CAP_HALF_LIFE = 3 * 86400  # a link's best real speed ("capacity") halves in 3 days unless it is reached again
PAUSE_SEC = 30           # a link that fails to connect is skipped for this long
DECAY_SEC = 5            # recent-traffic memory: halves in ~20 s
PRIOR_UP, PRIOR_DOWN = 50_000.0, 500_000.0  # assume "mostly downloading" when idle
PROBE_HOST, PROBE_PORT, PROBE_PATH = "cp.cloudflare.com", 80, "/generate_204"  # HTTP check, also spots a
                                                                                # prepaid "out of credit" page
PING_HOST, PING_PORT = "8.8.8.8", 53  # the cheap check: one tiny DNS question to Google Public DNS (~200 bytes)
PROBE_SEC = 15           # cheap check interval per link (~1.2 MB a day per link)
HTTP_CHECK_SEC = 120     # the HTTP check runs this often (~0.5 MB a day per link), and at once when a ping fails
FAILS_TO_DOWN = 3        # consecutive failed checks before a link counts as cut (fast re-checks: ~8 s)
FAST_SEC = 2.5           # a link that missed a check is re-checked this often until it is clearly up or down
DOWN_CHECK_SEC = 5       # a cut link is checked this often (cheap DNS question) so it is back in seconds
DOWN_SLOW_AFTER = 600    # ... and every PROBE_SEC once it has been cut for this long
PROBATION_SEC = 60       # a link that just came back gets a smaller share for this long
NOTIFY_DOWN_AFTER = 20   # a cut is only notified when it lasts this long (blips are only recorded)
HOLD_SEC = 15            # when no link works, a new connection waits this long for one to come back
STAGGER_MIN, STAGGER_MAX = 0.25, 1.5  # start the next link after 2 x its answer time, within these bounds (s)
REMIND_SEC = 1800        # repeat the "still cut" notice this often
labels = {}              # adapter name -> friendly name (labels.local.txt)
no_notify = False
announced = set()        # links already seen once (the first scan is silent)

HISTORY_LEN, HISTORY_SEC = 150, 2   # dashboard speed graph: 150 samples, 2 s apart = 5 minutes
REPORT_HOUR = 9          # the daily usage report is shown at the first scan after this hour

links = {}  # ifindex -> link dict
live = {}  # token -> {"link", "sw", "cw", "t_open", "t_data"} for every open tunnel
last_open_ok = time.time()  # when a connection last went through (the supervisor's fail-open check reads it)
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
    conn_record("log", text=" ".join(str(x) for x in a))


# ---------- connection record: everything about the links, kept 14 days (connlog/YYYY-MM-DD.jsonl) ----------
# Every 15 s one line per link (alive? answer time, loss, quality, address, gateway, speed now), plus every log
# line (link added / gone / cut / back / rejoin / network changed ...). UTF-8 JSON lines: easy to read back and
# to find out when and why a link drops.
CONNLOG_DAYS = 14
_connlog_pruned = [""]
_connlog_file = {}
_connlog_lock = threading.Lock()


def conn_record(kind, **fields):
    try:
        d = os.path.join(STATE_DIR, "connlog")
        os.makedirs(d, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
        rec.update(fields)
        with _connlog_lock:
            f = _connlog_file.get(day)
            if f is None or f.closed or _connlog_file.get("dir") != d:
                for old in [v for k, v in _connlog_file.items() if k != "dir"]:
                    old.close()
                _connlog_file.clear()
                f = _connlog_file[day] = open(os.path.join(d, day + ".jsonl"), "a", encoding="utf-8")
                _connlog_file["dir"] = d
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        if _connlog_pruned[0] != day:  # once a day: forget files older than CONNLOG_DAYS
            _connlog_pruned[0] = day
            limit = time.time() - CONNLOG_DAYS * 86400
            for n in os.listdir(d):
                if n.endswith(".jsonl") and os.path.getmtime(os.path.join(d, n)) < limit:
                    os.remove(os.path.join(d, n))
    except Exception:
        pass


def history_days():
    try:
        return sorted((n[:-6] for n in os.listdir(os.path.join(STATE_DIR, "connlog")) if n.endswith(".jsonl")), reverse=True)
    except OSError:
        return []


def history_data(day):
    """One day of the connection record: per device a minute-by-minute state ('ok' / 'slow' / 'bad' / 'doubt' /
    'cut' / 'gone'), answer times, drops and % up, plus the events. For the history page."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return None
    path = os.path.join(STATE_DIR, "connlog", day + ".jsonl")
    rank = {"ok": 0, "slow": 1, "doubt": 2, "bad": 3, "cut": 4}
    devs, events, nolink = {}, [], set()
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return None
    with f:
        for line in f:
            try:
                r = json.loads(line)
                t = r["t"][11:16]
                minute = int(t[:2]) * 60 + int(t[3:5])
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            if not isinstance(r, dict):
                continue
            k = r.get("kind")
            if k == "link":
                st = "cut" if not r.get("ok") else ("doubt" if r.get("suspect") else
                                                   {"bad": "bad", "slow": "slow"}.get(r.get("quality"), "ok"))
                d = devs.setdefault(r.get("link", "?"), {"min": {}, "rtt": {}, "pages": {}})
                if rank[st] >= rank.get(d["min"].get(minute, "ok"), 0) or minute not in d["min"]:
                    d["min"][minute] = st
                if r.get("rtt_ms") is not None:
                    d["rtt"].setdefault(minute, []).append(r["rtt_ms"])
                if r.get("page"):
                    d["pages"][r["page"]] = r["t"][11:19]
            elif k == "none":
                nolink.add(minute)
            elif k == "event":
                events.append({"t": r["t"][11:19], "text": r.get("text", "")})
    out = {"day": day, "days": history_days(), "devices": [], "events": events[-300:], "nolink": sorted(nolink)}
    for name, d in sorted(devs.items()):
        mins = d["min"]
        seen = sorted(mins)
        drops, prev = 0, None
        for m in seen:
            if mins[m] == "cut" and prev not in (None, "cut"):
                drops += 1
            prev = mins[m]
        up = sum(1 for m in seen if mins[m] != "cut")
        rtt = {m: sorted(v)[len(v) // 2] for m, v in d["rtt"].items()}
        out["devices"].append({"name": name, "states": {str(m): v for m, v in mins.items()},
                               "rtt": {str(m): v for m, v in rtt.items()}, "drops": drops,
                               "up_pct": round(100 * up / len(seen)) if seen else None,
                               "minutes": len(seen), "pages": d["pages"]})
    return out


def conn_snapshot():
    """One line per link (and per stopped tethering / Wi-Fi adapter) for the connection record."""
    if not links:
        conn_record("none")
    for l in sorted(links.values(), key=label):
        conn_record("link", link=label(l), adapter=l["name"], ip=l["ip"], gw=l.get("gw") or "",
                    ok=l["healthy"], rtt_ms=l["rtt"], loss_pct=l["loss"], quality=quality(l), suspect=l.get("suspect", False),
                    page=l.get("page", "") if l.get("why") == "redirect" else "",
                    why=l.get("why") or "", down_mbps=l.get("now_down", 0.0), up_mbps=l.get("now_up", 0.0),
                    conns=sum(1 for r in live.values() if r["link"] is l))
    for n in sorted(stalled):
        conn_record("stalled", adapter=n)
    for desc, w in wifi_last.items():
        if w.get("lost"):
            conn_record("wifi_lost", adapter=desc, profile=w["profile"], tries=w["tries"],
                        lost_for_s=int(time.time() - w["lost"]))


def new_link(idx, name, ip):
    return dict(idx=idx, name=name, ip=ip, down=1.0, up=1.0, active=0, total=0, bytes=0, paused_until=0.0,
                measured_at=0.0, measured=False, healthy=True, fails=0, why="", down_since=0.0, last_notice=0.0,
                ctr=None, gw="", conn_fails=0, qc_at=0.0, b_up=0, b_down=0, now_up=0.0, now_down=0.0,
                http_at=0.0, pings=collections.deque(maxlen=20), rtt=None, loss=0, retry_at=0.0, measuring=False,
                suspect=False, watching=False, probation_until=0.0, told_down=False)


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
    for key, st in sorted(stability.items()):  # how stable each device was yesterday
        if key.endswith("@prev") and st.get("day") == yday:
            end = time.mktime(time.strptime(yday, "%Y-%m-%d")) + 86400
            span = max(end - st.get("first", end - 86400), 1)
            pct = max(0, min(100, round(100 * (1 - st.get("down_s", 0) / span))))
            lines.append(f"{key[:-5]}: 切断 {st.get('drops', 0)} 回・つながっていた {pct}% / {st.get('drops', 0)} drops, up {pct}%")
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
    conn_record("event", text=text)


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
    "$ip=(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue|Where-Object {$_.AddressState -eq 'Preferred'}|Select-Object -First 1).IPAddress;"
    "$s=Get-NetAdapterStatistics -Name $a.Name -ErrorAction SilentlyContinue;"
    "[pscustomobject]@{idx=[int]$_.InterfaceIndex;name=[string]$a.Name;desc=[string]$a.InterfaceDescription;ip=$ip;status=[string]$a.Status;"
    "metric=[int]($_.RouteMetric+$_.InterfaceMetric);gw=[string]$_.NextHop;bytes=[long]($s.SentBytes+$s.ReceivedBytes)}};"
    "$v=@(Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.DestinationPrefix -like '*/1'}).Count -gt 0;"
    "$ap=@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue|Where-Object {$_.IPAddress -like '169.254.*'}|ForEach-Object {"
    "$x=Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue;"
    "if($x -and $x.Status -eq 'Up'){[string]$x.Name+'|'+[string]$x.InterfaceDescription+'|'+[string]$x.ifIndex}});"
    "$cp=@(Get-NetConnectionProfile -ErrorAction SilentlyContinue|ForEach-Object {[string]$_.InterfaceAlias+'|'+[string]$_.Name});"
    "ConvertTo-Json -InputObject @{vpn=$v;links=@($r);stalled=@($ap);profiles=$cp} -Compress -Depth 4"
)


def discover_sync():
    """Return ({ifindex: (name, ip, adapter_byte_counter, gateway)}, vpn_holds_all_traffic, stalled_adapter_names)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
        capture_output=True, timeout=20, creationflags=0x08000000,
    ).stdout.decode("utf-8", "replace").strip()
    data = json.loads(out or "{}")
    for row in data.get("links") or []:
        if row.get("name"):
            adapter_desc[row["name"]] = row.get("desc") or ""
            if row.get("idx"):
                adapter_idx[row["name"]] = int(row["idx"])
    for row in data.get("stalled") or []:
        parts = str(row).split("|")
        if len(parts) >= 3 and parts[2].isdigit():
            adapter_idx[parts[0]] = int(parts[2])
    for x in data.get("profiles") or []:
        alias, _, prof = str(x).partition("|")
        if alias and prof:
            net_profile[alias] = prof
    found, vpn = classify(data.get("links") or [], bool(data.get("vpn")))
    return found, vpn, stalled_names(data.get("stalled") or [])


stalled = set()  # adapters that are plugged in but got no address (169.254.x): tethering stopped on the phone
stalled_told = set()  # ... and already notified about


def stalled_names(rows):
    """Connected adapters stuck on a 169.254.x address ('name|description'), real ones only - not VPN, virtual,
    Wi-Fi Direct or Bluetooth adapters, which sit on such addresses all the time."""
    out = set()
    for row in rows:
        name, _, desc = str(row).partition("|")
        desc = desc.rsplit("|", 1)[0] if desc.rsplit("|", 1)[-1].isdigit() else desc
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
        if r.get("status", "Up") != "Up":  # Windows keeps the route of a dropped Wi-Fi for a while: not a link
            continue
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
        ip = await resolve(host, 443, via=link)
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
            capacity.update({k: v for k, v in json.load(f).items() if sane_capacity(v)})  # drop impossible values
    except (OSError, ValueError, AttributeError):
        pass


def sane_capacity(v):
    if not isinstance(v, dict):
        return False
    d, u, a = v.get("down", 0), v.get("up", 0), v.get("at", 0)
    num = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (d, u, a))
    return num and 0 <= d <= CAP_SANE_MBPS and 0 <= u <= CAP_SANE_MBPS


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


# ---------- Wi-Fi rejoin: a phone hotspot that dropped is joined again ----------
# Windows tries to rejoin a dropped Wi-Fi a few times and then gives up (an iPhone hides its hotspot for a while
# after a drop, so those tries fail). link-bond keeps trying - only the network that was carrying internet as a
# link on that adapter, never another saved Wi-Fi. It talks to wlanapi directly: on Windows 11 `netsh wlan`
# needs location permission, these two calls do not.
WIFI_RETRY_FAST, WIFI_RETRY_SLOW, WIFI_FAST_FOR, WIFI_GIVE_UP = 30, 120, 600, 3 * 3600
wifi_last = {}     # adapter description -> {"profile", "lost", "next", "tries"}
adapter_desc = {}  # adapter name -> description (from discovery)
net_profile = {}   # adapter name -> network name Windows shows for it (= the Wi-Fi profile)


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


class _WLAN_INFO(ctypes.Structure):
    _fields_ = [("guid", _GUID), ("desc", ctypes.c_wchar * 256), ("state", ctypes.c_ulong)]


class _WLAN_PARAMS(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_ulong), ("profile", ctypes.c_wchar_p), ("ssid", ctypes.c_void_p),
                ("bssids", ctypes.c_void_p), ("bsstype", ctypes.c_ulong), ("flags", ctypes.c_ulong)]


def wlan_call(join=None):
    """[(description, state)] of the Wi-Fi adapters (state 1 = connected, 4 = disconnected). With join=(description,
    profile): ask Windows to join that saved profile on that adapter, and return the result code instead."""
    try:
        api = ctypes.windll.wlanapi
    except (OSError, AttributeError):  # no Wi-Fi service on this PC
        return [] if join is None else -1
    h, ver = ctypes.c_void_p(), ctypes.c_ulong()
    if api.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h)) != 0:
        return [] if join is None else -1
    lst = ctypes.c_void_p()
    try:
        if api.WlanEnumInterfaces(h, None, ctypes.byref(lst)) != 0:
            return [] if join is None else -1
        n = ctypes.c_ulong.from_address(lst.value).value
        items = (_WLAN_INFO * n).from_address(lst.value + 8)
        if join is None:
            return [(i.desc, i.state) for i in items]
        for i in items:
            if i.desc == join[0]:
                prm = _WLAN_PARAMS(0, join[1], None, None, 1, 0)  # by profile name, infrastructure network
                return api.WlanConnect(h, ctypes.byref(i.guid), ctypes.byref(prm), None)
        return -2
    finally:
        if lst.value:
            api.WlanFreeMemory(lst)
        api.WlanCloseHandle(h, None)


def wifi_plan(ifaces, link_names, now):
    """What to rejoin now: [(adapter description, profile)]. Remembers which network each Wi-Fi link was on."""
    todo = []
    by_desc = {d: n for n, d in list(adapter_desc.items())}
    for desc, state in ifaces:
        name = by_desc.get(desc)
        if state == 1:
            prof = dict(net_profile).get(name or "")
            if name in link_names and prof:
                if wifi_last.get(desc, {}).get("profile") != prof:
                    save_wifi(desc, prof)
                wifi_last[desc] = {"profile": prof, "lost": 0, "next": 0, "tries": 0}
            elif desc in wifi_last and prof and prof != wifi_last[desc]["profile"]:
                wifi_last.pop(desc)  # you (or Windows) chose another network: leave it alone
            continue
        w = wifi_last.get(desc)
        if not w or state != 4:
            continue
        if not w["lost"]:
            w["lost"], w["next"] = now, now + 5
        if now - w["lost"] > WIFI_GIVE_UP:
            continue  # tried for hours: the phone is away or you turned it off
        if now >= w["next"]:
            w["tries"] += 1
            w["next"] = now + (WIFI_RETRY_FAST if now - w["lost"] < WIFI_FAST_FOR else WIFI_RETRY_SLOW)
            todo.append((desc, w["profile"]))
    return todo


def save_wifi(desc, prof):
    """Remember on disk which network each Wi-Fi adapter carried as a link (a restart while it is down still rejoins)."""
    try:
        d = {}
        try:
            d = json.load(open(state_path("wifi.json"), encoding="utf-8"))
        except (OSError, ValueError):
            pass
        d[desc] = prof
        with open(state_path("wifi.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except OSError:
        pass


def load_wifi():
    try:
        for desc, prof in json.load(open(state_path("wifi.json"), encoding="utf-8")).items():
            wifi_last.setdefault(desc, {"profile": prof, "lost": 0, "next": 0, "tries": 0})
    except (OSError, ValueError, AttributeError):
        pass


async def wifi_loop():
    load_wifi()
    while True:
        await asyncio.sleep(10)
        try:
            ifaces = await asyncio.to_thread(wlan_call)
            for desc, prof in wifi_plan(ifaces, {l["name"] for l in links.values()}, time.time()):
                w = wifi_last[desc]
                if w["tries"] == 1:
                    add_event(f"Wi-Fi が切れたのでつなぎ直します（{prof}） / rejoining Wi-Fi")
                rc = await asyncio.to_thread(wlan_call, (desc, prof))
                if rc != 0 and re.search(r" \d+$", prof):  # Windows may show "name 2" for the profile "name"
                    rc = await asyncio.to_thread(wlan_call, (desc, re.sub(r" \d+$", "", prof)))
                log(f"rejoining Wi-Fi {prof} (try {w['tries']}): result {rc}")
        except Exception as e:
            log("wifi rejoin check failed:", type(e).__name__)


# ---------- automatic repair: a stuck phone adapter is restarted (needs install-repair.ps1 once) ----------
# Some drops are on the PC side: USB tethering that still shows "connected" but carries nothing, an adapter left on
# a 169.254 address, a Wi-Fi driver that will not rejoin. Restarting that one adapter makes it ask for a new address
# and reconnect. That needs administrator rights, so it goes through the on-demand task "link-bond-repair"
# (C:\ProgramData\link-bond-repair, set up once with install-repair.ps1). link-bond only drops a request file;
# the helper accepts phone adapters only and at most once in 5 minutes / 12 times a day per adapter.
REPAIR_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "link-bond-repair")
REPAIR_AFTER = {"cut": 90, "stalled": 60, "wifi": 300}  # seconds stuck before a restart is asked for
REPAIR_EVERY = 600                                      # then at most every 10 minutes per adapter
adapter_idx = {}   # adapter name -> interface index (from discovery)
stalled_since = {}  # adapter name -> when it got stuck on a 169.254 address
repair_last = {}    # interface index -> when a restart was last asked for
_repair_ok = [None, 0.0]  # is the helper installed? (cached for an hour)


def repair_ready(now=None):
    now = now or time.time()
    if _repair_ok[0] is None or now - _repair_ok[1] > 3600:
        ok = os.path.isdir(os.path.join(REPAIR_DIR, "requests"))
        if ok:
            try:
                r = subprocess.run(["schtasks", "/query", "/tn", "link-bond-repair"], capture_output=True, timeout=15,
                                   creationflags=0x08000000)
                ok = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
        _repair_ok[0], _repair_ok[1] = ok, now
    return _repair_ok[0]


def repair_plan(now=None):
    """[(interface index, who, why)] to restart now: a cut link whose adapter is still there (no answer at all,
    not an "out of data" page), an adapter stuck without an address, a Wi-Fi link that will not rejoin."""
    now = now or time.time()
    todo = []
    for l in list(links.values()):
        if (not l["healthy"] and l.get("why") == "no-response" and l.get("down_since")
                and now - l["down_since"] > REPAIR_AFTER["cut"]):
            todo.append((l["idx"], label(l), "応答なし / no answer"))
    for name, since in list(stalled_since.items()):
        if now - since > REPAIR_AFTER["stalled"] and name in adapter_idx:
            todo.append((adapter_idx[name], labels.get(name, name), "アドレスなし / no address"))
    by_desc = {d: n for n, d in list(adapter_desc.items())}
    for desc, w in list(wifi_last.items()):
        name = by_desc.get(desc)
        if w.get("lost") and now - w["lost"] > REPAIR_AFTER["wifi"] and name in adapter_idx:
            todo.append((adapter_idx[name], labels.get(name, name), "Wi-Fi がつながらない / Wi-Fi will not rejoin"))
    return [(i, who, why) for i, who, why in todo if i and now - repair_last.get(i, 0) > REPAIR_EVERY]


def ask_repair(idx):
    with open(os.path.join(REPAIR_DIR, "requests", f"{int(idx)}.req"), "w", encoding="ascii") as f:
        f.write(str(int(idx)))
    r = subprocess.run(["schtasks", "/run", "/tn", "link-bond-repair"], capture_output=True, timeout=20,
                       creationflags=0x08000000)
    return r.returncode == 0


async def repair_loop():
    told_missing = 0.0
    while True:
        await asyncio.sleep(15)
        try:
            for n in list(stalled_since):
                if n not in stalled:
                    stalled_since.pop(n)
            for n in stalled:
                stalled_since.setdefault(n, time.time())
            plan = [] if vpn_on else repair_plan()
            if not plan:
                continue
            if not await asyncio.to_thread(repair_ready):
                if time.time() - told_missing > 86400:  # once a day: say what would help
                    told_missing = time.time()
                    add_event("自動復旧は未設定です（link-bond フォルダの install-repair.ps1 を 1 回実行） / auto-repair not set up")
                continue
            for idx, who, why in plan:
                repair_last[idx] = time.time()
                ok = await asyncio.to_thread(ask_repair, idx)
                add_event(f"自動復旧: {who} のアダプタの入れ直しを頼みました（{why}） / auto-repair: adapter restart asked")
                log(f"auto-repair: restart asked for {who} (ifIndex {idx}, {why}): {'sent' if ok else 'task did not start'}")
        except Exception as e:
            log("auto-repair check failed:", type(e).__name__)


async def refresh_loop():
    global vpn_on, vpn_present, vpn_bypass
    last = None
    while True:
        try:
            found, v, st = await asyncio.to_thread(discover_sync)
            discovered[0] = True
            for n in st - stalled:
                log("tethering stopped (no address):", n)
            for n in st & stalled:  # still stuck on the next scan: tell
                if n not in stalled_told and not any(l["name"] == n for l in links.values()):
                    stalled_told.add(n)
                    lab = labels.get(n, n)
                    notify_link(f"link-bond: {lab} テザリングが止まっています / tethering stopped",
                                "ケーブルはつながっていますが、スマホからアドレスが来ていません。スマホでテザリングを OFF→ON してください。\n"
                                "Connected, but the phone gives no address - switch tethering off and on.")
            stalled_told.intersection_update(st)
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
                    _prev_octets.pop(idx, None)
                    drop_link_tunnels(gone)
                    notify_link(*gone_message(gone["name"]))
                    note_flap(label(gone))
            for idx, (name, ip, ctr, gw) in found.items():
                l = links.get(idx)
                if l is None:
                    l = links[idx] = new_link(idx, name, ip)
                    _prev_octets.pop(idx, None)
                    l["gw"] = gw
                    known = adopt_capacity(l)
                    log("link added:", name, ip, f"(known: down {l['down']:.1f} / up {l['up']:.1f} Mbps)" if known else "")
                    wake_waiters()
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
async def probe_ip(link):
    """The check's own address, asked over this link (another link's answer is not trusted here); when DNS is
    blocked on this network, the shared lookup."""
    for srv in DNS_SERVERS:
        r = await _link_lookup(link, PROBE_HOST, srv)
        if r:
            return r[0][0]
    return await resolve(PROBE_HOST, PROBE_PORT)


async def probe(link):
    """(ok, why). why: 'redirect' = answers but is not the internet (captive portal / prepaid credit or data
    used up), 'no-response' = nothing comes back (no signal, tethering off, data cut)."""
    try:
        loop = asyncio.get_running_loop()
        s = mk_sock(link)
        try:
            ip = await probe_ip(link)
            await asyncio.wait_for(loop.sock_connect(s, (ip, PROBE_PORT)), 5)
            r, w = await asyncio.open_connection(sock=s)
        except BaseException:
            s.close()
            raise
        w.write(f"GET {PROBE_PATH} HTTP/1.1\r\nHost: {PROBE_HOST}\r\nConnection: close\r\n\r\n".encode())
        line = await asyncio.wait_for(r.readline(), 6)
        parts = line.split()
        if len(parts) >= 2 and parts[1] == b"204":
            w.close()
            return True, ""
        if parts:  # something else answered: keep where it sends us (an operator's "out of data" page, a login ...)
            page = parts[1].decode("latin-1", "replace")[:4]
            try:
                for _ in range(30):
                    h = await asyncio.wait_for(r.readline(), 3)
                    if h in (b"\r\n", b"\n", b""):
                        break
                    if h.lower().startswith(b"location:"):
                        page += " " + h.split(b":", 1)[1].strip().decode("latin-1", "replace")[:200]
            except Exception:
                pass
            link["page"] = page
        w.close()
        return False, "redirect" if parts else "no-response"
    except Exception:
        return False, "no-response"


flap_log = {}  # label -> times (s) the device dropped, newest last
stability = {}  # label -> {"day", "drops", "down_s", "first"}: today's drops and seconds cut (stability.json)


def _stab(key, now):
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    st = stability.get(key)
    if not st or st.get("day") != day:
        if st:
            stability[key + "@prev"] = st  # yesterday, for the daily report
        st = stability[key] = {"day": day, "drops": 0, "down_s": 0.0, "first": now}
    return st


def note_stability(l, what, now=None):
    now = now or time.time()
    st = _stab(label(l), now)
    if what == "down":
        st["drops"] += 1
    elif what == "up" and l.get("down_since"):
        midnight = time.mktime(time.strptime(st["day"], "%Y-%m-%d"))
        st["down_s"] += now - max(l["down_since"], midnight)
    try:
        with open(state_path("stability.json"), "w", encoding="utf-8") as f:
            json.dump(stability, f, ensure_ascii=False)
    except OSError:
        pass


def load_stability():
    try:
        with open(state_path("stability.json"), encoding="utf-8") as f:
            stability.update({k: v for k, v in json.load(f).items() if isinstance(v, dict)})
    except (OSError, ValueError, AttributeError):
        pass


def stability_today(l, now=None):
    """(drops today, % of today's time it was usable) for one device."""
    now = now or time.time()
    st = _stab(label(l), now)
    down = st["down_s"] + ((now - max(l["down_since"], st["first"])) if not l["healthy"] and l.get("down_since") else 0)
    span = max(now - st["first"], 1.0)
    return st["drops"], max(0, min(100, round(100 * (1 - down / span))))


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
        l["suspect"] = False
        l["why"], l["page"], l["probe_bad"] = "", "", False
        if not l["healthy"]:
            l["healthy"], l["paused_until"] = True, 0.0
            l["probation_until"] = now + PROBATION_SEC
            try:
                wake_waiters()
            except RuntimeError:  # no running loop (a unit test)
                pass
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
        secs = int(time.time() - l["down_since"])
        add_event(f"{name}: 復旧（{secs} 秒切れていました） / back after {secs} s")
        note_stability(l, "up")
        if l["told_down"]:
            l["told_down"] = False
            notify_link(f"link-bond: {name} 復旧 / back online", f"{name} はまたネットに出られます（{secs} 秒）。 / {name} has internet again.")
        return
    if event == "down":
        note_flap(name)
        note_stability(l, "down")
        add_event(f"{name}: 切断 / cut")
        drop_link_tunnels(l)
        asyncio.ensure_future(tell_cut_later(l, l["down_since"]))  # tell only if it lasts
        return
    mins = int((time.time() - l["down_since"]) / 60)
    if l["why"] == "redirect":
        jp = "つながっているのにネットに出られません。プリペイドの残高・データ切れ、または認証待ちの可能性があります。"
        en = "Connected but no internet - prepaid credit or data may have run out."
    else:
        jp = "応答がありません。電波なし・テザリングOFF・データ切れの可能性があります。"
        en = "No response - weak signal, tethering off, or data used up."
    head = "切れました / lost internet" if event == "cut" else f"まだ切れています({mins}分) / still cut"
    if event == "cut":
        l["told_down"] = True
    notify_link(f"link-bond: {name} {head}", f"{jp}\n{en}")


async def tell_cut_later(l, since):
    """A cut that is over within NOTIFY_DOWN_AFTER s is a blip: recorded, not notified."""
    await asyncio.sleep(NOTIFY_DOWN_AFTER)
    if links.get(l["idx"]) is l and not l["healthy"] and l["down_since"] == since:
        tell(l, "cut")


def suspect(l, why="connect"):
    """Something failed on this link: take it out of the rotation and re-check it every FAST_SEC at once."""
    if l is SYSTEM_LINK or vpn_on or l.get("idx") not in links:
        return
    if l["healthy"] and not l["suspect"]:
        l["suspect"] = True
        log(f"link suspect ({why}): {l['name']} - checking it every {FAST_SEC} s")
    if not l["watching"]:
        l["watching"] = True
        asyncio.ensure_future(watch(l))


async def watch(l):
    """Fast checks for a doubtful or cut link until it is clearly fine again. A suspect link is cut after
    FAILS_TO_DOWN misses in a row (~8 s); a cut link is checked every DOWN_CHECK_SEC so it rejoins in seconds."""
    try:
        while l.get("idx") in links and links.get(l["idx"]) is l and not vpn_on:
            now = time.time()
            try:
                ok, why = await check_link(l, now, quick=True)
                event = apply_probe(l, ok, why)
                if event:
                    tell(l, event)
            except Exception as e:
                log("quick check failed:", type(e).__name__)
                ok = False
            if ok:
                l["suspect"] = False
                if l["healthy"]:
                    return
            if l["healthy"]:
                await asyncio.sleep(FAST_SEC)
            else:
                cut_for = time.time() - l["down_since"]
                await asyncio.sleep(DOWN_CHECK_SEC if cut_for < DOWN_SLOW_AFTER else PROBE_SEC)
    finally:
        l["watching"] = False


async def quick_check(l):
    """Kept for callers: a doubtful link is now handled by suspect() / watch()."""
    suspect(l)


def _dns_question():
    name = b"".join(bytes([len(p)]) + p.encode() for p in "one.one.one.one".split(".")) + b"\0"
    return struct.pack("!HHHHHH", 0x4C42, 0x0100, 1, 0, 0, 0) + name + struct.pack("!HH", 1, 1)


DNS_Q = _dns_question()


class _OneDatagram(asyncio.DatagramProtocol):
    def __init__(self, server=None):
        self.answer = asyncio.get_running_loop().create_future()
        self.server = server

    def datagram_received(self, data, addr):
        if self.server and addr and addr[0] != self.server:
            return  # not from the server we asked: ignore
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
        tr, pr = await loop.create_datagram_endpoint(lambda: _OneDatagram(PING_HOST), sock=s)
        t0 = time.perf_counter()
        tr.sendto(DNS_Q, (PING_HOST, PING_PORT))
        data = await asyncio.wait_for(pr.answer, 3)
        return len(data) >= 12 and data[:2] == DNS_Q[:2], round((time.perf_counter() - t0) * 1000)
    except Exception:
        return False, None
    finally:
        if tr:
            tr.close()
        else:
            s.close()


# ---------- names -> addresses, built for unstable links ----------
# Windows asks the DNS server of ONE adapter; when that phone is cut, every name lookup hangs for seconds even
# though other links work. link-bond asks Google / Cloudflare DNS over every working link at the same time AND
# Windows itself, takes the first good answer, keeps it for its TTL, and when nothing answers it uses the last
# known address (a name rarely moves). Local names (no dot, .local, the hosts file) go to Windows only.
DNS_SERVERS = ("8.8.8.8", "1.1.1.1")  # noqa: secret - public DNS of Google and Cloudflare, not personal
DNS_MIN_TTL, DNS_MAX_TTL, DNS_STALE_SEC, DNS_CACHE_MAX = 30, 3600, 86400, 4000
dns_cache = {}  # name -> [addresses, fresh until, usable as a last resort until]
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localhost", ".arpa", ".test")


def dns_packet(name):
    """(query id, packet) for an A question about `name`. ValueError for a name DNS cannot carry."""
    labels_ = name.rstrip(".").split(".")
    if not name or len(name) > 253 or any(not x or len(x) > 63 for x in labels_):
        raise ValueError("bad name")
    qid = int.from_bytes(os.urandom(2), "big")
    q = b"".join(bytes([len(x)]) + x.encode("idna") for x in labels_) + b"\0"
    return qid, struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + q + struct.pack("!HH", 1, 1)


def _dns_name(data, off):
    """Read a (possibly compressed) name: (lower-case name, offset after it)."""
    parts, jumped, end, hops = [], False, off, 0
    while True:
        if off >= len(data) or hops > 30:
            raise ValueError("bad name")
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                raise ValueError("bad pointer")
            if not jumped:
                end = off + 2
            off, jumped, hops = ((n & 0x3F) << 8) | data[off + 1], True, hops + 1
            continue
        parts.append(data[off + 1:off + 1 + n].decode("ascii", "replace").lower())
        off += 1 + n
    return ".".join(parts), (end if jumped else off)


def dns_answer(data, qid, name):
    """The IPv4 addresses and smallest TTL in a DNS reply to OUR question, or None (wrong id / name, truncated,
    an error, no address)."""
    try:
        if len(data) < 12:
            return None
        rid, flags, qd, an, _, _ = struct.unpack("!HHHHHH", data[:12])
        if rid != qid or not flags & 0x8000 or flags & 0x0200 or flags & 0x000F or qd != 1:
            return None
        qn, off = _dns_name(data, 12)
        if qn != name.lower().rstrip("."):
            return None
        off += 4
        ips, ttl = [], DNS_MAX_TTL
        for _ in range(an):
            _, off = _dns_name(data, off)
            typ, cls, t, ln = struct.unpack("!HHIH", data[off:off + 10])
            off += 10
            if typ == 1 and cls == 1 and ln == 4:
                ips.append(socket.inet_ntoa(data[off:off + 4]))
                ttl = min(ttl, t)
            off += ln
        return (ips, ttl) if ips else None
    except (ValueError, struct.error, IndexError):
        return None


async def dns_over(link, name, server, timeout=2.0):
    """Ask `server` about `name` over this link only. (addresses, ttl) or None."""
    loop = asyncio.get_running_loop()
    qid, pkt = dns_packet(name)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.setblocking(False)
    tr = None
    try:
        if link["idx"] is not None:
            sk.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", link["idx"]))
            sk.bind((link["ip"], 0))
        tr, pr = await loop.create_datagram_endpoint(lambda: _OneDatagram(server), sock=sk)
        tr.sendto(pkt, (server, 53))
        return dns_answer(await asyncio.wait_for(pr.answer, timeout), qid, name)
    except Exception:
        return None
    finally:
        if tr:
            tr.close()
        else:
            sk.close()


_DNS_POOL = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="dns")  # Windows lookups only, never the
_inflight = {}  # (name, link) -> the one lookup running for it                    # shared pool (it would starve)
# An "out of data" or login page often answers EVERY name with its own private address. Such answers from the
# public DNS over a link are thrown away; from Windows they are only used when nothing better comes within 0.5 s.
def _not_public(a, strict):
    """strict: anything that is not a normal internet address (private, shared, loopback, link-local, multicast,
    reserved). Not strict: only the ranges an operator's page would use - loopback and 0.0.0.0 are left alone,
    because a hosts file blocks names with them on purpose."""
    if strict:
        return not a.is_global or a.is_multicast
    if a.is_loopback or a.is_unspecified or (a.version == 4 and a.packed[0] == 0):
        return False
    return not a.is_global and not a.is_multicast


def public_ips(ips, strict=True):
    out = []
    for i in ips:
        try:
            if not _not_public(ipaddress.ip_address(i), strict):
                out.append(i)
        except ValueError:
            pass
    return out


async def _system_lookup(name, port):
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.run_in_executor(
            _DNS_POOL, socket.getaddrinfo, name, port, socket.AF_INET, socket.SOCK_STREAM), 10)
        ips = list(dict.fromkeys(i[4][0] for i in infos))
        return (ips, 300, "sys") if ips else None
    except Exception:
        return None


async def _link_lookup(link, name, server):
    r = await dns_over(link, name, server)
    if not r:
        return None
    ips = public_ips(r[0])
    return (ips, r[1], link["idx"]) if ips else None


def flush_link_cache(idx):
    """A link turned out to show an 'out of data' / login page: forget the addresses it told us."""
    for k in [k for k, v in dns_cache.items() if len(v) > 3 and v[3] == idx]:
        dns_cache.pop(k, None)


def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def resolve(host, port=443, via=None):
    """One IPv4 address for `host`. A known address is used at once (an expired one too, while a fresh lookup runs
    in the background); a new name is looked up over every working link and Windows at the same time.
    `via` = look it up over that link only (a link's own check must not trust what another link said)."""
    if is_ip(host):
        return host
    name = host.lower().rstrip(".")
    now = time.time()
    if via is None:
        c = dns_cache.get(name)
        if c and c[1] > now:
            return c[0][0]
        if c and c[2] > now:  # expired but known: use it now, refresh it in the background
            _single(name, port, None)
            return c[0][0]
    r = await asyncio.shield(_single(name, port, via))
    if r:
        return r[0][0]
    c = dns_cache.get(name)
    if c and c[2] > now:  # nothing answers: the last known address is far better than a failure
        return c[0][0]
    raise ConnectionError("cannot resolve " + host)


def _single(name, port, via):
    """One lookup per name at a time, however many connections want it."""
    key = (name, via["idx"] if via is not None else None)
    f = _inflight.get(key)
    if f is None or f.done():
        f = _inflight[key] = asyncio.ensure_future(_lookup(name, port, via))
        f.add_done_callback(lambda _f, k=key: _inflight.pop(k, None) if _inflight.get(k) is _f else None)
    return f


async def _lookup(name, port, via):
    loop = asyncio.get_running_loop()
    local = "." not in name or name.endswith(LOCAL_SUFFIXES) or name == "localhost"
    jobs = [asyncio.ensure_future(_system_lookup(name, port))]
    if not local and not vpn_on:
        ls = [via] if via is not None else sorted((l for l in links.values() if l["healthy"] and not l["suspect"]),
                                                  key=lambda l: l["rtt"] or 999)[:3]
        for k, l in enumerate(ls):
            jobs.append(asyncio.ensure_future(_link_lookup(l, name, DNS_SERVERS[k % 2])))
            jobs.append(asyncio.ensure_future(_later(0.8, lambda l=l, k=k: _link_lookup(l, name, DNS_SERVERS[(k + 1) % 2]))))
    got, private, private_at, end = None, None, None, loop.time() + 5
    pending = set(jobs)
    try:
        while pending and got is None:
            left = end - loop.time()
            if private is not None:
                left = min(left, private_at + 0.5 - loop.time())
            if left <= 0:
                break
            done, pending = await asyncio.wait(pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for d in done:
                r = d.result() if not d.cancelled() and d.exception() is None else None
                if not r:
                    continue
                if r[2] == "sys" and not local and not public_ips(r[0], strict=False):
                    if private is None:  # a private address for a public name: maybe an operator's page
                        private, private_at = r, loop.time()
                    continue
                got = r
                break
    finally:
        for j in jobs:
            j.cancel()
    got = got or private
    if got and via is None:
        now = time.time()
        if len(dns_cache) >= DNS_CACHE_MAX:
            for k in sorted(dns_cache, key=lambda k: dns_cache[k][1])[:DNS_CACHE_MAX // 4]:
                dns_cache.pop(k, None)
        ttl = max(DNS_MIN_TTL, min(DNS_MAX_TTL, got[1]))
        dns_cache[name] = [got[0], now + ttl, now + DNS_STALE_SEC, got[2]]
    return got


async def _later(delay, make):
    """make() after delay seconds (the coroutine is only created if it is still wanted)."""
    await asyncio.sleep(delay)
    return await make()


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


async def check_link(l, now, quick=False):
    """One check. The cheap DNS question says whether the link answers; the web check (HTTP) says whether the
    internet really works - with prepaid data used up, DNS still answers but nothing else goes through. So the web
    check runs whenever the DNS question failed, whenever the link is in doubt or cut (quick checks), whenever the
    last web check failed, and every HTTP_CHECK_SEC. Its answer wins. Real traffic arriving on the link also counts
    as proof that it works."""
    ok, rtt = await ping(l)
    note_ping(l, ok, rtt)
    why = ""
    bad_before = l.get("probe_bad", False)
    need = (not ok or bad_before or now - l["http_at"] >= HTTP_CHECK_SEC
            or (quick and (l["suspect"] or not l["healthy"])))
    if need and not l["healthy"] and now - l["down_since"] > DOWN_SLOW_AFTER and now - l["http_at"] < 60:
        need = False  # cut for a long time: one web check a minute is enough (prepaid data)
    if need:
        l["http_at"] = now
        hok, why = await probe(l)
        l["probe_bad"] = not hok
        if hok:
            ok, why = True, ""   # the web works (DNS may simply be blocked on this network)
        else:
            ok = False
            if why == "redirect":
                flush_link_cache(l["idx"])
    elif bad_before:
        ok, why = False, l.get("why") or "no-response"
    if not ok and time.time() - l.get("data_at", 0) < 10:
        ok, why = True, ""       # data is coming in over it right now: it works
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
    ls = [l for l in ls if not l["watching"]]
    for l, (ok, why) in zip(ls, await asyncio.gather(*(check_link(l, now) for l in ls))):
        if not ok:
            suspect(l, "check")  # re-checked every FAST_SEC from now on: cut in seconds, not in 45 s
            continue
        event = apply_probe(l, ok, why)
        if event:
            tell(l, event)


discovered = [False]  # the first discovery has finished (no "all down" before we even know the links)


async def health_loop():
    any_ok, misses = True, 0
    while True:
        await asyncio.sleep(PROBE_SEC)
        try:
            await health_tick()
        except Exception as e:
            log("health check failed:", type(e).__name__, str(e)[:80])
        try:
            conn_snapshot()
        except Exception as e:
            log("connection record failed:", type(e).__name__)
        now_ok = any(l["healthy"] for l in links.values())
        misses = 0 if now_ok else misses + 1
        if not discovered[0] or (not now_ok and misses < 2):
            continue
        if any_ok and not now_ok and not vpn_on:
            notify("link-bond: 全部の回線が切れています / ALL links are down",
                   "ネットに出られる回線がありません。スマホのテザリング・電波・データ残量を確かめてください。\n"
                   "No link reaches the internet: check the phones' tethering, signal and data.")
            add_event("全部の回線が切れました / all links down")
        elif not any_ok and now_ok:
            add_event("回線が戻りました / a link is back")
        any_ok = now_ok


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
    """Expected usefulness (Mbps) given what the PC is doing right now: mostly uploading -> uplink counts more.
    A link that just came back, or keeps dropping, gets a smaller share: on unstable links the steady one should
    carry the long downloads and streams."""
    s = upload_share()
    w = max(l["down"] * (1 - s) + l["up"] * s, 0.2)
    if l.get("probation_until", 0) > time.time():
        w *= 0.3
    flaps = len([t for t in flap_log.get(label(l), []) if time.time() - t < 1800]) if l is not SYSTEM_LINK else 0
    return w / (1 + flaps)


def candidates(host, only=None):
    """Links to try, best first. `only` = a device label: the dedicated port of that device (no fallback)."""
    if vpn_on:
        return [SYSTEM_LINK]
    now = time.time()
    if only is not None:
        return [l for l in links.values() if label(l) == only and l["healthy"]]
    u = ([l for l in links.values() if l["paused_until"] <= now and l["healthy"] and not l["suspect"]]
         or [l for l in links.values() if l["healthy"]] or list(links.values()))
    if is_pinned(host):
        return sorted(u, key=lambda l: -weight(l))
    # ALL links are used at the same time, each in proportion to its speed: the next connection goes to the link
    # with the least WORKING connections per Mbps. A link that drops simply leaves this list; the rest carry it all.
    return sorted(u, key=lambda l: (busy(l, now) + 1) / weight(l))


async def open_via(host, port, only=None):
    """Connect to host:port over the best link. On unstable links: if the first link does not connect within
    ~2x its answer time, the next one is tried at the same time, and the first to connect wins (a dead link costs
    a fraction of a second, not 4 s). When no link works at all, it waits up to HOLD_SEC for one to come back."""
    global blocked_total, last_open_ok
    if is_blocked(host):
        blocked_total += 1
        blocked_hosts[host.lower()] += 1
        raise PermissionError("blocked: " + host)
    deadline = time.time() + HOLD_SEC
    last = None
    bond_tried = only is not None  # a device's own port never goes through the relay
    while True:
        try:
            ip = await resolve(host, port)
        except ConnectionError as e:
            last = e
            ip = None
        if ip and not bond_tried:
            bond_tried = True  # once per connection, not on every hold-and-retry round
            if bondable(host, port, ip):
                try:
                    r, w = await open_bonded(host, port)
                    BOND_LINK["active"] += 1
                    BOND_LINK["total"] += 1
                    last_open_ok = time.time()
                    return BOND_LINK, r, w
                except ConnectionError:
                    pass  # the relay did not work out: the normal path below, at once
        cands = candidates(host, only) if ip else []
        if ip and not cands and only is not None:
            raise ConnectionError(f"link {only!r} is not available")
        if ip and not cands:
            last = ConnectionError("no link at the moment")  # every adapter is gone: wait for one below
        if ip and cands:
            if cands != [SYSTEM_LINK] and not vpn_bypass and best_if(ip) not in links:
                cands = [SYSTEM_LINK]  # Windows routes this destination somewhere else (VPN just started, ...)
            try:
                l, r, w = await race_connect(cands, ip, port, host)
                last_open_ok = time.time()
                return l, r, w
            except ConnectionError as e:
                last = e
        if only is not None or cands_is_system() or time.time() >= deadline:
            break
        if ip and any(l["healthy"] and not l["suspect"] for l in links.values()):
            break  # links work: it is this server that does not answer - fail now, do not hold
        await wait_for_link(min(2.0, max(0.1, deadline - time.time())))
    raise ConnectionError(f"no link could reach {host} ({last})")


def cands_is_system():
    return vpn_on


link_back = None  # an asyncio.Event made in serve(): wakes connections waiting for a link to come back


async def wait_for_link(timeout):
    ev = link_back
    if ev is None:
        await asyncio.sleep(timeout)
        return
    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass


def wake_waiters():
    """A link is (back) up: wake every connection that waits. The signal stays on for a second (none is missed)."""
    if link_back is not None:
        link_back.set()
        asyncio.get_running_loop().call_later(1.0, link_back.clear)


async def link_connect(l, sk, addr):
    """The TCP connect itself (one place, so the self-test can make a link that never answers)."""
    await asyncio.get_running_loop().sock_connect(sk, addr)


def stagger(l):
    rtt = (l.get("rtt") or 300) / 1000
    return max(STAGGER_MIN, min(STAGGER_MAX, 2 * rtt))


_fail_logged = {}  # (link, host) -> when a failure was last written to the log


async def race_connect(cands, ip, port, host):
    """Staggered connect: start on the best link, add the next one when the first is slow. First to connect wins;
    the losers are closed without counting as failures. Returns (link, reader, writer)."""
    a = ipaddress.ip_address(ip)
    if a.is_multicast or a.is_unspecified or a.is_reserved:
        raise ConnectionError(f"{ip} cannot be connected to")  # not a link problem: no suspicion
    loop = asyncio.get_running_loop()
    socks = {}

    async def attempt(l, timeout):
        sk = mk_sock(l)
        socks[id(l)] = sk
        await asyncio.wait_for(link_connect(l, sk, (ip, port)), timeout)
        return l, sk

    pending, errors, winner, of = set(), [], None, {}
    order = list(cands)
    try:
        while order or pending:
            if order and winner is None:
                l = order.pop(0)
                t = asyncio.ensure_future(attempt(l, 8 if not order else 6))
                of[t] = l
                pending.add(t)
                wait = stagger(l) if order else None
            else:
                wait = None
            done, pending = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is None:
                    if winner is None:
                        winner = t.result()
                else:
                    l, e = of[t], t.exception()
                    errors.append(e)
                    try:
                        socks.pop(id(l)).close()
                    except Exception:
                        pass
                    if not isinstance(e, ConnectionRefusedError):  # refused = the server said no: the link works
                        l["conn_fails"] += 1
                        # some hosts never answer anywhere (trackers blocked by the network): one host timing out
                        # is not the link's fault - two different hosts within 30 s are
                        th = l.setdefault("to_hosts", {})
                        tnow = time.time()
                        th[host] = tnow
                        for h in [h for h, t in th.items() if tnow - t > 30]:
                            th.pop(h, None)
                        others = any(x is not l and x["healthy"] and not x["suspect"] for x in links.values())
                        if l is not SYSTEM_LINK and (len(th) >= 2 or not others):
                            suspect(l, "connect")
                    key = (l["name"], host)
                    if time.time() - _fail_logged.get(key, 0) > 60:  # the record does not need every retry
                        _fail_logged[key] = time.time()
                        log("connect failed:", l["name"], host, type(e).__name__)
            if winner is not None:
                break
            if not pending and not order:
                break
    finally:
        for t in pending:  # the slower attempts: not failures, just not needed
            t.cancel()
        for k, sk in list(socks.items()):
            if winner is None or k != id(winner[0]):
                try:
                    sk.close()
                except Exception:
                    pass
    if winner is None:
        raise ConnectionError(str(errors[-1]) if errors else "no link")
    l, sk = winner
    try:
        r, w = await asyncio.open_connection(sock=sk)
    except Exception:
        sk.close()
        raise ConnectionError("connection lost at once")
    l["active"] += 1
    l["total"] += 1
    l["conn_fails"] = 0
    return l, r, w


async def pipe(r, w, link, kind, rule=None, rec=None):
    try:
        while True:
            b = await r.read(16384 if rule else 65536)
            if not b:
                break
            w.write(b)
            if rec is not None:
                rec["t_data"] = time.time()
                rec["n_" + kind] = rec.get("n_" + kind, 0) + len(b)  # (a very big one marks the host as bulk)
            link["bytes"] += len(b)
            link["b_" + kind] += len(b)
            if kind == "down":
                link["data_at"] = time.time()
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
        learn_from(rec, link, host)
        for w in (sw, cw):
            try:
                w.close()
            except Exception:
                pass


# ---------- one connection over all links (relay) ----------
# A big transfer (a video, a download, an upload) normally runs over ONE link. Here it is cut into pieces, the
# pieces go over EVERY link to a small relay on Cloudflare, are put back together there and go on to the site from
# one place - and the same in the other direction. So a single connection gets about the SUM of all links.
# The rules of the pieces (frames, signature, scheduler) are in relay/README.md. relay.local.txt switches it on:
#   line 1 = relay address (wss://...), line 2 = the shared secret. The file is never published or logged.
RELAY_FILE = "relay.local.txt"
BULK_FILE = "bulk.local.txt"        # your own extra hosts that are worth bonding (one per line)
LEARNED_FILE = "bulk-learned.json"  # hosts that moved >= 8 MB in one connection (learned by itself)
CF_FILE = "cf-ips.json"             # Cloudflare's own address ranges (a site behind Cloudflare cannot be reached by the relay)
BOND_OPEN_SEC = 6.0                 # the relay must say OK within this time, else the normal path is used
BOND_IDLE_SEC = 45                 # a bonded connection with no data either way is closed (the relay costs while open)
BOND_HOST_BAD_SEC = 600             # a host the relay failed to reach is not bonded for this long
BOND_RELAY_OFF_SEC = 600            # the relay itself refusing us (HTTP error, quota ...): off for this long
BOND_NET_OFF_SEC = 60               # relay not reachable at all: a short pause (so no connection waits 6 s each)
LEARN_BYTES = 8_000_000
LEARN_KEEP_SEC = 30 * 86400
LEARN_MAX = 500
CF_REFRESH_SEC = 86400
# Video sites are left out until checked through the relay: their stream links can be tied to the address that
# opened the page. (Low-quality mode caps them anyway.) Add them in bulk.local.txt to try.
BULK_BUILTIN = ("akamaized.net", "dl.google.com", "gvt1.com", "steamcontent.com",
                "download.microsoft.com", "githubusercontent.com", "objects.githubusercontent.com")
NO_RELAY_PORTS = (25, 465, 587)
SECOND_LEVEL = {"co", "com", "ne", "or", "ac", "go", "gr", "ad", "net", "org", "gov", "edu"}  # co.jp, com.au ...

# frame types (every WebSocket binary message is exactly one frame; numbers are big-endian)
T_DATA, T_ACK, T_FIN, T_ERR, T_OK, T_PING, T_PONG = 1, 2, 3, 4, 5, 6, 7
# scheduler numbers (the relay uses the same rule for the other direction)
MIN_WIN, MAX_WIN = 64 << 10, 16 << 20
MIN_CHUNK, MAX_CHUNK = 4 << 10, 64 << 10
RATE_INIT, RATE_FLOOR = 1 << 20, 8 << 10
MAX_BOND_LINKS = 16
BOND_TOTAL_CAP = 24 << 20      # never more than this unacked in total (the relay keeps 32 MiB of out-of-order data)
BOND_REORDER_MAX = 32 << 20
BOND_PAUSE_HIGH, BOND_PAUSE_LOW = 8 << 20, 2 << 20  # the app reads slowly: stop acknowledging until it caught up
BOND_DRAIN_HIGH = 512 << 10
WS_BUF = 128 << 10
WS_MAX_MSG = 4 << 20
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

BOND_LINK = new_link(None, "bonded", None)  # tunnel()/pipe() take this as "the link" of a bonded connection
bond_stats = {"bytes": 0}   # payload carried through the relay (each byte once)
bond_flows = set()          # the flows that are open right now
relay_off_until, relay_why = 0.0, None
host_bad = {}               # host -> time until which it is not bonded (the relay could not reach it)
learned_bulk = {}           # registered domain -> last seen
cf_nets = None              # Cloudflare ranges (None = not known yet: nothing is bonded)
cf_state = {"at": 0.0, "tried": 0.0}  # when the list was fetched / when we last looked for the file
_relay_cache = {"key": None, "cfg": None}
_bulk_cache = {"key": None, "pats": ()}
_learned_loaded = [False]


# ----- settings -----
def relay_cfg():
    """The relay address and secret from relay.local.txt (None = feature off). Read again when the file changes."""
    p = state_path(RELAY_FILE)
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
    except OSError:
        _relay_cache.update(key=None, cfg=None)
        return None
    if _relay_cache["key"] == key:
        return _relay_cache["cfg"]
    cfg = None
    try:
        with open(p, encoding="utf-8-sig") as f:
            lines = [x.strip() for x in f.read().splitlines() if x.strip() and not x.strip().startswith("#")]
        u = urllib.parse.urlsplit(lines[0])
        secret = bytes.fromhex(lines[1])
        if u.scheme in ("ws", "wss") and u.hostname and len(secret) >= 16:
            tls = u.scheme == "wss"
            cfg = dict(host=u.hostname, port=u.port or (443 if tls else 80), tls=tls, base=u.path.rstrip("/"),
                       secret=secret)
    except (OSError, ValueError, IndexError):
        cfg = None
    _relay_cache.update(key=key, cfg=cfg)
    return cfg


def reg_domain(host):
    """The name a site is known by: last two labels (three after co / com ... + a country code: example.co.jp)."""
    parts = host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def bulk_patterns():
    p = state_path(BULK_FILE)
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
    except OSError:
        _bulk_cache.update(key=None, pats=())
        return ()
    if _bulk_cache["key"] != key:
        pats = []
        try:
            with open(p, encoding="utf-8-sig") as f:
                for line in f:
                    t = line.split("#")[0].strip().lower().lstrip("*.")
                    if t:
                        pats.append(t)
        except OSError:
            pass
        _bulk_cache.update(key=key, pats=tuple(pats))
    return _bulk_cache["pats"]


def load_learned():
    """bulk-learned.json: {registered domain: last seen}. Entries older than 30 days are forgotten."""
    _learned_loaded[0] = True
    learned_bulk.clear()
    try:
        with open(state_path(LEARNED_FILE), encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        for k, v in d.items():
            if isinstance(k, str) and isinstance(v, (int, float)) and now - v < LEARN_KEEP_SEC:
                learned_bulk[k.lower()] = float(v)
    except (OSError, ValueError, AttributeError):
        pass


def save_learned():
    try:
        with open(state_path(LEARNED_FILE), "w", encoding="utf-8") as f:
            json.dump(learned_bulk, f)
    except OSError:
        pass


def learn_bulk(host):
    """A direct connection to this host moved a lot of data: it is worth bonding next time."""
    if not host or is_ip(host) or "." not in host:
        return
    if not _learned_loaded[0]:
        load_learned()
    now = time.time()
    for k in [k for k, v in learned_bulk.items() if now - v > LEARN_KEEP_SEC]:
        learned_bulk.pop(k, None)
    key = host.lower().rstrip(".")  # the exact host only: never a whole domain with its login pages
    new = key not in learned_bulk
    learned_bulk[key] = now
    while len(learned_bulk) > LEARN_MAX:
        learned_bulk.pop(min(learned_bulk, key=learned_bulk.get), None)
    if new:
        log("bond: learned a bulk host:", key)
    save_learned()


def named_bulk(host):
    """A download/video host named in the built-in list or in bulk.local.txt."""
    h = host.lower().rstrip(".")
    return any(h == p or h.endswith("." + p) for p in BULK_BUILTIN + bulk_patterns())


def is_bulk(host):
    h = host.lower().rstrip(".")
    if named_bulk(h):
        return True
    if not _learned_loaded[0]:
        load_learned()
    if is_ip(h):
        return h in learned_bulk
    key = h
    if key in learned_bulk:
        learned_bulk[key] = time.time()
        return True
    return False


def load_cf():
    global cf_nets
    try:
        with open(state_path(CF_FILE), encoding="utf-8") as f:
            d = json.load(f)
        nets = [ipaddress.ip_network(x) for x in d["v4"]]
        if nets:
            cf_nets = nets
            cf_state["at"] = float(d.get("at", 0))
            return cf_state["at"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 0


def in_cloudflare(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in (cf_nets or ()) if n.version == a.version)


async def fetch_cf():
    """Cloudflare's address list (a few hundred bytes) over any working link; kept in cf-ips.json."""
    global cf_nets
    for l in sorted((l for l in links.values() if l["healthy"] and not l["suspect"]), key=lambda l: l["rtt"] or 999):
        try:
            r, w = await asyncio.wait_for(connect_tls(l, "www.cloudflare.com"), 15)
            try:
                w.write(b"GET /ips-v4 HTTP/1.1\r\nHost: www.cloudflare.com\r\nConnection: close\r\n"
                        b"User-Agent: link-bond\r\n\r\n")
                raw = b""
                while len(raw) < 65536:
                    more = await asyncio.wait_for(r.read(65536), 15)
                    if not more:
                        break
                    raw += more
            finally:
                w.close()
            head, _, body = raw.partition(b"\r\n\r\n")
            if b" 200 " not in head.split(b"\r\n")[0]:
                continue
            nets = []
            for line in body.decode("ascii", "ignore").split():
                try:
                    nets.append(ipaddress.ip_network(line.strip()))
                except ValueError:
                    pass
            if len(nets) >= 5:
                cf_nets = nets
                cf_state["at"] = time.time()
                with open(state_path(CF_FILE), "w", encoding="utf-8") as f:
                    json.dump({"at": time.time(), "v4": [str(n) for n in nets]}, f)
                return True
        except Exception:
            continue
    return False


async def cf_loop():
    """Once a day (only when a relay is set up): refresh the Cloudflare ranges. Until they are known nothing is bonded."""
    while True:
        wait = 300
        try:
            if relay_cfg() is not None:
                if cf_nets is None:
                    load_cf()
                if time.time() - cf_state["at"] > CF_REFRESH_SEC and not await fetch_cf():
                    wait = 30  # a few hundred bytes: try again soon (at start the links may not be up yet)
        except Exception:
            pass
        await asyncio.sleep(wait)


def bond_links():
    """The links a bonded connection may use right now."""
    return [l for l in links.values() if l["healthy"] and not l["suspect"]]


def bond_off_reason():
    """Why bonding is not available at all right now (None = available)."""
    if relay_cfg() is None:
        return "no relay set up"
    if relay_off_until > time.time():
        return relay_why or "relay paused"
    if vpn_on:
        return "VPN on"
    if len(bond_links()) < 2:
        return "fewer than 2 links"
    if cf_nets is None:
        if time.time() - cf_state["tried"] > 5:  # (a file read at most every 5 s)
            cf_state["tried"] = time.time()
            load_cf()
        if cf_nets is None:
            return "Cloudflare ranges not known yet"
    return None


def bondable(host, port, ip):
    """Should this connection go over all links through the relay? Only big transfers, and only to sites the
    relay can reach (not behind Cloudflare itself)."""
    if bond_off_reason() is not None:
        return False
    if not (1 <= port <= 65535) or port in NO_RELAY_PORTS:
        return False
    if is_blocked(host) or not ip or in_cloudflare(ip):
        return False
    if is_pinned(host) and not named_bulk(host):  # a login site keeps one address; named download hosts are no logins
        return False
    if host_bad.get(host.lower(), 0) > time.time():
        return False
    return is_bulk(host)


# ----- frames -----
def f_data(seq, payload):
    return struct.pack(">BI", T_DATA, seq) + payload


def f_ack(cum, entries):
    return struct.pack(">BIB", T_ACK, cum, len(entries)) + b"".join(struct.pack(">BQ", n, v) for n, v in entries)


def f_fin(total):
    return struct.pack(">BI", T_FIN, total)


def f_err(text):
    return bytes([T_ERR]) + text.encode("utf-8", "replace")[:100]


def f_ok():
    return bytes([T_OK])


def f_ping(ms):
    return struct.pack(">BQ", T_PING, ms)


def f_pong(ms):
    return struct.pack(">BQ", T_PONG, ms)


def decode_frame(msg):
    """One WebSocket message -> (type, ...) or None if it is not a valid frame.
    (T_DATA, seq, payload) (T_ACK, cum, {link: bytes}) (T_FIN, total) (T_ERR, text) (T_OK,) (T_PING, ms) (T_PONG, ms)"""
    try:
        t = msg[0]
        if t == T_DATA and len(msg) > 5:
            return T_DATA, struct.unpack_from(">I", msg, 1)[0], msg[5:]
        if t == T_ACK:
            cum, n = struct.unpack_from(">IB", msg, 1)
            if len(msg) != 6 + 9 * n:
                return None
            return T_ACK, cum, {struct.unpack_from(">B", msg, 6 + 9 * i)[0]: struct.unpack_from(">Q", msg, 7 + 9 * i)[0]
                                for i in range(n)}
        if t == T_FIN and len(msg) == 5:
            return T_FIN, struct.unpack_from(">I", msg, 1)[0]
        if t == T_ERR:
            return T_ERR, bytes(msg[1:]).decode("utf-8", "replace")
        if t == T_OK:
            return (T_OK,)
        if t in (T_PING, T_PONG) and len(msg) == 9:
            return t, struct.unpack_from(">Q", msg, 1)[0]
    except (IndexError, struct.error):
        pass
    return None


def relay_dst(host, port):
    return base64.urlsafe_b64encode(f"{host}:{port}".encode()).decode().rstrip("=")


def relay_sig(secret, flow, link, t, d):
    return hmac.new(secret, f"{flow}|{link}|{t}|{d}".encode(), hashlib.sha256).hexdigest()


def relay_path(cfg, flow, link, t, host, port):
    """/f/<flow>?l=<link>&t=<unix s>&d=<destination>&s=<signature> (the part after the relay address)."""
    d = relay_dst(host, port)
    return f"{cfg['base']}/f/{flow}?l={link}&t={t}&d={d}&s={relay_sig(cfg['secret'], flow, link, t, d)}"


# ----- the scheduler (same rule on both sides) -----
def l_window(rate, rtt):
    return min(max(rate * (rtt + 0.1) * 2, MIN_WIN), MAX_WIN)


def l_eta(inflight, n, rate, rtt):
    return (inflight + n) / rate + rtt / 2


def l_chunk(rate):
    return int(min(max(rate * 0.05, MIN_CHUNK), MAX_CHUNK))


def pick_link(blinks, queued, fixed=0):
    """The link a piece should go on: the one where it arrives first. Returns (link, n, room) or None.
    room False = that link's window is full: WAIT for an ACK. A worse link is never used instead - that is what
    keeps a slow, lossy link from dragging a fast one down. fixed = the piece exists already (n bytes)."""
    best = None
    for b in blinks:
        n = fixed or min(l_chunk(b.rate), queued)
        if n <= 0:
            continue
        e = l_eta(b.inflight, n, b.rate, b.rtt)
        if best is None or e < best[0]:
            best = (e, b, n)
    if best is None:
        return None
    _, b, n = best
    return b, n, b.inflight + n <= l_window(b.rate, b.rtt)


class BLink:
    """One link inside one flow: its WebSocket and what the scheduler knows about it."""

    def __init__(self, n, link=None):
        self.n, self.link, self.ws = n, link, None
        self.state = "down"      # down | connecting | wait (socket up, no OK yet) | up | dead (silent) -> up again when heard
        r = (link or {}).get("rtt")
        self.rtt = (r / 1000.0) if r else 0.3
        self.rate = RATE_INIT    # bytes/s, learned from what the peer confirms
        self.used = False        # data has gone over it once (a link that comes back starts at half its old rate)
        self.rtt_ok = False      # a real answer time was measured on the current socket
        self.reset_counts()
        self.last_heard = self.ping_at = self.opened = self.dead_at = 0.0
        self.next_try = 0.0

    def reset_counts(self):
        """Counters belong to one WebSocket (the relay starts them at 0 for every new one)."""
        self.sent = self.acked = self.rep = 0     # DATA bytes sent on it / confirmed by the peer / last confirmed total
        self.rx = self.rx_rep = 0                 # DATA bytes received on it / the total we confirm to the peer
        self.rt_t, self.rt_acked, self.rt_sent = 0.0, 0, 0
        self.peak = 0                             # most bytes in flight since the last rate update

    @property
    def inflight(self):
        return max(0, self.sent - self.acked)

    @property
    def live(self):
        return self.state == "up"


class Chunk:
    __slots__ = ("seq", "data", "tried", "link", "end", "t")

    def __init__(self, seq, data):
        self.seq, self.data = seq, data
        self.tried = set()   # link numbers it went on already
        self.link = -1       # the link of its latest copy
        self.end = 0         # that link's byte count after this copy: confirmed once the link's total reaches it
        self.t = 0.0


# ----- a minimal WebSocket client (RFC 6455) -----
class WSError(Exception):
    pass


class WSHandshakeError(WSError):
    def __init__(self, status):
        WSError.__init__(self, "HTTP %s" % status)
        self.status = status


def ws_xor(data, key):
    """Apply the 4-byte mask (the same operation masks and unmasks)."""
    n = len(data)
    if not n:
        return b""
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def ws_frame(opcode, payload, mask=True, fin=True):
    n = len(payload)
    m = 0x80 if mask else 0
    b0 = (0x80 if fin else 0) | opcode
    if n < 126:
        head = bytes([b0, m | n])
    elif n < 65536:
        head = bytes([b0, m | 126]) + struct.pack(">H", n)
    else:
        head = bytes([b0, m | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        return head + key + ws_xor(payload, key)
    return head + payload


class WS:
    """One WebSocket. send() queues a message (control messages first), recv() returns the next binary message
    (ping / pong / close are handled inside; text messages are ignored). None = closed."""

    def __init__(self, r, w, mask=True):
        self.r, self.w, self.mask = r, w, mask
        self.ctl, self.dq = collections.deque(), collections.deque()
        self.ev = asyncio.Event()
        self.closing = self.closed = False
        self.close_code = None
        self.task = asyncio.ensure_future(self._wloop())

    def send(self, payload, ctl=False, opcode=2):
        if self.closing or self.closed:
            return
        (self.ctl if ctl else self.dq).append(ws_frame(opcode, payload, self.mask))
        self.ev.set()

    async def _wloop(self):
        try:
            while True:
                while self.ctl or (self.dq and not self.closing):
                    self.w.write(self.ctl.popleft() if self.ctl else self.dq.popleft())
                    if self.w.transport.get_write_buffer_size() > WS_BUF:
                        await self.w.drain()
                if self.closing:
                    break
                self.ev.clear()
                await self.ev.wait()
            await asyncio.wait_for(self.w.drain(), 3)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.closed = True
            try:
                self.w.close()
            except Exception:
                pass

    def close(self, code=1000):
        """Say goodbye (what is queued as control messages still goes out), then close."""
        if self.closing or self.closed:
            return
        self.ctl.append(ws_frame(8, struct.pack(">H", code), self.mask))
        self.closing = True
        self.dq.clear()
        self.ev.set()

    def abort(self):
        self.closing = self.closed = True
        self.dq.clear()
        self.ctl.clear()
        try:
            self.w.transport.abort()
        except Exception:
            pass
        self.ev.set()

    async def recv(self):
        frag, frag_op = None, 0
        try:
            while True:
                b0, b1 = await self.r.readexactly(2)
                fin, op, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
                if b0 & 0x70:
                    raise WSError("reserved bits")
                if n == 126:
                    n = struct.unpack(">H", await self.r.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await self.r.readexactly(8))[0]
                key = await self.r.readexactly(4) if b1 & 0x80 else None
                if n > WS_MAX_MSG:
                    raise WSError("message too big")
                data = await self.r.readexactly(n) if n else b""
                if key:
                    data = ws_xor(data, key)
                if op >= 8:  # control message
                    if not fin or n > 125:
                        raise WSError("bad control message")
                    if op == 8:
                        self.close_code = struct.unpack(">H", data[:2])[0] if n >= 2 else 1005
                        self.close(self.close_code if self.close_code < 5000 else 1000)
                        return None
                    if op == 9:
                        self.send(data, ctl=True, opcode=10)
                    continue
                if op == 0:
                    if frag is None:
                        raise WSError("unexpected continuation")
                    frag.append(data)
                    if sum(len(x) for x in frag) > WS_MAX_MSG:
                        raise WSError("message too big")
                    if not fin:
                        continue
                    data, op, frag = b"".join(frag), frag_op, None
                else:
                    if frag is not None:
                        raise WSError("interrupted fragments")
                    if not fin:
                        frag, frag_op = [data], op
                        continue
                if op == 2:
                    return data
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return None


async def ws_handshake(r, w, host_header, path):
    """HTTP upgrade. Returns a WS or raises WSHandshakeError(status)."""
    key = base64.b64encode(os.urandom(16)).decode()
    w.write((f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: link-bond\r\n\r\n").encode())
    head = await r.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if status != 101:
        raise WSHandshakeError(status)
    hdr = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        hdr[k.strip().lower()] = v.strip()
    want = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
    if hdr.get("sec-websocket-accept") != want:
        raise WSHandshakeError(0)
    return WS(r, w)


_ssl_ctx = []


async def open_ws(l, cfg, path):
    """A WebSocket to the relay over link l (the same bound socket as any other connection of that link)."""
    ip = await resolve(cfg["host"], cfg["port"])
    sk = mk_sock(l)
    try:
        await link_connect(l, sk, (ip, cfg["port"]))
        if cfg["tls"]:
            if not _ssl_ctx:
                _ssl_ctx.append(ssl.create_default_context())
            r, w = await asyncio.open_connection(sock=sk, ssl=_ssl_ctx[0], server_hostname=cfg["host"])
        else:
            r, w = await asyncio.open_connection(sock=sk)
    except BaseException:
        sk.close()
        raise
    default = 443 if cfg["tls"] else 80
    try:
        return await ws_handshake(r, w, cfg["host"] + ("" if cfg["port"] == default else ":%d" % cfg["port"]), path)
    except BaseException:
        w.close()
        raise


# ----- one bonded connection -----
class BondFail(Exception):
    """kind: host (the relay could not reach the site) | relay (the relay refuses us) | net (relay not reachable)"""

    def __init__(self, kind, why):
        Exception.__init__(self, why)
        self.kind, self.why = kind, why


class BondWriter:
    """What tunnel() / pipe() write the app's bytes to (looks like an asyncio StreamWriter)."""

    def __init__(self, flow):
        self.f = flow

    def write(self, data):
        self.f.app_write(data)

    async def drain(self):
        await self.f.app_drain()

    def close(self):
        self.f.app_close()

    def is_closing(self):
        return self.f.closing or self.f.dead

    def get_extra_info(self, name, default=None):
        return default

    async def wait_closed(self):
        return None


class BondFlow:
    def __init__(self, cfg, host, port):
        self.cfg, self.host, self.port = cfg, host, port
        self.flow = os.urandom(16).hex()
        self.reader = asyncio.StreamReader(limit=1 << 20)
        self.writer = BondWriter(self)
        self.lk = {}              # link number -> BLink
        self.nums = {}            # id(link dict) -> link number
        self.dead = False         # finished (why = None) or aborted
        self.why = None
        self.tasks = set()
        self.first_ok = asyncio.get_running_loop().create_future()
        self.init_left = self.init_total = 0
        self.http_fail = []
        self.ws_seen = False
        # upstream (app -> relay)
        self.wbuf = bytearray()
        self.closing = False      # the app closed its side: send FIN when everything is out
        self.close_calls = 0
        self.closed_at = 0.0
        self.fin_sent = False
        self.next_seq = 0
        self.cum = 0              # every seq below this is confirmed by the relay
        self.chunks = {}          # seq -> Chunk (kept until confirmed)
        self.resend = collections.deque()
        self.resend_set = set()
        self.drain_ev = asyncio.Event()
        self._kicked = False
        # downstream (relay -> app)
        self.cum_rx = 0           # next seq we need
        self.rx_buf = {}
        self.rx_buf_bytes = 0
        self.fin_rx = None
        self.eof = False
        self.paused = False
        self.ack_dirty = False
        self.ack_bytes = 0
        self.last_ack = 0.0
        now = time.monotonic()
        self.last_data = self.last_rx = now
        self.no_live_since = now
        self.t_rate = self.t_reinj = self.t_maint = now
        bond_flows.add(self)

    # ---- opening ----
    def blink_for(self, l):
        n = self.nums.get(id(l))
        if n is None:
            if len(self.lk) >= MAX_BOND_LINKS:
                return None
            n = len(self.lk)
            self.nums[id(l)] = n
            self.lk[n] = BLink(n, l)
        return self.lk[n]

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def start(self, timeout):
        """Open a WebSocket on every link; return as soon as the relay says OK on one of them."""
        self.spawn(self.ticker())
        for l in bond_links()[:MAX_BOND_LINKS]:
            b = self.blink_for(l)
            if b is not None:
                self.init_total += 1
                self.init_left += 1
                self.spawn(self.open_link(b, True))
        if not self.init_total:
            self.abort("no link", tell=False)
            raise BondFail("net", "no link")
        try:
            await asyncio.wait_for(asyncio.shield(self.first_ok), timeout)
        except asyncio.TimeoutError:
            kind = "host" if self.ws_seen else "net"
            self.abort("no OK in time", tell=False)
            raise BondFail(kind, "no OK in time")
        except BondFail:
            self.abort("failed to start", tell=False)
            raise

    def fail_start(self, e):
        if not self.first_ok.done():
            self.first_ok.set_exception(e)
        self.first_ok.exception()  # (marks it as seen: nobody may be waiting for it)

    async def open_link(self, b, initial=False):
        b.state = "connecting"
        b.next_try = time.monotonic() + 3.0
        path = relay_path(self.cfg, self.flow, b.n, int(time.time()), self.host, self.port)
        ws, status = None, None
        try:
            ws = await asyncio.wait_for(open_ws(b.link, self.cfg, path), BOND_OPEN_SEC)
        except WSHandshakeError as e:
            status = e.status
        except Exception:
            pass
        if initial:
            self.init_left -= 1
        if ws is None or self.dead:
            if ws is not None:
                ws.abort()
            b.state = "down"
            if initial and status is not None:
                self.http_fail.append(status)
            if initial and self.init_left <= 0 and not self.first_ok.done() \
                    and not any(x.state in ("wait", "up") for x in self.lk.values()):
                if len(self.http_fail) >= self.init_total:
                    self.fail_start(BondFail("relay", "HTTP %s" % self.http_fail[0]))
                else:
                    self.fail_start(BondFail("net", "relay not reachable"))
            return
        self.ws_seen = True
        old = b.rate
        b.reset_counts()
        b.rtt_ok = False
        if b.used:
            b.rate = max(RATE_FLOOR, old / 2)  # a link that comes back starts careful
        b.ws, b.state = ws, "wait"
        b.opened = b.last_heard = time.monotonic()
        self.spawn(self.rx_loop(b, ws))

    async def rx_loop(self, b, ws):
        try:
            while True:
                msg = await ws.recv()
                if msg is None:
                    break
                self.on_msg(b, msg)
        except Exception as e:
            ws.abort()
            if not isinstance(e, (WSError, ConnectionError, OSError)):
                log("bond: internal error:", type(e).__name__)
        finally:
            self.ws_gone(b, ws)

    # ---- sending: the scheduler ----
    def kick(self):
        if not self._kicked and not self.dead:
            self._kicked = True
            asyncio.get_running_loop().call_soon(self.pump)

    def _acct(self, b, n, kind):
        l = b.link
        if l is not None:
            l["bytes"] += n
            l["b_" + kind] += n
            if kind == "down":
                l["data_at"] = time.time()

    def send_chunk(self, b, c, now):
        n = len(c.data)
        b.ws.send(f_data(c.seq, c.data))
        b.sent += n
        b.used = True
        c.link, c.end, c.t = b.n, b.sent, now
        c.tried.add(b.n)
        self.peak_up(b)
        self._acct(b, n, "up")

    @staticmethod
    def peak_up(b):
        if b.inflight > b.peak:
            b.peak = b.inflight

    def pump(self):
        """Hand pieces to links while there is room. Stops (waits for an ACK) as soon as the best link is full."""
        self._kicked = False
        if self.dead:
            return
        now = time.monotonic()
        live = [b for b in self.lk.values() if b.state == "up"]
        if live:
            total = sum(b.inflight for b in live)
            for _ in range(4000):
                c = None
                while self.resend:
                    s = self.resend[0]
                    c = self.chunks.get(s)
                    if c is None or s < self.cum:
                        self.resend.popleft()
                        self.resend_set.discard(s)
                        c = None
                        continue
                    break
                if c is not None:
                    p = pick_link(live, 0, len(c.data))
                elif self.wbuf:
                    p = pick_link(live, len(self.wbuf))
                else:
                    break
                if p is None or not p[2] or total + p[1] > BOND_TOTAL_CAP:
                    break
                b, n, _ = p
                if c is not None:
                    self.resend.popleft()
                    self.resend_set.discard(c.seq)
                else:
                    c = Chunk(self.next_seq, bytes(self.wbuf[:n]))
                    del self.wbuf[:n]
                    self.chunks[c.seq] = c
                    self.next_seq += 1
                    bond_stats["bytes"] += n
                self.send_chunk(b, c, now)
                self.last_data = now
                total += n
            if self.closing and not self.fin_sent and not self.wbuf and not self.resend:
                self.fin_sent = True
                for b in live:
                    b.ws.send(f_fin(self.next_seq), ctl=True)
        if len(self.wbuf) <= BOND_DRAIN_HIGH:
            self.drain_ev.set()
        self.check_done()

    def requeue(self, b):
        """Everything that went out on this link and is not confirmed goes back to the front of the queue."""
        seqs = []
        for s in range(self.cum, self.next_seq):
            c = self.chunks.get(s)
            if c is not None and c.link == b.n and c.end > b.acked and s not in self.resend_set:
                seqs.append(s)
        b.acked = b.sent
        if seqs:
            merged = sorted(set(self.resend) | set(seqs))
            self.resend = collections.deque(merged)
            self.resend_set = set(merged)

    def link_dead(self, b, now):
        b.state, b.dead_at = "dead", now
        self.requeue(b)
        self.ack_dirty = True  # the last ACK may have gone into the silent link: say it again on another
        self.kick()

    def reinject(self, now):
        """A piece that stays unconfirmed too long (among the 8 lowest) is sent again on the best OTHER link."""
        live = [b for b in self.lk.values() if b.state == "up"]
        if len(live) < 2:
            return
        for s in range(self.cum, min(self.next_seq, self.cum + 8)):
            c = self.chunks.get(s)
            if c is None or s in self.resend_set:
                continue
            cur = self.lk.get(c.link)
            if cur is None or cur.state != "up" or c.end <= cur.acked:
                continue  # its link has it already (an earlier hole holds the line) or the link is gone
            if now - c.t <= cur.rtt * 2 + cur.inflight / cur.rate + 0.5:
                continue
            p = pick_link([b for b in live if b.n not in c.tried], 0, len(c.data))
            if p is not None and p[2]:
                self.send_chunk(p[0], c, now)

    # ---- receiving ----
    def on_msg(self, b, msg):
        now = time.monotonic()
        b.last_heard = now
        if b.state == "dead":
            b.state = "up"
            b.rate = max(RATE_FLOOR, b.rate / 2)
            self.kick()
        fr = decode_frame(msg)
        if fr is None:
            return
        t = fr[0]
        if t == T_DATA:
            self.on_data(b, fr[1], fr[2], now)
        elif t == T_ACK:
            self.on_ack(fr[1], fr[2])
        elif t == T_OK:
            if b.state == "wait":
                b.state = "up"
                self.ping(b, now)
                if self.fin_sent:
                    b.ws.send(f_fin(self.next_seq), ctl=True)
                if not self.first_ok.done():
                    self.first_ok.set_result(True)
                self.kick()
        elif t == T_FIN:
            self.fin_rx = fr[1]
            self.deliver()
            self.send_ack(now)
        elif t == T_ERR:
            if not self.first_ok.done():
                self.fail_start(BondFail("host", fr[1]))
            self.abort("relay: " + fr[1], tell=False)
        elif t == T_PING:
            b.ws.send(f_pong(fr[1]), ctl=True)
        elif t == T_PONG:
            s = max(0.001, (int(now * 1000) - fr[1]) / 1000.0)
            b.rtt = b.rtt + (s - b.rtt) / 8 if b.rtt_ok else s  # the first real answer replaces the guess
            b.rtt_ok = True

    def on_data(self, b, seq, payload, now):
        n = len(payload)
        b.rx += n
        self._acct(b, n, "down")
        if not self.paused:
            b.rx_rep = b.rx
        self.last_rx = now
        self.ack_dirty = True
        self.ack_bytes += n
        if seq >= self.cum_rx and seq not in self.rx_buf and not self.eof:
            self.rx_buf[seq] = payload
            self.rx_buf_bytes += n
            if self.rx_buf_bytes > BOND_REORDER_MAX:
                self.abort("reorder overflow")
                return
            self.deliver()
        if self.ack_bytes >= (256 << 10):
            self.send_ack(now)

    def deliver(self):
        """Give the app what is complete and in order."""
        if self.eof:
            return
        buf = self.rx_buf
        while not self.paused and self.cum_rx in buf:
            d = buf.pop(self.cum_rx)
            self.rx_buf_bytes -= len(d)
            self.cum_rx += 1
            bond_stats["bytes"] += len(d)
            self.reader.feed_data(d)
            if len(getattr(self.reader, "_buffer", b"")) > BOND_PAUSE_HIGH:
                self.paused = True
        if self.fin_rx is not None and self.cum_rx >= self.fin_rx and not self.eof:
            self.eof = True
            self.reader.feed_eof()
            self.ack_dirty = True
            self.send_ack(time.monotonic())
            self.check_done()

    def best_ctl(self):
        c = [b for b in self.lk.values() if b.state == "up"] or [b for b in self.lk.values() if b.state == "dead"]
        return min(c, key=lambda b: b.rtt) if c else None

    def send_ack(self, now):
        b = self.best_ctl()
        self.ack_bytes = 0
        if b is None or b.ws is None:
            return
        self.ack_dirty = False
        self.last_ack = now
        b.ws.send(f_ack(self.cum_rx, [(x.n, x.rx_rep) for x in self.lk.values() if x.rx_rep]), ctl=True)

    def on_ack(self, cum, totals):
        cum = min(cum, self.next_seq)
        if cum > self.cum:
            for s in range(self.cum, cum):
                self.chunks.pop(s, None)
            self.cum = cum
        for n, total in totals.items():
            x = self.lk.get(n)
            if x is None:
                continue
            d = total - x.rep if total >= x.rep else total
            x.rep = total
            if d > 0:
                x.acked = min(x.sent, x.acked + d)
        self.kick()

    def ping(self, b, now):
        b.ping_at = now
        b.ws.send(f_ping(int(now * 1000)), ctl=True)

    # ---- links coming and going ----
    def ws_gone(self, b, ws):
        if b.ws is not ws:
            return
        if ws.close_code == 1011 and not self.dead:
            self.abort("relay closed the flow", tell=False)
            return
        b.ws = None
        b.state = "down"
        self.requeue(b)
        self.kick()

    def drop_ws(self, b):
        ws = b.ws
        if ws is not None:
            ws.abort()
            self.ws_gone(b, ws)

    def maintain(self, now):
        """Every 3 s: a link that was cut loses its socket; a link that is healthy and has none gets one."""
        present = {id(l) for l in links.values()}
        for b in list(self.lk.values()):
            l = b.link
            if b.ws is not None and (id(l) not in present or not l["healthy"]):
                self.drop_ws(b)
        for l in bond_links():
            b = self.blink_for(l)
            if b is not None and b.state == "down" and now >= b.next_try:
                self.spawn(self.open_link(b))

    # ---- the app side ----
    def app_write(self, data):
        if self.closing or self.dead or not data:
            return
        self.wbuf += data
        self.kick()

    async def app_drain(self):
        while len(self.wbuf) > BOND_DRAIN_HIGH and not self.dead:
            self.drain_ev.clear()
            await self.drain_ev.wait()
        if self.dead and self.why:
            raise ConnectionError(self.why)

    def app_close(self):
        self.close_calls += 1
        if self.dead:
            return
        if self.close_calls == 1:
            self.closing = True
            self.closed_at = time.monotonic()
            self.kick()
        elif not self.eof:
            self.abort("app closed")  # the app is gone before the answer was complete

    # ---- housekeeping ----
    async def ticker(self):
        while not self.dead:
            await asyncio.sleep(0.02)
            try:
                self.tick(time.monotonic())
            except Exception as e:
                log("bond: internal error:", type(e).__name__)
                self.abort("internal error")

    def tick(self, now):
        if self.paused and len(getattr(self.reader, "_buffer", b"")) < BOND_PAUSE_LOW:
            self.paused = False
            for b in self.lk.values():
                b.rx_rep = b.rx
            self.ack_dirty = True
            self.deliver()
        n_live = 0
        for b in self.lk.values():
            if b.state == "up" and now - b.last_heard > max(2.0, 4 * b.rtt):
                self.link_dead(b, now)
            elif b.state == "dead" and now - b.dead_at > 10:
                self.drop_ws(b)
            elif b.state == "wait" and now - b.opened > 8:
                self.drop_ws(b)
            if b.state == "up":
                n_live += 1
            if b.state in ("up", "dead") and now - b.ping_at >= 1.0:
                self.ping(b, now)
        if n_live:
            self.no_live_since = now
        elif now - self.no_live_since > 60:
            self.abort("no link for 60 s")
            return
        if (self.ack_dirty and now - self.last_ack >= 0.04) or (now - self.last_rx < 10 and now - self.last_ack >= 1.0):
            self.send_ack(now)  # (and once a second while data comes: an ACK lost with a link is repeated)
        if now - self.t_rate >= 0.25:
            self.t_rate = now
            for b in self.lk.values():
                if b.state in ("up", "dead"):
                    self.rate_window(b, now)
        if now - self.t_reinj >= 0.1:
            self.t_reinj = now
            self.reinject(now)
        if now - self.t_maint >= 1.0:
            self.t_maint = now
            self.maintain(now)
            if now - max(self.last_data, self.last_rx) > BOND_IDLE_SEC:
                self.abort("idle")
                return
            if self.closing and not self.eof and now - max(self.closed_at, self.last_rx) > 20:
                self.abort("app went away")
                return
        self.pump()

    def rate_window(self, b, now):
        """Every 250 ms: how fast the peer confirms bytes on this link. Only a window where the link was really
        full (in flight reached half the window) says something: a player that fetches in bursts, with idle
        seconds between, must not make the next burst start slow. A faster sample counts at once; a slower one
        only by 1/4."""
        if b.rt_t:
            dt = now - b.rt_t
            sample = (b.acked - b.rt_acked) / dt
            if b.sent > b.rt_sent and b.peak >= l_window(b.rate, b.rtt) / 2:
                b.rate = max(RATE_FLOOR, sample if sample > b.rate else b.rate * 0.75 + sample * 0.25)
        b.rt_t, b.rt_acked, b.rt_sent = now, b.acked, b.sent
        b.peak = b.inflight

    def check_done(self):
        if self.dead or not (self.fin_sent and self.eof and self.cum >= self.next_seq):
            return
        self.send_ack(time.monotonic())
        self.finish()

    def finish(self):
        self.dead = True
        self.cleanup(1000)

    def abort(self, why, tell=True):
        if self.dead:
            return
        self.dead, self.why = True, why
        if not self.eof:
            self.reader.set_exception(ConnectionError(why))
        if tell:
            b = self.best_ctl()
            if b is not None and b.ws is not None:
                b.ws.send(f_err(why), ctl=True)
        self.cleanup(1011)

    def cleanup(self, code):
        bond_flows.discard(self)
        if sum(b.rx + b.sent for b in self.lk.values()) > 1 << 20:  # what each link carried (for tuning)
            log("bond: flow done -", "; ".join(f"{label(b.link)} down {b.rx >> 10} KB up {b.sent >> 10} KB rtt {b.rtt * 1000:.0f} ms rate {b.rate * 8 / 1e6:.1f} Mbps" for b in self.lk.values()))
        for b in self.lk.values():
            if b.ws is not None:
                b.ws.close(code)
                b.ws = None
            b.state = "down"
        cur = asyncio.current_task()
        for t in list(self.tasks):
            if t is not cur:
                t.cancel()
        self.chunks.clear()
        self.rx_buf.clear()
        self.drain_ev.set()
        if not self.first_ok.done():
            self.first_ok.set_exception(BondFail("net", "closed"))
            self.first_ok.exception()


def bond_note_fail(host, e):
    """What to remember after a bonded open failed: not bonding this host / pausing the relay for a while."""
    global relay_off_until, relay_why
    now = time.time()
    if e.kind == "host":
        host_bad[host.lower()] = now + BOND_HOST_BAD_SEC
        log("bond: relay cannot reach", host, "- not bonded for 10 min")
    elif e.kind == "relay":
        relay_off_until, relay_why = now + BOND_RELAY_OFF_SEC, "relay refused (" + e.why + ")"
        log("bond: relay off for 10 min (" + e.why + ")")
    else:
        relay_off_until, relay_why = now + BOND_NET_OFF_SEC, "relay not reachable"
        log("bond: relay not reachable - paused for 1 min")


async def open_bonded(host, port):
    """One connection over all links through the relay. Returns (reader, writer) or raises."""
    cfg = relay_cfg()
    if cfg is None:
        raise ConnectionError("no relay")
    f = BondFlow(cfg, host, port)
    try:
        await f.start(BOND_OPEN_SEC)
    except BondFail as e:
        bond_note_fail(host, e)
        raise ConnectionError("bond: " + e.kind)
    return f.reader, f.writer


def bond_status():
    why = bond_off_reason()
    return {"on": why is None, "flows": len(bond_flows), "bytes": bond_stats["bytes"], "why_off": why}


def learn_from(rec, link, host):
    """A direct tunnel that moved a lot: remember the host as bulk (the next connection to it is bonded)."""
    if link is BOND_LINK or link is SYSTEM_LINK or not host or is_pinned(host):
        return  # (a login site is never bonded, so learning it is useless)
    if max(rec.get("n_up", 0), rec.get("n_down", 0)) >= LEARN_BYTES:
        learn_bulk(host)


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
DL_OUTAGE_MAX = 900      # a download waits this long for a link to come back before it gives up
ALLOW_LOCAL_DOWNLOADS = False  # tests only
downloads = {}                 # id -> job dict
_dl_seq = 0


class ServerError(Exception):
    """The server answered with an error (404 ...): retrying over another link would not help."""


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
            ip = await resolve(self.host, self.port)
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
    return [l for l in links.values() if l["healthy"] and not l["suspect"]] or [l for l in links.values() if l["healthy"]]


async def wait_links(job, deadline_s=DL_OUTAGE_MAX):
    """Every link is cut: the download waits (shown as waiting) and goes on when one is back."""
    t0 = time.time()
    while not dl_links():
        if job["state"] != "running":
            raise Aborted()
        if time.time() - t0 > deadline_s:
            raise ConnectionError("回線が戻りませんでした / no link came back")
        job["waiting"] = True
        await asyncio.sleep(2)
    job["waiting"] = False
    return dl_links()


async def run_download(job):
    try:
        scheme, host, port, path = parse_url(job["url"])
        first_host = host
        extra = dict(job.get("headers") or {})

        def hdrs():  # the browser's cookies only go to the host they belong to (not after a redirect elsewhere)
            return extra if host == first_host else {k: v for k, v in extra.items() if k != "Cookie"}

        ls = await wait_links(job)
        best = max(ls, key=weight)
        # 1. ask for the first byte: follows redirects, learns the size and whether Range works
        for _ in range(6):
            for attempt in range(6):  # a link that drops right now: the next one, or wait for one to come back
                c = Conn(best, scheme, host, port)
                try:
                    status, hdr = await c.request(path, (0, 0), hdrs())
                    break
                except (OSError, ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                    c.close()
                    if attempt == 5:
                        raise
                    suspect(best, "download")
                    await asyncio.sleep(1)
                    ls = await wait_links(job)
                    best = max(ls, key=weight)
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
            for attempt in range(5):
                with open(part, "wb") as f:
                    job["done"] = 0
                    c = Conn(best, scheme, host, port)
                    try:
                        status, hdr = await c.request(path, None, hdrs())
                        if status >= 400:
                            raise ServerError(f"HTTP {status}")

                        def put(b, l=best):
                            if job["state"] != "running":
                                raise Aborted()
                            f.write(b)
                            job["done"] += len(b)
                            count(l, len(b))

                        await c.read_body(hdr, put)
                        c.close()
                        break
                    except (Aborted, ServerError):
                        c.close()
                        raise
                    except Exception:
                        c.close()
                        if attempt == 4:
                            raise
                        suspect(best, "download")
                        await asyncio.sleep(1)
                        best = max(await wait_links(job), key=weight)
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
                        raise ServerError(f"HTTP {st}" if st >= 400 else "range not honoured")
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
                        while len(done_set) < n and job["state"] == "running" and l["healthy"]:
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
                            except ServerError as e:  # the server said no (expired link, too many connections)
                                conn.close()
                                server_errs.append(str(e))
                                if i not in done_set:
                                    if inflight.get(i, [0, 1])[1] <= 1:
                                        inflight.pop(i, None)
                                        queue.appendleft(i)
                                    else:
                                        inflight[i][1] -= 1
                                await asyncio.sleep(2)
                                if len(server_errs) >= 12:
                                    break
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
                                if fails >= 3:
                                    suspect(l, "download")
                                if tries[i] > 30 or fails >= 4:
                                    break  # this link is not working for this file now: the others carry on
                                await asyncio.sleep(1)
                    finally:
                        conn.close()

                crew = {}  # task -> link
                server_errs = []

                async def manage():
                    """Keep WORKERS_PER_LINK workers on every working link, the whole time; wait out outages."""
                    outage, died = None, {}
                    seen_done, progress_at = job["done"], time.time()
                    while len(done_set) < n and job["state"] == "running":
                        if len(server_errs) >= 12:
                            job["error_reason"] = "サーバーが断りました（" + server_errs[-1] + "） / the server refused"
                            return
                        if job["done"] != seen_done:
                            seen_done, progress_at = job["done"], time.time()
                        elif dl_links() and time.time() - progress_at > 600:
                            job["error_reason"] = "10 分進みませんでした / no progress for 10 minutes"
                            return
                        for t in [t for t in crew if t.done()]:
                            died[id(crew.pop(t))] = time.time()
                        now_ls = dl_links()
                        if not now_ls:
                            outage = outage or time.time()
                            job["waiting"] = True
                            if time.time() - outage > DL_OUTAGE_MAX:
                                return
                        else:
                            outage = None
                            job["waiting"] = False
                            for l in now_ls:
                                if time.time() - died.get(id(l), 0) < 10:
                                    continue  # its workers just gave up on this file: give it a moment
                                have = sum(1 for x in crew.values() if x is l)
                                for _ in range(WORKERS_PER_LINK - have):
                                    crew[asyncio.ensure_future(worker(l))] = l
                        await asyncio.sleep(1)

                try:
                    await manage()
                finally:
                    for t in crew:
                        t.cancel()
                    await asyncio.gather(*crew, return_exceptions=True)
            if len(done_set) < n and job["state"] == "running":
                raise ConnectionError(job.get("error_reason") or "途中で失敗しました（回線が戻りませんでした） / download failed part-way")
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


class _GUID16(ctypes.Structure):
    _fields_ = [("d", ctypes.c_ubyte * 16)]


class _MIB_IF_ROW2(ctypes.Structure):  # netioapi.h; 1352 bytes on 64-bit Windows (checked)
    _fields_ = [("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong), ("InterfaceGuid", _GUID16),
                ("Alias", ctypes.c_wchar * 257), ("Description", ctypes.c_wchar * 257),
                ("PhysicalAddressLength", ctypes.c_ulong), ("PhysicalAddress", ctypes.c_ubyte * 32),
                ("PermanentPhysicalAddress", ctypes.c_ubyte * 32), ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong),
                ("TunnelType", ctypes.c_int), ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int),
                ("AccessType", ctypes.c_int), ("DirectionType", ctypes.c_int), ("Flags", ctypes.c_ubyte),
                ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
                ("NetworkGuid", _GUID16), ("ConnectionType", ctypes.c_int),
                ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
                ("InOctets", ctypes.c_uint64), ("InUcastPkts", ctypes.c_uint64), ("InNUcastPkts", ctypes.c_uint64),
                ("InDiscards", ctypes.c_uint64), ("InErrors", ctypes.c_uint64), ("InUnknownProtos", ctypes.c_uint64),
                ("InUcastOctets", ctypes.c_uint64), ("InMulticastOctets", ctypes.c_uint64),
                ("InBroadcastOctets", ctypes.c_uint64), ("OutOctets", ctypes.c_uint64), ("OutUcastPkts", ctypes.c_uint64),
                ("OutNUcastPkts", ctypes.c_uint64), ("OutDiscards", ctypes.c_uint64), ("OutErrors", ctypes.c_uint64),
                ("OutUcastOctets", ctypes.c_uint64), ("OutMulticastOctets", ctypes.c_uint64),
                ("OutBroadcastOctets", ctypes.c_uint64), ("OutQLen", ctypes.c_uint64)]


def read_octets(idx):
    """(received, sent, link speed in bit/s) of a network adapter as Windows counts them - ALL of the PC's traffic
    on it, not only what goes through link-bond. 32-bit counters (they wrap; see sample_history). None if unknown."""
    try:  # 64-bit counters first (they never wrap: a counter that goes down was reset)
        r2 = _MIB_IF_ROW2()
        r2.InterfaceIndex = idx
        if ctypes.sizeof(_MIB_IF_ROW2) == 1352 and ctypes.windll.iphlpapi.GetIfEntry2(ctypes.byref(r2)) == 0:
            return r2.InOctets, r2.OutOctets, r2.ReceiveLinkSpeed, True
    except Exception:
        pass
    try:
        row = _MIB_IFROW()
        row.dwIndex = idx
        if ctypes.windll.iphlpapi.GetIfEntry(ctypes.byref(row)) == 0:
            return row.dwInOctets, row.dwOutOctets, (0 if row.dwSpeed == 0xFFFFFFFF else row.dwSpeed)
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
            wide = len(oc) > 3 and oc[3]
            if prev is not None and wide and (oc[0] < prev[0] or oc[1] < prev[1]):
                prev = None  # 64-bit counters only go down when the adapter was reset: not traffic
            if prev is not None:  # 32-bit counters: a wrap-around is handled by the modulo
                m = (1 << 64) if wide else (1 << 32)
                d_ = round(((oc[0] - prev[0]) % m) * 8 / dt / 1e6, 2)
                u_ = round(((oc[1] - prev[1]) % m) * 8 / dt / 1e6, 2)
                # A reconnected adapter starts counting from 0 again; the modulo would read that as ~4 GB in one
                # sample (17 000 Mbps). Faster than the adapter itself (or than anything) = a reset, not traffic.
                speed = oc[2] / 1e6 if len(oc) > 2 and oc[2] else 0
                limit = min(CAP_SANE_MBPS, max(speed * 1.5, 1000)) if speed else CAP_SANE_MBPS
                if d_ <= limit and u_ <= limit:
                    down, up = d_, u_
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
            "suspect": l.get("suspect", False), "drops_today": stability_today(l)[0], "uptime_today": stability_today(l)[1],
            "checked_ago": round(now - l["ping_at"]) if l.get("ping_at") else None, "last_ok": l.get("ping_ok"),
            "best_at": (capacity.get(cap_key(l)) or {}).get("at") or None,
            "peak_down": max(hist_down.get(k) or [0]), "peak_up": max(hist_up.get(k) or [0]),
            "hist_down": list(hist_down.get(k, [])), "hist_up": list(hist_up.get(k, [])),
        })
    return {
        "version": __version__, "update": dict(update_info) or None, "news": news(),
        "last_open_age": round(time.time() - last_open_ok),
        "vpn_on": vpn_on, "vpn_present": vpn_present, "vpn_bypass": vpn_bypass,
        "bypass_exe": bypass_exe(), "upload_share": round(upload_share(), 2),
        "modes": {"eco": eco_on, "block": block_on, "power": power_on},
        "proxy_applied": proxy_applied(), "supervised": supervised(), "proxy_port": PROXY_PORT,
        "eco": {"on": eco_on, "rules": [{"host": r.pattern, "cap_mbps": r.mbps, "MB": round(r.bytes / 1e6, 1)} for r in eco_rules]},
        "blocked": {"on": block_on, "total": blocked_total, "top": blocked_hosts.most_common(8)},
        "links": out,
        "bond": bond_status(),
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
            "source": j.get("source", "manual"), "waiting": bool(j.get("waiting")),
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
        elif method == "GET" and path in ("/history", "/history.html"):
            try:
                with open(os.path.join(HERE, "history.html"), encoding="utf-8") as f:
                    out = http_resp("200 OK", "text/html; charset=utf-8", f.read())
            except OSError:
                out = http_resp("404 Not Found", "text/plain", "history.html is missing")
        elif method == "GET" and path.startswith("/api/history"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            day = (q.get("day") or [time.strftime("%Y-%m-%d")])[0]
            data = (await asyncio.to_thread(history_data, day)) or {"day": day, "days": history_days(), "devices": [],
                                                                       "events": [], "nolink": []}
            out = http_resp("200 OK", "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False))
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
        elif method == "POST" and path == "/api/update":  # the box's "update" button
            if hdr.get("x-linkbond") != "1":
                out = http_resp("403 Forbidden", "text/plain", "forbidden")
            else:
                ok = await asyncio.to_thread(install_update) if update_info.get("version") else False
                out = http_resp("200 OK", "application/json; charset=utf-8",
                                json.dumps({"installed": ok, "url": update_info.get("url"),
                                            "state": update_info.get("state"), "error": update_info.get("error")}))
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
update_info = {}  # {"version", "url", "zip", "auto", "state", "error"} when a newer release exists


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
    items = [x.strip().lstrip("-* ").split(" / ")[0].replace("**", "").rstrip(":： ") for x in (m.group(1).splitlines() if m else []) if x.strip()]
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


async def confirm_update():
    """The new version has run fine for 2 minutes: the supervisor must not roll it back any more."""
    await asyncio.sleep(120)
    confirm_pending(HERE)


def confirm_pending(here):
    """Remove the roll-back marker - only when it is about THIS version (an older process must not remove the
    marker of a version it just installed)."""
    p = os.path.join(here, "update-pending.json")
    try:
        with open(p, encoding="utf-8") as f:
            to = json.load(f).get("to")
        if to and vtuple(to) == vtuple(__version__):
            os.remove(p)
            log(f"update to {__version__} confirmed")
            return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


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
        for _ in range(180):  # a download over all links is not cut by the restart (waits up to 30 minutes)
            if not any(j.get("state") == "running" for j in downloads.values()):
                break
            await asyncio.sleep(10)
        log(f"new version on disk ({__version__} -> {file_version(SELF_FILE)}): restarting into it")
        save_usage()
        save_capacity()
        os._exit(0)


def check_release():
    """The newest published release of UPDATE_REPO: (version, page url, source zip url, passed the tests) or None.
    'Passed the tests' = the release build (which runs selftest.py first) attached its zip. HTTPS, no proxy."""
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": "link-bond", "Accept": "application/vnd.github+json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    tag = str(data.get("tag_name", "")).lstrip("vV")
    tested = any(str(a.get("name", "")).startswith("link-bond-windows-exe") for a in data.get("assets") or [])
    return (tag, data.get("html_url", ""), data.get("zipball_url", ""), tested) if tag else None


# What an update may write: the program files only. Settings, usage, capacity, *.local.txt, logs stay as they are.
UPDATE_TOP = {"bond.py", "widget.pyw", "dashboard.html", "history.html", "repair.ps1", "install-repair.ps1", "selftest.py", "run.ps1", "stop.ps1", "open.ps1",
              "proxy.ps1", "start-hidden.ps1", "install.ps1", "keep-connected.ps1", "install.bat", "uninstall.bat",
              "eco.txt", "blocklist.txt", "pinned.txt", "CHANGELOG.md", "README.md", "README.ja.md", "LICENSE"}
UPDATE_DIRS = ("browser-extension/", "docs/")
UPDATE_MAX_BYTES = 30_000_000
WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)


def update_blocker():
    """Why this copy cannot replace its own files ('' = it can)."""
    if SELF_FILE is None:
        return "exe"  # the packaged .exe cannot swap itself: the release page is opened instead
    if os.path.isdir(os.path.join(HERE, ".git")):
        return "git"  # a developer's working copy: never overwrite it with a download
    return ""


def update_members(names):
    """Map the files of a GitHub source zip ("owner-repo-sha/path") to the paths an update may write."""
    out = {}
    for n in names:
        if n.endswith("/") or "/" not in n:
            continue
        rel = n.split("/", 1)[1]
        parts = rel.split("/")
        if not rel or any(x in ("", ".", "..") or x[-1] in ". " or WIN_RESERVED.match(x) for x in parts) \
                or ":" in rel or "\\" in rel:
            continue
        if rel in UPDATE_TOP or (rel.startswith(UPDATE_DIRS) and len(parts) == 2):
            out[n] = rel
    return out


def apply_update(blob, version, here=None):
    """Put a downloaded release (GitHub source zip) in place. Checks first, writes after; bond.py goes last,
    because writing it makes the running link-bond restart into the new version (the box restarts by itself too).
    The replaced files are kept in update-backup/. Returns the written paths; raises ValueError when unusable."""
    here = here or HERE
    if len(blob) > UPDATE_MAX_BYTES:
        raise ValueError("download too big")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            files = {rel: z.read(n) for n, rel in update_members(z.namelist()).items()}
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a zip file: {e}")
    if "bond.py" not in files or "widget.pyw" not in files:
        raise ValueError("not a link-bond release")
    for name in ("bond.py", "widget.pyw", "selftest.py"):
        if name in files:
            try:
                compile(files[name].decode("utf-8"), name, "exec")
            except (SyntaxError, UnicodeDecodeError, ValueError) as e:
                raise ValueError(f"{name} is broken: {e}")
    m = re.search(rb'^__version__ = "([^"]+)"', files["bond.py"], re.M)
    if not m or vtuple(m.group(1).decode()) != vtuple(version):
        raise ValueError("the download is not the announced version")
    backup = os.path.join(here, "update-backup")

    def same(rel):
        try:
            with open(os.path.join(here, *rel.split("/")), "rb") as f:
                return f.read() == files[rel]
        except OSError:
            return False
    files = {rel: data for rel, data in files.items() if not same(rel)}  # unchanged files are left alone
    if os.path.isdir(backup):  # one generation: the version that is running now
        shutil.rmtree(backup, ignore_errors=True)
    order = sorted(files, key=lambda r: {"bond.py": 1, "proxy.ps1": 2, "run.ps1": 3}.get(r, 0))  # these last
    # The supervisor (run.ps1) puts update-backup/ back if the new version keeps dying right after start;
    # a new version that runs fine for a while removes this marker (confirm_update).
    with open(os.path.join(here, "update-pending.json"), "w", encoding="utf-8") as f:
        json.dump({"to": version, "files": order, "at": time.time()}, f)
    written, created = [], []
    try:
        for rel in order:
            dst = os.path.join(here, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                b = os.path.join(backup, *rel.split("/"))
                os.makedirs(os.path.dirname(b), exist_ok=True)
                with open(dst, "rb") as f_in, open(b, "wb") as f_out:
                    f_out.write(f_in.read())
            else:
                created.append(rel)
            tmp = dst + ".new"
            with open(tmp, "wb") as f:
                f.write(files[rel])
            for attempt in range(5):  # a virus scanner or the indexer may hold the file for a moment
                try:
                    os.replace(tmp, dst)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(1)
            written.append(rel)
    except Exception:
        # never leave a mix of old and new files: put back what was replaced, remove what was added
        for rel in written:
            dst = os.path.join(here, *rel.split("/"))
            try:
                if rel in created:
                    os.remove(dst)
                else:
                    shutil.copyfile(os.path.join(backup, *rel.split("/")), dst)
            except OSError:
                pass
        try:
            os.remove(os.path.join(here, "update-pending.json"))
        except OSError:
            pass
        raise
    return written


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "link-bond"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as r:
        return r.read(UPDATE_MAX_BYTES + 1)


_install_lock = threading.Lock()


def install_update():
    """Download and put in place the release in update_info. True when the files are (already) replaced.
    Only one at a time: the automatic install and the box's button cannot overlap or repeat."""
    if not _install_lock.acquire(blocking=False):
        update_info["error"] = None
        return False  # one is running right now (its state shows "installing")
    try:
        if update_info.get("state") == "installed":
            return True
        v, src = update_info.get("version"), update_info.get("zip")
        why = update_blocker()
        if not update_info.get("tested"):
            update_info["error"] = "the tests of this release have not passed yet - try again later"
            return False
        if why == "git" and v:
            ok, msg = git_pull()
            log("update via git pull:", "ok" if ok else msg)
            if ok:
                update_info["state"], update_info["error"] = "installed", None
            else:  # local changes (a developer copy): leave it to them - the page button, no retries
                update_info["state"], update_info["error"], update_info["auto"] = None, msg, False
            return ok
        if not v or not src or why:
            update_info["error"] = {"exe": "this is the .exe build: download the new zip from the page",
                                    "git": "developer copy (git): update with git pull"}.get(why, "nothing to install")
            return False
        update_info["state"], update_info["error"] = "installing", None
        try:
            written = apply_update(download(src), v)
        except Exception as e:
            update_info["state"], update_info["error"] = "failed", f"{type(e).__name__}: {e}"[:200]
            log("update failed:", update_info["error"])
            return False
        update_info["state"] = "installed"
    finally:
        _install_lock.release()
    log(f"update {__version__} -> {v}: {len(written)} files replaced (old ones in update-backup)")
    if not supervised():
        notify(f"link-bond {v} を入れました / installed",
               "デスクトップの「回線まとめ」を開き直すと新しい版になります。\nReopen link-bond to finish.")
    return True


def git_pull():
    """A git copy (like the developer's PC) is updated with `git pull --ff-only` - only when nothing is changed
    locally, so no work is ever overwritten. (True, '') or (False, why)."""
    def git(*a):
        return subprocess.run(["git", "-C", HERE] + list(a), capture_output=True, timeout=120, creationflags=0x08000000)
    try:
        st = git("status", "--porcelain", "--untracked-files=no")
        if st.returncode != 0:
            return False, "git is not usable here"
        if st.stdout.strip():
            return False, "local changes in the git copy: not pulling"
        r = git("pull", "--ff-only")
        return (r.returncode == 0), r.stderr.decode("utf-8", "replace").strip()[:150]
    except (OSError, subprocess.SubprocessError) as e:
        return False, type(e).__name__


def skipped_version():
    try:
        return open(state_path("update-skip.txt"), encoding="utf-8").read().strip()
    except OSError:
        return ""


RELEASE_CHECK_SEC = 6 * 3600
first_seen = {}  # version -> when this run first saw it published


def busy_now():
    """Connections carrying data in the last 20 s (an update restart would cut them)."""
    now = time.time()
    return sum(1 for r in live.values() if now - r.get("t_data", 0) < 20) >= 1


async def release_loop():
    """At start and every 6 hours: is there a newer published version? It is downloaded and put in place by
    itself (the running link-bond and the box then restart into it). Where that is not possible (the .exe,
    a developer's git copy) you are told once per version and the box offers the download page."""
    if not UPDATE_REPO:
        return
    await asyncio.sleep(20)  # let the links come up first
    while True:
        wait = RELEASE_CHECK_SEC
        try:
            rel = await asyncio.to_thread(check_release)
            have = (SELF_FILE and file_version(SELF_FILE)) or __version__  # already installed, not restarted yet
            if rel and vtuple(rel[0]) > vtuple(have):
                if update_info.get("version") != rel[0]:
                    update_info.clear()
                update_info.update(version=rel[0], url=rel[1], zip=rel[2], tested=rel[3],
                                   auto=update_blocker() in ("", "git"))
                if rel[0] == skipped_version():  # this version was rolled back: wait for the next one
                    update_info["auto"] = False
                if update_info["auto"] and not rel[3]:
                    log(f"new version {rel[0]} published, its tests have not passed yet: waiting")
                    wait = 1800
                elif update_info["auto"] and any(j.get("state") == "running" for j in downloads.values()):
                    wait = 600  # do not restart in the middle of a download: try again in 10 minutes
                elif update_info["auto"] and busy_now() and time.time() - first_seen.setdefault(rel[0], time.time()) < 6 * 3600:
                    wait = 600  # people are using the internet right now: install at a quiet moment (at most 6 h later)
                elif update_info["auto"]:
                    log(f"new version {rel[0]} published: installing it")
                    if not await asyncio.to_thread(install_update) and update_info.get("auto"):
                        wait = 1800
                else:
                    p = state_path("update-notified.txt")
                    try:
                        told = open(p, encoding="utf-8").read().strip()
                    except OSError:
                        told = ""
                    if told != rel[0]:
                        notify(f"link-bond の新しい版 {rel[0]} があります / update available",
                               f"いまは {__version__}。小さな箱の「更新する」でページが開きます。\n{rel[1]}")
                        with open(p, "w", encoding="utf-8") as f:
                            f.write(rel[0])
        except Exception as e:
            log("update check failed:", type(e).__name__, str(e)[:120])
            wait = 1800  # no internet yet, GitHub busy ...: try again in 30 minutes
        await asyncio.sleep(wait)


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
        asyncio.create_task(confirm_update())
        if sys.platform == "win32":
            asyncio.create_task(wifi_loop())
            asyncio.create_task(repair_loop())
        asyncio.create_task(release_loop())
        asyncio.create_task(cf_loop())
    global link_back
    link_back = asyncio.Event()
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
    load_stability()
    load_eco()
    load_blocklist()
    load_ports()
    announce_version()
    rb = os.path.join(HERE, "update-rolled-back.txt")
    if os.path.exists(rb):  # the supervisor put the previous version back (the new one kept dying)
        try:
            bad = open(rb, encoding="utf-8-sig").read().split()[-1]
            with open(state_path("update-skip.txt"), "w", encoding="utf-8") as f:
                f.write(bad)
        except (OSError, IndexError):
            pass
        notify("link-bond: 新しい版が動かなかったので元に戻しました / update rolled back",
               f"いまは {__version__} で動いています。次の版が出たら、また自動で入れ替えます。\n"
               f"The new version kept failing; running {__version__} again.")
        add_event(f"更新を元に戻しました（{__version__}） / update rolled back")
        try:
            os.remove(rb)
        except OSError:
            pass
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
