// The dashboard page in Playwright's Chromium, for tests/test_web_dashboard.py.
//   node web_dashboard_browser.js URL SHOTS SIZES: the page at each size ({name: [width, height]}): a screenshot of each (SHOTS/dashboard-
//     NAME.png, device scale factor 2) and, on stdout, {name: {tiles, rects, columns, scroll, scrollWidth, scrollHeight, status, errors}}.
//   node web_dashboard_browser.js URL --live: the page open at 800x600; prints {label} (the first tile's), waits for a line on stdin
//     (the test has changed the reading), then prints {label, same_page, errors} once the tile shows it, or after 15 s.
//   node web_dashboard_browser.js URL --arrive TITLE: the page open at 1024x768; prints the tile's state ({label, faded, opacity, shown,
//     needle}), waits for a line on stdin (the test has given it a reading, or taken it away), then, once its label changes, samples it
//     every frame for 1.5 s and prints {before, after, frames: [{opacity, number}], same_tile, errors}.
//   node web_dashboard_browser.js URL --arrange: the page at 1024x768 with a mouse: dragging to reorder, the drag's text zones, the grip
//     and the × on mouseover, the remove zone, the reset link, the preferences across reloads (corrupt, blocked, a tile new to them) and
//     the tiles' see-through look, a click soon after a drop, the focus after the × pressed from the keyboard; prints {step: what was
//     seen}.
//   node web_dashboard_browser.js URL --touch: the page at 390x844 with touch (CDP touch events): a long press picks a tile up (buzz,
//     grip and ×, remove zone) and moves it; a short tap is a click; a swipe scrolls; held at the bottom edge it scrolls to the page's
//     end only; prints {step: what was seen}.
//   node web_dashboard_browser.js URL --zoom: the page at 1024x768 with a mouse and keys: the zoom cursors, a click zooming a tile and
//     back, Esc; zoomed, prints {label} (Temperature's, beside the tile shown), waits for a line on stdin (the test has changed its
//     reading), then pages with the keys, mouse drags and wheel deltaX, past both ends, and drags across the text; prints {step: what
//     was seen}; with a single tile, no zoom.
//   node web_dashboard_browser.js URL --zoom-touch: the page at 390x844 with touch: a tap zooms, swipes (slow, a flick, on the text)
//     page, a vertical swipe and a long press do nothing, a tap goes back where the page was scrolled; prints {step: what was seen}.
//   node web_dashboard_browser.js URL --fill CASES: for each case {name: [width, height, hidden titles]}, the page with those hidden;
//     prints {name: {rects, columns, scroll}}.
"use strict";
const { chromium } = require("playwright");
const readline = require("readline");

async function open(browser, url, width, height, options = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, ...(options.context || {}) });
  if (options.init) await context.addInitScript(options.init.fn, options.init.arg);
  const page = await context.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(url, { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0 && /updated/.test(document.getElementById("status").textContent),
                             null, { timeout: 15000 });
  return { context, page, errors };
}

async function sizes(browser, url, shots, wanted) {
  const report = {};
  for (const [name, [width, height]] of Object.entries(wanted)) {
    const { context, page, errors } = await open(browser, url, width, height);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(1200);   // (the gauges' rise from their floor when the page opens, RISE_MS)
    await page.screenshot({ path: `${shots}/dashboard-${name}.png`, fullPage: true });
    report[name] = { ...await page.evaluate(() => ({
      tiles: document.querySelectorAll(".tile").length,
      rects: [...document.querySelectorAll(".tile")].map((t) => t.getBoundingClientRect().toJSON()),
      columns: document.body.dataset.columns,
      scroll: document.body.dataset.scroll,
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
      status: document.getElementById("status").textContent,
    })), errors };
    await context.close();
  }
  process.stdout.write(JSON.stringify(report));
}

async function live(browser, url) {
  const { page, errors } = await open(browser, url, 800, 600);
  const label = () => page.evaluate(() => document.querySelector(".tile").getAttribute("aria-label"));
  await page.evaluate(() => { window.__mark = 1; document.querySelector(".tile").__mark = 1; });
  const before = await label();
  process.stdout.write(JSON.stringify({ label: before }) + "\n");
  await new Promise((resolve) => readline.createInterface({ input: process.stdin }).once("line", resolve));
  try {
    await page.waitForFunction((was) => document.querySelector(".tile").getAttribute("aria-label") !== was, before, { timeout: 15000 });
  } catch (e) { /* reported as the label unchanged */ }
  const same = await page.evaluate(() => window.__mark === 1);
  const sameTile = await page.evaluate(() => document.querySelector(".tile").__mark === 1);   // moved in place, not drawn again
  await page.waitForTimeout(1200);   // (the gauge's move to the new reading, MOVE_MS)
  const shown = await page.evaluate(() => [...document.querySelectorAll(".tile")[0].querySelectorAll("svg text")].map((t) => t.textContent).join("|"));
  process.stdout.write(JSON.stringify({ label: await label(), same_page: same, same_tile: sameTile, shown, errors }) + "\n");
}

