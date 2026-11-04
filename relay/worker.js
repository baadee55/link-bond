// link-bond relay: reassembles one TCP flow carried over several WebSockets.
// Worker `fetch` authenticates (HMAC-SHA256) and routes to a Durable Object (`Flow`)
// that owns the origin TCP socket, reorders upstream data and schedules downstream data.
// Protocol: see README.md. No secrets, signatures or destinations are ever logged.

import { connect } from "cloudflare:sockets";

const KiB = 1024;
const MiB = 1024 * 1024;

const T_DATA = 1, T_ACK = 2, T_FIN = 3, T_ERR = 4, T_OK = 5, T_PING = 6, T_PONG = 7;

const FLOW_RE = /^\/f\/([0-9a-f]{32})$/;
const BLOCKED_PORTS = new Set([25, 465, 587]);
const MAX_SKEW_S = 300;
const MAX_REORDER = 32 * MiB;
const CONNECT_TIMEOUT_MS = 8000;
const NO_LINK_S = 60;
const IDLE_S = 45;  // an open flow keeps this object running (Cloudflare's daily allowance): close idle ones soon

const enc = new TextEncoder();

// ---------------------------------------------------------------- Worker ----

function plain(status) {
  return new Response(null, { status });
}

function hexToBytes(h) {
  if (typeof h !== "string" || h.length % 2 || !/^[0-9a-f]*$/.test(h)) return null;
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
  return out;
}

function parseDest(d) {
  try {
    let b = d.replace(/-/g, "+").replace(/_/g, "/");
    while (b.length % 4) b += "=";
    const bin = atob(b);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const s = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    const i = s.lastIndexOf(":");
    if (i < 1) return null;
    const host = s.slice(0, i);
    const portStr = s.slice(i + 1);
    if (!/^\d{1,5}$/.test(portStr)) return null;
    const port = parseInt(portStr, 10);
    if (port < 1 || port > 65535) return null;
    if (!/^[A-Za-z0-9._-]{1,253}$/.test(host)) return null;
    return { host, port };
  } catch {
    return null;
  }
}

