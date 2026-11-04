// Smoke test for the relay. Start it first:  npx wrangler dev --local --port 8787
// Then:  node smoke.mjs        (reads BOND_SECRET from .dev.vars; RELAY=host:port to override)
import net from "node:net";
import http from "node:http";
import fs from "node:fs";
import crypto from "node:crypto";

const RELAY = process.env.RELAY || "127.0.0.1:8787";
const vars = fs.readFileSync(new URL("./.dev.vars", import.meta.url), "utf8");
const SECRET = (/^BOND_SECRET=([0-9a-f]{64})\s*$/m.exec(vars) || [])[1];
if (!SECRET) { console.error("no BOND_SECRET in .dev.vars"); process.exit(2); }

let failed = 0;
function check(name, ok, extra = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!ok) failed++;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rnd = (n) => Math.floor(Math.random() * n);

function sign(flow, l, t, d) {
  return crypto.createHmac("sha256", Buffer.from(SECRET, "hex"))
    .update(`${flow}|${l}|${t}|${d}`).digest("hex");
}
function mkUrl(flow, l, host, port, { t, bad, path } = {}) {
  const tt = t ?? Math.floor(Date.now() / 1000);
  const d = Buffer.from(`${host}:${port}`).toString("base64url");
  let s = sign(flow, l, tt, d);
  if (bad) s = s.replace(/^./, s[0] === "0" ? "1" : "0");
  return `${path || "/f/" + flow}?l=${l}&t=${tt}&d=${d}&s=${s}`;
}
const newFlow = () => crypto.randomBytes(16).toString("hex");

// ---- frames
const frame = (type, len) => { const b = Buffer.alloc(1 + len); b[0] = type; return b; };
function dataFrame(seq, payload) {
  const b = frame(1, 4 + payload.length);
  b.writeUInt32BE(seq, 1);
  payload.copy(b, 5);
  return b;
}
function finFrame(total) { const b = frame(3, 4); b.writeUInt32BE(total, 1); return b; }

// ---- raw HTTP status of a ws upgrade attempt
function httpStatus(pathQuery) {
  const [host, port] = RELAY.split(":");
  return new Promise((resolve) => {
    const req = http.request({
      host, port: +port, path: pathQuery, method: "GET",
      headers: {
        Connection: "Upgrade", Upgrade: "websocket",
        "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": crypto.randomBytes(16).toString("base64"),
      },
    });
    req.on("response", (res) => { resolve(res.statusCode); res.resume(); });
    req.on("upgrade", (res, sock) => { resolve(res.statusCode); sock.destroy(); });
    req.on("error", () => resolve(-1));
    req.end();
  });
}

// ---- minimal bonded client (one flow, several WS)
class Peer {
  constructor(flow, host, port, nlinks) {
    this.flow = flow; this.host = host; this.port = port;
    this.ws = []; this.okCount = 0; this.errs = [];
    this.rx = new Array(nlinks).fill(0);   // DATA bytes received per link (for ACKs)
    this.cum = 0; this.buf = new Map(); this.down = [];
    this.finTotal = null; this.closes = new Map();
    this.dirty = false;
    this.waiters = [];
    this.perLinkChunks = new Array(nlinks).fill(0);
    for (let i = 0; i < nlinks; i++) this.open(i);
    this.ackTimer = setInterval(() => { if (this.dirty) this.ack(); }, 40);
  }
  open(i) {
    const ws = new WebSocket(`ws://${RELAY}${mkUrl(this.flow, i, this.host, this.port)}`);
    ws.binaryType = "arraybuffer";
    ws.onmessage = (ev) => this.onMsg(i, Buffer.from(ev.data));
    ws.onclose = (ev) => { this.closes.set(i, ev.code); this.poke(); };
    ws.onerror = () => {};
    this.ws[i] = ws;
  }
  poke() { for (const w of this.waiters.splice(0)) w(); }
  async until(cond, ms, what) {
    const end = Date.now() + ms;
    while (!cond()) {
      if (Date.now() > end) throw new Error("timeout waiting for " + what);
      await new Promise((r) => { this.waiters.push(r); setTimeout(r, 50); });
    }
  }
  onMsg(i, m) {
    switch (m[0]) {
      case 5: this.okCount++; break;
      case 4: this.errs.push(m.subarray(1).toString()); break;
      case 6: { const p = frame(7, 8); m.copy(p, 1, 1, 9); this.ws[i].send(p); break; }
      case 1: {
        const seq = m.readUInt32BE(1);
        this.rx[i] += m.length - 5;
        this.perLinkChunks[i]++;
        if (seq >= this.cum && !this.buf.has(seq)) {
          this.buf.set(seq, m.subarray(5));
          while (this.buf.has(this.cum)) { this.down.push(this.buf.get(this.cum)); this.buf.delete(this.cum); this.cum++; }
        }
        this.dirty = true;
        break;
      }
      case 3: this.finTotal = m.readUInt32BE(1); this.ack(); break;
      default: break;
    }
    this.poke();
  }
  ack() {
    this.dirty = false;
    const n = this.rx.length;
    const b = frame(2, 5 + n * 9);
    b.writeUInt32BE(this.cum, 1);
    b[5] = n;
    this.rx.forEach((v, i) => { b[6 + i * 9] = i; b.writeBigUInt64BE(BigInt(v), 7 + i * 9); });
    const w = this.ws.find((x) => x.readyState === 1);
    if (w) w.send(b);
  }
  sendOn(i, buf) { this.ws[i].send(buf); }
  stop() { clearInterval(this.ackTimer); for (const w of this.ws) try { w.close(); } catch { /* */ } }
}