async function arrive(browser, url, title) {
  const { page, errors } = await open(browser, url, 1024, 768);
  const state = () => page.evaluate((title) => {
    const tile = [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === title);
    const texts = [...tile.querySelectorAll("svg text")].map((t) => t.textContent);
    return { label: tile.getAttribute("aria-label"), faded: tile.classList.contains("faded"), opacity: Number(getComputedStyle(tile).opacity),
             shown: texts.join("|"), number: tile.querySelector("svg text.reading").textContent, needle: (tile.querySelector("polygon") || {getAttribute: () => null}).getAttribute("points") };
  }, title);
  await page.waitForTimeout(1300);   // (the opening's rise)
  await page.evaluate((title) => {
    window.__tile = [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === title);
  }, title);
  const before = await state();
  process.stdout.write(JSON.stringify(before) + "\n");
  await new Promise((resolve) => readline.createInterface({ input: process.stdin }).once("line", resolve));
  try {
    await page.waitForFunction((was) => window.__tile.getAttribute("aria-label") !== was || !window.__tile.isConnected, before.label,
                               { timeout: 15000 });
  } catch (e) { /* reported as the label unchanged */ }
  const frames = await page.evaluate(() => new Promise((resolve) => {   // the tile, frame by frame, as it brightens and its gauge moves
    const out = [], start = performance.now();
    const step = (now) => {
      const t = window.__tile;
      out.push({ opacity: Number(getComputedStyle(t).opacity), number: t.querySelector("svg text.reading").textContent });
      if (now - start < 1500) requestAnimationFrame(step); else resolve(out);
    };
    requestAnimationFrame(step);
  }));
  const sameTile = await page.evaluate((title) => window.__tile.isConnected &&
    [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === title) === window.__tile, title);
  process.stdout.write(JSON.stringify({ before, after: await state(), frames, same_tile: sameTile, errors }) + "\n");
}

// ---------- arranging ----------
const KEY = "ecobot.dashboard.v1";
const titles = (page) => page.evaluate(() => [...document.querySelectorAll("#grid .tile .title")].map((t) => t.textContent));
const tileOf = (page, title) => page.locator(".tile", { has: page.locator(".title", { hasText: new RegExp(`^${title}$`) }) });
const settle = (page) => page.waitForTimeout(350);   // (the reflow's easing, 220 ms)

async function at(page, title, fx, fy) {   // a point of a tile, as fractions of it
  const r = await tileOf(page, title).boundingBox();
  return [r.x + r.width * fx, r.y + r.height * fy];
}
async function centreOf(page, locator) {
  const r = await locator.boundingBox();
  return [r.x + r.width / 2, r.y + r.height / 2];
}
async function mouseDrag(page, [x0, y0], [x1, y1], during) {
  await page.mouse.move(x0, y0);
  await page.mouse.down();
  for (let i = 1; i <= 12; i++) await page.mouse.move(x0 + (x1 - x0) * i / 12, y0 + (y1 - y0) * i / 12);
  await page.waitForTimeout(300);
  const seen = during ? await during() : null;
  await page.mouse.up();
  await settle(page);
  return seen;
}
const selection = (page) => page.evaluate(() => { const s = String(window.getSelection()); window.getSelection().removeAllRanges(); return s; });
const statusText = (page) => page.evaluate(() => document.getElementById("status").textContent);
const stored = (page) => page.evaluate((key) => localStorage.getItem(key), KEY);
const midDrag = (page) => page.evaluate(() => ({
  zone: getComputedStyle(document.getElementById("remove")).transform, over: document.getElementById("remove").classList.contains("over"),
  dragging: document.body.classList.contains("dragging"),
  moved: [...document.querySelectorAll("#grid .tile:not(.lifted)")].filter((t) => t.style.transform).map((t) => t.querySelector(".title").textContent),
}));

