// The dashboard page in Playwright's Chromium, for tests/test_web_dashboard.py.
//   node web_dashboard_browser.js URL SHOTS SIZES: the page at each size ({name: [width, height]}): a screenshot of each (SHOTS/dashboard-
//     NAME.png, device scale factor 2) and, on stdout, {name: {tiles, rects, columns, scroll, scrollWidth, scrollHeight, status, errors,
//     zoomed}}; then the first tile zoomed (Enter): its look, and a screenshot (SHOTS/dashboard-NAME-zoomed.png).
//   node web_dashboard_browser.js URL --live: the page open at 800x600; prints {label} (the first tile's), waits for a line on stdin
//     (the test has changed the reading), then prints {label, same_page, errors} once the tile shows it, or after 15 s.
//   node web_dashboard_browser.js URL --arrive TITLE: the page open at 1024x768; prints the tile's state ({label, faded, opacity, shown,
//     needle}), waits for a line on stdin (the test has given it a reading, or taken it away), then, once its label changes, samples it
//     every frame for 1.5 s and prints {before, after, frames: [{opacity, number}], same_tile, errors}.
//   node web_dashboard_browser.js URL --arrange PART: the page at 1024x768 with a mouse, a part of the arranging (arrangeMode):
//     "dragged" (dragging to reorder, the drag's text zones, the grip and the × on mouseover, the remove zone, the reset link, the
//     preferences across a reload, the tiles' see-through look), "prefs" (the preferences corrupt or from elsewhere (a tile new to
//     them), a click soon after a drop, the focus after the × pressed from the keyboard; beside it, storage blocked); prints {step: what
//     was seen}.
//   node web_dashboard_browser.js URL --touch PART: the page at 390x844 with touch (CDP touch events), a part (touchMode): "taps" (a
//     short tap is a click; a swipe scrolls; held at the bottom edge it scrolls to the page's end only), "held" (a long press picks a
//     tile up (buzz, grip and ×, remove zone) and moves it; held in place, its × hides it); prints {step: what was seen}.
//   node web_dashboard_browser.js URL --zoom PART: the page at 1024x768 with a mouse and keys, a part of the zoom (zoomMode): "in" (the
//     zoom cursors, a click zooming a tile; zoomed, prints {label} (Temperature's, beside the tile shown), waits for a line on stdin (the
//     test has changed its reading), then pages with the keys past the first), "pager" (mouse drags and wheel deltaX), "last" (past the
//     last, drags across the text, a click back), "single" (a single tile zoomed, paging bouncing, back by a click and by Esc), "out" (a
//     double-click, Esc, the bounce cut short), "keys" (the keyboard, Esc mid-zoom); prints {step: what was seen}.
//   node web_dashboard_browser.js URL --zoom-touch PART: the page at 390x844 with touch, a part (zoomTouchMode): "back" (a tap zooms, a
//     tap goes back where the page was scrolled), "swipes" (zoomed by a tap, swipes (slow, a flick, on the text) page, a vertical swipe
//     and a long press do nothing, a tap back from another tile); prints {step: what was seen}.
//   node web_dashboard_browser.js URL --weather PART: the page at 1024x768, its data altered on the way, a part of the weather behind the
//     tiles (WEATHER): each reading at, and just short of, the mark that calls up its effect; the canvas behind the tiles and the tiles'
//     red frames; "stop animations"; the temporary test links; every effect at once (?fx=all: the frame time, the light, the particles);
//     ?fx= as the page opens, a phone, reduced motion; prints {step: what was seen}.
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
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0 && /last reading/.test(document.getElementById("status").textContent),
                             null, { timeout: 15000 });
  return { context, page, errors };
}

async function sizes(browser, url, shots, wanted) {
  const report = {};
  await Promise.all(Object.entries(wanted).map(async ([name, [width, height]]) => {   // (side by side, a page each)
    const { context, page, errors } = await open(browser, url, width, height);
    await page.evaluate(() => document.fonts.ready);
    await calm(page, 5000);   // (the gauges' rise from their floor when the page opens, RISE_MS)
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
    // zoomed (Enter on the first tile): its look (box, title, text, table) and a screenshot (SHOTS/dashboard-NAME-zoomed.png)
    await page.focus(".tile");
    await page.keyboard.press("Enter");
    await calm(page, 400);
    await page.screenshot({ path: `${shots}/dashboard-${name}-zoomed.png` });
    report[name].zoomed = await page.evaluate(() => {
      const t = document.querySelector("#grid .tile"), rect = (e) => e.getBoundingClientRect().toJSON();
      const table = [...t.querySelectorAll(".unit, .chip, .figure, .tablearea")].map((e) => getComputedStyle(e));
      return { zoomed: document.body.classList.contains("zoomed"), box: rect(t), title: rect(t.querySelector(".title")),
               texts: [...t.querySelectorAll("svg text")].map(rect).filter((r) => r.width),
               table: [...new Set(table.map((c) => `${c.visibility} ${c.opacity}`))] };
    });
    await context.close();
  }));
  process.stdout.write(JSON.stringify(Object.fromEntries(Object.keys(wanted).map((name) => [name, report[name]]))));
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
// The page at rest, rather than a fixed wait: nothing in its document changed (a gauge's needle, a tile's transform, a class) for 50 ms
// and no animation or transition running (an endless one aside); at most `cap` ms: the fixed wait it stands for where that was a margin,
// longer for the gauges' rise and moves (which the frames counted after them must not include, however slow the machine is just then).
// (Not for what a timer starts later, a long press or a swipe's end: a wait for that.)
const calm = (page, cap = 450) => page.evaluate((cap) => new Promise((resolve) => {
  const start = performance.now();
  let changed = start;
  const seen = new MutationObserver(() => { changed = performance.now(); });
  seen.observe(document.documentElement, { subtree: true, attributes: true, childList: true, characterData: true });
  const moving = () => document.getAnimations().some((a) => a.playState === "running" && a.effect.getComputedTiming().endTime !== Infinity);
  const check = (now) => {
    if (moving()) changed = Math.max(changed, now);
    if (performance.now() - changed >= 50 || now - start >= cap) { seen.disconnect(); resolve(); } else requestAnimationFrame(check);
  };
  requestAnimationFrame(check);
}), cap);

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
  await calm(page);   // (the others made room)
  const seen = during ? await during() : null;
  await page.mouse.up();
  await calm(page);
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

// In two parts (each its own process: the tests run them side by side): "dragged" (dragged, hidden, reloaded and reset) and "prefs"
// (preferences unreadable or from elsewhere, a click soon after a drop and the × from the keyboard; then storage blocked, on a page of
// its own beside it).
async function arrangeMode(browser, url, part) {
  const out = {}, errors = [];
  if (part === "dragged") await arranged(browser, url, out, errors);
  else await Promise.all([arrangedFromPrefs(browser, url, out, errors), arrangedBlocked(browser, url, out)]);
  out.errors = errors;
  process.stdout.write(JSON.stringify(out));
}
async function arranged(browser, url, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768);
  await calm(page, 5000);   // (the gauges' rise)
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
  await calm(page);
  // mouseover: the frame too, on the tile under the mouse only (unseen otherwise)
  out.hoverFrames = await page.evaluate(() => [...document.querySelectorAll(".tile")].slice(0, 3).map((t) => getComputedStyle(t, "::before").borderTopColor));
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
  await calm(page);
  await tileOf(page, "Rain").locator(".close").click();
  await calm(page);
  out.closed = { titles: await titles(page), status: await statusText(page) };
  // dropped on the remove zone: Wind hidden
  out.removing = await mouseDrag(page, await at(page, "Wind", 0.5, 0.3), [500, 20], () => midDrag(page));
  out.removed = { titles: await titles(page), stored: JSON.parse(await stored(page)) };
  // reloaded: the same order and hidden tiles
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  await calm(page, 300);
  out.reloaded = { titles: await titles(page), status: await statusText(page) };
  // reset: every tile back in the default order, the link gone, nothing kept
  await page.getByText("reset preferences").click();
  await calm(page);
  out.reset = { titles: await titles(page), status: await statusText(page), stored: await stored(page) };
  allErrors.push(...errors);
  await context.close();
}
async function arrangedFromPrefs(browser, url, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768);
  const initial = await titles(page);
  // preferences corrupt: the defaults; a tile new to the saved order: in its usual place (after the tile before it by default)
  await page.evaluate((key) => localStorage.setItem(key, "{not json"), KEY);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  out.corrupt = await titles(page);
  const saved = initial.filter((t) => t !== "Rain" && t !== "Temperature").concat(["Temperature", "Gone sensor"]);
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
  await calm(page);
  out.clickAfterDrop = await page.evaluate(() => window.__clicks);
  out.zoomedAfterDrop = await page.evaluate(() => document.body.classList.contains("zoomed"));   // (that click zoomed Wind: back)
  await page.keyboard.press("Escape");
  await calm(page);
  out.keptOthers = JSON.parse(await stored(page));
  // the × pressed from the keyboard: the focus goes on to the next tile's ×; with the last tile hidden, to the reset link
  await calm(page);
  await page.keyboard.press("Shift");   // (the keyboard in use: the first tile's × focused as by the keyboard)
  await page.evaluate(() => document.querySelector(".close").focus());
  const focused = () => page.evaluate(() => document.activeElement.getAttribute("aria-label") || document.activeElement.textContent);
  out.keyboardHide = [await focused()];
  await page.keyboard.press("Enter");
  await calm(page);
  out.keyboardHide.push(await focused());
  const allBut = initial.filter((t) => t !== "Rain");
  await page.evaluate(([key, hidden]) => localStorage.setItem(key, JSON.stringify({ order: [], hidden })), [KEY, allBut]);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  await page.keyboard.press("Tab");   // (the tile itself first, then its ×)
  out.keyboardTile = await focused();
  await page.keyboard.press("Tab");
  out.keyboardHide.push(await focused());
  await page.keyboard.press("Enter");
  await calm(page);
  out.keyboardHide.push(await focused());
  allErrors.push(...errors);
  await context.close();
}
async function arrangedBlocked(browser, url, out) {
  // storage blocked: the page works, nothing saved, no error
  const blocked = await open(browser, url, 1024, 768, { init: { fn: () => {
    Object.defineProperty(window, "localStorage", { get() { throw new DOMException("blocked", "SecurityError"); } });
  } } });
  await calm(blocked.page, 300);
  await mouseDrag(blocked.page, await at(blocked.page, "Temperature", 0.85, 0.3), await at(blocked.page, "Humidity", 0.5, 0.4));
  await blocked.page.mouse.move(...await at(blocked.page, "Rain", 0.5, 0.4));
  await calm(blocked.page);
  await tileOf(blocked.page, "Rain").locator(".close").click();
  await calm(blocked.page);
  out.blocked = { titles: await titles(blocked.page), errors: blocked.errors };
  await blocked.context.close();
}

