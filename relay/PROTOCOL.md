# link-bond relay: ONE connection over ALL links at once (spec v1)

> The wire protocol below is what bond.py and relay/worker.js speak. Client policy details (which hosts are bonded,
> learning, timeouts) have moved on since - bond.py is the reference for those.

Goal: a single TCP flow (one video stream, one download, one upload) gets ~the SUM of all phone links.
Every link has its own public IP, so the stream is split on the PC, carried over every link to a relay on
Cloudflare (Worker + Durable Object), put back together there, and sent to the site from one place. Downstream
is the same in reverse. This is the user's core requirement: "束ねて最高の速さ".

Two parts, built in parallel, must interoperate exactly through this protocol:
- A. relay/ : Cloudflare Worker (plain JS module, no build step) + Durable Object class `Flow`.
- B. bond.py client (Python 3.9+ stdlib only, asyncio) + test harness.

## 1. Transport

Per bonded flow, the client opens ONE WebSocket per healthy link (each over its own bound socket, TLS to the relay
host) to:

    GET {relay_url}/f/{flow}?l={link}&t={unix_s}&d={dst_b64url}&s={sig}

- `flow`: 32 lowercase hex chars (16 random bytes), new per flow.
- `link`: small integer 0..15 identifying the link inside this flow (client chooses; stable for a link).
- `t`: client's unix time in seconds (int).
- `d`: base64url (no padding) of UTF-8 "host:port" (host = DNS name or IPv4 literal).
- `s`: lowercase hex HMAC-SHA256(secret_bytes, f"{flow}|{link}|{t}|{d}") where secret_bytes = bytes.fromhex(secret).
- Worker `fetch`: path must be /f/<32hex>; Upgrade: websocket; |now - t| <= 300; sig matches (constant-time
  compare); port 1..65535 and not in {25, 465, 587}; else 403 / 400 (plain response, no detail). Then
  `env.FLOW.get(env.FLOW.idFromName(flow), {locationHint: "apac"}).fetch(request)`.