async function arrangeMode(browser, url) {
  const out = {};
  const { context, page, errors } = await open(browser, url, 1024, 768);
  await page.waitForTimeout(1200);
  out.initial = await titles(page);
  // the look: the tile's own background clear, its foreground opaque
  out.look = await page.evaluate(() => {
    const tile = [...document.querySelectorAll(".tile")].find((t) => !t.classList.contains("faded"));
    const style = getComputedStyle(tile), frame = getComputedStyle(tile, "::before");
    return { background: style.backgroundColor, image: style.backgroundImage, opacity: style.opacity, title: getComputedStyle(tile.querySelector(".title")).color,
             reading: getComputedStyle(tile.querySelector("svg text.reading")).fill, frame: frame.borderTopColor, frameWidth: frame.borderTopWidth,
             grid: getComputedStyle(document.getElementById("grid")).backgroundColor, body: getComputedStyle(document.body).backgroundColor };
  });
  // mouseover: the grip and the × show on the tile under the mouse only
  await page.mouse.move(...await at(page, "Humidity", 0.5, 0.4));
  await page.waitForTimeout(300);
  out.hover = await page.evaluate(() => [...document.querySelectorAll(".tile")].slice(0, 3).map((t) => ({
    grip: getComputedStyle(t.querySelector(".grip")).opacity, close: getComputedStyle(t.querySelector(".close")).opacity })));
  // a drag from the gauge: Temperature to Atmospheric pressure's place, the others making room meanwhile
  out.during = await mouseDrag(page, await at(page, "Temperature", 0.85, 0.3), await at(page, "Atmospheric pressure", 0.5, 0.4), () => midDrag(page));
  out.dragged = await titles(page);
  out.transforms = await page.evaluate(() => [...document.querySelectorAll(".tile")].filter((t) => getComputedStyle(t).transform !== "none").length);
  out.storedAfterDrag = JSON.parse(await stored(page));
  // drags starting on the text: nothing moves, the text is selected
  out.text = {};
  for (const [name, locator] of [["title", tileOf(page, "Rain").locator(".title")], ["table", tileOf(page, "Humidity").locator(".figure").first()],
                                 ["reading", tileOf(page, "Humidity").locator("svg text.reading")]]) {
    const r = await locator.boundingBox(), y = r.y + r.height / 2;   // (across it, end to end)
    const before = await titles(page);
    const during = await mouseDrag(page, [r.x + 2, y], [r.x + r.width - 1, y + 1], () => midDrag(page));
    out.text[name] = { same: JSON.stringify(before) === JSON.stringify(await titles(page)), dragging: during.dragging, selected: await selection(page) };
  }
  // the ×: Rain hidden, the reset link shown
  await page.mouse.move(...await at(page, "Rain", 0.5, 0.4));
  await page.waitForTimeout(250);
  await tileOf(page, "Rain").locator(".close").click();
  await settle(page);
  out.closed = { titles: await titles(page), status: await statusText(page) };
  // dropped on the remove zone: Wind hidden
  out.removing = await mouseDrag(page, await at(page, "Wind", 0.5, 0.3), [500, 20], () => midDrag(page));
  out.removed = { titles: await titles(page), stored: JSON.parse(await stored(page)) };
  // reloaded: the same order and hidden tiles
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  await page.waitForTimeout(300);
  out.reloaded = { titles: await titles(page), status: await statusText(page) };
  // reset: every tile back in the default order, the link gone, nothing kept
  await page.getByText("reset preferences").click();
  await settle(page);
  out.reset = { titles: await titles(page), status: await statusText(page), stored: await stored(page) };
  // preferences corrupt: the defaults; a tile new to the saved order: in its usual place (after the tile before it by default)
  await page.evaluate((key) => localStorage.setItem(key, "{not json"), KEY);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  out.corrupt = await titles(page);
  const saved = out.initial.filter((t) => t !== "Rain" && t !== "Temperature").concat(["Temperature", "Gone sensor"]);
  await page.evaluate(([key, order]) => localStorage.setItem(key, JSON.stringify({ order, hidden: ["Gone sensor"], later: true })), [KEY, saved]);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  out.newTile = await titles(page);
  // (dropped on another tile, so the browser sends no click for the drop; a click on a tile 150 ms later arrives)
  await page.evaluate(() => { window.__clicks = []; addEventListener("click", (e) => window.__clicks.push(e.target.closest(".tile") ? "tile" : "page")); });
  const [h0, r0] = [await at(page, "Humidity", 0.5, 0.3), await at(page, "Rain", 0.5, 0.4)];
  await page.mouse.move(...h0);
  await page.mouse.down();
  for (let i = 1; i <= 12; i++) await page.mouse.move(h0[0] + (r0[0] - h0[0]) * i / 12, h0[1] + (r0[1] - h0[1]) * i / 12);
  await page.mouse.up();
  await page.waitForTimeout(150);
  await page.mouse.click(...await at(page, "Wind", 0.5, 0.3));
  await settle(page);
  out.clickAfterDrop = await page.evaluate(() => window.__clicks);
  out.zoomedAfterDrop = await page.evaluate(() => document.body.classList.contains("zoomed"));   // (that click zoomed Wind: back)
  await page.keyboard.press("Escape");
  await settle(page);
  out.keptOthers = JSON.parse(await stored(page));
  // the × pressed from the keyboard: the focus goes on to the next tile's ×; with the last tile hidden, to the reset link
  await settle(page);
  await page.keyboard.press("Shift");   // (the keyboard in use: the first tile's × focused as by the keyboard)
  await page.evaluate(() => document.querySelector(".close").focus());
  const focused = () => page.evaluate(() => document.activeElement.getAttribute("aria-label") || document.activeElement.textContent);
  out.keyboardHide = [await focused()];
  await page.keyboard.press("Enter");
  await settle(page);
  out.keyboardHide.push(await focused());
  const allBut = out.initial.filter((t) => t !== "Rain");
  await page.evaluate(([key, hidden]) => localStorage.setItem(key, JSON.stringify({ order: [], hidden })), [KEY, allBut]);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  await page.keyboard.press("Tab");
  out.keyboardHide.push(await focused());
  await page.keyboard.press("Enter");
  await settle(page);
  out.keyboardHide.push(await focused());
  out.errors = errors;
  await context.close();
  // storage blocked: the page works, nothing saved, no error
  const blocked = await open(browser, url, 1024, 768, { init: { fn: () => {
    Object.defineProperty(window, "localStorage", { get() { throw new DOMException("blocked", "SecurityError"); } });
  } } });
  await blocked.page.waitForTimeout(300);
  await mouseDrag(blocked.page, await at(blocked.page, "Temperature", 0.85, 0.3), await at(blocked.page, "Humidity", 0.5, 0.4));
  await blocked.page.mouse.move(...await at(blocked.page, "Rain", 0.5, 0.4));
  await blocked.page.waitForTimeout(250);
  await tileOf(blocked.page, "Rain").locator(".close").click();
  await settle(blocked.page);
  out.blocked = { titles: await titles(blocked.page), errors: blocked.errors };
  await blocked.context.close();
  process.stdout.write(JSON.stringify(out));
}