// Touch through the browser's own input (CDP), as a finger: the browser decides on scrolling, long presses and taps. In two parts (each
// its own page in its own process: the tests run them side by side): "taps" (a tap, a swipe and a long press at the bottom edge) and
// "held" (a tile picked up, moved and dropped, then one held in place and hidden by its ×).
async function touchMode(browser, url, part) {
  const out = {}, errors = [];
  await touchPage(browser, url, part, out, errors);
  out.errors = errors;
  process.stdout.write(JSON.stringify(out));
}
async function touchPage(browser, url, part, out, allErrors) {
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
  await calm(page, 5000);   // (the gauges' rise)
  if (part === "held") {
    await touchHeld(page, touch, state, out);
  } else {
    out.initial = await titles(page);
    await touchTaps(page, touch, state, out);
  }
  allErrors.push(...errors);
  await context.close();
}
async function touchTaps(page, touch, state, out) {
  // a short tap: a click, nothing picked up
  const t0 = await at(page, "Temperature", 0.85, 0.3);
  await touch("touchStart", [t0]);
  await page.waitForTimeout(80);
  await touch("touchEnd", []);
  await page.waitForTimeout(600);
  out.tap = { ...await state(), titles: await titles(page), zoomed: await page.evaluate(() => document.body.classList.contains("zoomed")) };
  await page.keyboard.press("Escape");   // (the tap zoomed the tile: back)
  await calm(page, 400);
  // a swipe up before the long press: the page scrolls, nothing picked up
  const s0 = await at(page, "Humidity", 0.5, 0.3);
  await touch("touchStart", [s0]);
  for (let i = 1; i <= 10; i++) { await touch("touchMove", [[s0[0], s0[1] - 30 * i]]); await page.waitForTimeout(16); }
  await touch("touchEnd", []);
  await page.waitForTimeout(700);
  out.swipe = { ...await state(), titles: await titles(page) };
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
  await calm(page);
  out.edge = { end, seen, after: await page.evaluate(() => window.scrollY) };
}
async function touchHeld(page, touch, state, out) {
  // a long press: picked up (buzz, grip and ×, the remove zone), then moved to Atmospheric pressure's place
  const p0 = await at(page, "Temperature", 0.85, 0.3), p1 = await at(page, "Atmospheric pressure", 0.5, 0.4);
  await touch("touchStart", [p0]);
  await page.waitForTimeout(700);
  out.held = await state();
  for (let i = 1; i <= 12; i++) { await touch("touchMove", [[p0[0] + (p1[0] - p0[0]) * i / 12, p0[1] + (p1[1] - p0[1]) * i / 12]]); await page.waitForTimeout(16); }
  await calm(page, 300);   // (the others made room)
  out.moving = { ...await state(), during: await midDrag(page) };
  await touch("touchEnd", []);
  await calm(page);
  out.dropped = { ...await state(), titles: await titles(page) };
  // a long press let go in place: its grip and × stay; a tap on the × hides it
  const h0 = await at(page, "Humidity", 0.85, 0.3);
  await touch("touchStart", [h0]);
  await page.waitForTimeout(700);
  await touch("touchEnd", []);
  await calm(page, 300);
  out.armed = await state();
  const c = await centreOf(page, tileOf(page, "Humidity").locator(".close"));
  await touch("touchStart", [c]);
  await page.waitForTimeout(50);
  await touch("touchEnd", []);
  await calm(page);
  out.closed = { titles: await titles(page), status: await statusText(page) };
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
      if (title) {   // (and its title's place, and its table's opacity: a figure's, as drawn)
        const t = window.__tile(title), r = t.getBoundingClientRect(), h = t.querySelector(".title").getBoundingClientRect();
        const f = getComputedStyle(t.querySelector(".figure"));
        frames.push({ t: now, left: r.left, top: r.top, width: r.width, height: r.height, titleTop: h.top, titleSize: h.height,
                      table: f.visibility === "hidden" ? 0 : +f.opacity });
      }
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
  // A tile's look: its box; its title, reading and (a dial's) band word, each on the screen; its table's visibility and opacity
  window.__look = (title) => {
    const t = window.__tile(title), rect = (e) => e && e.getBoundingClientRect().toJSON();
    const table = [...t.querySelectorAll(".unit, .chip, .figure, .tablearea")].map((e) => getComputedStyle(e));
    return { box: rect(t), title: rect(t.querySelector(".title")), reading: rect(t.querySelector("svg text.reading")),
             texts: [...t.querySelectorAll("svg text")].map(rect).filter((r) => r.width),
             table: [...new Set(table.map((c) => `${c.visibility} ${c.opacity}`))] };
  };
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
  const recording = page.evaluate((title) => window.__record(title, 700), title);   // (to the table's fade back in, after the zoom out)
  await page.waitForTimeout(50);
  await page.mouse.click(...point);
  const rec = await recording;
  const after = rec.frames.filter((x) => x.t >= rec.at);
  return { ...motion(rec, ["left", "top", "width", "height"]), anims: await page.evaluate(() => window.__anims), ...await page.evaluate(() => window.__state()),
           tables: after.map((x) => x.table), titles: after.map((x) => [(x.titleTop - x.top) / x.width, x.titleSize / x.width]) };
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
  await calm(page);
  return { during: Math.round(during), ...await page.evaluate(() => window.__state()) };
}
// a trackpad's swipe over (no wheel event for WHEEL_IDLE_MS, then the strip settling), at rest
const swiped = async (page) => { await page.waitForTimeout(160); await calm(page); };
const cursorAt = (page, [x, y]) => page.evaluate(([x, y]) => getComputedStyle(document.elementFromPoint(x, y)).cursor, [x, y]);