- Every other path -> 404. No CORS, no HTML, nothing else served.
- Secret: Worker secret `BOND_SECRET` (64 hex chars). Client: file `relay.local.txt` next to bond.py:
  line 1 = relay URL (wss://host or ws://127.0.0.1:8787 for tests), line 2 = secret hex. Missing file = feature off.
  The file is gitignored; the URL and secret must never be logged or shown.

## 2. Frames (each WebSocket binary message = exactly one frame; big-endian)

| type | name | body |
|---|---|---|
| 1 | DATA | u32 seq, then payload (1..65536 bytes) |
| 2 | ACK  | u32 cum (every seq < cum received), u8 n, n x (u8 link, u64 bytes_received_on_that_link_total) |
| 3 | FIN  | u32 total (number of DATA chunks this side sent in total = final seq + 1; 0 if none) |
| 4 | ERR  | UTF-8 reason (short). Aborts the whole flow. |
| 5 | OK   | (relay -> client only) empty. Destination TCP connected. Sent on every WS of the flow once connected (also to WSs that join later). |
| 6 | PING | u64 sender_time_ms |
| 7 | PONG | u64 the same value echoed |

- Seq numbers are per direction (client->relay "up", relay->client "down"), start at 0, one per DATA chunk.
- A receiver drops DATA it already has (seq < cum or already buffered) but still counts its bytes on that link.
- `bytes_received_on_that_link_total` counts DATA payload bytes received on that link's WS (duplicates included).
- Text messages are ignored.

## 3. Relay (Durable Object `Flow`)

- On the first WS: accept (WebSocketPair, `server.accept()` - NOT hibernation API, we need the socket alive),
  `connect({hostname, port})` from `cloudflare:sockets`, await `socket.opened` (timeout 8 s). On failure: send ERR
  ("connect: <reason>") on all WSs and close them (code 1011). On success send OK to all.
- Join: later WSs with the same flow + a link number are added (a new WS for a link number replaces the old one).
- Downstream: read the origin socket (`socket.readable.getReader()`), cut into chunks sized for the link it will go
  on (see 5), send with the scheduler. Stop reading while the scheduler has no room (backpressure).
- Upstream: reorder DATA by seq, write in order to `socket.writable` (await writer.write). On peer FIN with total N,
  after all < N written: `writer.close()` (half close).
- Origin EOF: send FIN(total) to the client (on every live WS).
- Both directions finished (FIN sent and acked-all, FIN received and all written) -> close WSs 1000, close socket.
- No live WS for 60 s -> close everything. No data either way for 10 min -> close.
- Limits: reorder buffer 32 MiB max (exceed -> ERR "reorder overflow"). Never more than one origin socket per DO.
- Works within: 30 s CPU budget per incoming message (reset by every incoming message), 128 MB memory.

## 4. Client (bond.py)

- `relay_cfg()` reads relay.local.txt (cache, re-read when mtime changes).
- `bondable(host, port, ip)`: relay configured AND >= 2 healthy, non-suspect links AND host not pinned (is_pinned)
  AND not blocked AND ip not in Cloudflare ranges (fetched from https://www.cloudflare.com/ips-v4 once a day over
  any link, cached in cf-ips.json; until known: do not bond) AND (host matches bulk patterns OR is in learned bulk).
  - Built-in bulk patterns (suffix match): googlevideo.com, fbcdn.net, video.xx.fbcdn.net, cdninstagram.com,
    akamaized.net, ttvnw.net, tiktokcdn.com, dl.google.com, gvt1.com, steamcontent.com, download.microsoft.com,
    githubusercontent.com, objects.githubusercontent.com. Plus bulk.local.txt (user additions).
  - Learned: a direct tunnel that moved >= 8 MB down or up -> its host (registered domain level: last two labels,
    three for known 2nd-level like co.jp) is added to bulk-learned.json {host: last_seen}; forget after 30 days;
    max 500.
  - relay failures for a destination host (ERR / no OK) -> that host not bonded for 10 min.
  - relay itself failing (HTTP != 101 on all links, e.g. quota) -> relay off for 10 min, logged once.
- `open_via` tries the bonded path first when bondable: open WS on every eligible link in parallel (connect over
  the link with the same mk_sock/link_connect as today, resolve the relay host with resolve()), TLS
  server_hostname = relay host, WS handshake (RFC 6455, client masks frames). Wait for the first OK up to 6 s.
  Success -> return (BOND_LINK, reader, writer) where BOND_LINK is a pseudo link dict like SYSTEM_LINK
  ("name": "bonded") so tunnel()/pipe() work unchanged. Failure -> normal race_connect path (no delay beyond that).
- A WS that is not up when OK arrives keeps trying in the background; a link that becomes healthy later during the
  flow gets a WS added (check every 3 s). A link removed/cut: its WS closed, chunks reinjected.
- Accounting: bytes arriving/sent on each real link's WS add to that link's bytes/b_down/b_up/data_at like pipe().
- status.json gets "bond": {"on": bool, "flows": n, "bytes": total payload via relay, "why_off": str|None}.

## 5. Scheduler (identical rule on both sides; this is the part that decides speed)

Per link state: `rtt` (s, EWMA 1/8 from PING/PONG every 1 s; init: client = link["rtt"]/1000 or 0.3, relay 0.3),
`rate` (bytes/s, from ACK progress: bytes newly acked on that link per 250 ms window, EWMA 1/4, init 256 KiB/s,
floor 8 KiB/s), `sent`, `acked` (from ACK per-link totals), `inflight = sent - acked`, `last_heard` (any message).

    window(l) = clamp(rate * (rtt + 0.1) * 2, 64 KiB, 16 MiB)
    eta(l, n) = (inflight + n) / rate + rtt / 2

Send rule for the next chunk (length n): among LIVE links pick the one with the smallest eta(l, n).
If that link's inflight + n > window(l): WAIT for an ACK (do NOT spill to a worse link). This is what keeps a
0.6 Mbps / 1.7 s link from slowing a 60 Mbps link: the slow link only gets a chunk when it would actually arrive
first. Chunk size for the chosen link: clamp(rate * 0.05, 4 KiB, 64 KiB) (read only that much from the queue).

Reinjection (check every 100 ms): a chunk is OVERDUE when not acked after `rtt*2 + inflight/rate + 0.5 s` of the
link it went on. If it is among the 8 lowest unacked seqs, resend it on the best other live link (at most once per
link per chunk; window rule applies). Receiver dedups.

Liveness: a link is DEAD when nothing was heard on it for max(2 s, 4*rtt) while we expect traffic (pings run every
1 s). Dead -> stop scheduling on it, all its unacked chunks go to the front of the send queue (seq order). If heard
again -> live again (rate restarts at half the old value). WS closed -> remove link; client re-opens (3 s).

ACKs: the receiver sends ACK on the live link with the smallest rtt: every 40 ms while data arrives, and at once after
every 256 KiB received, and on every FIN.

Sender keeps chunks until cum-acked. Total unacked bytes bounded by sum(window). Relay stops reading the origin and the
client stops reading the app socket (drain() waits) when no link has room.

## 6. Tests (gates; nothing ships before all pass)

Local, no Cloudflare: `npx wrangler dev` runs relay/ on 127.0.0.1:8787 (relay.local.txt -> ws://127.0.0.1:8787).
Link simulator: each fake link = a local TCP shaping proxy in front of the relay (per direction: rate limit, fixed
one-way delay, optional random stalls, optional death = close, optional blackhole = stop forwarding silently).
The client's links in the test connect to their proxy port instead of the relay host. Origin = a local TCP server
that sends N MB of random data (and one that receives N MB and returns its sha256). NOTE: Workers in production refuse
localhost destinations; in wrangler dev it works. If it does not, use a test-only env var on the Worker.

1. Two links 20 Mbit/s + 20 Mbit/s, 40 ms: 40 MB down -> >= 34 Mbit/s. Same for up.
2. Asymmetric: 60 Mbit/s 40 ms + 0.6 Mbit/s 850 ms one-way with random 0.5-1.5 s stalls: 60 MB down -> >= 0.95 x the
   speed of the 60 Mbit/s link alone through the same relay.
3. Link death: 2 x 20 Mbit/s, one link dies (close) at 30 %, another run blackholed at 30 %: completes, sha256 equal,
   no gap in delivery longer than 3 s.
4. All links gone 10 s then back: the flow survives and completes.
5. Small: 100 requests of 10 KB through a bonded flow work (correctness), FIN both ways works (half-close).