// Touch through the browser's own input (CDP), as a finger: the browser decides on scrolling, long presses and taps
async function touchMode(browser, url) {
  const out = {};
  const { context, page, errors } = await open(browser, url, 390, 844, {
    context: { hasTouch: true, isMobile: true },
    init: { fn: () => {
      window.__buzz = [];
      navigator.vibrate = (ms) => { window.__buzz.push(ms); return true; };
      window.__clicks = 0;
      addEventListener("click", (e) => { if (e.target.closest && e.target.closest(".tile")) window.__clicks++; });
    } },
  });
  const cdp = await context.newCDPSession(page);
  const touch = (type, points) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: points.map(([x, y]) => ({ x, y })) });
  const state = () => page.evaluate(() => ({
    buzz: window.__buzz, clicks: window.__clicks, scrollY: window.scrollY,
    zone: document.getElementById("remove").classList.contains("on"),
    armed: [...document.querySelectorAll(".tile.armed")].map((t) => t.querySelector(".title").textContent),
    close: (() => { const c = document.querySelector(".tile.armed .close"); return c ? getComputedStyle(c).opacity : null; })(),
  }));
  await page.waitForTimeout(1200);
  out.initial = await titles(page);
  // a short tap: a click, nothing picked up
  const t0 = await at(page, "Temperature", 0.85, 0.3);
  await touch("touchStart", [t0]);
  await page.waitForTimeout(80);
  await touch("touchEnd", []);
  await page.waitForTimeout(600);
  out.tap = { ...await state(), titles: await titles(page), zoomed: await page.evaluate(() => document.body.classList.contains("zoomed")) };
  await page.keyboard.press("Escape");   // (the tap zoomed the tile: back)
  await page.waitForTimeout(400);
  // a swipe up before the long press: the page scrolls, nothing picked up
  const s0 = await at(page, "Humidity", 0.5, 0.3);
  await touch("touchStart", [s0]);
  for (let i = 1; i <= 10; i++) { await touch("touchMove", [[s0[0], s0[1] - 30 * i]]); await page.waitForTimeout(16); }
  await touch("touchEnd", []);
  await page.waitForTimeout(700);
  out.swipe = { ...await state(), titles: await titles(page) };
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(300);
  // a long press: picked up (buzz, grip and ×, the remove zone), then moved to Atmospheric pressure's place
  const p0 = await at(page, "Temperature", 0.85, 0.3), p1 = await at(page, "Atmospheric pressure", 0.5, 0.4);
  await touch("touchStart", [p0]);
  await page.waitForTimeout(700);
  out.held = await state();
  for (let i = 1; i <= 12; i++) { await touch("touchMove", [[p0[0] + (p1[0] - p0[0]) * i / 12, p0[1] + (p1[1] - p0[1]) * i / 12]]); await page.waitForTimeout(16); }
  await page.waitForTimeout(300);
  out.moving = { ...await state(), during: await midDrag(page) };
  await touch("touchEnd", []);
  await settle(page);
  out.dropped = { ...await state(), titles: await titles(page) };
  // a long press let go in place: its grip and × stay; a tap on the × hides it
  const h0 = await at(page, "Humidity", 0.85, 0.3);
  await touch("touchStart", [h0]);
  await page.waitForTimeout(700);
  await touch("touchEnd", []);
  await page.waitForTimeout(300);
  out.armed = await state();
  const c = await centreOf(page, tileOf(page, "Humidity").locator(".close"));
  await touch("touchStart", [c]);
  await page.waitForTimeout(50);
  await touch("touchEnd", []);
  await settle(page);
  out.closed = { titles: await titles(page), status: await statusText(page) };
  // held near the bottom edge: the page scrolls to its end and no further (the lifted tile mustn't stretch it without end)
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(200);
  const end = await page.evaluate(() => document.documentElement.scrollHeight - innerHeight);
  const e0 = await at(page, "Temperature", 0.85, 0.3);
  await touch("touchStart", [e0]);
  await page.waitForTimeout(700);
  for (let i = 1; i <= 6; i++) { await touch("touchMove", [[e0[0], e0[1] + (830 - e0[1]) * i / 6]]); await page.waitForTimeout(16); }
  const seen = [];
  for (let i = 0; i < 12; i++) { await page.waitForTimeout(250); seen.push(await page.evaluate(() => window.scrollY)); }
  await touch("touchEnd", []);
  await settle(page);
  out.edge = { end, seen, after: await page.evaluate(() => window.scrollY) };
  out.errors = errors;
  await context.close();
  process.stdout.write(JSON.stringify(out));
}