// The zoom in six parts, each its own page in its own process (the tests run them side by side): "in" (the cursors, a click zooming a
// tile, the live reading beside it, the keys), "pager" (zoomed: mouse drags, a trackpad's swipes), "last" (zoomed: the last tile, a
// drag across the text, a click back out), "single" (a single tile zoomed, paging only bouncing), "out" (a double-click, Esc, the
// bounce cut short), "keys" (the keyboard, Esc mid-way through a zoom-in)
async function zoomMode(browser, url, part) {
  const out = {}, errors = [];
  await zoomPage(browser, url, part, out, errors);
  out.errors = errors;
  process.stdout.write(JSON.stringify(out) + "\n");
}
async function zoomPage(browser, url, part, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768, { init: { fn: ZOOM_HELPERS } });
  await calm(page, 5000);   // (the gauges' rise)
  out.initial = await titles(page);
  out.gridRect = (await tileOf(page, "Humidity").boundingBox());
  if (part === "in") {
    const textPoint = async (title, selector) => centreOf(page, tileOf(page, title).locator(selector).first());
    out.cursor = { gauge: await cursorAt(page, await at(page, "Humidity", 0.85, 0.3)), title: await cursorAt(page, await textPoint("Humidity", ".title")),
                   figure: await cursorAt(page, await textPoint("Humidity", ".figure")), reading: await cursorAt(page, await textPoint("Humidity", "svg text.reading")) };
    // a click on Humidity's gauge: zoomed, in 200 ms, to fill the window
    out.zoomIn = await zoomClick(page, "Humidity", await at(page, "Humidity", 0.85, 0.3));
    out.zoomedCursor = { gauge: await cursorAt(page, await at(page, "Humidity", 0.85, 0.3)), margin: await cursorAt(page, [20, 384]),
                         title: await cursorAt(page, await textPoint("Humidity", ".title")) };
    out.grip = await page.evaluate(() => getComputedStyle(window.__tile("Humidity").querySelector(".close")).display);
    out.zoomedLook = await page.evaluate(() => window.__look("Humidity"));                // (its title and gauge filling the window, no table)
    out.neighbourTable = await page.evaluate(() => window.__look("Temperature").table);   // (the gallery's others: no table either)
    // live: a new reading for Temperature, the tile beside it off the screen, reaches it in place
    await page.evaluate(() => { window.__mark = window.__tile("Temperature"); });
    process.stdout.write(JSON.stringify({ label: await page.evaluate(() => window.__mark.getAttribute("aria-label")) }) + "\n");
    await new Promise((resolve) => readline.createInterface({ input: process.stdin }).once("line", resolve));
    try { await page.waitForFunction(() => /21\.5/.test(window.__mark.getAttribute("aria-label")), null, { timeout: 15000 }); } catch (e) { /* (reported) */ }
    await calm(page, 1000);   // (the gauge's move, MOVE_MS)
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
  } else if (part === "pager") {
    // zoomed on Humidity, by a click on its gauge
    await page.mouse.click(...await at(page, "Humidity", 0.85, 0.3));
    await calm(page);
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
    await calm(page, 500);
    out.drag.pastFirst = await pagerDrag(page, [300, 300], 300);
    // a trackpad's two-finger swipe: wheel deltaX, kept from the browser
    await page.mouse.move(800, 300);
    out.wheel = {};
    for (let i = 0; i < 8; i++) { await page.mouse.wheel(50, 0); await page.waitForTimeout(16); }
    await swiped(page);
    out.wheel.far = await page.evaluate(() => window.__state());
    for (let i = 0; i < 6; i++) { await page.mouse.wheel(10, 0); await page.waitForTimeout(60); }   // (slowly: no flick)
    await swiped(page);
    out.wheel.short = await page.evaluate(() => window.__state());
    for (let i = 0; i < 8; i++) { await page.mouse.wheel(-50, 0); await page.waitForTimeout(16); }
    await swiped(page);
    out.wheel.back = await page.evaluate(() => window.__state());
    out.wheel.prevented = await page.evaluate(() => window.__wheels);
  } else if (part === "last") {
    // zoomed on Humidity, by a click on its gauge
    await page.mouse.click(...await at(page, "Humidity", 0.85, 0.3));
    await calm(page);
    // the last tile: → past it bounces, as does a swipe on
    const n = (await titles(page)).length;
    for (let i = 1; i < n; i++) { await page.keyboard.press("ArrowRight"); await page.waitForTimeout(30); }   // (from the second: once past the last)
    await calm(page, 500);
    out.last = { reached: await page.evaluate(() => window.__state()), pastLast: await keyStep(page, "ArrowRight") };
    for (let i = 0; i < 8; i++) { await page.mouse.wheel(50, 0); await page.waitForTimeout(16); }
    await swiped(page);
    out.last.wheeled = await page.evaluate(() => window.__state());
    out.last.dragged = await pagerDrag(page, [800, 300], -400);
    // a drag on the text selects it: no paging, still zoomed
    const shown = out.last.reached.shown;
    out.text = {};
    for (const [name, selector] of [["title", ".title"], ["reading", "svg text.reading"]]) {
      const r = await tileOf(page, shown).locator(selector).first().boundingBox(), y = r.y + r.height / 2;
      await mouseDrag(page, [r.x + 2, y], [r.x + r.width - 1, y + 1]);
      out.text[name] = { selected: await selection(page), ...await page.evaluate(() => window.__state()) };
    }
    // a click without moving on the gauge: back to the dashboard, in 200 ms, the tile in its place
    out.zoomOut = await zoomClick(page, shown, await at(page, shown, 0.85, 0.3));
    out.outRect = await tileOf(page, shown).boundingBox();
    out.outTable = await page.evaluate((shown) => window.__look(shown).table, shown);   // (its table back)
  } else if (part === "single") {
    // one tile only: the zoom cursor, a click zooms it (title and gauge, no table); the keys, a drag and a trackpad's swipe only
    // bounce (a gallery of one); a click goes back, Esc too
    await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: [...document.querySelectorAll(".tile .title")].map((t) => t.textContent).filter((t) => t !== "Humidity") })), KEY);
    await page.reload({ waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll(".tile").length === 1);
    await calm(page, 1500);   // (the gauge's rise)
    const one = await at(page, "Humidity", 0.85, 0.3);
    out.single = { cursor: await cursorAt(page, one), gridRect: await tileOf(page, "Humidity").boundingBox() };
    await page.mouse.click(...one);
    await calm(page);
    out.single.zoomed = { ...await page.evaluate(() => window.__state()), cursor: await cursorAt(page, one),
                          look: await page.evaluate(() => window.__look("Humidity")) };
    out.single.keys = { right: await keyStep(page, "ArrowRight"), left: await keyStep(page, "ArrowLeft"), space: await keyStep(page, " ") };
    out.single.dragged = { left: await pagerDrag(page, [800, 300], -400), right: await pagerDrag(page, [200, 300], 400) };
    for (let i = 0; i < 8; i++) { await page.mouse.wheel(50, 0); await page.waitForTimeout(16); }
    await swiped(page);
    out.single.wheeled = { ...await page.evaluate(() => window.__state()), prevented: await page.evaluate(() => window.__wheels) };
    await page.mouse.click(...one);
    await calm(page);
    out.single.back = { ...await page.evaluate(() => window.__state()), rect: await tileOf(page, "Humidity").boundingBox() };
    await page.mouse.click(...one);
    await calm(page);
    await page.keyboard.press("Escape");
    await calm(page);
    out.single.esc = await page.evaluate(() => window.__state());
  } else if (part === "out") {
    // a double-click (its clicks 300 ms apart, slower than the zoom): zoomed, not zoomed and straight back out
    const wind = await at(page, "Wind", 0.85, 0.3);
    await page.mouse.move(...wind);
    await page.mouse.down(); await page.mouse.up();
    await page.waitForTimeout(300);
    await page.mouse.down({ clickCount: 2 }); await page.mouse.up({ clickCount: 2 });
    await calm(page);
    // Esc: back too
    out.esc = { before: await page.evaluate(() => window.__state()) };
    await page.keyboard.press("Escape");
    await calm(page);
    out.esc.after = await page.evaluate(() => window.__state());
    out.esc.titles = await titles(page);
    // a click on the gauge doesn't take the focus: no × once the pointer has gone
    await page.mouse.move(1020, 4);
    await calm(page);
    out.esc.close = await page.evaluate(() => ({ focused: document.activeElement === window.__tile("Wind"),
                                                opacity: getComputedStyle(window.__tile("Wind").querySelector(".close")).opacity }));
    // the bounce at the last tile, cut short by Esc: the dashboard back in its place at once; or by a key: that step slides
    const n2 = (await titles(page)).length;
    await page.mouse.click(...await at(page, "Wind", 0.85, 0.3));
    await calm(page);
    for (let i = 0; i < n2; i++) { await page.keyboard.press("ArrowRight"); await page.waitForTimeout(30); }
    await calm(page);
    await page.keyboard.press("ArrowRight");   // (past the last: the bounce)
    await page.waitForTimeout(30);
    const cut = page.evaluate(() => window.__record(null, 300));
    await page.keyboard.press("ArrowLeft");
    const rec = await cut;
    const near100 = rec.frames.filter((f) => f.t >= rec.at + 90 && f.t <= rec.at + 130).map((f) => Math.round(f.x));
    await calm(page);
    out.bounceCut = { step: { mid: near100, ...await page.evaluate(() => window.__state()) } };
    await page.keyboard.press("ArrowRight"); await calm(page);
    await page.keyboard.press("ArrowRight");   // (the bounce again)
    await page.waitForTimeout(30);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(60);
    out.bounceCut.esc = await page.evaluate(() => ({ grid: getComputedStyle(document.getElementById("grid")).transform,
                                                     firstLeft: Math.round(document.querySelector("#grid .tile").getBoundingClientRect().left),
                                                     zoomed: document.body.classList.contains("zoomed") }));
    await calm(page);
  } else if (part === "keys") {
    await page.keyboard.press("Shift");   // (the keyboard in use: a focused tile's × shown as by the keyboard)
    // the keyboard: every tile takes the focus; Enter on one zooms it, the focus going along as it pages, the others out of reach; Esc
    // back, the focus on the tile shown last; Space zooms too
    const focusState = () => page.evaluate(() => ({ ...window.__state(), focus: document.activeElement.classList.contains("tile") ? document.activeElement.querySelector(".title").textContent : document.activeElement.tagName,
                                                     reachable: [...document.querySelectorAll("#grid .tile")].filter((t) => !t.inert).map((t) => t.querySelector(".title").textContent) }));
    out.keyboard = { tabbable: await page.evaluate(() => [...document.querySelectorAll("#grid .tile")].every((t) => t.tabIndex === 0)) };
    await page.evaluate(() => window.__tile("Temperature").focus());
    await calm(page);
    out.keyboard.focusedClose = await page.evaluate(() => getComputedStyle(window.__tile("Temperature").querySelector(".close")).opacity);
    await page.keyboard.press("Enter");
    await calm(page);
    out.keyboard.enter = await focusState();
    await page.keyboard.press("ArrowRight");
    await calm(page);
    out.keyboard.right = await focusState();
    await page.keyboard.press("Tab");
    out.keyboard.tab = await focusState();
    await page.evaluate(() => window.__tile("Humidity").focus());
    await page.keyboard.press("Escape");
    await calm(page);
    out.keyboard.esc = await focusState();
    await page.keyboard.press(" ");
    await calm(page);
    out.keyboard.space = await focusState();
    await page.keyboard.press("Escape");
    await calm(page);
    // Esc 100 ms into a zoom-in: back from where the tile is drawn (no jump to the full size), its table hidden until it's in its grid place
    // (never drawn large); the others keep their tables as they fade out, gone only once they're in the gallery
    await calm(page);
    {
      const recording = page.evaluate(() => window.__record("Humidity", 700));
      await page.waitForTimeout(50);
      await page.mouse.click(...await at(page, "Humidity", 0.85, 0.3));
      // (timed in the page from the click, frame by frame, rather than by waits around round trips to it: the others' tables 30 ms
      // in, then Esc ~100 ms in, its own round trip aside)
      const others = await page.evaluate(() => new Promise((resolve) => {
        let seen = null;
        const step = () => {
          const since = performance.now() - window.__at;
          if (!seen && since >= 30) seen = window.__look("Temperature").table;
          if (since >= 90) resolve(seen); else requestAnimationFrame(step);
        };
        step();
      }));
      await page.keyboard.press("Escape");
      const { frames: f, at: esc } = await recording, last = f[f.length - 1], after = f.filter((x) => x.t >= esc);
      out.escMid = { others, last, zoomed: await page.evaluate(() => document.body.classList.contains("zoomed")),
                     atEsc: f.filter((x) => x.t < esc).pop().width, peakAfter: Math.max(...after.map((x) => x.width)),
                     tableWhileLarge: Math.max(...after.filter((x) => x.width > last.width + 1).map((x) => x.table)), tableEnd: last.table };
      await calm(page);
    }
  }
  allErrors.push(...errors);
  await context.close();
}

