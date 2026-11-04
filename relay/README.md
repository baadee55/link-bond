# link-bond relay

A small Cloudflare Worker plus a Durable Object (`Flow`) that lets link-bond carry **one** TCP connection over
**several** links at once. The client opens one WebSocket per link to `/f/<flow>`; the relay reorders the
upstream data, writes it to the destination from a single place, and splits the downstream data back across the
WebSockets with a rate/RTT-aware scheduler. Frame formats and the scheduler rules are defined in the link-bond
protocol spec (`BOND-SPEC.md`); this directory is the server side only.

Requests are authenticated with HMAC-SHA256 over `flow|link|unix_time|destination` using a shared secret.
Everything else answers 404. Ports 25, 465 and 587 are refused. Nothing is logged.

## Deploy

1. Put the real custom domain in `wrangler.jsonc` under `routes` (uncomment the placeholder). Keep the real
   domain out of the repository.
2. `npx wrangler deploy`
3. `npx wrangler secret put BOND_SECRET` and paste 64 hex characters (32 random bytes), e.g. from
   `node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"`.

The Durable Object uses the SQLite-backed class migration, so it works on the free plan.

## Client file `relay.local.txt`

Next to `bond.py`, two lines, git-ignored, never logged:

```
wss://relay.example.com
<the same 64 hex characters as BOND_SECRET>
```

For local tests the first line is `ws://127.0.0.1:8787`. A missing file turns the feature off.

## Local test

```
cd relay
# .dev.vars contains BOND_SECRET=<64 hex test value>   (git-ignored)
npx wrangler dev --local --port 8787
node smoke.mjs          # in a second terminal
```

`wrangler dev` allows connections to 127.0.0.1, so a local TCP server works as the destination. A deployed Worker
cannot reach localhost destinations.
