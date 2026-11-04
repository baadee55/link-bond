"""Relay gate tests (NOT part of the CI self-test): ONE connection over several fake links through the real relay code.

Needs the relay running locally:   cd relay ; npx wrangler dev --local --port 8787     (BOND_SECRET in relay/.dev.vars)
Every fake link is a local TCP shaping proxy in front of the relay (rate, one-way delay, random stalls, death,
black hole). The client (bond.py) connects to the proxy instead of the relay. Nothing leaves this PC.

    python relaytest.py            all gates
    python relaytest.py 1 6        only gates 1 and 6      (2u / 3u ... = the upload variants are part of 2 / 3 / 4)

Prints a table of measured Mbit/s per gate with PASS / FAIL against the thresholds of the spec (section 6)."""
import asyncio
import collections
import hashlib
import os
import random
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bond  # noqa: E402

RELAY_PORT = 8787
HERE = os.path.dirname(os.path.abspath(__file__))
MB = 1_000_000
bond.STATE_DIR = tempfile.mkdtemp(prefix="linkbond-relaytest-")  # never the real settings, never the real relay
bond.no_notify = True
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

results = []   # (gate, what, measured, needed, ok)
real_link_connect = bond.link_connect
SIM = {}       # link name -> SimLink


def record(gate, what, measured, needed, ok):
    results.append((gate, what, measured, needed, ok))
    print("%-4s %-52s %-26s %-18s %s" % (gate, what, measured, needed, "PASS" if ok else "FAIL"), flush=True)


def dev_secret():
    """The test secret of the local relay (from relay/.dev.vars). It is only written to the temp relay.local.txt."""
    with open(os.path.join(HERE, "relay", ".dev.vars"), encoding="utf-8") as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k == "BOND_SECRET":
                return v.strip().strip("\"'")
    raise SystemExit("BOND_SECRET not found in relay/.dev.vars")