// The zoomed view on a phone, with a finger (CDP touch events)
// In two parts (each its own page in its own process: the tests run them side by side): "back" (a tap zooming the tile, a tap straight
// back) and "swipes" (the same tile zoomed by a tap, then the swipes, a long press, and a tap back from another tile).
async function zoomTouchMode(browser, url, part) {
  const out = {};
  const { context, page, errors } = await open(browser, url, 390, 844, { context: { hasTouch: true, isMobile: true }, init: { fn: () => {
    window.__buzz = [];
    navigator.vibrate = (ms) => { window.__buzz.push(ms); return true; };
  } } });
  await page.evaluate(ZOOM_HELPERS);
  const cdp = await context.newCDPSession(page);
  const touch = (type, points) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: points.map(([x, y]) => ({ x, y })) });
  const tap = async ([x, y]) => {   // (then its click, and the zoom it starts or ends at rest)
    const before = await page.evaluate(() => window.__at);
    await touch("touchStart", [[x, y]]); await page.waitForTimeout(60); await touch("touchEnd", []);
    await page.waitForFunction((before) => window.__at !== before, before, { timeout: 450 }).catch(() => {});
    await calm(page, 450);
  };
  const swipe = async ([x, y], dx, { steps = 10, pause = 250, dy = 0 } = {}) => {
    await touch("touchStart", [[x, y]]);
    for (let i = 1; i <= steps; i++) { await touch("touchMove", [[x + dx * i / steps, y + dy * i / steps]]); if (pause) await page.waitForTimeout(16); }
    await page.waitForTimeout(pause);
    await touch("touchEnd", []);
    await calm(page, 500);
    return page.evaluate(() => window.__state());
  };
  await calm(page, 5000);   // (the gauges' rise)
  out.initial = await titles(page);
  // scrolled down a row or two: the tile at the top left tapped
  const scrolled = await page.evaluate(() => { const t = document.querySelectorAll("#grid .tile")[4]; scrollTo(0, t.getBoundingClientRect().top + scrollY - 8); return scrollY; });
  await page.waitForTimeout(300);
  out.scrolled = scrolled;
  out.tapped = await page.evaluate(() => document.elementFromPoint(100, 150).closest(".tile").querySelector(".title").textContent);
  await tap(await at(page, out.tapped, 0.85, 0.3));
  if (part === "back") {
    out.zoomed = { ...await page.evaluate(() => window.__state()), rect: await tileOf(page, out.tapped).boundingBox(),
                   look: await page.evaluate((title) => window.__look(title), out.tapped) };
    // a tap straight back: the page scrolled where it was
    await page.waitForTimeout(400);   // (not a double tap)
    await tap([200, 300]);
    out.backStraight = { ...await page.evaluate(() => window.__state()), scrollY: await page.evaluate(() => scrollY) };
  } else await zoomTouchSwipes(page, touch, tap, swipe, out);
  out.errors = errors;
  await context.close();
  process.stdout.write(JSON.stringify(out));
}
async function zoomTouchSwipes(page, touch, tap, swipe, out) {
  out.swipesTapped = out.tapped;
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
  await calm(page, 300);
  out.held = { ...await page.evaluate(() => window.__state()), buzz: await page.evaluate(() => window.__buzz),
               zone: await page.evaluate(() => document.getElementById("remove").classList.contains("on")) };
  await swipe([90, 300], 250);
  await swipe([90, 300], 250);
  await swipe([90, 300], 250);
  out.backAt = await page.evaluate(() => window.__state());
  // a tap, paged away: back to the dashboard, the tile shown last on the screen
  await page.waitForTimeout(400);   // (not a double tap)
  await tap([200, 300]);
  out.out = { ...await page.evaluate(() => window.__state()), scrollY: await page.evaluate(() => scrollY), titles: await titles(page),
              lastShown: await page.evaluate((title) => { const r = window.__tile(title).getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }, out.backAt.shown) };
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

// ---------- the weather behind the tiles ----------
// The page with its data altered on the way: `readings` {title: value} for the tiles' readings, rain_rate as it is; the US AQI Good
// unless given (the stand-in's is Moderate)
async function weatherPage(browser, url, options = {}) {
  const state = { readings: {} };
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 }, deviceScaleFactor: 1, ...(options.context || {}) });
  await context.addInitScript((pace) => {   // (every frame the page asks for, counted; and every fetch's data read)
    window.__frames = 0;
    const ask = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (f) => { window.__frames++; return ask(pace === 1 ? f : (ts) => f(ts * pace)); };   // (its time `pace` times as fast)
    window.__fetched = 0;
    const get = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await get(...args), json = response.json.bind(response);
      response.json = async () => { const data = await json(); window.__fetched++; return data; };   // (counted before the page renders it)
      return response;
    };
  }, options.pace || 1);
  if (options.init) await context.addInitScript(options.init.fn, options.init.arg);
  if (options.animations) await context.route((u) => u.pathname.endsWith("/dashboard"), async (route) => {   // (as served with
    const response = await route.fetch(), page = await response.text();                                       // MCP_DASHBOARD_ANIMATIONS)
    await route.fulfill({ response, body: page.replace('data-animations="on"', `data-animations="${options.animations}"`) });
  });
  if (options.bare) await context.route((u) => u.pathname.endsWith("/dashboard"), async (route) => {   // (the page as it will be:
    const response = await route.fetch(), page = await response.text();                                // no temporary test links)
    await route.fulfill({ response, body: page.replace(/<!-- TEMPORARY:[\s\S]*<!-- END TEMPORARY: effect test links -->/, "") });
  });
  await context.route(/\/dashboard\/data$/, async (route) => {
    const response = await route.fetch(), data = await response.json();
    const readings = { "US AQI": 40, ...state.readings };
    for (const t of data.tiles) if (t.title in readings) t.value = readings[t.title];
    if ("rain_rate" in readings) data.rain_rate = readings.rain_rate;
    await route.fulfill({ response, json: data });
  });
  const page = await context.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(url + (options.query || ""), { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0 && /last reading/.test(document.getElementById("status").textContent));
  // The readings now these (fetched again: the page fetches when it's shown again)
  const reads = async (readings) => {
    state.readings = readings;
    const fetched = await page.evaluate(() => { const n = window.__fetched; document.dispatchEvent(new Event("visibilitychange")); return n; });
    await page.waitForFunction((n) => window.__fetched > n, fetched);   // (fetched and drawn)
  };
  return { context, page, errors, reads };
}
const weatherSeen = (page) => page.evaluate(() => {
  const s = Sky.state(), link = document.querySelector("#motion .animations");
  return { effects: s.effects, levels: s.levels, running: s.running,
           framed: [...document.querySelectorAll("#grid .tile.alert .title")].map((t) => t.textContent),
           link: link ? link.textContent : null };
});
// The bottom corners: "stop animations" (its words, its box), the test row's and its "clear"'s boxes (null without them), the strip's;
// whether the link is clear of the strip, and on the screen
const cornerSeen = (page) => page.evaluate(() => {
  const box = (e) => e ? e.getBoundingClientRect().toJSON() : null, link = document.querySelector("#motion .animations");
  const clear = [...document.querySelectorAll("#fxtest button")].find((b) => b.textContent === "clear");
  const m = box(document.getElementById("motion")), s = box(document.getElementById("status"));
  return { link: link ? link.textContent : null, motion: m, row: box(document.getElementById("fxtest")), clear: box(clear), strip: s,
           window: [innerWidth, innerHeight], apart: m.right <= s.left || s.right <= m.left || m.bottom <= s.top || s.bottom <= m.top,
           onScreen: m.left >= 0 && m.right <= innerWidth && m.bottom <= innerHeight };
});
const canvasInk = (page) => page.evaluate(() => {   // how many of the canvas's pixels have anything drawn
  const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  let n = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i]) n++;
  return n;
});
const canvasLight = (page) => page.evaluate(() => {   // how bright the canvas is, all told: its pixels' luminance by their alpha, averaged
  const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  let sum = 0;
  for (let i = 0; i < d.length; i += 4) sum += (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) * d[i + 3] / 255;
  return sum / (d.length / 4);
});
// The wind's leaves for `ms`, every frame: their mean speed across on screen (negative: leftward), how many at most went against the
// wind (rightward: in a loop); and where each was drawn on the canvas (with the wind alone, every sprite drawn is a leaf), frame to
// frame: how many steps it made leftward and how many rightward
const windSeen = (page, ms) => page.evaluate((ms) => new Promise((resolve) => {
  const g = document.querySelector("#sky canvas").getContext("2d"), drawImage = g.drawImage, clearRect = g.clearRect, drawn = [];
  g.clearRect = function (...args) { drawn.push([]); return clearRect.apply(this, args); };   // (a frame begins)
  g.drawImage = function (...args) { if (drawn.length) drawn[drawn.length - 1].push(args[1]); return drawImage.apply(this, args); };
  const vx = [], start = performance.now();
  let against = 0;
  const step = (now) => {
    const w = Sky.state().wind;
    vx.push(w.vx);
    against = Math.max(against, w.against);
    if (now - start < ms) return requestAnimationFrame(step);
    delete g.drawImage; delete g.clearRect;
    let left = 0, right = 0;
    for (let i = 1; i < drawn.length; i++) {
      const [a, b] = [drawn[i - 1], drawn[i]];
      if (a.length !== b.length) continue;
      for (let j = 0; j < a.length; j++) { const dx = b[j] - a[j]; if (Math.abs(dx) < 30) { if (dx < 0) left++; else if (dx > 0) right++; } }
    }
    const { hits, whirl } = Sky.state().wind;   // (the rain's knocks on the leaves: none without it)
    resolve({ vx: vx.reduce((a, b) => a + b, 0) / vx.length, against, left, right, frames: drawn.length, hits, whirl });
  };
  requestAnimationFrame(step);
}), ms);
// The rain's drops for `ms`, a look every 100 ms: where they are across in the window's lower half, counted in columns 32 art px
// wide; how uneven that is across (each look's columns: their variance over their mean, averaged over the looks) and over time (each
// column's counts, look to look: the same, averaged over the columns); scattered evenly at random both are about 1. And the drops'
// mean speed across (px/s, negative leftward), averaged.
const rainSeen = (page, ms) => page.evaluate((ms) => new Promise((resolve) => {
  const looks = [], vx = [];
  let start = 0;
  const spread = (counts) => {   // variance over mean
    const mean = counts.reduce((a, b) => a + b, 0) / counts.length;
    return mean ? counts.reduce((a, b) => a + (b - mean) ** 2, 0) / counts.length / mean : 0;
  };
  const look = () => {
    start = start || performance.now();
    const { xs, width, vx: v } = Sky.state().rain, counts = new Array(Math.floor(width / 32)).fill(0);
    for (const x of xs) if (x < counts.length * 32) counts[Math.floor(x / 32)]++;
    looks.push(counts);
    vx.push(v);
    if (performance.now() - start < ms) return setTimeout(look, 100);
    const columns = looks[0].map((_, i) => looks.map((c) => c[i]));
    resolve({ looks: looks.length, drops: looks.reduce((a, c) => a + c.reduce((x, y) => x + y, 0), 0) / looks.length,
              across: looks.reduce((a, c) => a + spread(c), 0) / looks.length,
              overTime: columns.reduce((a, c) => a + spread(c), 0) / columns.length, vx: vx.reduce((a, b) => a + b, 0) / vx.length });
  };
  // (from 50 frames in, 1.5 s or more of the page's time: the effects faded in, the wind's gusts under way)
  const begun = () => Sky.state().frames >= 50 ? look() : setTimeout(begun, 50);
  begun();
}), ms);
// uv's beams over 40 s of the page's time (on a page whose clock runs on fast: `pace`, below; each frame drawn 0.1 s on, Sky's
// longest step): where each beam's halo was drawn (with uv alone, the glow, then each beam's halo and core), its least and greatest x
// (art px), how many times it turned back (from going one way to the other), and how faint and bright it was
const uvSeen = (page) => page.evaluate(() => new Promise((resolve) => {
  const g = document.querySelector("#sky canvas").getContext("2d"), drawImage = g.drawImage, clearRect = g.clearRect, drawn = [];
  g.clearRect = function (...args) { drawn.push([]); return clearRect.apply(this, args); };   // (a frame begins)
  g.drawImage = function (...args) { if (drawn.length) drawn[drawn.length - 1].push([args[1], this.globalAlpha]); return drawImage.apply(this, args); };
  const step = () => {
    if (drawn.length < 400) return requestAnimationFrame(step);
    const frames = drawn.filter((f) => f.length === drawn[0].length), beams = (frames[0].length - 1) / 2;
    resolve({ frames: frames.length, beams: Array.from({ length: beams }, (_, b) => {
      const at = frames.map((f) => f[1 + 2 * b]);
      let turns = 0, way = 0;
      for (let i = 1; i < at.length; i++) {
        const now = Math.sign(at[i][0] - at[i - 1][0]);
        if (now && way && now !== way) turns++;
        if (now) way = now;
      }
      return { least: Math.min(...at.map((d) => d[0])), most: Math.max(...at.map((d) => d[0])), turns,
               faint: Math.min(...at.map((d) => d[1])), bright: Math.max(...at.map((d) => d[1])) };
    }) });
  };
  requestAnimationFrame(step);
}));
// humidity's beads condensing on the glass and running down it, every frame over `frames` of Sky's (each 0.1 s of the page's time,
// its clock run on fast: `pace`): how many ran; any that went up (between two frames, by more than nothing); the furthest one went
// down from where it began; the furthest one wandered either side of where it began, and how many wandered 2 px or more; their mean
// offset from where they began (px, negative leftward); the lowest any sprite reached (art px, its bottom row + 1) of the window's
// height; where those that ended were last seen (lowest and highest, of the height); how often a bead that had moved had its track
// behind it; the most track pixels wet at once, how many had dried by the end, and of a pixel of each bead's track (as it passed 20
// px down), how many were drier by the end; px run and s taken on an earlier track and off one; how many pixels showed on the
// tracks' canvas where the glass was dry, at the end; how many beads were cut short (gone more than 10 px above where they'd end: a
// frame's run at most is 8.4) and how many still ones vanished (gone at a radius of 1 or more, not faded, not merged into another);
// the beads at the start and at the end; where the still ones appeared (each's height as it first showed, of the window's); merges,
// and the runs done ({merged, r, ran, fade}); every 10th frame, the most pixels drawn in the canvas's top two rows, and the still
// beads drawn whole (r 2 or more, not drying): of the canvas's pixels in their sprites, the alpha (0-255) of those drawn, mean and
// most; the still beads showing and the beads running, each's mean count; and of each bead's track (off an earlier track) as it
// passed 20 px down, a pixel of it: how many px across around it were wiped clear (crisp), the bead's width then, its fog as shown
// (and not wiped) then, the fog shown beside the track (a few px either side of it), and at the end its fog as shown and how wiped
const humiditySeen = (page, frames) => page.evaluate((frames) => new Promise((resolve) => {
  const last = new Map(), lastSites = new Map(), ended = [], first = Sky.state().frames, count = Sky.state().particles.humidity;
  let n = 0, up = 0, down = 0, wander = 0, offset = 0, samples = 0, lowest = 0, moved = 0, behind = 0, wet = 0, cut = 0, popped = 0;
  let top = 0, inked = 0, alphas = 0, most = 0, looked = 0, still = 0, running = 0, counts = 0;
  const wandered = new Set(), trails = new Map(), born = new Map(), wiped = new Map();
  const canvas = document.querySelector("#sky canvas"), g = canvas.getContext("2d");
  const step = () => {
    const h = Sky.state().humidity, now = new Map(h.runners.map((d) => [d.id, d])), absorbed = new Set(h.absorbed);
    for (const [id, d] of last) if (!now.has(id) && !absorbed.has(id)) { ended.push(d.y / h.height); if (d.end - d.y > 10) cut++; }
    const sites = new Map(h.sites.map((s) => [s.id, s]));
    for (const [id, s] of lastSites) if (!sites.has(id) && !now.has(id) && !absorbed.has(id) && s.r >= 1 && s.alpha > 0.15) popped++;
    lastSites.clear();
    for (const [id, s] of sites) { lastSites.set(id, s); if (s.r > 0 && !born.has(id)) born.set(id, s.y / h.height); }
    for (const d of h.runners) {
      const before = last.get(d.id);
      if (!before) n++;
      else if (d.y < before.y - 1e-9) up++;
      down = Math.max(down, d.y - d.y0);
      wander = Math.max(wander, Math.abs(d.x - d.x0));
      if (Math.abs(d.x - d.x0) >= 2) wandered.add(d.id);
      offset += d.x - d.x0; samples++;
      lowest = Math.max(lowest, d.bottom / h.height);
      if (d.y - d.y0 > 10) { moved++; if (d.behind) behind++; }
      if (d.y - d.y0 > 20 && d.trail && !trails.has(d.id)) {
        trails.set(d.id, [...d.trail, h.wetAt(...d.trail)]);
        const [x, y] = d.trail, crisp = (x) => h.cleanAt(x, y) > h.crisp;
        if (!d.on && crisp(x)) {
          let a = x, b = x;
          while (crisp(a - 1)) a--;
          while (crisp(b + 1)) b++;
          const side = [a - 3, a - 2, b + 2, b + 3].map((x) => h.fogAt(x, y)[0]);
          wiped.set(d.id, { across: b - a + 1, w: d.w, at: [x, y], shown: h.fogAt(x, y)[0], fog: h.fogAt(x, y)[1], clean: h.cleanAt(x, y),
                            beside: side.reduce((s, v) => s + v, 0) / side.length });
        }
      }
    }
    wet = Math.max(wet, h.wet);
    last.clear();
    for (const [id, d] of now) last.set(id, d);
    if ((Sky.state().frames - first) % 10 === 0) {   // (the canvas as drawn this frame)
      const rows = g.getImageData(0, 0, canvas.width, 2).data;
      let k = 0;
      for (let i = 3; i < rows.length; i += 4) if (rows[i]) k++;
      top = Math.max(top, k);
      still += h.sites.filter((s) => s.r > 0).length; running += h.runners.length; counts++;
      for (const s of h.sites) if (s.r >= 2 && s.alpha === 1 && s.drawn) {
        const [x, y, w, hh] = s.drawn, d = g.getImageData(x, y, w, hh).data;
        for (let i = 3; i < d.length; i += 4) if (d[i]) { inked++; alphas += d[i]; most = Math.max(most, d[i]); }
        looked++;
      }
    }
    if (Sky.state().frames - first < frames) return requestAnimationFrame(step);
    resolve({ runners: n, up, down, wander, meander: h.meander, wandered: wandered.size, offset: offset / Math.max(samples, 1), lowest,
              ended: ended.length ? [Math.min(...ended), Math.max(...ended)] : null, behind: behind / Math.max(moved, 1), wet,
              dried: h.dried, trails: trails.size, drier: [...trails.values()].filter(([x, y, w]) => h.wetAt(x, y) < w).length,
              on: h.on, off: h.off, stray: h.stray(), cut, popped, count: [count, Sky.state().particles.humidity],
              born: [...born.values()], merges: h.merges, merged: h.merged, done: h.done, largest: h.largest, top,
              look: { beads: looked, mean: alphas / Math.max(inked, 1), most },
              showing: { still: still / Math.max(counts, 1), running: running / Math.max(counts, 1) },
              wiped: [...wiped.values()].map((t) => ({ ...t, end: { shown: h.fogAt(...t.at)[0], clean: h.cleanAt(...t.at) } })),
              fog: h.fog(), first: h.first, refogged: h.refogged, colours: h.colours() });
  };
  requestAnimationFrame(step);
}), frames);
// humidity off (the humidity below 90 %), every frame after till the fog has cleared and no still bead is left (at most `frames`):
// the fog's mean thickness in the window's middle and at its edges as it clears (the first frame each is gone, and the edges' then);
// the still beads showing at the start; each still one as it went (gone at a radius of 1 or more, not faded: popped; else: where
// it was, its distance from the middle, 0 there to 1 at the edges, and the frame); the frames taken and what was left
const humidityClears = (page, frames) => page.evaluate((frames) => new Promise((resolve) => {
  document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { wind: 1 } }));
  const last = new Map(), gone = [], start = Sky.state().humidity.sites.filter((s) => s.r > 0).length, [W, H] = Sky.state().size;
  let n = 0, popped = 0, middleGone = null, edgeGone = null, edgeThen = null, shrank = 0;
  const step = () => {
    const h = Sky.state().humidity, fog = h.fog(), sites = new Map(h.sites.map((s) => [s.id, s])), absorbed = new Set(h.absorbed);
    for (const [id, s] of last) if (!sites.has(id) && !absorbed.has(id) && s.r > 0) {
      if (s.r >= 1 && s.alpha > 0.15) popped++;
      gone.push({ far: Math.max(Math.abs(2 * s.x / W - 1), Math.abs(2 * s.y / H - 1)), frame: n });
    }
    for (const [id, s] of sites) if (last.has(id) && s.r < last.get(id).r - 1e-9) shrank++;
    last.clear();
    for (const [id, s] of sites) last.set(id, s);
    if (middleGone === null && fog.middle < 0.005) { middleGone = n; edgeThen = fog.edge; }
    if (edgeGone === null && fog.edge < 0.005) edgeGone = n;
    n++;
    if (n < frames && (fog.cover > 0 || [...sites.values()].some((s) => s.r > 0))) return requestAnimationFrame(step);
    resolve({ start, middleGone, edgeGone, edgeThen, gone, popped, shrank, frames: n, left: { cover: fog.cover, thickness: fog.thickness,
              still: [...sites.values()].filter((s) => s.r > 0).length }, effects: Sky.state().effects });
  };
  requestAnimationFrame(step);
}), frames);
const framesIn = async (page, ms) => {   // the frames the page asked for in this long
  const before = await page.evaluate(() => window.__frames);
  await page.waitForTimeout(ms);
  return (await page.evaluate(() => window.__frames)) - before;
};

