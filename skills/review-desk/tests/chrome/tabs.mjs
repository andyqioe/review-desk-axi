// Open N desk tabs in one Chrome profile (each new tab becomes the active one, so earlier tabs are hidden),
// report which ones loaded, and check that a hidden tab shows a message posted meanwhile once it is shown.
// Exits 1 when a tab did not load or missed the message. Usage: node tabs.mjs <desk url> <count> <chrome debug port>
const [url, count, port] = [process.argv[2], +process.argv[3], process.argv[4]];
const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0; const waiting = new Map();
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); } });
const send = (method, params = {}, sessionId) => new Promise((r) => { const i = ++id; waiting.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const targets = [];
for (let i = 0; i < count; i++) {
  // each new tab becomes the active one, so earlier tabs go hidden as in a real window
  const t = await send("Target.createTarget", { url });
  targets.push(t.result.targetId);
  await sleep(700);
}
await sleep(4000);
const rows = [];
let failed = 0;
for (const [i, tid] of targets.entries()) {
  const a = await send("Target.attachToTarget", { targetId: tid, flatten: true });
  const sid = a.result.sessionId;
  const v = await send("Runtime.evaluate", { expression: "JSON.stringify({title: document.title, vis: document.visibilityState, url: location.href.slice(0, 40)})", returnByValue: true }, sid);
  const got = v.result?.result?.value;
  if (!got || !JSON.parse(got).title) failed++;
  rows.push(`tab ${i + 1}: ${got ?? JSON.stringify(v.result ?? v.error)}`);
}
console.log(rows.join("\n"));
// a message posted while tab 1 is hidden shows up once tab 1 is shown again
const u = new URL(url), sidPath = u.pathname.split("/")[2], token = u.searchParams.get("t");
const text = `posted-while-hidden-${Date.now()}`;
await fetch(new URL(`/api/${sidPath}/message`, url), { method: "POST", headers: { "X-Desk-Token": token, "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
await sleep(1500);
const first = (await send("Target.attachToTarget", { targetId: targets[0], flatten: true })).result.sessionId;
const before = await send("Runtime.evaluate", { expression: `document.body.innerText.includes(${JSON.stringify(text)})`, returnByValue: true }, first);
await send("Target.activateTarget", { targetId: targets[0] });
await sleep(2500);
const after = await send("Runtime.evaluate", { expression: `JSON.stringify({vis: document.visibilityState, shown: document.body.innerText.includes(${JSON.stringify(text)})})`, returnByValue: true }, first);
if (!JSON.parse(after.result.result.value).shown) failed++;
console.log("tab 1 hidden, message on screen:", before.result.result.value, "| after showing tab 1:", after.result.result.value);
const health = await (await fetch(new URL("/health", url))).json();
console.log("server clients:", JSON.stringify(health.clients));
ws.close();
if (failed) { console.log(`FAIL: ${failed} check(s)`); process.exitCode = 1; } else console.log("PASS");