// ---- local TCP echo server (half-close aware)
function startEcho() {
  const srv = net.createServer({ allowHalfOpen: true }, (s) => {
    s.on("data", (d) => s.write(d));
    s.on("end", () => s.end());
    s.on("error", () => {});
  });
  return new Promise((r) => srv.listen(0, "127.0.0.1", () => r(srv)));
}

async function testEcho(echoPort) {
  const flow = newFlow();
  const peer = new Peer(flow, "127.0.0.1", echoPort, 2);
  try {
    await peer.until(() => peer.okCount >= 2 || peer.errs.length, 10000, "OK");
    check("OK arrives on both WS", peer.okCount === 2 && !peer.errs.length, peer.errs.join(","));
    if (peer.okCount < 2) return;

    // 60 random-size chunks; send in shuffled order across the 2 WS with jitter
    const chunks = [];
    for (let i = 0; i < 60; i++) chunks.push(crypto.randomBytes(1 + rnd(65536)));
    const total = Buffer.concat(chunks);
    const order = chunks.map((_, i) => i);
    for (let g = 0; g < order.length; g += 8) {
      const part = order.slice(g, g + 8).sort(() => Math.random() - 0.5);
      order.splice(g, part.length, ...part);
    }
    for (const seq of order) {
      peer.sendOn(rnd(2), dataFrame(seq, chunks[seq]));
      if (rnd(4) === 0) await sleep(rnd(15));
    }
    peer.sendOn(0, finFrame(chunks.length));

    await peer.until(() => peer.finTotal !== null || peer.errs.length, 20000, "FIN from relay");
    check("FIN arrives from relay (origin EOF after half-close)", peer.finTotal !== null && !peer.errs.length, peer.errs.join(","));
    await peer.until(() => peer.cum >= peer.finTotal, 10000, "all down data");
    const got = Buffer.concat(peer.down);
    check("downstream echo equals upstream data (reordered across 2 WS)",
      got.length === total.length && got.equals(total), `${got.length}/${total.length} bytes, ${peer.finTotal} chunks`);
    check("downstream used both WS", peer.perLinkChunks.every((c) => c > 0), `chunks per link ${peer.perLinkChunks.join("/")}`);
    peer.ack();
    await peer.until(() => peer.closes.size === 2, 10000, "WS close");
    check("both WS closed 1000 after both FIN", [...peer.closes.values()].every((c) => c === 1000),
      [...peer.closes.values()].join(","));
  } catch (e) {
    check("echo flow", false, e.message);
  } finally {
    peer.stop();
  }
}

async function testHalfCloseFinOnly(echoPort) {
  // zero data: FIN(0) must half-close; echo ends; relay sends FIN(0)
  const peer = new Peer(newFlow(), "127.0.0.1", echoPort, 1);
  try {
    await peer.until(() => peer.okCount >= 1 || peer.errs.length, 10000, "OK");
    peer.sendOn(0, finFrame(0));
    await peer.until(() => peer.finTotal !== null, 10000, "FIN");
    check("FIN(0) both ways", peer.finTotal === 0);
    peer.ack();
    await peer.until(() => peer.closes.size === 1, 10000, "close");
    check("closed 1000 after empty flow", peer.closes.get(0) === 1000);
  } catch (e) {
    check("empty flow", false, e.message);
  } finally { peer.stop(); }
}

async function testConnectFail() {
  const peer = new Peer(newFlow(), "127.0.0.1", 1, 1);
  try {
    await peer.until(() => peer.errs.length > 0, 15000, "ERR");
    check("connect failure -> ERR 'connect: ...'", /^connect: /.test(peer.errs[0]), peer.errs[0]);
    await peer.until(() => peer.closes.size === 1, 5000, "close");
    check("connect failure closes WS 1011", peer.closes.get(0) === 1011, String(peer.closes.get(0)));
  } catch (e) {
    check("connect failure", false, e.message);
  } finally { peer.stop(); }
}

async function testHttp() {
  const f = newFlow();
  check("bad signature -> 403", (await httpStatus(mkUrl(f, 0, "127.0.0.1", 80, { bad: true }))) === 403);
  check("stale timestamp -> 403",
    (await httpStatus(mkUrl(f, 0, "127.0.0.1", 80, { t: Math.floor(Date.now() / 1000) - 1000 }))) === 403);
  check("port 25 -> 403", (await httpStatus(mkUrl(f, 0, "127.0.0.1", 25))) === 403);
  check("other path -> 404", (await httpStatus("/x")) === 404);
  check("/f/ with bad flow -> 404", (await httpStatus("/f/zzz?l=0")) === 404);
}

const echo = await startEcho();
const echoPort = echo.address().port;
await testHttp();
await testEcho(echoPort);
await testHalfCloseFinOnly(echoPort);
await testConnectFail();
echo.close();
console.log(failed ? `\n${failed} FAILED` : "\nALL PASSED");
process.exit(failed ? 1 : 0);