// MCP_DASHBOARD_ANIMATIONS off (the page as ecobot-mcp serves it then: data-animations="off" on its <html>): every effect asked for
// and called for, nothing drawn; the rest as ever
async function animationsOff(browser, url, out) {
  const { context, page, errors, reads } = await weatherPage(browser, url, { query: "?fx=all", animations: "off" });
  await reads({ "UV index": 11, Wind: 60, Temperature: -3, rain_rate: 2, Humidity: 97, "US AQI": 160 });
  const seen = async () => ({ ...await weatherSeen(page), frames: await page.evaluate(() => Sky.state().frames), ink: await canvasInk(page),
                              ...await page.evaluate(() => ({ sky: getComputedStyle(document.getElementById("sky")).display,
                                                              corner: getComputedStyle(document.getElementById("corner")).display,
                                                              row: !!document.getElementById("fxtest") && document.getElementById("fxtest").checkVisibility() })) });
  await page.waitForTimeout(500);   // (time for frames, were any drawn)
  out.off = await seen();
  out.tiles = await titles(page);
  out.strip = await statusText(page);
  await page.mouse.move(...await at(page, "Rain", 0.5, 0.4));
  await calm(page);
  await tileOf(page, "Rain").locator(".close").click();
  await page.waitForTimeout(300);
  const { frames, ink, running } = await seen();
  out.hidden = { strip: await statusText(page), frames, ink, running };
  out.errors = errors;
  await context.close();
}

