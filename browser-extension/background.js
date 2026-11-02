// link-bond downloads: hand every browser download to link-bond, which fetches it over ALL connections at once.
// Safe by design: when link-bond is not running, refuses a URL, or fails, the browser downloads the file as usual.
"use strict";
const LB = "http://127.0.0.1:1081";
const ours = new Set();          // URLs we gave back to the browser ourselves: never intercept them again

async function linkBondUp() {
  try { return (await fetch(LB + "/status.json", { cache: "no-store" })).ok; } catch (e) { return false; }
}

function giveBack(url) {         // let the browser download it the normal way
  ours.add(url);
  chrome.downloads.download({ url });
}

async function follow(job, url) {  // if link-bond fails, fall back to a normal download
  for (let i = 0; i < 3600; i++) {
    await new Promise(r => setTimeout(r, 2000));
    let j = null;
    try {
      const s = await (await fetch(LB + "/status.json", { cache: "no-store" })).json();
      j = (s.downloads || []).find(d => d.id === job);
    } catch (e) { return giveBack(url); }
    if (!j || j.state === "done" || j.state === "cancelled") return;
    if (j.state === "error") return giveBack(url);
  }
}

chrome.downloads.onCreated.addListener(async (item) => {
  const url = item.finalUrl || item.url || "";
  if (ours.has(url)) { ours.delete(url); return; }
  if (!/^https?:\/\//i.test(url)) return;               // blob:, data:, file: stay with the browser
  if (!(await linkBondUp())) return;                     // link-bond not running: nothing changes
  try { await chrome.downloads.cancel(item.id); await chrome.downloads.erase({ id: item.id }); } catch (e) {}
  let cookie = "";
  try { cookie = (await chrome.cookies.getAll({ url })).map(c => `${c.name}=${c.value}`).join("; "); } catch (e) {}
  const body = {
    url, cookie, from_browser: true,
    referer: item.referrer || "",
    user_agent: navigator.userAgent,
    filename: (item.filename || "").split(/[\\/]/).pop(),
  };
  let job = null;
  try {
    const r = await fetch(LB + "/api/download", {
      method: "POST", headers: { "X-LinkBond": "1", "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (r.ok) job = (await r.json()).job;
  } catch (e) {}
  if (!job) return giveBack(url);                        // refused (e.g. a local address): normal download
  follow(job, url);
});