async function sigOk(env, flow, l, t, d, s) {
  const secret = hexToBytes(String(env.BOND_SECRET || ""));
  if (!secret || secret.length !== 32) return false;
  const sig = hexToBytes(s);
  if (!sig || sig.length !== 32) return false;
  const key = await crypto.subtle.importKey(
    "raw", secret, { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  // crypto.subtle.verify compares in constant time.
  return crypto.subtle.verify("HMAC", key, sig, enc.encode(`${flow}|${l}|${t}|${d}`));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const m = FLOW_RE.exec(url.pathname);
    if (!m) return plain(404);
    if (request.method !== "GET" ||
        (request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      return plain(400);
    }
    const q = url.searchParams;
    const l = q.get("l"), t = q.get("t"), d = q.get("d"), s = q.get("s");
    if (!/^\d{1,2}$/.test(l || "") || parseInt(l, 10) > 15 ||
        !/^\d{1,12}$/.test(t || "") ||
        !/^[A-Za-z0-9_-]{1,400}$/.test(d || "") ||
        !/^[0-9a-f]{64}$/.test(s || "")) {
      return plain(400);
    }
    if (Math.abs(Math.floor(Date.now() / 1000) - parseInt(t, 10)) > MAX_SKEW_S) return plain(403);
    if (!(await sigOk(env, m[1], l, t, d, s))) return plain(403);
    const dest = parseDest(d);
    if (!dest) return plain(400);
    if (BLOCKED_PORTS.has(dest.port)) return plain(403);
    const id = env.FLOW.idFromName(m[1]);
    return env.FLOW.get(id, { locationHint: "apac" }).fetch(request);
  },
};

// -------------------------------------------------------- scheduler maths ---

const now = () => Date.now() / 1000;
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

class Link {
  constructor(id, ws, t) {
    this.id = id;
    this.ws = ws;
    this.rtt = 0.3;
    this.pinged = false;
    this.rtprop = 0.3;      // min of the last 10 PONG RTTs: the unloaded path delay (window/eta use it, not the queue-inflated EWMA)
    this.rtts = [];
    this.rate = 1 * MiB;
    this.sent = 0;          // DATA payload bytes we sent on this WS
    this.acked = 0;         // per-link total from the peer's ACKs
    this.rx = 0;            // DATA payload bytes received on this WS
    this.lastHeard = t;
    this.dead = false;
    this.rateT = t;
    this.rateAcked = 0;
    this.limited = false;   // window-limited during the current rate interval
    this.rateSent = 0;
    this.blocked = false;   // a send was held back by this link's window this interval
    this.progressAt = t;
  }
  get inflight() { return this.sent - this.acked; }
}

const winOf = (l) => clamp(l.rate * (l.rtprop + 0.1) * 2, 64 * KiB, 16 * MiB);
const etaOf = (l, n) => (l.inflight + n) / l.rate + l.rtprop / 2;
const csizeOf = (l) => Math.floor(clamp(l.rate * 0.05, 4 * KiB, 64 * KiB));

function mk(type, bodyLen) {
  const b = new Uint8Array(1 + bodyLen);
  b[0] = type;
  return b;
}

function connectReason(e) {
  const m = String((e && e.message) || e || "");
  if (/time/i.test(m)) return "timeout";
  if (/refus/i.test(m)) return "refused";
  if (/denied|not allowed|blocked|loopback|private|prohibit/i.test(m)) return "denied";
  return "failed";
}

// --------------------------------------------------------- Durable Object ---

export class Flow {
  constructor(state, env) {
    this.env = env;
    this.links = new Map();
    this.closed = false;
    this.started = false;
    this.connected = false;
    this.socket = null;
    this.reader = null;
    this.writer = null;

    // downstream (origin -> client)
    this.nextSeq = 0;
    this.pending = [];
    this.pendingOff = 0;
    this.pendingBytes = 0;
    this.unacked = new Map();   // seq -> chunk
    this.retx = [];             // sorted seqs waiting for (re)send
    this.eof = false;
    this.finSent = false;
    this.downTotal = 0;
    this.downCum = 0;
    this.roomWaiter = null;
    this.pumping = false;

    // upstream (client -> origin)
    this.upCum = 0;
    this.upBuf = new Map();
    this.upBufBytes = 0;
    this.wq = [];
    this.wqBytes = 0;
    this.writing = false;
    this.upTotal = null;
    this.upClosed = false;
    this.ackDirty = false;
    this.ackBytes = 0;
    this.ackTimer = null;

    const t = now();
    this.lastData = t;
    this.noLiveSince = t;
    this.rateTick = t;
    this.tickTimer = null;
    this.pingTimer = null;
    this.connTimer = null;
    this.bytesIn = 0;
    this.lastUpData = 0;
  }

  // ------------------------------------------------------------- entry ----

  async fetch(request) {
    if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") return plain(400);
    const q = new URL(request.url).searchParams;
    const id = parseInt(q.get("l"), 10);
    const dest = parseDest(q.get("d") || "");
    if (!(id >= 0 && id <= 15) || !dest) return plain(400);

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.binaryType = "arraybuffer";
    server.accept();

    if (this.closed) {
      try { server.send(this.errFrame("closed")); server.close(1011, "closed"); } catch { /* gone */ }
      return new Response(null, { status: 101, webSocket: client });
    }

    this.addLink(id, server);
    if (!this.started) {
      this.started = true;
      this.tickTimer = setInterval(() => this.guard(() => this.tick()), 100);
      this.pingTimer = setInterval(() => this.guard(() => this.pingAll()), 1000);
      this.connectOrigin(dest);
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  guard(fn) {
    try { fn(); } catch { this.fail("internal"); }
  }

  // ------------------------------------------------------------- links ----

  addLink(id, ws) {
    const old = this.links.get(id);
    if (old) {
      this.links.delete(id);
      this.reinject(id, true);
      try { old.ws.close(1000, "replaced"); } catch { /* gone */ }
    }
    const l = new Link(id, ws, now());
    this.links.set(id, l);
    ws.addEventListener("message", (ev) => {
      if (this.links.get(id) !== l) return;
      try { this.onMessage(l, ev.data); } catch { this.fail("internal"); }
    });
    const gone = () => { if (this.links.get(id) === l) this.dropLink(l); };
    ws.addEventListener("close", gone);
    ws.addEventListener("error", gone);
    this.sendPing(l);
    if (this.connected) this.send(l, mk(T_OK, 0));
    if (this.finSent) this.sendFin(l);
    this.wake();
    this.pump();
  }

  dropLink(l) {
    if (this.links.get(l.id) !== l) return;
    this.links.delete(l.id);
    this.reinject(l.id, true);
    try { l.ws.close(1000, "bye"); } catch { /* gone */ }
    this.wake();
    if (this.lastUpData && !this.closed) this.sendAck();
    if (!this.closed) this.pump();
  }

  live() {
    const out = [];
    for (const l of this.links.values()) if (!l.dead) out.push(l);
    return out;
  }

  send(l, frame) {
    try { l.ws.send(frame); return true; } catch { this.dropLink(l); return false; }
  }

  sendAll(frame) {
    for (const l of [...this.links.values()]) this.send(l, frame);
  }

  errFrame(reason) {
    const r = enc.encode(String(reason).slice(0, 100));
    const f = mk(T_ERR, r.length);
    f.set(r, 1);
    return f;
  }

  // ------------------------------------------------------ origin socket ----

  async connectOrigin(dest) {
    let sock;
    try {
      sock = connect({ hostname: dest.host, port: dest.port }, { allowHalfOpen: true });
      this.socket = sock;
      await Promise.race([
        sock.opened,
        new Promise((_, rej) => {
          this.connTimer = setTimeout(() => rej(new Error("timeout")), CONNECT_TIMEOUT_MS);
        }),
      ]);
    } catch (e) {
      clearTimeout(this.connTimer);
      this.fail("connect: " + connectReason(e));
      return;
    }
    clearTimeout(this.connTimer);
    this.connTimer = null;
    if (this.closed) { try { sock.close(); } catch { /* gone */ } return; }
    this.connected = true;
    this.writer = sock.writable.getWriter();
    this.reader = sock.readable.getReader();
    sock.closed.catch(() => {});
    this.sendAll(mk(T_OK, 0));
    this.pumpWrites();
    this.readLoop();
  }

  async readLoop() {
    try {
      for (;;) {
        // Backpressure: do not read while the scheduler has no room.
        while (!this.closed && (this.pendingBytes > 0 || !this.hasRoom())) {
          await new Promise((r) => { this.roomWaiter = r; });
        }
        if (this.closed) return;
        const { value, done } = await this.reader.read();
        if (this.closed) return;
        if (done) { this.eof = true; this.pump(); return; }
        if (value && value.length) {
          this.pending.push(value);
          this.pendingBytes += value.length;
          this.lastData = now();
          this.pump();
        }
      }
    } catch {
      if (!this.closed) this.fail("origin read");
    }
  }

  wake() {
    const r = this.roomWaiter;
    if (r) { this.roomWaiter = null; r(); }
  }

  // ------------------------------------------------- downstream scheduler ----

  // A link whose RTT is still unmeasured gets no data while another live link is measured
  // (a first guess would put a window of data on a slow link and stall the reorder buffer).
  gateUnmeasured() {
    for (const l of this.links.values()) if (!l.dead && l.pinged) return true;
    return false;
  }

  bestFor(n, exclude) {
    const gate = this.gateUnmeasured();
    let best = null, bestEta = Infinity;
    for (const l of this.links.values()) {
      if (l.dead || (gate && !l.pinged) || (exclude && exclude.has(l.id))) continue;
      const e = etaOf(l, n);
      if (e < bestEta) { bestEta = e; best = l; }
    }
    return best;
  }

  hasRoom() {
    while (this.retx.length && !this.unacked.has(this.retx[0])) this.retx.shift();
    if (this.retx.length) return false;
    const l = this.bestFor(4 * KiB, null);
    return !!l && l.inflight + 4 * KiB <= winOf(l);
  }

  takeInto(dst, off, n) {
    let o = 0;
    while (o < n) {
      const b = this.pending[0];
      const k = Math.min(b.length - this.pendingOff, n - o);
      dst.set(b.subarray(this.pendingOff, this.pendingOff + k), off + o);
      o += k;
      this.pendingOff += k;
      if (this.pendingOff === b.length) { this.pending.shift(); this.pendingOff = 0; }
    }
    this.pendingBytes -= n;
  }

  sendChunk(c, l) {
    const n = c.frame.length - 5;
    if (l.inflight === 0) l.progressAt = now();
    l.sent += n;
    if (l.inflight >= winOf(l) / 2) l.limited = true;
    c.link = l.id;
    c.linkEnd = l.sent;
    c.sentAt = now();
    c.sentOn.add(l.id);
    c.queued = false;
    this.lastData = c.sentAt;
    this.send(l, c.frame);
  }

  pump() {
    if (this.pumping || this.closed || !this.connected) return;
    this.pumping = true;
    try {
      for (;;) {
        while (this.retx.length && !this.unacked.has(this.retx[0])) this.retx.shift();
        if (this.retx.length) {
          const c = this.unacked.get(this.retx[0]);
          const n = c.frame.length - 5;
          const l = this.bestFor(n, null);
          if (!l) break;
          if (l.inflight + n > winOf(l)) { l.limited = l.blocked = true; break; }   // wait for an ACK
          this.retx.shift();
          this.sendChunk(c, l);
          continue;
        }
        if (this.pendingBytes === 0) break;
        const gate = this.gateUnmeasured();
        let best = null, bestEta = Infinity, bestN = 0;
        for (const l of this.links.values()) {
          if (l.dead || (gate && !l.pinged)) continue;
          const n = Math.min(this.pendingBytes, csizeOf(l));
          const e = etaOf(l, n);
          if (e < bestEta) { bestEta = e; best = l; bestN = n; }
        }
        if (!best) break;
        if (best.inflight + bestN > winOf(best)) { best.limited = best.blocked = true; break; }   // never spill to a worse link
        const frame = mk(T_DATA, 4 + bestN);
        new DataView(frame.buffer).setUint32(1, this.nextSeq);
        this.takeInto(frame, 5, bestN);
        const c = { seq: this.nextSeq, frame, link: -1, linkEnd: 0, sentAt: 0, sentOn: new Set(), queued: false };
        this.unacked.set(c.seq, c);
        this.nextSeq++;
        this.sendChunk(c, best);
      }
      if (this.eof && this.pendingBytes === 0 && !this.finSent && this.retx.length === 0) {
        this.finSent = true;
        this.downTotal = this.nextSeq;
        for (const l of this.live()) this.sendFin(l);
      }
    } finally {
      this.pumping = false;
    }
    this.wake();
  }

  sendFin(l) {
    const f = mk(T_FIN, 4);
    new DataView(f.buffer).setUint32(1, this.downTotal);
    this.send(l, f);
  }

  // Put chunks that went out on link `id` back on the send queue.
  // `all`: unconditional (link removed/replaced); otherwise only those the peer has not counted yet.
  reinject(id, all, l) {
    let added = false;
    for (const c of this.unacked.values()) {
      if (c.link !== id || c.queued) continue;
      if (!all && l && c.linkEnd <= l.acked) continue;
      c.queued = true;
      this.retx.push(c.seq);
      added = true;
    }
    if (added) this.retx.sort((a, b) => a - b);
  }

  markDead(l) {
    l.dead = true;
    this.reinject(l.id, false, l);
    if (this.lastUpData) this.sendAck();
  }

  // Resend one overdue chunk among the 8 lowest unacked on the best other live link.
  reinjectOverdue(t) {
    let i = 0;
    for (const c of this.unacked.values()) {
      if (i++ >= 8) break;
      if (c.queued) continue;
      const cur = this.links.get(c.link);
      if (!cur || cur.dead) continue;
      if (c.linkEnd <= cur.acked) continue;   // peer already has it; an earlier hole blocks cum
      const limit = cur.rtt * 2 + cur.inflight / cur.rate + 0.5;
      if (t - c.sentAt <= limit) continue;
      const n = c.frame.length - 5;
      const l = this.bestFor(n, c.sentOn);
      if (!l || l.inflight + n > winOf(l)) continue;
      this.sendChunk(c, l);
    }
  }

  updateRates(t) {
    for (const l of this.links.values()) {
      const dt = t - l.rateT;
      if (dt < 0.25) continue;
      // Only window-limited intervals measure the link; idle/app-limited ones keep the old rate.
      if (l.limited && (l.sent !== l.rateSent || l.blocked)) {
        const sample = (l.acked - l.rateAcked) / dt;
        if (sample > l.rate) l.rate = sample;
        else l.rate += (sample - l.rate) / 4;
        if (l.rate < 8 * KiB) l.rate = 8 * KiB;
      }
      l.limited = l.blocked = false;
      l.rateT = t;
      l.rateAcked = l.acked;
      l.rateSent = l.sent;
    }
  }

  // ------------------------------------------------------------ timers ----

  tick() {
    if (this.closed) return;
    const t = now();
    for (const l of [...this.links.values()]) {
      if (!l.dead && t - l.lastHeard > Math.max(2, 4 * l.rtt)) this.markDead(l);
    }
    this.updateRates(t);
    this.reinjectOverdue(t);
    if (this.live().length) this.noLiveSince = t;
    else if (t - this.noLiveSince > NO_LINK_S) { this.shutdown(1001, "no links"); return; }
    if (t - this.lastData > IDLE_S) { this.shutdown(1000, "idle"); return; }
    this.pump();
  }

  sendPing(l) {
    const f = mk(T_PING, 8);
    new DataView(f.buffer).setBigUint64(1, BigInt(Date.now()));
    this.send(l, f);
  }

  pingAll() {
    if (this.closed) return;
    for (const l of [...this.links.values()]) this.sendPing(l);
    if (this.lastUpData && now() - this.lastUpData < 10) this.sendAck();
    if (this.finSent && !this.downDone()) {
      const l = this.bestFor(0, null);
      if (l) this.sendFin(l);
    }
  }

  // ---------------------------------------------------------- messages ----

  onMessage(l, data) {
    if (typeof data === "string" || this.closed) return;
    let buf = data;
    if (ArrayBuffer.isView(buf)) buf = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    if (!buf || typeof buf.byteLength !== "number" || buf.byteLength < 1) return;
    const t = now();
    l.lastHeard = t;
    if (l.dead) {
      l.dead = false;
      l.rate = Math.max(8 * KiB, l.rate / 2);
      this.wake();
    }
    const dv = new DataView(buf);
    switch (dv.getUint8(0)) {
      case T_DATA: this.onData(l, buf, dv); break;
      case T_ACK: this.onAck(buf, dv, t); break;
      case T_FIN:
        if (buf.byteLength >= 5) {
          this.upTotal = dv.getUint32(1);
          this.sendAck();
          this.checkUp();
          this.checkDone();
        }
        break;
      case T_ERR: this.shutdown(1011, "peer error"); break;
      case T_PING:
        if (buf.byteLength >= 9) {
          const f = mk(T_PONG, 8);
          f.set(new Uint8Array(buf, 1, 8), 1);
          this.send(l, f);
        }
        break;
      case T_PONG:
        if (buf.byteLength >= 9) {
          const s = (Date.now() - Number(dv.getBigUint64(1))) / 1000;
          if (s >= 0 && s < 60) {
            if (l.pinged) l.rtt += (s - l.rtt) / 8; else { l.rtt = s; l.pinged = true; }
            l.rtts.push(s);
            if (l.rtts.length > 10) l.rtts.shift();
            l.rtprop = Math.min(...l.rtts);
          }
        }
        break;
      default: break;
    }
  }

  onAck(buf, dv, t) {
    if (buf.byteLength < 6) return;
    const cum = dv.getUint32(1);
    const n = dv.getUint8(5);
    if (buf.byteLength < 6 + n * 9) return;
    let o = 6;
    for (let i = 0; i < n; i++, o += 9) {
      const l = this.links.get(dv.getUint8(o));
      if (!l) continue;
      const v = Math.min(Number(dv.getBigUint64(o + 1)), l.sent);
      if (v > l.acked) { l.acked = v; l.progressAt = t; }
    }
    if (cum > this.downCum) {
      this.downCum = Math.min(cum, this.nextSeq);
      for (const seq of this.unacked.keys()) {
        if (seq < this.downCum) this.unacked.delete(seq); else break;
      }
    }
    this.checkDone();
    this.pump();
  }

  onData(l, buf, dv) {
    const len = buf.byteLength - 5;
    if (len < 1 || len > 65536) { this.fail("bad data"); return; }
    l.rx += len;
    this.lastData = now();
    this.lastUpData = this.lastData;
    const seq = dv.getUint32(1);
    this.ackDirty = true;
    this.ackBytes += len;
    if (seq >= this.upCum && !this.upBuf.has(seq)) {
      const payload = new Uint8Array(buf, 5, len).slice();  // own copy at offset 0 (a view into the frame is not safe to write)
      this.upBuf.set(seq, payload);
      this.upBufBytes += len;
      while (this.upBuf.has(this.upCum)) {
        const p = this.upBuf.get(this.upCum);
        this.upBuf.delete(this.upCum);
        this.upBufBytes -= p.length;
        this.wq.push(p);
        this.wqBytes += p.length;
        this.upCum++;
      }
      if (this.upBufBytes + this.wqBytes > MAX_REORDER) { this.fail("reorder overflow"); return; }
      this.pumpWrites();
    }
    if (this.ackBytes >= 256 * KiB) this.sendAck();
    else if (!this.ackTimer) {
      this.ackTimer = setTimeout(() => {
        this.ackTimer = null;
        this.guard(() => this.sendAck());
      }, 40);
    }
  }

  sendAck() {
    if (this.closed) return;
    if (this.ackTimer) { clearTimeout(this.ackTimer); this.ackTimer = null; }
    let best = null;
    for (const l of this.links.values()) if (!l.dead && (!best || l.rtt < best.rtt)) best = l;
    if (!best) return;
    const ls = [...this.links.values()];
    const f = mk(T_ACK, 5 + ls.length * 9);
    const dv = new DataView(f.buffer);
    dv.setUint32(1, this.upCum);
    dv.setUint8(5, ls.length);
    let o = 6;
    for (const l of ls) {
      dv.setUint8(o, l.id);
      dv.setBigUint64(o + 1, BigInt(l.rx));
      o += 9;
    }
    this.ackDirty = false;
    this.ackBytes = 0;
    this.send(best, f);
  }

  // ------------------------------------------------------------ upstream ----

  async pumpWrites() {
    if (this.writing || !this.writer || this.closed) return;
    this.writing = true;
    try {
      while (this.wq.length && !this.closed) {
        const buf = this.wq.shift();  // one piece at a time (big merged writes lost the connection on real sites)
        this.wqBytes -= buf.length;
        await this.writer.write(buf);
      }
    } catch (e) {
      if (!this.closed) this.fail("origin write: " + String((e && e.message) || e).slice(0, 80));
      return;
    } finally {
      this.writing = false;
    }
    this.checkUp();
    this.checkDone();
  }

  checkUp() {
    if (this.upClosed || !this.writer || this.upTotal === null) return;
    if (this.upCum < this.upTotal || this.wq.length || this.writing) return;
    this.upClosed = true;
    this.writer.close().catch(() => {});
  }

  downDone() {
    return this.finSent && this.downCum >= this.downTotal;
  }

  checkDone() {
    if (this.closed) return;
    if (this.downDone() && this.upClosed && this.upTotal !== null && this.upCum >= this.upTotal) {
      this.sendAck();
      this.shutdown(1000, "done");
    }
  }

  // ----------------------------------------------------------- teardown ----

  fail(reason) {
    if (this.closed) return;
    try { this.sendAll(this.errFrame(reason)); } catch { /* gone */ }
    this.shutdown(1011, reason);
  }

  shutdown(code, reason) {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.tickTimer);
    clearInterval(this.pingTimer);
    clearTimeout(this.ackTimer);
    clearTimeout(this.connTimer);
    this.tickTimer = this.pingTimer = this.ackTimer = this.connTimer = null;
    const r = String(reason || "").slice(0, 100);
    if (this.downTotal || this.bytesIn) {  // what each link carried (no hosts, no secrets) - for tuning
      try {
        console.log(JSON.stringify({ bond: r, down: this.nextSeq, upBytes: this.bytesIn, links: [...this.links.values()].map((l) => ({
          id: l.id, sent: l.sent, acked: l.acked, rx: l.rx, rtt: Math.round(l.rtt * 1000), rtprop: Math.round(l.rtprop * 1000),
          rate: Math.round(l.rate * 8 / 1e5) / 10 })) }));
      } catch { /* logging only */ }
    }
    for (const l of this.links.values()) { try { l.ws.close(code, r); } catch { /* gone */ } }
    this.links.clear();
    this.unacked.clear();
    this.retx = [];
    this.pending = [];
    this.pendingBytes = 0;
    this.upBuf.clear();
    this.wq = [];
    try { if (this.reader) this.reader.cancel().catch(() => {}); } catch { /* gone */ }
    try { if (this.writer && !this.upClosed) this.writer.abort().catch(() => {}); } catch { /* gone */ }
    try { if (this.socket) this.socket.close().catch(() => {}); } catch { /* gone */ }
    this.wake();
  }
}