// ---------- the zoomed view and its gallery ----------
// In the page: the tile shown (the one wholly on the screen), the strip's offset, and a recorder of either, frame by frame
const ZOOM_HELPERS = () => {
  const titleOf = (t) => t.querySelector(".title").textContent;
  window.__shown = () => {
    const t = [...document.querySelectorAll("#grid .tile")].find((t) => { const r = t.getBoundingClientRect(); return r.left >= -1 && r.right <= innerWidth + 1; });
    return t ? titleOf(t) : null;
  };
  window.__track = () => new DOMMatrixReadOnly(getComputedStyle(document.getElementById("grid")).transform).m41;
  window.__state = () => ({ zoomed: document.body.classList.contains("zoomed"), shown: window.__shown(), track: Math.round(window.__track()) });
  window.__tile = (title) => [...document.querySelectorAll("#grid .tile")].find((t) => titleOf(t) === title);
  // every frame for `ms`: the tile's rect (title given) or the strip's offset; and when the last click or key came
  window.__record = (title, ms) => new Promise((resolve) => {
    const frames = [], start = performance.now();
    const step = (now) => {
      if (title) { const r = window.__tile(title).getBoundingClientRect(); frames.push({ t: now, left: r.left, top: r.top, width: r.width, height: r.height }); }
      else frames.push({ t: now, x: window.__track() });
      if (now - start < ms) requestAnimationFrame(step); else resolve({ frames, at: window.__at });
    };
    requestAnimationFrame(step);
  });
  addEventListener("click", () => { window.__at = performance.now(); }, true);
  addEventListener("click", () => { window.__anims = document.getAnimations().filter((a) => !(a instanceof CSSTransition)).map((a) => a.effect.getTiming().duration); });
  addEventListener("keydown", (e) => { window.__at = performance.now(); setTimeout(() => window.__keys.push([e.key, e.defaultPrevented])); });
  addEventListener("wheel", (e) => { setTimeout(() => window.__wheels.push(e.defaultPrevented)); }, { passive: true });
  window.__keys = []; window.__wheels = [];
};
// A recording summed up: where it started and ended, whether it passed through between, and how long after the click or key it came to rest
function motion(rec, key) {
  const f = rec.frames, last = f[f.length - 1], same = (a, b) => key.every((k) => Math.abs(a[k] - b[k]) < 0.5);
  const after = f.filter((x) => x.t >= rec.at);
  const rest = after.find((x, i) => after.slice(i).every((y) => same(y, last)));
  return { first: after[0], last, between: after.some((x) => !same(x, after[0]) && !same(x, last)), settledMs: rest ? Math.round(rest.t - rec.at) : null,
           extreme: key[0] === "x" ? Math.max(...after.map((x) => Math.abs(x.x - last.x))) : null };
}
async function zoomClick(page, title, point) {   // a click, the tile's rect recorded through the zoom
  const recording = page.evaluate((title) => window.__record(title, 500), title);
  await page.waitForTimeout(50);
  await page.mouse.click(...point);
  const rec = await recording;
  return { ...motion(rec, ["left", "top", "width", "height"]), anims: await page.evaluate(() => window.__anims), ...await page.evaluate(() => window.__state()) };
}
async function keyStep(page, key, wait = 400) {   // a key, the strip recorded as it slides
  const recording = page.evaluate(() => window.__record(null, 450));
  await page.waitForTimeout(50);
  await page.keyboard.press(key);
  const rec = await recording;
  await page.waitForTimeout(Math.max(0, wait - 400));
  const m = motion(rec, ["x"]);
  return { ...await page.evaluate(() => window.__state()), settledMs: m.settledMs, between: m.between, extreme: Math.round(m.extreme) };
}
async function pagerDrag(page, [x0, y], dx, { steps = 12, pause = 300 } = {}) {   // a mouse drag sideways; the strip's offset midway
  await page.mouse.move(x0, y);
  await page.mouse.down();
  for (let i = 1; i <= steps; i++) await page.mouse.move(x0 + dx * i / steps, y);
  const during = await page.evaluate(() => window.__track());
  await page.waitForTimeout(pause);
  await page.mouse.up();
  await page.waitForTimeout(450);
  return { during: Math.round(during), ...await page.evaluate(() => window.__state()) };
}
const cursorAt = (page, [x, y]) => page.evaluate(([x, y]) => getComputedStyle(document.elementFromPoint(x, y)).cursor, [x, y]);