# ------------------------------------------------------------------ the link simulator ----
class SimLink:
    """A fake link: a TCP proxy in front of the relay. Per direction: rate, one-way delay. Stalls, death, black hole."""

    def __init__(self, name, mbit, delay_ms, rtt_ms=None, stalls=False):
        self.name, self.delay = name, delay_ms / 1000.0
        self.rate = {"up": mbit * 125000.0, "down": mbit * 125000.0}
        self.free = {"up": 0.0, "down": 0.0}
        self.stall_until = 0.0
        self.dead = self.black = False
        self.conns = set()
        self.stalls = stalls
        self.rtt_ms = rtt_ms if rtt_ms is not None else 2 * delay_ms
        self.server = self.port = None
        self.tasks = []

    async def start(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        if self.stalls:
            self.tasks.append(asyncio.ensure_future(self.stall_loop()))
        SIM[self.name] = self

    def close(self):
        self.server.close()
        for t in self.tasks:
            t.cancel()
        self.kill()
        SIM.pop(self.name, None)

    async def stall_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(random.uniform(1.0, 4.0))
            dur = random.uniform(0.5, 1.5)
            self.stall_until = loop.time() + dur
            for d in self.free:
                self.free[d] = max(self.free[d], self.stall_until)

    def kill(self):
        """Death: every connection is closed, new ones are closed at once."""
        self.dead = True
        for cw, sw in list(self.conns):
            for w in (cw, sw):
                try:
                    w.transport.abort()
                except Exception:
                    pass
        self.conns.clear()

    def revive(self):
        self.dead = False

    async def accept(self, cr, cw):
        if self.dead:
            cw.transport.abort()
            return
        try:
            sr, sw = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
        except OSError:
            cw.transport.abort()
            return
        conn = (cw, sw)
        self.conns.add(conn)
        try:
            await asyncio.gather(self.shape(cr, sw, "up"), self.shape(sr, cw, "down"))
        finally:
            self.conns.discard(conn)
            for w in (cw, sw):
                try:
                    w.close()
                except Exception:
                    pass

    async def shape(self, r, w, d):
        """Bytes in at r, out at w: at most `rate` bytes/s, each byte `delay` later. The queue is short (about the
        delay x rate), so a full link pushes back on the sender like a real one."""
        loop = asyncio.get_running_loop()
        q = collections.deque()
        qb = [0]
        have, space = asyncio.Event(), asyncio.Event()
        limit = max(65536, int(self.rate[d] * self.delay * 1.5) + 65536)

        async def rd():
            try:
                while True:
                    while qb[0] >= limit:
                        space.clear()
                        await space.wait()
                    b = await r.read(32768)
                    if not b:
                        break
                    ready = max(loop.time(), self.free[d]) + len(b) / self.rate[d]
                    self.free[d] = ready
                    q.append((ready + self.delay, b))
                    qb[0] += len(b)
                    have.set()
            except Exception:
                pass
            q.append((0, None))
            have.set()

        async def wr():
            try:
                while True:
                    while not q:
                        have.clear()
                        await have.wait()
                    t, b = q[0]
                    if b is None:
                        break
                    while True:
                        wait = max(t, self.stall_until) - loop.time()
                        if self.black:
                            wait = max(wait, 0.1)
                        if wait <= 0:
                            break
                        await asyncio.sleep(min(wait, 0.1))
                    q.popleft()
                    qb[0] -= len(b)
                    space.set()
                    w.write(b)
                    await w.drain()
            except Exception:
                pass

        rt = asyncio.ensure_future(rd())
        try:
            await wr()
        finally:
            rt.cancel()


# ------------------------------------------------------------------ the origin servers ----
async def serve_chunks(w, data):
    for i in range(0, len(data), 65536):
        w.write(data[i:i + 65536])
        await w.drain()


async def source_server(data, hold=0.0):
    """Sends `data` as soon as someone connects, then closes."""
    async def h(r, w):
        try:
            await serve_chunks(w, data)
        except Exception:
            pass
        w.close()
    s = await asyncio.start_server(h, "127.0.0.1", 0)
    return s, s.sockets[0].getsockname()[1]


class Sink:
    """Receives until the client says it is done (FIN), answers with the SHA-256 of what it got, then closes."""

    def __init__(self):
        self.rx, self.times, self.sha = 0, [], hashlib.sha256()
        self.server = self.port = None

    async def start(self):
        async def h(r, w):
            try:
                while True:
                    b = await r.read(65536)
                    if not b:
                        break
                    self.rx += len(b)
                    self.sha.update(b)
                    self.times.append(time.perf_counter())
                w.write(self.sha.hexdigest().encode())
                await w.drain()
            except Exception:
                pass
            w.close()
        self.server = await asyncio.start_server(h, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def max_gap(self):
        return max((b - a for a, b in zip(self.times, self.times[1:])), default=0.0)


# ------------------------------------------------------------------ measuring ----
def use_links(*sims):
    """The links the client may use (fake link dicts: their connections go to the proxies)."""
    bond.links.clear()
    out = []
    for i, s in enumerate(sims, 1):
        l = bond.new_link(1, s.name, "127.0.0.1")
        l["rtt"] = s.rtt_ms
        bond.links[i] = l
        out.append(l)
    bond.relay_off_until = 0.0
    bond.host_bad.clear()
    return out


async def sim_connect(l, sk, addr):
    s = SIM.get(l["name"])
    if s is None:
        await real_link_connect(l, sk, addr)
    else:
        await asyncio.get_running_loop().sock_connect(sk, ("127.0.0.1", s.port))


bond.link_connect = sim_connect


def split_bytes(ls):
    return " ".join("%s:%.1fMB" % (l["name"], (l["b_down"] + l["b_up"]) / MB) for l in ls)


async def run_down(nbytes, sims, mid=None, mid_at=0.3):
    """Download nbytes from an origin through ONE bonded flow. mid() is called once when mid_at of it has arrived."""
    ls = use_links(*sims)
    data = os.urandom(nbytes)
    want = hashlib.sha256(data).hexdigest()
    srv, port = await source_server(data)
    try:
        r, w = await bond.open_bonded("127.0.0.1", port)
        total, got, fired = 0, hashlib.sha256(), False
        t0 = last = time.perf_counter()
        gap, first = 0.0, True
        while True:
            b = await asyncio.wait_for(r.read(262144), 60)
            if not b:
                break
            now = time.perf_counter()
            if first:
                first = False
            else:
                gap = max(gap, now - last)
            last = now
            total += len(b)
            got.update(b)
            if mid and not fired and total >= mid_at * nbytes:
                fired = True
                mid()
        dt = time.perf_counter() - t0
        w.close()
        return dict(mbit=total * 8 / dt / 1e6, ok=total == nbytes and got.hexdigest() == want, gap=gap, dt=dt, links=split_bytes(ls))
    finally:
        srv.close()


async def run_up(nbytes, sims, mid=None, mid_at=0.3):
    """Upload nbytes to a sink through ONE bonded flow; the sink answers with the SHA-256 once the client closes."""
    ls = use_links(*sims)
    data = os.urandom(nbytes)
    want = hashlib.sha256(data).hexdigest().encode()
    sink = Sink()
    await sink.start()

    async def watch():
        fired = False
        while True:
            await asyncio.sleep(0.02)
            if mid and not fired and sink.rx >= mid_at * nbytes:
                fired = True
                mid()
    wt = asyncio.ensure_future(watch())
    try:
        r, w = await bond.open_bonded("127.0.0.1", sink.port)
        t0 = time.perf_counter()
        for i in range(0, nbytes, 65536):
            w.write(data[i:i + 65536])
            await asyncio.wait_for(w.drain(), 60)
        w.close()  # FIN: the sink sees the end of the upload, answers, and its answer must still come back
        reply = await asyncio.wait_for(r.read(), 90)
        dt = time.perf_counter() - t0
        return dict(mbit=nbytes * 8 / dt / 1e6, ok=reply == want and sink.rx == nbytes, gap=sink.max_gap(), dt=dt, links=split_bytes(ls))
    finally:
        wt.cancel()
        sink.server.close()


async def attempt(gate, what, make, check_fn, needed):
    """Run one measurement; whatever goes wrong is a FAIL line (the other measurements still run)."""
    try:
        x = await make()
    except Exception as e:
        record(gate, what, "%s %s" % (type(e).__name__, e), needed, False)
        for f in list(bond.bond_flows):  # a stuck flow must not go on in the background
            f.abort("test over", tell=True)
        return None
    ok, text = check_fn(x)
    record(gate, what, text + ("" if x["ok"] else " BAD DATA"), needed, ok and x["ok"])
    return x


def fmt(x):
    return "%.1f Mbit/s" % x["mbit"]


# ------------------------------------------------------------------ the gates ----
async def gate1():
    for kind, fn in (("down", run_down), ("up", run_up)):
        a, b = SimLink("A", 20, 40), SimLink("B", 20, 40)
        await a.start()
        await b.start()
        x = await attempt("1", "2 x 20 Mbit/s, 40 ms: 40 MB " + kind, lambda: fn(40 * MB, [a, b]),
                          lambda x: (x["mbit"] >= 34, fmt(x)), ">= 34")
        if x:
            print("       carried:", x["links"], flush=True)
        a.close()
        b.close()


async def gate2():
    for kind, fn in (("down", run_down), ("up", run_up)):
        fast = SimLink("F", 60, 40)
        slow = SimLink("S", 0.6, 850, stalls=True)
        await fast.start()
        await slow.start()
        solo = await attempt("2", "60 Mbit/s alone, 60 MB " + kind, lambda: fn(60 * MB, [fast]), lambda x: (True, fmt(x)), "(baseline)")
        if solo:
            both = await attempt("2", "+ 0.6 Mbit/s 850 ms stalls, 60 MB " + kind, lambda: fn(60 * MB, [fast, slow]),
                                 lambda x: (x["mbit"] / solo["mbit"] >= 0.95, "%s = %.2f x" % (fmt(x), x["mbit"] / solo["mbit"])),
                                 ">= 0.95 x baseline")
            if both:
                print("       carried:", both["links"], flush=True)
        fast.close()
        slow.close()


async def gate3():
    for kind, fn in (("down", run_down), ("up", run_up)):
        for how in ("death", "blackhole"):
            a, b = SimLink("A", 20, 40), SimLink("B", 20, 40)
            await a.start()
            await b.start()
            hit = (lambda: a.kill()) if how == "death" else (lambda: setattr(a, "black", True))
            await attempt("3", "2 x 20 Mbit/s, link %s at 30%% (%s, 30 MB)" % (how, kind), lambda: fn(30 * MB, [a, b], mid=hit),
                          lambda x: (x["gap"] <= 3.0, "%s gap %.1f s" % (fmt(x), x["gap"])), "ok, sha, gap <= 3 s")
            a.close()
            b.close()


async def gate4():
    for kind, fn in (("down", run_down), ("up", run_up)):
        a, b = SimLink("A", 20, 40), SimLink("B", 20, 40)
        await a.start()
        await b.start()

        def out():
            a.kill()
            b.kill()
            asyncio.get_running_loop().call_later(10.0, lambda: (a.revive(), b.revive()))
        await attempt("4", "all links gone 10 s at 30%% (%s, 40 MB)" % kind, lambda: fn(40 * MB, [a, b], mid=out),
                      lambda x: (True, "%s in %.0f s" % (fmt(x), x["dt"])), "completes, sha")
        a.close()
        b.close()


async def gate5():
    a, b = SimLink("A", 20, 40), SimLink("B", 20, 40)
    await a.start()
    await b.start()
    use_links(a, b)
    cfg_dir = bond.STATE_DIR
    with open(os.path.join(cfg_dir, bond.BULK_FILE), "w", encoding="utf-8") as f:
        f.write("127.0.0.1\n")
    import ipaddress
    bond.cf_nets = [ipaddress.ip_network("10.16.0.0/13")]

    async def req_server(r, w):
        try:
            body = await r.readexactly(10000)
            # the reply: the SHA-256 of the request, then the request backwards (so a mix-up shows)
            w.write(hashlib.sha256(body).hexdigest().encode() + body[::-1])
            await w.drain()
            rest = await r.read()  # waits for the client's FIN (half-close works) ...
            w.write(b"|fin:%d" % len(rest))  # ... and can still answer after it
            await w.drain()
        except Exception:
            pass
        w.close()
    srv = await asyncio.start_server(req_server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    bad, via_bond = [], [0]

    async def one(i):
        body = os.urandom(10000)
        l, r, w = await bond.open_via("127.0.0.1", port)
        if l is bond.BOND_LINK:
            via_bond[0] += 1
        w.write(body)
        await w.drain()
        head = await asyncio.wait_for(r.readexactly(64 + 10000), 30)
        w.close()  # half-close: the server answers once more
        tail = await asyncio.wait_for(r.read(), 30)
        if not (head[:64] == hashlib.sha256(body).hexdigest().encode() and head[64:] == body[::-1] and tail == b"|fin:0"):
            bad.append(i)
    t0 = time.perf_counter()
    for g in range(10):
        await asyncio.gather(*[one(g * 10 + k) for k in range(10)])
    dt = time.perf_counter() - t0
    record("5", "100 x 10 KB requests, each its own bonded flow", "%d wrong, %d via relay, %.1f s" % (len(bad), via_bond[0], dt),
           "0 wrong, 100 via relay", not bad and via_bond[0] == 100)
    srv.close()
    a.close()
    b.close()


async def gate6():
    a, b = SimLink("A", 20, 40), SimLink("B", 20, 40)
    await a.start()
    await b.start()
    ls = use_links(a, b)
    size = 8 * MB
    blob = os.urandom(size)

    async def burst_server(r, w):
        try:
            while True:
                if not await r.read(1):
                    break
                await serve_chunks(w, blob)
        except Exception:
            pass
        w.close()
    srv = await asyncio.start_server(burst_server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    r, w = await bond.open_bonded("127.0.0.1", port)
    speeds, okk = [], True
    for i in range(5):
        if i:
            await asyncio.sleep(3.0)
        w.write(b"G")
        t0 = time.perf_counter()
        got = hashlib.sha256()
        n = 0
        while n < size:
            c = await asyncio.wait_for(r.read(min(262144, size - n)), 30)
            if not c:
                okk = False
                break
            got.update(c)
            n += len(c)
        speeds.append(n * 8 / (time.perf_counter() - t0) / 1e6)
        okk = okk and got.hexdigest() == hashlib.sha256(blob).hexdigest()
    w.close()
    rel = [s / speeds[0] for s in speeds[1:]]
    record("6", "5 bursts of 8 MB, 3 s idle between (one flow)", " ".join("%.1f" % s for s in speeds) + (" Mbit/s" if okk else " BAD DATA"),
           "each >= 34, 2-5 >= 0.9 x #1", okk and min(speeds) >= 34 and min(rel) >= 0.9)
    print("       carried:", split_bytes(ls), flush=True)
    srv.close()
    a.close()
    b.close()


GATES = {"1": gate1, "2": gate2, "3": gate3, "4": gate4, "5": gate5, "6": gate6}


async def main(which):
    # the temp relay.local.txt: the local relay and its test secret (never the real relay)
    with open(os.path.join(bond.STATE_DIR, bond.RELAY_FILE), "w", encoding="utf-8") as f:
        f.write("ws://127.0.0.1:%d\n%s\n" % (RELAY_PORT, dev_secret()))
    print("BOND_SECRET: present (read from relay/.dev.vars, written only to the temp state folder)")
    try:
        socket.create_connection(("127.0.0.1", RELAY_PORT), 2).close()
    except OSError:
        raise SystemExit("the relay is not running on 127.0.0.1:%d (npx wrangler dev --local --port %d in relay/)" % (RELAY_PORT, RELAY_PORT))
    print("%-4s %-52s %-26s %-18s %s" % ("gate", "test", "measured", "needed", ""))
    for g in which:
        try:
            await asyncio.wait_for(GATES[g](), 900)
        except Exception as e:
            record(g, "gate stopped", "%s: %s" % (type(e).__name__, e), "", False)
    print()
    print("%-4s %-52s %-26s %-18s %s" % ("gate", "test", "measured", "needed", ""))
    for g, what, m, need, ok in results:
        print("%-4s %-52s %-26s %-18s %s" % (g, what, m, need, "PASS" if ok else "FAIL"))
    bad = [r for r in results if not r[4]]
    print("\nALL GATES PASSED" if not bad and results else "\n%d FAILED" % len(bad))
    return 1 if bad or not results else 0


if __name__ == "__main__":
    which = [a[0] for a in sys.argv[1:]] or list(GATES)
    sys.exit(asyncio.run(main(which)))
