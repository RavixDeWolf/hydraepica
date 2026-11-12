// TIK-1509 slices 2/3 page logic, run against the REAL index.html functions with a stub DOM. node test_browser_page.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "..", "harness", "stage", "index.html"), "utf8");
const grab = (name) => {                      // the source of one top-level function (or a block starting at `let`) by brace matching
  const i = html.indexOf(name); if (i < 0) throw new Error("not in index.html: " + name);
  let j = html.indexOf("{", i), d = 0; for (; j < html.length; j++) { if (html[j] === "{") d++; if (html[j] === "}" && --d === 0) break; }
  return html.slice(i, j + 1);
};
const src = ["let bLive = null, bBusy = false;", grab("async function loadBrowser"), grab("function browserLiveToggle"), "let bAdmin = null;",
             grab("async function browserShowOpen"), grab("async function browserOpen"),
             "let curView = null;", grab("function switchView")].join("\n");
let fails = 0, n = 0;
const check = (name, ok, d) => { n++; if (!ok) { fails++; console.log("FAIL", name, d === undefined ? "" : JSON.stringify(d)); } else console.log("ok  ", name); };
function world() {
  const els = {};
  const el = (id) => els[id] || (els[id] = { id, textContent: "", src: "", value: "", disabled: false, style: {}, _attr: {},
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); },
                 toggle(c, f) { if (f) this._s.add(c); else this._s.delete(c); } },
    setAttribute(k, v) { this._attr[k] = v; } });
  const w = { calls: [], timers: [], cleared: [], hidden: false, responses: {} };
  const ctx = { AGENT: "Atlas", document: { getElementById: el, querySelectorAll: () => [], get hidden() { return w.hidden; } },
    camStop() {}, loadSettings() {}, loadModes() {}, loadIot() {}, rememberPlace() {}, H: { "X-Init-Data": "x" }, JSON,
    String, Boolean, Promise, Error, RegExp,
    api: async (p, o) => { w.calls.push([p, o && o.method || "GET", o && o.body]); const r = w.responses[p]; if (r instanceof Error) throw r; return r; },
    setInterval: (f, ms) => { w.timers.push({ f, ms }); return w.timers.length; },
    clearInterval: (id) => { w.cleared.push(id); }, setTimeout: (f, ms) => { w.after = ms; return 1; } };
  vm.createContext(ctx); vm.runInContext(src, ctx);
  return { w, el, run: (c) => vm.runInContext(c, ctx), ctx };
}
let finished = false;
process.on("exit", () => { if (!finished) { console.log("FAIL the test did not run to the end (a promise never settled)"); process.exitCode = 1; } });
(async () => {
  let t = world();
  t.w.responses["/api/browser"] = { title: "T", url: "https://e.com/", src: "data:image/png;base64,AA" };
  await t.run("loadBrowser()");
  check("L1 a good frame shows title+url, sets the picture, not stale", t.el("b-title").textContent === "T — https://e.com/" && t.el("b-shot").src.startsWith("data:image/png") && !t.el("b-shot").classList.contains("stale"));
  t.w.responses["/api/browser"] = new Error("Atlas browser not answering (view)");
  await t.run("loadBrowser()");
  check("L2 a hang dims the OLD picture and says it is old and not answering", t.el("b-shot").classList.contains("stale") && /not answering/.test(t.el("b-title").textContent) && /OLD/.test(t.el("b-title").textContent), t.el("b-title").textContent);
  check("L2b ... the old src is kept (not blanked as if there were nothing) but flagged", t.el("b-shot").src.startsWith("data:image/png"));
  t.w.responses["/api/browser"] = new Error("503");
  await t.run("loadBrowser()");
  check("L3 any other failure says unavailable + OLD", /Browser unavailable/.test(t.el("b-title").textContent) && t.el("b-shot").classList.contains("stale"));
  t.w.responses["/api/browser"] = { title: null, url: null, src: null, note: "no page open" };
  await t.run("loadBrowser()");
  check("L4 recovery clears the stale flag and shows the note", !t.el("b-shot").classList.contains("stale") && t.el("b-title").textContent === "no page open");
  // overlap guard
  t = world(); const releases = [];
  t.ctx.api = async (p) => { t.w.calls.push([p]); await new Promise(r => releases.push(r)); return { title: "", url: "u", src: "s" }; };
  const a = t.run("loadBrowser()"); const b2 = t.run("loadBrowser()"); await new Promise(r => setImmediate(r)); releases.forEach(r => r()); await a; await b2;
  check("L5 a second poll while one is in flight is skipped (no pile-up)", t.w.calls.length === 1, t.w.calls);
  // live toggle
  t = world(); t.w.responses["/api/browser"] = { title: "", url: "u", src: "s" };
  t.run("browserLiveToggle()"); await new Promise(r => setImmediate(r));
  check("V1 turning Live on polls now and then every 2 s", t.w.timers.length === 1 && t.w.timers[0].ms === 2000 && t.w.calls.length === 1);
  check("V2 the switch shows on + aria-checked true", t.el("b-live").classList.contains("on") && t.el("b-live")._attr["aria-checked"] === "true");
  t.run("browserLiveToggle()");
  check("V3 turning it off clears the timer", t.w.cleared.length === 1 && !t.el("b-live").classList.contains("on") && t.el("b-live")._attr["aria-checked"] === "false");
  t.run("browserLiveToggle(false)"); check("V4 off when already off clears nothing more", t.w.cleared.length === 1);
  t.run("browserLiveToggle(true)"); await new Promise(r => setImmediate(r)); t.w.calls.length = 0; t.w.hidden = true; t.w.timers[t.w.timers.length - 1].f(); await new Promise(r => setImmediate(r));
  check("V5 a hidden app does not poll", t.w.calls.length === 0);
  t.w.hidden = false; t.w.timers[t.w.timers.length - 1].f(); await new Promise(r => setImmediate(r));
  check("V6 a visible app does", t.w.calls.length === 1);
  // leaving the Browser tab stops Live; entering it loads once and shows the URL box for Rav
  t = world(); t.w.responses["/api/browser"] = { title: "", url: "u", src: "s" }; t.w.responses["/api/me"] = { settings_admin: true };
  t.run('switchView("browser")'); await new Promise(r => setImmediate(r)); t.run("browserLiveToggle(true)");
  t.run('switchView("stage")');
  check("W1 leaving the Browser tab turns Live off (no hidden polling in the background)", t.w.cleared.length === 1 && !t.el("b-live").classList.contains("on"));
  t.run('switchView("stage")'); check("W2 switching between other tabs does not touch the timer", t.w.cleared.length === 1);
  // the URL box is Rav's
  t = world(); t.w.responses["/api/me"] = { person: "rav", settings_admin: true };
  await t.run("browserShowOpen()"); check("O1 Rav sees the URL box", t.el("b-open").style.display === "flex");
  t = world(); t.w.responses["/api/me"] = { person: "jo", settings_admin: false };
  await t.run("browserShowOpen()"); check("O2 anyone else does not", t.el("b-open").style.display === "none");
  t = world(); t.w.responses["/api/me"] = new Error("401");
  await t.run("browserShowOpen()"); check("O3 a failed /api/me hides it (fail closed)", t.el("b-open").style.display === "none");
  // open
  t = world(); t.el("b-url").value = "  https://example.com/x  "; t.w.responses["/api/browser/open"] = { ok: true, url: "https://example.com/x", tab: "T1" };
  await t.run("browserOpen()");
  const post = t.w.calls.find(c => c[0] === "/api/browser/open");
  check("P1 posts the trimmed url as JSON with POST", post && post[1] === "POST" && JSON.parse(post[2]).url === "https://example.com/x", post);
  check("P2 clears the box, says it opened, schedules a refresh", t.el("b-url").value === "" && /Opened in a new tab/.test(t.el("b-title").textContent) && t.w.after === 1500);
  check("P3 the button is re-enabled", t.el("b-open-go").disabled === false);
  t = world(); t.el("b-url").value = "javascript:alert(1)"; t.w.responses["/api/browser/open"] = new Error("need url= (http or https only)");
  await t.run("browserOpen()");
  check("P4 a server refusal is shown and the text stays for editing", /Not opened: need url/.test(t.el("b-title").textContent) && t.el("b-url").value === "javascript:alert(1)" && t.el("b-open-go").disabled === false);
  t = world(); t.el("b-url").value = "   "; await t.run("browserOpen()");
  check("P5 an empty box sends nothing", t.w.calls.length === 0);
  finished = true; console.log(n + " checks, " + fails + " failed"); process.exit(fails ? 1 : 0);
})();