async function zoomMode(browser, url) {
  const out = {};
  const { context, page, errors } = await open(browser, url, 1024, 768, { init: { fn: ZOOM_HELPERS } });
  await page.waitForTimeout(1200);
  out.initial = await titles(page);
  out.gridRect = (await tileOf(page, "Humidity").boundingBox());
  const textPoint = async (title, selector) => centreOf(page, tileOf(page, title).locator(selector).first());
  out.cursor = { gauge: await cursorAt(page, await at(page, "Humidity", 0.85, 0.3)), title: await cursorAt(page, await textPoint("Humidity", ".title")),
                 figure: await cursorAt(page, await textPoint("Humidity", ".figure")), reading: await cursorAt(page, await textPoint("Humidity", "svg text.reading")) };
  // a click on Humidity's gauge: zoomed, in 200 ms, to fill the window
  out.zoomIn = await zoomClick(page, "Humidity", await at(page, "Humidity", 0.85, 0.3));
  out.zoomedCursor = { gauge: await cursorAt(page, await at(page, "Humidity", 0.85, 0.3)), margin: await cursorAt(page, [20, 384]),
                       title: await cursorAt(page, await textPoint("Humidity", ".title")) };
  out.grip = await page.evaluate(() => getComputedStyle(window.__tile("Humidity").querySelector(".close")).display);
  // live: a new reading for Temperature, the tile beside it off the screen, reaches it in place
  await page.evaluate(() => { window.__mark = window.__tile("Temperature"); });
  process.stdout.write(JSON.stringify({ label: await page.evaluate(() => window.__mark.getAttribute("aria-label")) }) + "\n");
  await new Promise((resolve) => readline.createInterface({ input: process.stdin }).once("line", resolve));
  try { await page.waitForFunction(() => /21\.5/.test(window.__mark.getAttribute("aria-label")), null, { timeout: 15000 }); } catch (e) { /* (reported) */ }
  await page.waitForTimeout(1000);   // (the gauge's move, MOVE_MS)
  out.live = { label: await page.evaluate(() => window.__mark.getAttribute("aria-label")), sameTile: await page.evaluate(() => window.__mark === window.__tile("Temperature")),
               ...await page.evaluate(() => window.__state()) };
  // the keys: ← to Temperature (now showing the new reading), ← again: the first, a bounce; Space, →, Backspace
  out.keys = {};
  out.keys.left = await keyStep(page, "ArrowLeft");
  out.keys.leftReading = await page.evaluate(() => window.__mark.querySelector("svg text.reading").textContent);
  out.keys.pastFirst = await keyStep(page, "ArrowLeft");
  out.keys.space = await keyStep(page, " ");
  out.keys.right = await keyStep(page, "ArrowRight");
  out.keys.backspace = await keyStep(page, "Backspace");
  out.keys.url = page.url() === url;
  out.keys.prevented = await page.evaluate(() => window.__keys);
  // a mouse drag on the gauge: followed as it goes; let go past halfway: the next; a short slow one: back; a short quick one: the next
  const gauge = () => at(page, "Humidity", 0.85, 0.3);
  const g = await page.evaluate(() => window.__state());
  out.drag = { start: g };
  out.drag.far = await pagerDrag(page, [900, 300], -600);
  out.drag.short = await pagerDrag(page, [800, 300], -100);
  out.drag.flick = await pagerDrag(page, [800, 300], -120, { steps: 3, pause: 0 });
  out.drag.back = await pagerDrag(page, [200, 300], 600);
  // the first tile, dragged on rightwards: it gives less and less, then springs back
  await page.keyboard.press("ArrowLeft"); await page.keyboard.press("ArrowLeft");
  await page.waitForTimeout(500);
  out.drag.pastFirst = await pagerDrag(page, [300, 300], 300);
  // a trackpad's two-finger swipe: wheel deltaX, kept from the browser
  await page.mouse.move(800, 300);
  out.wheel = {};
  for (let i = 0; i < 8; i++) { await page.mouse.wheel(50, 0); await page.waitForTimeout(16); }
  await page.waitForTimeout(500);
  out.wheel.far = await page.evaluate(() => window.__state());
  for (let i = 0; i < 6; i++) { await page.mouse.wheel(10, 0); await page.waitForTimeout(60); }   // (slowly: no flick)
  await page.waitForTimeout(500);
  out.wheel.short = await page.evaluate(() => window.__state());
  for (let i = 0; i < 8; i++) { await page.mouse.wheel(-50, 0); await page.waitForTimeout(16); }
  await page.waitForTimeout(500);
  out.wheel.back = await page.evaluate(() => window.__state());
  out.wheel.prevented = await page.evaluate(() => window.__wheels);
  // the last tile: → past it bounces, as does a swipe on
  const n = (await titles(page)).length;
  for (let i = 0; i < n; i++) { await page.keyboard.press("ArrowRight"); await page.waitForTimeout(30); }
  await page.waitForTimeout(500);
  out.last = { reached: await page.evaluate(() => window.__state()), pastLast: await keyStep(page, "ArrowRight") };
  for (let i = 0; i < 8; i++) { await page.mouse.wheel(50, 0); await page.waitForTimeout(16); }
  await page.waitForTimeout(500);
  out.last.wheeled = await page.evaluate(() => window.__state());
  out.last.dragged = await pagerDrag(page, [800, 300], -400);
  // a drag on the text selects it: no paging, still zoomed
  const shown = out.last.reached.shown;
  out.text = {};
  for (const [name, selector] of [["title", ".title"], ["reading", "svg text.reading"], ["table", ".figure"]]) {
    const r = await tileOf(page, shown).locator(selector).first().boundingBox(), y = r.y + r.height / 2;
    await mouseDrag(page, [r.x + 2, y], [r.x + r.width - 1, y + 1]);
    out.text[name] = { selected: await selection(page), ...await page.evaluate(() => window.__state()) };
  }
  // a click without moving on the gauge: back to the dashboard, in 200 ms, the tile in its place
  out.zoomOut = await zoomClick(page, shown, await at(page, shown, 0.85, 0.3));
  out.outRect = await tileOf(page, shown).boundingBox();
  // Esc: back too
  await page.mouse.click(...await at(page, "Wind", 0.85, 0.3));
  await page.waitForTimeout(400);
  out.esc = { before: await page.evaluate(() => window.__state()) };
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  out.esc.after = await page.evaluate(() => window.__state());
  out.esc.titles = await titles(page);
  // one tile only: no zoom cursor, a click zooms nothing
  await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: [...document.querySelectorAll(".tile .title")].map((t) => t.textContent).filter((t) => t !== "Rain") })), KEY);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  await page.waitForTimeout(300);
  const one = await at(page, "Rain", 0.85, 0.3);
  out.single = { cursor: await cursorAt(page, one) };
  await page.mouse.click(...one);
  await page.waitForTimeout(400);
  out.single.zoomed = await page.evaluate(() => document.body.classList.contains("zoomed"));
  out.errors = errors;
  await context.close();
  process.stdout.write(JSON.stringify(out) + "\n");
}

