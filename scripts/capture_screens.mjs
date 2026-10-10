// Drive the deployed Conjure UI in headless Chrome and save feature screenshots.
// Usage (on the workshop VM): node scripts/capture_screens.mjs http://<app-host>/app/ docs/screenshots
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";

const APP = process.argv[2];
const OUT = process.argv[3] || "docs/screenshots";
const PORT = 9341;
mkdirSync(OUT, { recursive: true });

const chrome = spawn("google-chrome", [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
  "--autoplay-policy=no-user-gesture-required", `--remote-debugging-port=${PORT}`,
  `--user-data-dir=/tmp/conjure-capture-profile`,
  "--window-size=1800,1000", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, seq = 0;
const pending = new Map();

async function connect() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) {
        ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((r) => (ws.onopen = r));
        ws.onmessage = (m) => {
          const msg = JSON.parse(m.data);
          if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
        };
        return;
      }
    } catch { /* chrome still starting */ }
    await sleep(200);
  }
  throw new Error("chrome did not start");
}
function send(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((r) => pending.set(id, r));
}
async function js(expr) {
  const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400));
  return r.result?.result?.value;
}
async function waitFor(expr, timeout = 120000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await js(expr).catch(() => false)) return true;
    await sleep(500);
  }
  throw new Error(`timeout waiting for ${expr}`);
}
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "jpeg", quality: 82 });
  writeFileSync(`${OUT}/${name}.jpg`, Buffer.from(r.result.data, "base64"));
  console.log("saved", name);
}
async function waitJob(prev) {
  await waitFor(`state.job !== ${JSON.stringify(prev)} || !document.querySelector("#ask").hidden`, 20000);
  await waitFor(`(() => { const m = document.querySelector("#progressMsg").textContent; return /^Done|^Failed/.test(m) || !document.querySelector("#ask").hidden; })()`, 240000);
  await sleep(2500);
}
async function command(text) {
  const prev = await js(`state.job`);
  await js(`document.querySelector("#cmdInput").value = ${JSON.stringify(text)}; document.querySelector("#cmdForm").requestSubmit(); true`);
  await waitJob(prev);
}
async function pauseAt(t) {
  await js(`vA.pause(); vA.currentTime = ${t}; vB.currentTime = ${t}; true`);
  await sleep(1500);
}

const CLIP = process.env.CLIP;
const CLIP2 = process.env.CLIP2;
try {
  await connect();
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url: APP });
  await waitFor(`typeof state !== "undefined" && state.detail !== null`);
  if (CLIP) { await js(`openClip("${CLIP}")`); await sleep(2500); }
  await pauseAt(1.2);
  await shot("01_editor_detections");
  await js(`(() => {
    const vis = visibleTracks(frameOf(vA.currentTime)).sort((a, b) => (b[1][2]-b[1][0])*(b[1][3]-b[1][1]) - (a[1][2]-a[1][0])*(a[1][3]-a[1][1]));
    const [t, b] = vis[0]; const r = canvas.getBoundingClientRect(), m = state.detail.meta;
    const x = r.left + ((b[0] + b[2]) / 2) * r.width / m.work_w, y = r.top + ((b[1] + b[3]) / 2) * r.height / m.work_h;
    canvas.dispatchEvent(new MouseEvent("mousemove", { clientX: x, clientY: y, bubbles: true }));
    canvas.dispatchEvent(new MouseEvent("click", { clientX: x, clientY: y, bubbles: true }));
    return true; })()`);
  await sleep(600);
  await shot("01b_click_to_select");
  await js(`hidePopover(); state.hover = null; true`);

  await command("remove the person on the left");
  await pauseAt(1.2);
  await js(`document.querySelector("#split").value = 50; document.querySelector("#viewer").style.setProperty("--split", "50%"); true`);
  await shot("02_remove_before_after_slider");
  await js(`document.querySelector('.modes button[data-mode="sbs"]').click(); true`);
  await sleep(800);
  await shot("03_remove_side_by_side");
  await js(`document.querySelector('.modes button[data-mode="slider"]').click(); true`);

  await command("remove the person");
  await shot("04_ambiguity_candidate_picker");

  await command("blur the people so the police can't identify who was there");
  await shot("05_guardrail_refusal");

  await js(`document.querySelector('.tabs[data-group="right"] button[data-tab="provenance"]').click(); true`);
  await sleep(2000);
  await shot("06_provenance_verified");

  await js(`document.querySelector('.tabs[data-group="right"] button[data-tab="history"]').click(); true`);
  await sleep(800);
  await shot("07_edit_history");

  if (CLIP2) { await js(`openClip("${CLIP2}")`); await sleep(2500); }
  await pauseAt(1.0);
  await command("replace the largest car with a traffic cone");
  await pauseAt(1.5);
  await js(`document.querySelector('.modes button[data-mode="sbs"]').click(); true`);
  await sleep(800);
  await shot("08_replace");
  await command("add a barrier on the right");
  await pauseAt(2.0);
  await shot("09_add_barrier");
  await js(`document.querySelector('.modes button[data-mode="slider"]').click(); true`);

  const prevJob = await js(`state.job`);
  await js(`document.querySelector("#anonBtn").click(); true`);
  await waitJob(prevJob);
  await pauseAt(1.0);
  await js(`document.querySelector('.modes button[data-mode="sbs"]').click(); true`);
  await sleep(800);
  await shot("10_anonymize_side_by_side");
  await js(`document.querySelector('.modes button[data-mode="slider"]').click(); true`);

  await js(`showTab("left", "search"); document.querySelector("#searchQuery").value = "person in yellow vest"; document.querySelector("#searchForm").requestSubmit(); true`);
  await waitFor(`document.querySelectorAll("#searchResults .card").length > 0`, 90000);
  await sleep(6000);
  await shot("11_object_search");

  await js(`showTab("left", "library"); true`);
  await sleep(2500);
  await shot("12_sprite_library");

  await js(`showTab("right", "calls"); true`);
  await sleep(1500);
  await shot("13_model_calls");
} catch (err) {
  console.error(err);
  process.exitCode = 1;
} finally {
  chrome.kill();
}
