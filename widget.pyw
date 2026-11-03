"""link-bond widget - one small floating box to operate everything.

* small, movable (drag the title bar), semi-transparent (fades when the mouse is away), always on top, three sizes:
  "pill" (one line) / "normal" / "full" (downloads, balance, events, help)
* shows what matters: is every link alive (a tiny check every 15 s, with its response time), what it can do,
  how much data it used - and turns red only where something is wrong
* talks to the running link-bond on 127.0.0.1:1081; starts nothing by itself (open.ps1 does)
* only the Python standard library (tkinter)
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import messagebox, ttk

HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
BASE = os.environ.get("LINKBOND_URL", "http://127.0.0.1:1081")  # the env variable is for tests only
LOCK_PORT = 1082
PREFS = os.path.join(HERE, "widget.json")
BEAT = os.path.join(HERE, "widget.beat")  # touched every few seconds: the supervisor restarts a box that is gone
EXT_DIR = os.path.join(HERE, "browser-extension")

JA = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFF == 0x11 if sys.platform == "win32" else False
TX = {
    "allok": ("{} 回線 つながっています", "{} link(s) connected"), "off": ("OFF · いつもどおり", "OFF · normal"),
    "offsub": ("スイッチで ON（全回線をまとめて使う）", "switch ON to share all links"),
    "pcwide": ("PC全体", "whole PC"), "maxall": ("最大 ↓{} ↑{} Mbps（全回線）", "max ↓{} ↑{} Mbps (all links)"), "nolink": ("link-bond に接続できません", "cannot reach link-bond"),
    "none": ("回線が見つかりません", "No connection found"), "more": ("ほか", "+"),
    "eco": ("低画質モード", "Low-quality video"), "ecosub": ("動画を低画質にして通信量を減らす", "videos in lower quality, less data"),
    "block": ("広告ブロック", "Ad blocking"), "blocksub": ("広告の通信を止める", "refuse ad traffic"),
    "cut": ("切断", "cut"), "left": ("残り", "left"), "used": ("使用", "used"), "unm": ("未測定", "not measured"),
    "cap": ("実力", "can do"), "rtt": ("応答", "ping"), "loss": ("取りこぼし", "loss"), "ago": ("{}秒前", "{}s ago"),
    "checking": ("確認中…", "checking…"), "measuring": ("⚡ 測定中…", "⚡ measuring…"),
    "suspect": ("確認中（応答なし）", "checking (no answer)"), "drops": ("今日 {}回切断・{}%", "{} drops today · {}%"),
    "dlwait": ("⏸ 回線が戻るのを待っています", "⏸ waiting for a link to come back"),
    "measure": ("⚡ 速さを測る（1回線 約3MB）", "⚡ Measure speed (~3 MB per link)"), "measure_s": ("⚡ 測る", "⚡ Test"),
    "eco_s": ("低画質", "Low-res video"), "block_s": ("広告ブロック", "No ads"),
    "stalled": ("テザリング停止中 → スマホで OFF→ON", "tethering stopped → switch it off and on"),
    "vpnno": ("⚠ VPN 中のため束ねていません", "⚠ VPN on: not bonding"),
    "vpnboth": ("🔒 VPN（他のアプリ用）と束ねを両立中", "🔒 VPN (other apps) + bonding"),
    "vpnhow": ("両立させる方法", "How to use both"),
    "sup": ("⚠ 自動復旧が動いていません（デスクトップのアイコンから起動してください）", "⚠ safety net is not running (start from the desktop icon)"),
    "newver": ("🆕 新しい版 {} があります", "🆕 version {} is available"), "get": ("更新する", "Update"),
    "upnow": ("⏳ {} を入れています…", "⏳ installing {}…"),
    "updone": ("✓ {} を入れました。まもなく切り替わります", "✓ {} installed - switching in a moment"),
    "upfail": ("⚠ {} の自動更新に失敗（30 分後にまた試します）。「ページ」から手で入れられます", "⚠ update to {} failed (retrying in 30 min) - or get it from the page"),
    "page": ("ページ", "Page"),
    "updated": ("🆕 {} に更新しました", "🆕 updated to {}"), "changes": ("変更点", "What's new"),
    "graph": ("通信の様子（直近5分・全回線）", "Traffic (last 5 min, all links)"),
    "tab_graph": ("グラフ", "Graph"), "tab_dl": ("ダウンロード", "Download"), "tab_bal": ("残り容量", "Balance"),
    "tab_ev": ("出来事", "Events"),
    "dl": ("ダウンロード（全回線で同時に取る）", "Downloads (over all links at once)"),
    "dlauto": ("拡張機能を入れると、Chrome のダウンロードが自動で全回線に", "With the extension, Chrome downloads use all links"),
    "dlext": ("Chrome に入れる", "Add to Chrome"), "dlurl": ("ここに URL を貼る", "paste a URL"), "dlgo": ("取得", "Get"),
    "cancel": ("取消", "Cancel"), "show": ("開く", "Show"), "done": ("完了", "done"), "auto": ("自動", "auto"),
    "bal": ("残り容量（プリペイド）", "Balance (prepaid)"), "setbal": ("今の残りに設定", "Set balance"),
    "topup": ("チャージ +", "Top up +"),
    "events": ("出来事（新しい順）", "Events (newest first)"), "noevents": ("まだ何もありません", "nothing yet"),
    "open": ("詳しい画面", "Dashboard"), "hist": ("接続の記録", "Connection record"), "help": ("回線を増やす", "Add a link"), "quit": ("完全に終了", "Quit all"),
    "small": ("小さく", "Smaller"), "big": ("詳しく", "More"), "less": ("たたむ", "Less"),
    "alpha": ("透明度", "Opacity"), "top": ("常に手前に表示", "Always on top"), "fade": ("使わないときは薄くする", "Fade when idle"),
    "confirm": ("link-bond を完全に終了して、元の設定に戻します。よろしいですか？",
                "Quit link-bond completely and restore your original settings?"),
    "number": ("数字を入れてください", "enter a number"),
}
HELP = (
    "回線を増やす方法（3 本目以降も同じです）\n\n"
    "① スマホをもう 1 台、USB ケーブルで PC につなぐ\n"
    "   Android: 設定 → テザリング → 「USB テザリング」を ON\n"
    "   iPhone: 「インターネット共有」を ON（PC に Apple Devices / iTunes が必要）\n"
    "   ※ USB ポートが足りなければ、給電つき USB ハブを使います。\n\n"
    "② USB の Wi-Fi 子機（1,000〜2,000 円ほど）を挿して、別のスマホのホットスポットにつなぐ\n"
    "   PC 内蔵の Wi-Fi は同時に 1 つのホットスポットにしかつながりません。\n\n"
    "③ SIM を入れる USB モデム / ポケット Wi-Fi をつなぐ（USB 接続なら自動でネットワーク機器として出ます）\n\n"
    "④ 有線 LAN（家のインターネット）があれば、それも 1 本になります。\n\n"
    "つなぐと、約 10 秒でこの箱に新しい回線が出ます（設定は要りません）。\n"
    "速さが足し算になるのは、別の SIM・別の契約のときだけです。\n"
    "同じ種類のスマホ 2 台が同じアドレス帯を使って競合するときは、片方を Wi-Fi ホットスポットにしてください。"
) if JA else (
    "How to add more links (the 3rd, 4th ... work the same way)\n\n"
    "1) Plug another phone into the PC by USB cable and turn on USB tethering\n"
    "   (Android: Settings > Tethering; iPhone: Personal Hotspot, needs Apple Devices/iTunes on the PC).\n"
    "   Use a powered USB hub if you run out of ports.\n\n"
    "2) Plug in a USB Wi-Fi adapter and join another phone's hotspot (the built-in Wi-Fi joins only one at a time).\n\n"
    "3) A USB modem / pocket Wi-Fi with its own SIM shows up as a network adapter by itself.\n\n"
    "4) A wired LAN (home internet) counts as one more link.\n\n"
    "A new link appears here within ~10 seconds - no setup. Speeds add up only for different SIMs / contracts.\n"
    "If two phones use the same private address range and clash, make one of them a Wi-Fi hotspot."
)


def t(key):
    return TX[key][0 if JA else 1]


def vpn_help(path):
    """Steps to let a VPN and bonding work together (the VPN app excludes link-bond only)."""
    if JA:
        return ("NordVPN と一緒に使う方法（1回だけ）\n\n"
                "① NordVPN のアプリを開く\n"
                "② 設定（歯車）→「スプリットトンネリング」を ON\n"
                "③ 種類は「選択したアプリの VPN を無効にする」\n"
                "④「アプリを追加」→ 次のファイルを選ぶ:\n"
                f"     {path}\n"
                "    （場所はコピー済み。ファイルを選ぶ画面の「ファイル名」に貼り付けて開く）\n\n"
                "※ NordVPN は、外したアプリにも回線を 1 本しか使わせません。2 本以上を束ねるには PC 版の VPN を切ってください。")
    return ("Using a VPN together with link-bond (once)\n\n"
            "1) Open your VPN app (e.g. NordVPN)\n"
            "2) Settings > Split tunneling: ON\n"
            "3) Type: disable the VPN for selected apps\n"
            "4) Add app > choose this file:\n"
            f"     {path}\n"
            "   (the path is copied - paste it into the file name box)\n\n"
            "Note: NordVPN lets excluded apps use one link only. To bond two or more, turn the desktop VPN off.")


def ext_help(path):
    """How to add the browser extension that hands every download to link-bond."""
    if JA:
        return ("Chrome のダウンロードを自動で全回線にする（1回だけ）\n\n"
                "① Chrome のアドレス欄に  chrome://extensions  と入れて開く\n"
                "② 右上の「デベロッパー モード」を ON\n"
                "③「パッケージ化されていない拡張機能を読み込む」を押して、このフォルダを選ぶ:\n"
                f"     {path}\n"
                "    （場所はコピー済み。フォルダを選ぶ画面の上の欄に貼り付け）\n\n"
                "これで、Chrome で始めたダウンロードは link-bond が全回線で取ります。\n"
                "link-bond が止まっている時や取れないファイルは、いつも通り Chrome が取ります。\n"
                "使うプロファイルごとに 1 回ずつ入れてください（Edge も同じ手順: edge://extensions）。")
    return ("Make every Chrome download use all links (once)\n\n"
            "1) Open  chrome://extensions\n"
            "2) Turn on Developer mode (top right)\n"
            "3) Load unpacked > choose this folder:\n"
            f"     {path}\n"
            "   (the path is copied - paste it into the folder box)\n\n"
            "Downloads started in Chrome are then fetched by link-bond over all links. When link-bond is not\n"
            "running or cannot fetch a file, Chrome downloads it as usual. Once per profile (Edge: edge://extensions).")


# ---------- pure helpers (tested in selftest.py) ----------
def fmt_mb(mb):
    if mb is None:
        return ""
    return f"{mb / 1000:,.1f} GB" if mb >= 1000 else f"{mb:,.0f} MB"


def problem_list(s):
    """What is wrong right now (link-bond's own list; older versions: derived from the links)."""
    if not s:
        return []
    if "problems" in s:
        return list(s["problems"])
    return [f"{l['label']}: {t('cut')}" for l in s["links"] if not l["healthy"]] + \
           [f"{x['label']}: {t('stalled')}" for x in s.get("stalled") or []]


def short(p):
    return p.split(" / ")[0]


def pings(s):
    return [l["rtt_ms"] for l in s["links"] if l.get("healthy") and l.get("rtt_ms") is not None]


def max_total(s):
    """(down, up) Mbps: what all working links can do together - the sum of their capacities."""
    ls = [l for l in s["links"] if l.get("healthy") and l.get("measured")]
    return sum(l["down_mbps"] for l in ls), sum(l["up_mbps"] for l in ls)


def max_text(s):
    d, u = max_total(s)
    return t("maxall").format(f"{d:.0f}", f"{u:.0f}") if d > 0 else ""


def hero(s):
    """(title, subtitle, color key 'ok' | 'bad' | 'muted') for the top of the box."""
    if not s:
        return "⚠ " + t("nolink"), "", "bad"
    if not s["modes"]["power"]:
        return t("off"), t("offsub"), "muted"
    probs = problem_list(s)
    if probs:
        rest = f"  ({t('more')} {len(probs) - 1})" if len(probs) > 1 else ""
        return "⚠ " + short(probs[0]) + rest, "   ".join(short(p) for p in probs[1:3]), "bad"
    if not s["links"]:
        return "⚠ " + t("none"), "", "bad"
    sub = "   ".join(f"{l['label']} {l['rtt_ms']}ms" for l in s["links"] if l.get("rtt_ms") is not None)
    return "✓ " + t("allok").format(len(s["links"])), sub, "ok"


def pill_text(s):
    """One line for the collapsed pill. With a problem it names it (and the pill turns red)."""
    if not s:
        return "⚠ link-bond  —"
    if not s["modes"]["power"]:
        return "○ OFF"
    probs = problem_list(s)
    if probs:
        return "⚠ " + short(probs[0]) + (f"  {t('more')}{len(probs) - 1}" if len(probs) > 1 else "")
    p = pings(s)
    return f"● {len(s['links'])}{'回線' if JA else ' links'} OK" + (f" · {min(p)}ms" if p else "")


def link_status(l):
    """(text, color key 'ok' | 'slow' | 'bad' | 'muted') - is this link alive right now, and since when we know."""
    if not l["healthy"]:
        return "✖ " + t("cut"), "bad"
    if l.get("measuring"):
        return t("measuring"), "muted"
    if l.get("suspect"):
        return "? " + t("suspect"), "slow"
    if l.get("rtt_ms") is None:
        return t("checking"), "muted"
    q = l.get("quality", "good")
    txt = f"✓ {l['rtt_ms']}ms"
    if l.get("loss_pct"):
        txt += f" · {t('loss')} {l['loss_pct']}%"
    if l.get("checked_ago") is not None:
        txt += " · " + t("ago").format(l["checked_ago"])
    return txt, {"bad": "bad", "slow": "slow"}.get(q, "ok")


def link_detail(l):
    """What the link can do and the data: '実力 ↓39 ↑9 Mbps · 使用 5.4 GB'."""
    cap = f"{t('cap')} ↓{l['down_mbps']:.0f} ↑{l['up_mbps']:.0f}" if l.get("measured") else f"{t('cap')} {t('unm')}"
    data = f"{t('left')} {fmt_mb(l['remaining_MB'])}" if l.get("remaining_MB") is not None else f"{t('used')} {fmt_mb(l['used_MB'])}"
    out = f"{cap}  ·  {data}"
    if l.get("drops_today"):  # how unstable it was today: drops and % of the day it worked
        out += "  ·  " + t("drops").format(l["drops_today"], l.get("uptime_today", 100))
    return out


def event_lines(events, n=4):
    """The newest few events as text lines: 'HH:MM:SS  what happened'."""
    return "\n".join(f"{e['t']}  {e['text'].split(' — ')[0][:70]}" for e in events[:n])  # the title only, the cause is in the notice


# ---------- talking to link-bond ----------
def api(path, body=None, timeout=3):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-LinkBond": "1", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def open_history():
    open_dashboard("/history")


def open_dashboard(page="/"):
    prof = os.path.join(HERE, "ui-profile")
    for b in (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(b):
            subprocess.Popen([b, f"--app={BASE}{page}", f"--user-data-dir={prof}", "--no-first-run", "--window-size=1000,900"])
            return
    os.startfile(BASE + page)


# ---------- look ----------
BG, CARD, LINE = "#0b1222", "#141d33", "#24314f"
FG, MUTED, ACC = "#e6ecf7", "#8796b4", "#22d3ee"
OK, BAD, SLOW, ALARM = "#34d399", "#f87171", "#fbbf24", "#dc2626"
BTN, BTNH, PRI, PRIH, OFFC = "#1e2947", "#2a3963", "#0e7490", "#0891b2", "#3b4566"
FONT = "Yu Gothic UI"
TONE = {"ok": OK, "bad": BAD, "slow": SLOW, "muted": MUTED}


class Switch(tk.Canvas):
    """A round on/off switch."""

    def __init__(self, parent, command, scale=1.0):
        self.W, self.H = int(40 * scale), int(22 * scale)
        super().__init__(parent, width=self.W, height=self.H, bg=parent["bg"], highlightthickness=0, cursor="hand2")
        self.state, self.color = False, OK
        self.bind("<Button-1>", lambda e: command())
        self.draw()

    def set(self, on, color=OK):
        if (on, color) != (self.state, self.color):
            self.state, self.color = on, color
            self.draw()

    def draw(self):
        self.delete("all")
        w, h, r = self.W, self.H, self.H / 2
        col = self.color if self.state else OFFC
        self.create_oval(0, 0, h, h, fill=col, outline=col)
        self.create_oval(w - h, 0, w, h, fill=col, outline=col)
        self.create_rectangle(r, 0, w - r, h, fill=col, outline=col)
        x = w - h + 3 if self.state else 3
        self.create_oval(x, 3, x + h - 6, h - 3, fill="#ffffff", outline="#ffffff")


class Widget:
    def __init__(self, lock=None):
        self.lock = lock
        self.prefs = {"x": None, "y": None, "mode": "normal", "alpha": 0.94, "idle_alpha": 0.6, "fade": True, "top": True}
        try:
            with open(PREFS, encoding="utf-8-sig") as f:  # -sig: also reads a file saved with a BOM
                self.prefs.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.state, self.reachable, self.cards, self.jobs_sig = None, False, {}, None
        self.inside, self.leave_job = False, None

        r = self.root = tk.Tk()
        self.S = max(1.0, float(r.tk.call("tk", "scaling")) / 1.333)  # canvas sizes follow the screen's scaling
        r.title("link-bond")
        r.configure(bg=BG)
        r.resizable(False, False)
        try:
            r.attributes("-toolwindow", True)  # small native title bar: drag to move, proper keyboard / IME input
        except tk.TclError:
            pass
        r.attributes("-topmost", bool(self.prefs["top"]))
        r.attributes("-alpha", self.prefs["alpha"])
        r.protocol("WM_DELETE_WINDOW", self.close)
        r.bind("<Enter>", lambda e: self.hover(True))
        r.bind("<Leave>", lambda e: self.hover(False))
        r.bind("<Button-3>", self.menu)
        r.option_add("*Font", (FONT, 9))
        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("LB.TCombobox", fieldbackground=CARD, background=BTN, foreground=FG, arrowcolor=FG, bordercolor=LINE)
        style.map("LB.TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)])
        style.configure("LB.Horizontal.TProgressbar", troughcolor=CARD, background=ACC, bordercolor=CARD, lightcolor=ACC, darkcolor=ACC)

        # --- pill (one line) ---
        self.pill = tk.Label(r, text="", bg=CARD, fg=FG, font=(FONT, 10, "bold"), padx=12, pady=7, cursor="hand2")
        self.pill.bind("<Button-1>", lambda e: self.set_mode("normal"))

        # --- normal ---
        self.body = tk.Frame(r, bg=BG, padx=12, pady=10)
        tk.Frame(self.body, bg=BG, width=int(316 * self.S), height=0).pack()  # keeps the box a calm, fixed width
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill="x")
        tk.Label(bar, text="link-bond", bg=BG, fg=ACC, font=(FONT, 12, "bold")).pack(side="left")
        self.ver = tk.Label(bar, text="", bg=BG, fg=MUTED, font=(FONT, 8))
        self.ver.pack(side="left", padx=(5, 0), pady=(4, 0))
        self.btn_big = self.button(bar, "▾ " + t("big"), self.toggle_full, small=True)
        self.btn_big.pack(side="right")
        self.button(bar, "▴ " + t("small"), lambda: self.set_mode("pill"), small=True).pack(side="right", padx=(0, 4))
        self.measure_btn = self.button(bar, t("measure_s"), self.measure, primary=True, small=True)
        self.measure_btn.pack(side="right", padx=(0, 4))

        # the state at a glance
        self.herocard = tk.Frame(self.body, bg=CARD, padx=10, pady=6, highlightthickness=1, highlightbackground=LINE)
        self.herocard.pack(fill="x", pady=(6, 0))
        top = tk.Frame(self.herocard, bg=CARD)
        top.pack(fill="x")
        left = tk.Frame(top, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        self.herot = tk.Label(left, text="", bg=CARD, fg=OK, font=(FONT, 11, "bold"), anchor="w", justify="left",
                              wraplength=int(230 * self.S))
        self.herot.pack(fill="x")
        self.heromax = tk.Label(left, text="", bg=CARD, fg=ACC, font=(FONT, 12, "bold"), anchor="w")
        self.heromax.pack(fill="x")
        sw = tk.Frame(top, bg=CARD)
        sw.pack(side="right")
        self.power = Switch(sw, self.toggle_power, self.S)
        self.power.pack()
        tk.Label(sw, text=t("pcwide"), bg=CARD, fg=MUTED, font=(FONT, 7)).pack()
        self.heros = tk.Label(self.herocard, text="", bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w", justify="left",
                              wraplength=int(290 * self.S))

        # all devices in one card, two short lines each
        self.devs = tk.Frame(self.body, bg=CARD, padx=10, pady=4, highlightthickness=1, highlightbackground=LINE)
        self.devs.pack(fill="x", pady=(6, 0))

        # notices (each in its own frame: empty = no height)
        self.vpnrow = tk.Frame(self.body, bg=BG)
        self.vpnrow.pack(fill="x")
        self.vpnlab = tk.Label(self.vpnrow, text="", bg=BG, fg=BAD, font=(FONT, 9, "bold"))
        self.vpnbtn = self.button(self.vpnrow, t("vpnhow"), self.show_vpn_help, small=True)
        self.warnrow = tk.Frame(self.body, bg=BG)
        self.warnrow.pack(fill="x")
        self.warn = tk.Label(self.warnrow, text=t("sup"), bg=BG, fg=BAD, font=(FONT, 8), wraplength=int(300 * self.S), justify="left")
        self.uprow = tk.Frame(self.body, bg=BG)
        self.uprow.pack(fill="x")
        self.uplab = tk.Label(self.uprow, text="", bg=BG, fg=ACC, font=(FONT, 9, "bold"), wraplength=int(220 * self.S), justify="left")
        self.upbtn = self.button(self.uprow, t("get"), self.open_update, small=True)
        self.newsrow = tk.Frame(self.body, bg=BG)
        self.newsrow.pack(fill="x")
        self.newslab = tk.Label(self.newsrow, text="", bg=BG, fg=ACC, font=(FONT, 9, "bold"))
        self.newsbtn = self.button(self.newsrow, t("changes"), self.show_news, small=True)

        # modes: two switches on one line
        modes = tk.Frame(self.body, bg=CARD, padx=10, pady=5, highlightthickness=1, highlightbackground=LINE)
        modes.pack(fill="x", pady=(6, 0))
        self.eco = self.mode_switch(modes, "eco")
        tk.Frame(modes, bg=LINE, width=1).pack(side="left", fill="y", padx=10)
        self.blk = self.mode_switch(modes, "block")

        # --- full: one tab at a time, so the box never gets taller than the screen ---
        self.full = tk.Frame(self.body, bg=BG)
        tabs = tk.Frame(self.full, bg=BG)
        tabs.pack(fill="x", pady=(8, 0))
        self.panels, self.tabbtns = {}, {}
        for key in ("graph", "dl", "bal", "ev"):
            b = tk.Button(tabs, text=t("tab_" + key), bd=0, relief="flat", cursor="hand2", font=(FONT, 8, "bold"),
                          padx=6, pady=4, command=lambda k=key: self.set_tab(k))
            b.pack(side="left", fill="x", expand=True, padx=(0, 2))
            self.tabbtns[key] = b
            self.panels[key] = tk.Frame(self.full, bg=CARD, padx=10, pady=8, highlightthickness=1, highlightbackground=LINE)

        self.graph = tk.Canvas(self.panels["graph"], height=int(80 * self.S), bg=CARD, highlightthickness=0)
        self.graph.pack(fill="x")

        dl = self.panels["dl"]
        auto = tk.Frame(dl, bg=CARD)
        auto.pack(fill="x")
        tk.Label(auto, text=t("dlauto"), bg=CARD, fg=MUTED, font=(FONT, 8), wraplength=int(185 * self.S), justify="left",
                 anchor="w").pack(side="left", fill="x", expand=True)
        self.button(auto, t("dlext"), self.show_ext_help, small=True).pack(side="right")
        row = tk.Frame(dl, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        self.url = tk.Entry(row, bg=BG, fg=MUTED, insertbackground=FG, relief="flat", highlightthickness=1,
                            highlightbackground=LINE, highlightcolor=ACC)
        self.url.insert(0, t("dlurl"))
        self.url.bind("<FocusIn>", self.url_focus)
        self.url.bind("<Return>", lambda e: self.download())
        self.url.pack(side="left", ipady=4, fill="x", expand=True)
        self.button(row, t("dlgo"), self.download, primary=True, small=True).pack(side="left", padx=(6, 0))
        self.jobs = tk.Frame(dl, bg=CARD)
        self.jobs.pack(fill="x")
        self.msg = tk.Label(dl, text="", bg=CARD, fg=BAD, font=(FONT, 8), wraplength=int(290 * self.S), justify="left")
        self.msg.pack(anchor="w")

        bal = self.panels["bal"]
        row = tk.Frame(bal, bg=CARD)
        row.pack(fill="x")
        self.dev_pick = ttk.Combobox(row, state="readonly", width=15, style="LB.TCombobox")
        self.dev_pick.pack(side="left")
        self.amount = tk.Entry(row, width=7, bg=BG, fg=FG, insertbackground=FG, relief="flat", highlightthickness=1,
                               highlightbackground=LINE, highlightcolor=ACC)
        self.amount.pack(side="left", padx=6, ipady=4)
        self.unit = ttk.Combobox(row, state="readonly", width=3, values=["GB", "MB"], style="LB.TCombobox")
        self.unit.set("GB")
        self.unit.pack(side="left")
        row2 = tk.Frame(bal, bg=CARD)
        row2.pack(fill="x", pady=(6, 0))
        self.button(row2, t("setbal"), lambda: self.budget("remaining_MB"), small=True).pack(side="left")
        self.button(row2, t("topup"), lambda: self.budget("add_MB"), small=True).pack(side="left", padx=6)

        self.ev = tk.Label(self.panels["ev"], text="", bg=CARD, fg=MUTED, font=(FONT, 8), wraplength=int(290 * self.S),
                           justify="left", anchor="w")
        self.ev.pack(fill="x")

        self.foot = foot = tk.Frame(self.full, bg=BG)
        foot.pack(fill="x", pady=(8, 0), side="bottom")
        self.button(foot, t("open"), open_dashboard, small=True).pack(side="left")
        self.button(foot, t("hist"), open_history, small=True).pack(side="left", padx=(6, 0))
        self.button(foot, t("help"), lambda: messagebox.showinfo("link-bond", HELP, parent=self.root), small=True).pack(side="left", padx=6)
        self.button(foot, t("quit"), self.quit_all, small=True, fg=BAD).pack(side="right")

        self.set_tab(self.prefs.get("tab", "dl"), save=False)
        self.apply_mode(first=True)
        threading.Thread(target=self.poll, daemon=True).start()
        threading.Thread(target=self.lock_server, daemon=True).start()
        r.after(1000, self.tick)

    # ----- building blocks -----
    def button(self, parent, text, cmd, primary=False, small=False, fg=None):
        bg, hv = (PRI, PRIH) if primary else (BTN, BTNH)
        b = tk.Button(parent, text=text, command=cmd, bd=0, relief="flat", bg=bg, fg=fg or FG, activebackground=hv,
                      activeforeground=FG, cursor="hand2", font=(FONT, 8 if small else 9, "bold" if primary else "normal"),
                      padx=10 if small else 12, pady=3 if small else 6)
        b.bind("<Enter>", lambda e: b.config(bg=hv))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def hdr(self, parent, text, bg=BG):
        lab = tk.Label(parent, text=text, bg=bg, fg=ACC, font=(FONT, 9, "bold"))
        lab.pack(anchor="w", pady=(10, 4))
        return lab

    def card(self, parent, title):
        self.hdr(parent, title)
        c = tk.Frame(parent, bg=CARD, padx=10, pady=8, highlightthickness=1, highlightbackground=LINE)
        c.pack(fill="x")
        return c

    def mode_switch(self, parent, key):
        """A short name and its switch, side by side (the longer explanation is the hover text of the name)."""
        box = tk.Frame(parent, bg=CARD)
        box.pack(side="left", fill="x", expand=True)
        name = tk.Label(box, text=t(key + "_s"), bg=CARD, fg=FG, font=(FONT, 9, "bold"), anchor="w")
        name.pack(side="left")
        s = Switch(box, lambda: self.toggle_mode(key), self.S)
        s.pack(side="right")
        tip = tk.Label(self.root, text=t(key) + ": " + t(key + "sub"), bg=BTN, fg=FG, font=(FONT, 8), padx=6, pady=3)
        name.bind("<Enter>", lambda e: tip.place(in_=box, x=0, y=-int(26 * self.S)))
        name.bind("<Leave>", lambda e: tip.place_forget())
        return s

    def measure(self):
        """The full speed test (about 3 MB per link) - only when asked."""
        if messagebox.askyesno("link-bond", t("measure") + "\n\n" + ("全回線の速さを測ります。よろしいですか？" if JA else "Measure all links now?"),
                               parent=self.root):
            self.post("/api/measure", {})

    @staticmethod
    def show(widget, visible, **pack):
        """Show or hide a widget without moving the others (it lives in its own small frame). Uses the packing
        state, not visibility, so it also works while the whole box is folded into the pill."""
        packed = bool(widget.winfo_manager())
        if visible and not packed:
            widget.pack(**pack)
        elif not visible and packed:
            widget.pack_forget()

    # ----- window state -----
    def set_tab(self, key, save=True):
        key = key if key in self.panels else "dl"
        self.prefs["tab"] = key
        for k, p in self.panels.items():
            p.pack_forget()
            on = k == key
            self.tabbtns[k].config(bg=PRI if on else BTN, fg=FG if on else MUTED, activebackground=PRIH if on else BTNH)
        self.panels[key].pack(fill="x", pady=(4, 0), before=self.foot)
        if save:
            self.fit()
            self.save()

    def set_mode(self, mode):
        self.prefs["mode"] = mode
        self.apply_mode()
        self.save()

    def toggle_full(self):
        self.set_mode("normal" if self.prefs["mode"] == "full" else "full")

    def apply_mode(self, first=False):
        m = self.prefs["mode"]
        self.pill.pack_forget()
        self.body.pack_forget()
        self.full.pack_forget()
        if m == "pill":
            self.pill.pack()
        else:
            self.body.pack()
            if m == "full":
                self.full.pack(fill="x")
            self.btn_big.config(text=("▴ " + t("less")) if m == "full" else ("▾ " + t("big")))
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x, y = self.prefs["x"], self.prefs["y"]
        if x is None or first and not self.on_screen(x, y):
            x, y = self.root.winfo_screenwidth() - w - 24, 60
        elif not first:
            x, y = self.root.winfo_x(), self.root.winfo_y()
        x, y = self.clamp(x, y, w, h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.render()

    def clamp(self, x, y, w, h):
        """Keep the whole box (with its title bar and border) inside the work area of the monitor it is on:
        a pill at the right edge that grows must not run off the screen or under the taskbar."""
        r = self.root
        fw = max(0, r.winfo_rootx() - r.winfo_x()) * 2          # left + right border
        fh = max(0, r.winfo_rooty() - r.winfo_y()) + fw // 2    # title bar + top border + bottom border
        left, top, right, bottom = work_area(x, y) or (0, 0, r.winfo_screenwidth(), r.winfo_screenheight() - 48)
        x = max(left, min(x, right - (w + fw)))
        y = max(top, min(y, bottom - (h + fh)))
        return x, y

    def on_screen(self, x, y):
        return x is not None and -50 < x < self.root.winfo_screenwidth() - 50 and -10 < y < self.root.winfo_screenheight() - 50

    def save(self):
        try:
            self.prefs["x"], self.prefs["y"] = self.root.winfo_x(), self.root.winfo_y()
            with open(PREFS, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f)
        except (OSError, tk.TclError):
            pass

    def hover(self, inside):
        self.inside = inside
        if self.leave_job:
            self.root.after_cancel(self.leave_job)
            self.leave_job = None
        if inside or not self.prefs["fade"]:
            self.root.attributes("-alpha", max(self.prefs["alpha"], 0.9) if inside else self.prefs["alpha"])
        else:
            self.leave_job = self.root.after(1200, lambda: self.root.attributes("-alpha", self.prefs["idle_alpha"]))

    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        am = tk.Menu(m, tearoff=0)
        for pct in (30, 50, 70, 90):
            am.add_command(label=f"{pct}%", command=lambda p=pct: self.set_alpha(p / 100))
        m.add_cascade(label=t("alpha"), menu=am)
        top = tk.BooleanVar(value=self.prefs["top"])
        fade = tk.BooleanVar(value=self.prefs["fade"])
        m.add_checkbutton(label=t("top"), variable=top, command=lambda: self.pref("top", top.get(), lambda: self.root.attributes("-topmost", top.get())))
        m.add_checkbutton(label=t("fade"), variable=fade, command=lambda: self.pref("fade", fade.get()))
        m.add_separator()
        m.add_command(label=t("open"), command=open_dashboard)
        m.add_command(label=t("hist"), command=open_history)
        m.add_command(label=t("quit"), command=self.quit_all)
        m.tk_popup(e.x_root, e.y_root)

    def pref(self, k, v, after=None):
        self.prefs[k] = v
        if after:
            after()
        self.save()

    def set_alpha(self, a):
        self.prefs["alpha"] = a
        self.prefs["idle_alpha"] = max(0.25, a - 0.35)
        self.root.attributes("-alpha", a)
        self.save()

    def close(self):
        """✕ only hides the box; link-bond keeps working. The desktop icon (回線まとめ) shows it again."""
        self.prefs["hidden"] = True
        self.save()
        self.root.withdraw()

    # ----- talking -----
    def poll(self):
        while True:
            try:
                s = api("/status.json", timeout=2)
                self.state, self.reachable = s, True
            except Exception:
                self.reachable = False
            time.sleep(2)

    def post(self, path, body, then=None):
        def run():
            try:
                s = api(path, body)
                self.state, self.reachable = s, True
                err = ""
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", "replace")[:120]
            except Exception as e:
                err = str(e)[:120]
            self.root.after(0, lambda: (self.render(), self.msg.config(text=err), then and then()))
        threading.Thread(target=run, daemon=True).start()

    def toggle_power(self):
        if self.state:
            self.post("/api/mode", {"power": not self.state["modes"]["power"]})

    def toggle_mode(self, key):
        if self.state:
            self.post("/api/mode", {key: not self.state["modes"][key]})

    def budget(self, key):
        try:
            v = float(self.amount.get().replace(",", ""))
        except ValueError:
            self.msg.config(text=t("number"))
            return
        lab = self.dev_pick.get()
        if lab and v >= 0:
            self.post("/api/budget", {"label": lab, key: v * 1000 if self.unit.get() == "GB" else v}, lambda: self.amount.delete(0, "end"))

    def url_focus(self, e):
        if self.url.cget("fg") == MUTED:
            self.url.delete(0, "end")
            self.url.config(fg=FG)

    def download(self):
        u = self.url.get().strip()
        if u and self.url.cget("fg") != MUTED:
            self.post("/api/download", {"url": u}, lambda: self.url.delete(0, "end"))

    def copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def show_news(self):
        n = (self.state or {}).get("news") or {}
        messagebox.showinfo("link-bond", f"{n.get('from', '')} → {n.get('to', '')}\n\n{n.get('changes', '')}", parent=self.root)
        self.post("/api/mode", {"news_seen": True})

    def open_update(self):
        """Install the new version right here when link-bond can (it then restarts, and so does this box);
        otherwise (the .exe, a failed download) open the release page."""
        up = (self.state or {}).get("update") or {}
        if up.get("auto") and up.get("state") != "failed" and up.get("tested"):
            self.uplab.config(text=t("upnow").format(up.get("version", "")))

            def run():
                try:
                    r = api("/api/update", {}, timeout=300)
                except Exception as e:
                    r = {"installed": False, "url": up.get("url"), "error": str(e)[:120]}
                if not r.get("installed") and r.get("url") and r.get("state") == "failed":
                    os.startfile(r["url"])  # it could not be installed here: the page, to get it by hand
                if r.get("error"):  # shown on the update row itself (the message line sits in another tab)
                    self.root.after(0, lambda: self.uplab.config(text="⚠ " + str(r["error"])[:90]))
            threading.Thread(target=run, daemon=True).start()
        elif up.get("url"):
            os.startfile(up["url"])

    def show_vpn_help(self):
        path = (self.state or {}).get("bypass_exe") or "linkbond.exe"
        self.copy(path)
        messagebox.showinfo("link-bond", vpn_help(path), parent=self.root)

    def show_ext_help(self):
        self.copy(EXT_DIR)
        messagebox.showinfo("link-bond", ext_help(EXT_DIR), parent=self.root)

    def quit_all(self):
        if messagebox.askyesno("link-bond", t("confirm"), parent=self.root):
            try:
                api("/api/quit", {})  # stop.ps1 ends link-bond, its supervisor and this box together
            except Exception:  # link-bond is not answering: run stop.ps1 ourselves
                stop = os.path.join(HERE, "stop.ps1")
                if os.path.exists(stop):
                    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", stop],
                                     creationflags=0x08000000)
            self.root.withdraw()
            self.root.after(15000, self.root.destroy)  # in case nothing ended this box

    # ----- drawing -----
    def tick(self):
        self.render()
        self.check_new_version()
        now = time.time()
        if self.lock is not None and now - getattr(self, "_beat", 0) >= 5:
            self._beat = now
            try:
                with open(BEAT, "w") as f:
                    f.write(str(int(now)))
            except OSError:
                pass
        self.root.after(1000, self.tick)

    def check_new_version(self):
        """widget.pyw replaced on disk (a new version): start the new one and close this one (every 10 s)."""
        now = time.time()
        if getattr(sys, "frozen", False) or now - getattr(self, "_vcheck", 0) < 10:
            return
        self._vcheck = now
        me = os.path.abspath(__file__)
        try:
            m = os.path.getmtime(me)
        except OSError:
            return
        if not hasattr(self, "_mtime"):
            self._mtime = m
            return
        if m == self._mtime or now - m < 3:  # unchanged, or still being written
            return
        self._mtime = m
        try:
            with open(me, encoding="utf-8") as f:
                compile(f.read(), me, "exec")
        except (OSError, SyntaxError):
            return  # a broken file: keep running this version
        self.save()
        subprocess.Popen([sys.executable, me, "--replace"], cwd=HERE)
        self.root.destroy()

    def render(self):
        s = self.state if self.reachable else None
        alarm = not s or bool(problem_list(s))  # the small pill turns red on ANY problem
        self.pill.config(text=pill_text(s) if s else "⚠ link-bond  " + t("nolink"),
                         bg=ALARM if alarm else CARD, fg="#ffffff" if alarm else FG)
        title, sub, tone = hero(s)
        self.herot.config(text=title, fg=TONE[tone])
        self.heros.config(text=sub, fg=BAD if tone == "bad" else MUTED)
        self.show(self.heros, tone != "ok" and bool(sub), fill="x", pady=(2, 0))  # all fine: the links say the rest
        self.herocard.config(highlightbackground=BAD if tone == "bad" else LINE)
        mx = max_text(s) if s and s["modes"]["power"] else ""
        self.heromax.config(text=mx)
        self.show(self.heromax, bool(mx), fill="x")
        if not s:
            return
        self.ver.config(text=s.get("version", ""))
        self.power.set(s["modes"]["power"])
        self.eco.set(s["modes"]["eco"])
        self.blk.set(s["modes"]["block"])
        self.draw_cards(s)
        vp, both = bool(s.get("vpn_present")), bool(s.get("vpn_bypass"))
        self.vpnlab.config(text=t("vpnboth") if both else t("vpnno"), fg=OK if both else BAD)
        self.show(self.vpnlab, vp, side="left", pady=(8, 0))
        self.show(self.vpnbtn, vp and not both, side="right", pady=(8, 0))
        self.show(self.warn, not s["supervised"], anchor="w", pady=(8, 0))
        up = s.get("update") or {}
        ust = up.get("state")
        key = {"installing": "upnow", "installed": "updone", "failed": "upfail"}.get(ust, "newver")
        self.uplab.config(text=t(key).format(up.get("version", "")))
        self.upbtn.config(text=t("get") if up.get("auto") and ust != "failed" and up.get("tested") else t("page"))
        self.show(self.uplab, bool(up.get("version")), side="left", pady=(8, 0))
        self.show(self.upbtn, bool(up.get("version")) and ust not in ("installing", "installed"), side="right", pady=(8, 0))
        n = s.get("news") or {}
        self.newslab.config(text=t("updated").format(n.get("to", "")))
        self.show(self.newslab, bool(n.get("to")), side="left", pady=(8, 0))
        self.show(self.newsbtn, bool(n.get("to")), side="right", pady=(8, 0))
        self.ev.config(text=event_lines(s.get("events") or []) or t("noevents"))
        tot = s.get("total") or {}
        self.draw_graph(tot.get("hist_down") or [], tot.get("hist_up") or [])
        self.draw_jobs(s["downloads"][:3])
        self.root.update_idletasks()  # rows appear and disappear: keep the box exactly as tall as what it shows
        if abs(self.root.winfo_reqheight() - self.root.winfo_height()) > 2 or abs(self.root.winfo_reqwidth() - self.root.winfo_width()) > 2:
            self.fit()

    def draw_cards(self, s):
        keys = [l["label"] for l in s["links"]] + ["!" + x["label"] for x in s.get("stalled") or []]
        if keys != list(self.cards):
            for w in self.devs.winfo_children():
                w.destroy()
            self.cards = {}
            for i, k in enumerate(keys):
                if i:
                    tk.Frame(self.devs, bg=LINE, height=1).pack(fill="x", pady=3)
                c = tk.Frame(self.devs, bg=CARD)
                c.pack(fill="x")
                top = tk.Frame(c, bg=CARD)
                top.pack(fill="x")
                dot = tk.Canvas(top, width=int(9 * self.S), height=int(9 * self.S), bg=CARD, highlightthickness=0)
                dot.pack(side="left", padx=(0, 6))
                name = tk.Label(top, text=k.lstrip("!"), bg=CARD, fg=FG, font=(FONT, 9, "bold"))
                name.pack(side="left")
                stat = tk.Label(top, text="", bg=CARD, fg=OK, font=(FONT, 8, "bold"))
                stat.pack(side="right")
                det = tk.Label(c, text="", bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w")
                det.pack(fill="x", padx=(int(15 * self.S), 0))
                self.cards[k] = (c, dot, name, stat, det)
            names = [l["label"] for l in s["links"]]
            self.dev_pick.config(values=names)
            if names and self.dev_pick.get() not in names:
                self.dev_pick.set(names[0])
        for l in s["links"]:
            c, dot, name, stat, det = self.cards[l["label"]]
            txt, tone = link_status(l)
            col = TONE[tone]
            bad = tone == "bad"
            stat.config(text=txt, fg=col)
            det.config(text=link_detail(l), fg=BAD if bad else MUTED)
            name.config(fg=BAD if bad else FG)
            self.dot(dot, col)
        for x in s.get("stalled") or []:
            c, dot, name, stat, det = self.cards["!" + x["label"]]
            stat.config(text="✖", fg=BAD)
            det.config(text=t("stalled"), fg=BAD)
            name.config(fg=BAD)
            self.dot(dot, BAD)
        self.devs.config(highlightbackground=BAD if any(not l["healthy"] for l in s["links"]) or s.get("stalled") else LINE)

    def dot(self, canvas, color):
        canvas.delete("all")
        d = int(10 * self.S)
        canvas.create_oval(1, 1, d - 1, d - 1, fill=color, outline=color)

    def draw_graph(self, down, up):
        """All links together, last 5 minutes: download (cyan) and upload (amber)."""
        c = self.graph
        if not c.winfo_ismapped():
            return
        c.delete("all")
        w, h = int(c.winfo_width() or 300), int(c.cget("height"))
        top = max([1.0] + list(down) + list(up))
        c.create_text(6, 4, anchor="nw", text=f"{top:.0f} Mbps", fill=MUTED, font=(FONT, 7))
        for series, col in ((down, ACC), (up, SLOW)):
            if len(series) > 1:
                pts = []
                for i, v in enumerate(series):
                    pts += [w * i / (len(series) - 1), h - 4 - (h - 18) * v / top]
                c.create_line(*pts, fill=col, width=2, smooth=True)
        c.create_text(w - 6, 4, anchor="ne", text="↓", fill=ACC, font=(FONT, 8, "bold"))
        c.create_text(w - 18, 4, anchor="ne", text="↑", fill=SLOW, font=(FONT, 8, "bold"))

    def draw_jobs(self, jobs):
        sig = json.dumps(jobs, sort_keys=True)
        if sig == self.jobs_sig:
            return
        self.jobs_sig = sig
        for w in self.jobs.winfo_children():
            w.destroy()
        for j in jobs:
            fr = tk.Frame(self.jobs, bg=CARD)
            fr.pack(fill="x", pady=(8, 0))
            name = j["name"] if len(j["name"]) < 36 else j["name"][:33] + "…"
            tag = f"[{t('auto')}] " if j.get("source") == "browser" else ""
            tk.Label(fr, text=tag + name, bg=CARD, fg=FG, anchor="w", font=(FONT, 9)).pack(fill="x")
            pct = (j["done"] / j["size"] * 100) if j["size"] else 0
            share = " / ".join(f"{k} {v}%" for k, v in j["by_link"].items())
            if j["state"] == "running":
                ttk.Progressbar(fr, maximum=100, value=pct, style="LB.Horizontal.TProgressbar").pack(fill="x", pady=(3, 0))
                info = t("dlwait") + f"  ·  {pct:.0f}%" if j.get("waiting") else f"{pct:.0f}%  ·  {j['mbps']} Mbps  ·  {share}"
            elif j["state"] == "done":
                info = f"{t('done')}  {fmt_mb((j['size'] or j['done']) / 1e6)}  ·  {j['mbps']} Mbps  ·  {share}"
            else:
                info = j["error"] or j["state"]
            tk.Label(fr, text=info, bg=CARD, fg=BAD if j["state"] == "error" else MUTED, anchor="w", font=(FONT, 8),
                     wraplength=int(290 * self.S), justify="left").pack(fill="x")
            if j["state"] == "running":
                self.button(fr, t("cancel"), lambda i=j["id"]: self.post("/api/download/cancel", {"id": i}), small=True).pack(anchor="w", pady=(3, 0))
            elif j["state"] == "done":
                self.button(fr, t("show"), lambda i=j["id"]: self.post("/api/download/open", {"id": i}), small=True).pack(anchor="w", pady=(3, 0))
        self.fit()

    def fit(self):
        """Exactly as big as what it shows - and moved back if it would run off the screen."""
        r = self.root
        r.update_idletasks()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        x, y = self.clamp(r.winfo_x(), r.winfo_y(), w, h)
        r.geometry(f"{w}x{h}+{x}+{y}")

    # ----- single instance: a 2nd start just brings the first one forward -----
    def lock_server(self):
        """Another start of the box (desktop icon) sends b"raise": bring this one forward. Anything else is ignored."""
        if self.lock is None:
            return
        try:
            while True:
                c, _ = self.lock.accept()
                try:
                    c.settimeout(2)
                    want = c.recv(16) == b"raise"
                except OSError:
                    want = False
                finally:
                    c.close()
                if want:
                    self.root.after(0, self.raise_up)
        except OSError:
            pass

    def raise_up(self):
        if self.prefs.get("hidden"):
            self.prefs["hidden"] = False
            self.save()
        self.root.deiconify()
        self.root.lift()
        if self.prefs["mode"] == "pill":
            self.set_mode("normal")
        self.root.attributes("-topmost", True)
        self.root.attributes("-topmost", bool(self.prefs["top"]))


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def work_area(x, y):
    """(left, top, right, bottom) of the usable part (no taskbar) of the monitor at x, y; None if unknown."""
    try:
        u = ctypes.windll.user32
        u.MonitorFromPoint.restype = ctypes.c_void_p
        mon = u.MonitorFromPoint(_POINT(int(x), int(y)), 2)  # 2 = the nearest monitor
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if mon and u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
            w = mi.rcWork
            return w.left, w.top, w.right, w.bottom
    except Exception:
        pass
    return None


def take_lock():
    """The single-instance lock: a listening socket on LOCK_PORT, or None when another box holds it."""
    srv = socket.socket()
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        pass
    try:
        srv.bind(("127.0.0.1", LOCK_PORT))
        srv.listen(4)
        return srv
    except OSError:
        srv.close()
        return None


def ask_first_to_raise():
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1) as c:
            c.sendall(b"raise")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except Exception:
        pass
    argv = sys.argv[1:]
    opt = lambda name: argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
    shot, force_mode = opt("--shot"), opt("--mode")  # developer options: --shot out.png [--mode pill|normal|full]
    lock = None
    if not shot:
        tries = 40 if "--replace" in argv else 1  # started by an older version that is closing: wait for it
        for _ in range(tries):
            lock = take_lock()
            if lock:
                break
            time.sleep(0.25)
        if lock is None:  # a box is already open: bring it forward and leave
            if "--background" not in argv:  # the supervisor only makes sure one runs: it never pops it up
                ask_first_to_raise()
            sys.exit(0)
    app = Widget(lock)
    if app.prefs.get("hidden") and ("--replace" in argv or "--background" in argv):
        app.root.withdraw()  # it was hidden with x: an update or a restart by the supervisor keeps it hidden
    elif app.prefs.get("hidden"):
        app.prefs["hidden"] = False  # started by you (desktop icon / logon): show it
    if shot:  # photograph only our own window (it is DPI-aware, so the size is exact), then leave without saving
        app.save = lambda: None
        app.prefs.update(x=60, y=60, alpha=1.0, idle_alpha=1.0, fade=False)
        app.root.attributes("-alpha", 1.0)  # opaque: nothing behind the box may show through on a picture
    if force_mode:
        app.prefs["mode"] = force_mode
        app.apply_mode(first=True)
    if shot:
        def grab():
            from PIL import ImageGrab
            r = app.root
            r.update()
            x, y = r.winfo_rootx(), r.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + r.winfo_width(), y + r.winfo_height()), all_screens=True).save(shot)
            r.destroy()
        app.root.after(4500, grab)
    app.root.mainloop()