// The zoomed view on a phone, with a finger (CDP touch events)
async function zoomTouchMode(browser, url) {
  const out = {};
  const { context, page, errors } = await open(browser, url, 390, 844, { context: { hasTouch: true, isMobile: true }, init: { fn: () => {
    window.__buzz = [];
    navigator.vibrate = (ms) => { window.__buzz.push(ms); return true; };
  } } });
  await page.evaluate(ZOOM_HELPERS);
  const cdp = await context.newCDPSession(page);
  const touch = (type, points) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: points.map(([x, y]) => ({ x, y })) });
  const tap = async ([x, y]) => { await touch("touchStart", [[x, y]]); await page.waitForTimeout(60); await touch("touchEnd", []); await page.waitForTimeout(450); };
  const swipe = async ([x, y], dx, { steps = 10, pause = 250, dy = 0 } = {}) => {
    await touch("touchStart", [[x, y]]);
    for (let i = 1; i <= steps; i++) { await touch("touchMove", [[x + dx * i / steps, y + dy * i / steps]]); if (pause) await page.waitForTimeout(16); }
    await page.waitForTimeout(pause);
    await touch("touchEnd", []);
    await page.waitForTimeout(500);
    return page.evaluate(() => window.__state());
  };
  await page.waitForTimeout(1200);
  out.initial = await titles(page);
  // scrolled down a row or two: the tile at the top left tapped
  const scrolled = await page.evaluate(() => { const t = document.querySelectorAll("#grid .tile")[4]; scrollTo(0, t.getBoundingClientRect().top + scrollY - 8); return scrollY; });
  await page.waitForTimeout(300);
  out.scrolled = scrolled;
  out.tapped = await page.evaluate(() => document.elementFromPoint(100, 150).closest(".tile").querySelector(".title").textContent);
  await tap(await at(page, out.tapped, 0.85, 0.3));
  out.zoomed = { ...await page.evaluate(() => window.__state()), rect: await tileOf(page, out.tapped).boundingBox() };
  out.next = await swipe([300, 300], -250);
  out.previous = await swipe([90, 300], 250);
  out.flick = await swipe([300, 300], -80, { steps: 4, pause: 0 });
  const title = await centreOf(page, tileOf(page, out.flick.shown).locator(".title"));
  out.fromTitle = await swipe([title[0] + 100, title[1]], -250);
  out.vertical = await swipe([200, 400], 10, { dy: -250 });
  // held still: nothing picked up while zoomed
  await touch("touchStart", [[200, 300]]);
  await page.waitForTimeout(700);
  await touch("touchEnd", []);
  await page.waitForTimeout(300);
  out.held = { ...await page.evaluate(() => window.__state()), buzz: await page.evaluate(() => window.__buzz),
               zone: await page.evaluate(() => document.getElementById("remove").classList.contains("on")) };
  await swipe([90, 300], 250);
  await swipe([90, 300], 250);
  await swipe([90, 300], 250);
  out.backAt = await page.evaluate(() => window.__state());
  // a tap: back to the dashboard, scrolled where it was
  await tap([200, 300]);
  out.out = { ...await page.evaluate(() => window.__state()), scrollY: await page.evaluate(() => scrollY), titles: await titles(page) };
  out.errors = errors;
  await context.close();
  process.stdout.write(JSON.stringify(out));
}

