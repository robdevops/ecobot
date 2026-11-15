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
//     the tiles' see-through look; prints {step: what was seen}.
//   node web_dashboard_browser.js URL --touch: the page at 390x844 with touch (CDP touch events): a long press picks a tile up (buzz,
//     grip and ×, remove zone) and moves it; a short tap is a click; a swipe scrolls; prints {step: what was seen}.
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
  await mouseDrag(page, await at(page, "Humidity", 0.5, 0.3), await at(page, "Rain", 0.5, 0.4));
  out.keptOthers = JSON.parse(await stored(page));
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
  out.tap = { ...await state(), titles: await titles(page) };
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
    else await sizes(browser, url, second, JSON.parse(third));
  } finally {
    await browser.close();
  }
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