// The weather in parts, each its own page (and process: the tests run them side by side): "marks" (nothing called for, each effect at
// its mark and just short of it, smog's bands, rain's strength, the ice's depth), "several" (several at once, the canvas, the tiles see-
// through, their look, zoomed), "stop" ("stop animations", a reload, "start animations", the reset), "links" (the temporary test links;
// and beside them, MCP_DASHBOARD_ANIMATIONS off: nothing drawn),
// "all" (?fx=all: its frame time, its brightness against Hazardous smog alone, its particles), "opened" (?fx= when the page opens, uv
// and the wind alone, heavy rain alone and in the wind (on a phone too), uv's beams over 40 s, humidity's beads running down the
// glass alone and in the wind, a phone's strip, reduced motion)
const WEATHER = {
  async marks(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url);
    await calm(page, 5000);   // (the gauges' rise)
    out.nothing = { ...await weatherSeen(page), frames: await framesIn(page, 800), ink: await canvasInk(page) };
    // each mark: just short of it, then at it
    const marks = { rain: [{ rain_rate: 0 }, { rain_rate: 0.1 }], smog: [{ "US AQI": 50 }, { "US AQI": 51 }],
                    uv: [{ "UV index": 10.9 }, { "UV index": 11 }], humidity: [{ Humidity: 89.9 }, { Humidity: 90 }],
                    wind: [{ Wind: 15 }, { Wind: 15.1 }], heat: [{ Temperature: 34.9 }, { Temperature: 35 }],
                    ice: [{ Temperature: 0.1 }, { Temperature: -0.1 }], iceAtZero: [{ Temperature: 0 }, { Temperature: -0.1 }] };
    out.marks = {};
    for (const [name, [below, at]] of Object.entries(marks)) {
      await reads(below);
      const short = await weatherSeen(page);
      await reads(at);
      out.marks[name] = { below: short, at: await weatherSeen(page) };
    }
    out.smogBands = [];
    for (const aqi of [101, 151, 201, 301, 500]) { await reads({ "US AQI": aqi }); out.smogBands.push((await weatherSeen(page)).levels.smog); }
    out.rainStrength = [];
    for (const rate of [0.5, 8, 20]) { await reads({ rain_rate: rate }); out.rainStrength.push((await weatherSeen(page)).levels.rain); }
    out.humidityStrength = [];
    for (const rh of [91, 95, 100]) { await reads({ Humidity: rh }); out.humidityStrength.push((await weatherSeen(page)).levels.humidity); }
    out.iceDepth = [];
    for (const t of [-0.5, -5]) { await reads({ Temperature: t }); out.iceDepth.push((await weatherSeen(page)).levels.ice); }
    out.errors = errors;
    await context.close();
  },
  async several(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url);
    await calm(page, 5000);   // (the gauges' rise)
    // several at once: one canvas, one loop (at most 30 frames a second)
    await reads({ "UV index": 11, Wind: 60, Temperature: -3, rain_rate: 2, Humidity: 97, "US AQI": 160 });
    await calm(page, 5000);   // (the gauges' move to their new readings, MOVE_MS: frames of their own)
    // (the frames asked for and drawn in a second, counted in the page: by its own clock, so no round trip's delay counts)
    const rate = await page.evaluate(() => new Promise((resolve) => {
      const asked = window.__frames, drawn = Sky.state().frames, start = performance.now();
      setTimeout(() => {
        const per = 1000 / (performance.now() - start);
        resolve({ frames: (window.__frames - asked) * per, drawn: (Sky.state().frames - drawn) * per });
      }, 1000);
    }));
    out.several = { ...await weatherSeen(page), canvases: await page.evaluate(() => document.querySelectorAll("canvas").length),
                    ...rate, ink: await canvasInk(page) };
    // the canvas: the window's size, fixed, first in the page, behind the tiles (which come after it), clicks through it. (Behind: its
    // pointer-events on for a moment, so hit-testing sees it, a tile's middle is still the tile's)
    out.canvas = await page.evaluate(() => {
      const sky = document.getElementById("sky"), c = sky.querySelector("canvas"), r = sky.getBoundingClientRect(), cr = c.getBoundingClientRect();
      const st = getComputedStyle(sky);
      return { rect: [r.left, r.top, r.width, r.height], canvas: [cr.width >= innerWidth, cr.height >= innerHeight],
               position: st.position, z: st.zIndex, events: st.pointerEvents, first: document.body.firstElementChild === sky,
               pixelated: getComputedStyle(c).imageRendering,
               under: (() => {
                 const r = [...document.querySelectorAll("#grid .tile")][3].getBoundingClientRect();
                 sky.style.pointerEvents = "auto";
                 const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                 sky.style.pointerEvents = "";
                 return hit !== null && hit.closest(".tile") !== null;
               })() };
    });
    // the tiles see-through: a tile's blank margin shows what's drawn behind it (smog, Hazardous, over the bottom row)
    await reads({ "US AQI": 500 });
    await page.waitForTimeout(1800);
    const margin = async () => {
      const r = await tileOf(page, "PM10").boundingBox();
      return page.screenshot({ clip: { x: r.x + r.width * 0.04, y: r.y + r.height * 0.45, width: r.width * 0.12, height: r.height * 0.15 } });
    };
    const smoggy = await margin();
    await reads({});
    await page.waitForFunction(() => !Sky.state().running);   // (faded out: the loop ended)
    await calm(page);
    const clear1 = await margin(), clear2 = await margin();
    out.through = { differs: !smoggy.equals(clear1), still: clear1.equals(clear2) };
    out.look = await page.evaluate(() => {
      const tile = document.querySelector(".tile"), before = getComputedStyle(tile, "::before");
      return { background: getComputedStyle(tile).backgroundColor, opacity: getComputedStyle(tile).opacity,
               title: getComputedStyle(tile.querySelector(".title")).color, frame: before.borderTopColor };
    });
    await reads({ "UV index": 11 });
    out.look.alertFrame = await page.evaluate(() => getComputedStyle(document.querySelector(".tile.alert"), "::before").borderTopColor);
    out.look.dragFrames = await page.evaluate(() => {   // a framed tile dragged: lifted, then over the remove zone, its frame as any other's
      const tile = document.querySelector(".tile.alert"), frame = () => getComputedStyle(tile, "::before").borderTopColor;
      tile.classList.add("lifted");
      const lifted = frame();
      tile.classList.add("doomed");
      const doomed = frame();
      tile.classList.remove("lifted", "doomed");
      return { lifted, doomed, after: frame() };
    });
    // zoomed: the tile still see-through, its frame still red, the effect still behind it
    const [zx, zy] = await at(page, "UV index", 0.85, 0.3);
    await page.mouse.click(zx, zy);
    await calm(page);
    out.zoomed = await page.evaluate(() => {
      const shown = [...document.querySelectorAll("#grid .tile")].find((t) => !t.inert);
      return { zoomed: document.body.classList.contains("zoomed"), title: shown.querySelector(".title").textContent,
               background: getComputedStyle(shown).backgroundColor, grid: getComputedStyle(document.getElementById("grid")).backgroundColor,
               alert: shown.classList.contains("alert"), running: Sky.state().running };
    });
    out.errors = errors;
    await context.close();
  },
  async stop(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url);
    await calm(page, 5000);   // (the gauges' rise)
    await reads({ "UV index": 11 });
    await calm(page, 5000);   // (the gauge's move: frames of its own)
    // "stop animations": nothing drawn, no loop, the frames stay; kept; "reset preferences" starts them again
    await page.locator("#motion .animations").click();
    await page.waitForTimeout(200);
    out.stopped = { ...await weatherSeen(page), frames: await framesIn(page, 600), ink: await canvasInk(page),
                    stored: JSON.parse(await page.evaluate((key) => localStorage.getItem(key), KEY)).animations };
    await page.reload({ waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll(".tile.alert").length > 0);
    await calm(page, 5000);   // (the gauges' rise)
    out.reloaded = { ...await weatherSeen(page), frames: await framesIn(page, 500) };
    await page.locator("#motion .animations").click();
    await page.waitForTimeout(100);
    out.restarted = { ...await weatherSeen(page), frames: await framesIn(page, 500) };
    await page.locator("#motion .animations").click();   // (stopped again: reset puts them back)
    await page.mouse.move(...await at(page, "Rain", 0.5, 0.4));
    await calm(page);
    await tileOf(page, "Rain").locator(".close").click();
    await calm(page);
    await page.getByText("reset preferences").click();
    await page.waitForTimeout(100);
    out.reset = { ...await weatherSeen(page), frames: await framesIn(page, 500) };
    // from the keyboard: Enter on the link stops them, the focus staying on it (now "start animations")
    await page.focus("#motion .animations");
    await page.keyboard.press("Enter");
    out.keyboard = { ...await weatherSeen(page), focused: await page.evaluate(() => document.activeElement.textContent) };
    out.errors = errors;
    await context.close();
  },
  async links(browser, url, out) {
    const off = animationsOff(browser, url, out.off = {});   // (beside the rest)
    // the temporary test links: each turns its effect and frame on (the gauges keep their readings), "clear" ends them; the URL follows
    const { context, page, errors } = await weatherPage(browser, url);
    await calm(page, 5000);   // (the gauges' rise)
    const fx = (name) => page.locator("#fxtest button", { hasText: new RegExp(`^${name}`) }).first().click();
    const label = () => page.evaluate(() => document.querySelector(".tile[data-id='UV index']").getAttribute("aria-label"));
    out.links = { buttons: await page.evaluate(() => [...document.querySelectorAll("#fxtest button")].map((b) => b.textContent)) };
    for (const name of ["uv", "humidity", "wind", "heat", "ice"]) {
      await fx(name);
      out.links[name] = { ...await weatherSeen(page), url: await page.evaluate(() => location.search) };
    }
    out.links.label = await label();
    out.links.corner = await cornerSeen(page);
    out.links.smog = [];
    for (let i = 0; i < 6; i++) { await fx("smog"); out.links.smog.push((await weatherSeen(page)).levels.smog); }
    out.links.rain = [];
    for (let i = 0; i < 3; i++) { await fx("rain"); out.links.rain.push((await weatherSeen(page)).levels.rain); }
    await page.waitForFunction(() => Sky.state().iceReach > 2.5);   // (the ice grown in a little)
    const grown = await page.evaluate(() => Sky.state().iceReach);
    await fx("ice");   // (off: it melts back, the loop running on until it's gone)
    await page.waitForTimeout(300);
    out.links.melting = { grown, reach: await page.evaluate(() => Sky.state().iceReach), ...await weatherSeen(page) };
    out.links.pressed = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll("#fxtest button[data-fx]")]
      .map((b) => [b.dataset.fx, b.getAttribute("aria-pressed")])));
    out.links.role = await page.evaluate(() => document.getElementById("fxtest").getAttribute("role"));
    out.links.humiditySteps = [];   // (humidity, on: light; then heavy, then off)
    for (let i = 0; i < 2; i++) {
      await fx("humidity");
      out.links.humiditySteps.push({ level: (await weatherSeen(page)).levels.humidity, url: await page.evaluate(() => location.search),
                                     label: await page.evaluate(() => document.querySelector("#fxtest button[data-fx='humidity']").textContent) });
    }
    await fx("clear");
    await page.waitForTimeout(100);
    out.links.cleared = { ...await weatherSeen(page), url: await page.evaluate(() => location.search) };
    // every effect at once: ?fx=all (no link of its own)
    await page.goto(url + "?fx=all", { waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0 && /last reading/.test(document.getElementById("status").textContent));
    out.links.all = { ...await weatherSeen(page), url: await page.evaluate(() => location.search) };
    await off;
    out.errors = [...errors, ...out.off.errors];
    await context.close();
  },
  async all(browser, url, out) {
    const particles = (p) => p.evaluate(() => ({ ...Sky.state().particles, budget: Sky.state().budget }));
    const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=all" });
    await page.waitForTimeout(3000);
    out.cost = await page.evaluate(() => Sky.state().cost);   // (ms of script a frame, with every effect on)
    // the strongest single effect (Hazardous smog), as bright as "all" may be: faded in while the ice grows on
    const strongest = (async () => {
      const smog = await weatherPage(browser, url, { query: "?fx=smog5" });
      await smog.page.waitForFunction(() => Sky.state().frames >= 56);   // (faded in: 1.5 s, each frame 29 ms or more on)
      const light = await canvasLight(smog.page);
      await smog.context.close();
      return light;
    })();
    await page.waitForFunction(() => {   // (the ice grown all the way in: its reach the same for half a second)
      const r = Sky.state().iceReach, now = performance.now();
      if (window.__reach !== r) { window.__reach = r; window.__reachSince = now; }
      return r > 0 && now - window.__reachSince >= 500;
    }, null, { timeout: 15000 });
    out.light = { all: await canvasLight(page) };
    out.particles = { all: await particles(page) };
    out.light.smog5 = await strongest;
    out.errors = errors;
    await context.close();
  },
  async opened(browser, url, out) {
    const particles = (p) => p.evaluate(() => ({ ...Sky.state().particles, budget: Sky.state().budget }));
    // uv's beams over 40 s of the page's time, on a page of their own beside the rest: each swaying about its place
    const swaying = (async () => {
      const uv = await weatherPage(browser, url, { query: "?fx=uv", pace: 10, context: { viewport: { width: 640, height: 480 } } });   // (small: cheap)
      const seen = { ...await uvSeen(uv.page), errors: uv.errors };
      await uv.context.close();
      return seen;
    })();
    // humidity's beads condensing on the glass and running down it over 36 s of the page's time, alone and with the wind, beside the rest
    // (small: cheap)
    // (heavy: 100 %); and light (91 %), watched for 20 s
    const beading = Promise.all(["humidity2", "humidity2,wind", "humidity"].map(async (name) => {
      const p = await weatherPage(browser, url, { query: `?fx=${name}`, pace: 10, context: { viewport: { width: 640, height: 480 } } });
      const seen = await humiditySeen(p.page, name === "humidity" ? 200 : 360);
      if (name === "humidity2,wind") seen.clears = await humidityClears(p.page, 300);   // (then humidity off: the fog clearing)
      if (name === "humidity2") {   // (then every other effect on too, its share of the budget shrinking: watched 4 s more)
        await p.page.evaluate(() => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1, smog: 3, uv: 1, humidity: 1, wind: 1, heat: 1, ice: 5 } })));
        seen.squeezed = await humiditySeen(p.page, 40);
        // (then all off, faded out all the way, and humidity on again: the glass fresh, nothing left from before)
        await p.page.evaluate(() => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: {} })));
        await p.page.waitForFunction(() => !Sky.state().running);
        seen.again = await p.page.evaluate(() => {
          const before = Sky.state().humidity.wet;
          document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { humidity: 1 } }));
          const h = Sky.state().humidity;
          return { before, wet: h.wet, runners: h.runners.length, sites: h.sites.filter((s) => s.r > 0).length, stray: h.stray() };
        });
      }
      seen.errors = p.errors;
      await p.context.close();
      return seen;
    }));
    // heavy rain, alone and in the wind (and in the wind on a phone), its drops watched for 3 s, beside the rest; with the wind, the
    // leaves the drops knocked. (In the wind, the window's size: first light rain, as it fades in, its first few hits on the leaves.)
    const phoneSize = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };
    const raining = Promise.all([["rain2"], ["rain2,wind"], ["rain2,wind", phoneSize]].map(async ([name, context], i) => {
      const p = await weatherPage(browser, url, { query: `?fx=${i === 1 ? "rain,wind" : name}`, context });
      let light = null;
      if (i === 1) {
        await p.page.waitForFunction(() => Sky.state().wind.hits >= 3, null, { timeout: 15000 });
        light = await p.page.evaluate(() => Sky.state().wind);
        await p.page.evaluate(() => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1, wind: 1 } })));
      }
      const seen = { ...await rainSeen(p.page, 3000), leaves: await p.page.evaluate(() => Sky.state().wind), errors: p.errors };
      if (light) {   // (the heavy rain's hits alone: how many, their mean push, the mean change they made in a leaf's velocity)
        const { hits, push, kick } = seen.leaves, heavy = Math.max(hits - light.hits, 1);
        seen.leaves = { ...seen.leaves, hits: hits - light.hits, push: (hits * push - light.hits * light.push) / heavy,
                        kick: (hits * kick - light.hits * light.kick) / heavy };
        seen.light = { hits: light.hits, push: light.push, kick: light.kick };
      }
      await p.context.close();
      return seen;
    }));
    // without the temporary test links (the page as it will be), at 1024x768 and on a phone with a tile hidden: "stop animations" at
    // the bottom left on its own
    const bare = Promise.all([{}, { viewport: { width: 360, height: 740 }, hasTouch: true, isMobile: true }].map(async (context) => {
      const p = await weatherPage(browser, url, { bare: true, context,
        init: { fn: (key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: ["Rain"] })), arg: KEY } });
      await p.reads({ "UV index": 11 });
      const seen = { ...await cornerSeen(p.page), errors: p.errors };
      await p.context.close();
      return seen;
    }));
    // ?fx= when the page opens (each a page of its own, side by side, beside the rest)
    const params = Promise.all([["param", "?fx=smog3,wind,ice"], ["paramAll", "?fx=all"], ["paramRain", "?fx=rain2"],
                                ["paramOld", "?fx=leaves,radiation,drips"]].map(async ([name, query]) => {
      const opened = await weatherPage(browser, url, { query });
      await opened.page.waitForTimeout(200);
      out[name] = { ...await weatherSeen(opened.page), errors: opened.errors };
      await opened.context.close();
    }));
    // uv alone: its beams, no particles; the wind alone: its leaves within the budget, blowing right to left (and looping), watched
    // for 3 s beside the rest
    out.particles = {};
    out.rain = {};
    out.humidity = {};
    const alone = Promise.all(["uv", "wind"].map(async (name) => {
      const alone = await weatherPage(browser, url, { query: `?fx=${name}` });
      if (name === "wind") out.wind = await windSeen(alone.page, 3000);
      else await alone.page.waitForFunction(() => Sky.state().frames >= 20);   // (under way)
      const p = await particles(alone.page);
      out.particles[name] = { [name]: p[name], budget: p.budget };
      await alone.context.close();
    }));
    // a phone, a tile hidden, an effect on (the wind, its leaves looping there too): the strip (reset preferences · last reading) at
    // the bottom right, the time last; the test row and "stop animations" not over it (beside the rest)
    const phoned = (async () => {
      const phone = await weatherPage(browser, url, { query: "?fx=wind", context: { viewport: { width: 360, height: 740 }, hasTouch: true, isMobile: true },
        init: { fn: (key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: ["Rain"] })), arg: KEY } });
      out.phoneWind = await windSeen(phone.page, 2500);
      out.phoneCorner = await cornerSeen(phone.page);
      out.phone = await phone.page.evaluate(() => {
        const a = document.getElementById("fxtest").getBoundingClientRect(), b = document.getElementById("status").getBoundingClientRect();
        return { strip: document.getElementById("status").textContent, right: Math.abs(b.right - innerWidth) < 1 && Math.abs(b.bottom - innerHeight) < 1,
                 apart: a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top, onScreen: a.left >= 0 && a.bottom <= innerHeight };
      });
      await phone.context.close();
    })();
    // reduced motion: no loop, one still, faint frame; the frames as ever
    const still = await weatherPage(browser, url, { context: { reducedMotion: "reduce" } });
    await still.reads({ "UV index": 11, Temperature: -4 });
    await still.page.waitForTimeout(500);
    out.still = { ...await weatherSeen(still.page), frames: await framesIn(still.page, 800), ink: await canvasInk(still.page),
                  stateFrames: await still.page.evaluate(() => Sky.state().frames) };
    out.still.humidity = await still.page.evaluate(() => {   // (humidity on too: its still frame some seconds in)
      document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { humidity: 1 } }));
      const h = Sky.state().humidity;
      return { runners: h.runners.length, swelling: h.sites.filter((s) => s.r > 0).length, wet: h.wet, stray: h.stray(),
               running: Sky.state().running, frames: Sky.state().frames };
    });
    out.still.errors = still.errors;
    await still.context.close();
    await Promise.all([params, alone, phoned]);
    out.uv = await swaying;
    [out.humidity.alone, out.humidity.windy, out.humidityLight] = await beading;
    [out.rain.alone, out.rain.windy, out.rain.phone] = await raining;
    [out.bare, out.barePhone] = await bare;
    out.errors = [];
  },
};

async function weatherMode(browser, url, part) {
  const out = {};
  await WEATHER[part](browser, url, out);
  process.stdout.write(JSON.stringify(out));
}

(async () => {
  const [url, second, third] = process.argv.slice(2);
  const browser = await chromium.launch();
  try {
    if (second === "--live") await live(browser, url);
    else if (second === "--arrive") await arrive(browser, url, third);
    else if (second === "--arrange") await arrangeMode(browser, url, third);
    else if (second === "--touch") await touchMode(browser, url, third);
    else if (second === "--fill") await fillMode(browser, url, JSON.parse(third));
    else if (second === "--zoom") await zoomMode(browser, url, third);
    else if (second === "--zoom-touch") await zoomTouchMode(browser, url, third);
    else if (second === "--weather") await weatherMode(browser, url, third);
    else await sizes(browser, url, second, JSON.parse(third));
  } finally {
    await browser.close();
  }
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