async function fillMode(browser, url, cases) {
  const out = {};
  for (const [name, [width, height, hidden]] of Object.entries(cases)) {
    const { context, page, errors } = await open(browser, url, width, height, { init: { fn: ([key, hidden]) => {
      try { localStorage.setItem(key, JSON.stringify({ order: [], hidden })); } catch (e) { /* (about:blank) */ }
    }, arg: [KEY, hidden] } });
    await page.waitForTimeout(300);
    out[name] = { ...await page.evaluate(() => ({
      rects: [...document.querySelectorAll("#grid .tile")].map((t) => t.getBoundingClientRect().toJSON()),
      columns: document.body.dataset.columns, scroll: document.body.dataset.scroll,
      scrollWidth: document.documentElement.scrollWidth })), errors };
    await context.close();
  }
  process.stdout.write(JSON.stringify(out));
}

(async () => {
  const [url, second, third] = process.argv.slice(2);
  const browser = await chromium.launch();
  try {
    if (second === "--live") await live(browser, url);
    else if (second === "--arrive") await arrive(browser, url, third);
    else if (second === "--arrange") await arrangeMode(browser, url);
    else if (second === "--touch") await touchMode(browser, url);
    else if (second === "--fill") await fillMode(browser, url, JSON.parse(third));
    else if (second === "--zoom") await zoomMode(browser, url);
    else if (second === "--zoom-touch") await zoomTouchMode(browser, url);
    else await sizes(browser, url, second, JSON.parse(third));
  } finally {
    await browser.close();
  }
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
