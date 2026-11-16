// The dashboard page in Playwright's Chromium, for tests/test_web_dashboard.py.
//   node web_dashboard_browser.js URL SHOTS SIZES: the page at each size ({name: [width, height]}): a screenshot of each (SHOTS/dashboard-
//     NAME.png, device scale factor 2) and, on stdout, {name: {tiles, rects, columns, scroll, scrollWidth, scrollHeight, status, glass (each
//     tile's [kind, its glass highlights' paths]), errors,
//     zoomed, compass, compassZoomed}}; then the first tile zoomed (Enter): its look, and a screenshot (SHOTS/dashboard-NAME-zoomed.png);
//     then the compass (the wind's direction) zoomed: its look (compassLook), and a screenshot (SHOTS/dashboard-NAME-compass.png).
//   node web_dashboard_browser.js URL --live: the page open at 800x600; prints {label} (the first tile's), waits for a line on stdin
//     (the test has changed the reading, and the wind's direction), then prints {label, same_page, compass (its look, and its needle's
//     turn every frame since), errors} once the tile shows it, or after 15 s.
//   node web_dashboard_browser.js URL --arrive TITLE: the page open at 1024x768; prints the tile's state ({label, faded, opacity, shown,
//     needle}), waits for a line on stdin (the test has given it a reading, or taken it away), then, once its label changes, samples it
//     every frame for 1.5 s and prints {before, after, frames: [{opacity, number}], same_tile, compass: [before, after] (compassLook),
//     errors}.
//   node web_dashboard_browser.js URL --arrange PART: the page at 1024x768 with a mouse, a part of the arranging (arrangeMode):
//     "dragged" (dragging to reorder, the drag's text zones, the grip and the × on mouseover, the tiles' see-through look), "hidden"
//     (the ×, the remove zone, the reset link, the preferences across a reload), "prefs" (the preferences corrupt or from elsewhere (a
//     tile new to them), a click soon after a drop, the focus after the × pressed from the keyboard; beside it, storage blocked),
//     "stale" (the strip's reading time with a device's reading over an hour old, arrangedStale); prints
//     {step: what was seen}.
//   node web_dashboard_browser.js URL --touch PART: the page at 390x844 with touch (CDP touch events), a part (touchMode): "taps" (a
//     short tap is a click; a swipe scrolls), "edge" (held at the bottom edge it scrolls to the page's end only), "held" (a long press
//     picks a tile up (buzz, grip and ×, remove zone) and moves it; held in place, its × hides it); prints {step: what was seen}.
//   node web_dashboard_browser.js URL --zoom PART: the page at 1024x768 with a mouse and keys, a part of the zoom (zoomMode): "in" (the
//     zoom cursors, a click zooming a tile; zoomed, prints {label} (Temperature's, beside the tile shown), waits for a line on stdin (the
//     test has changed its reading), then pages with the keys past the first), "pager" (mouse drags), "wheel" (wheel deltaX), "last"
//     (past the last), "text" (drags across the text, a click back), "single" (a single tile zoomed, the keys and drags bouncing),
//     "lone" (a single tile zoomed: a swipe bouncing, back by a click and by Esc), "out" (a double-click, Esc, the bounce cut short),
//     "keys" (the keyboard, Esc mid-zoom); prints {step: what was seen}.
//   node web_dashboard_browser.js URL --zoom-touch PART: the page at 390x844 with touch, a part (zoomTouchMode): "back" (a tap zooms, a
//     tap goes back where the page was scrolled), "swipes" (zoomed by a tap, swipes (slow, a flick, on the text) page), "later" (a
//     vertical swipe and a long press do nothing), "return" (swiped back, a tap back from another tile); prints {step: what was seen}.
//   (--arrange, --touch, --zoom and --zoom-touch: PART, or several joined by "+" (dragged+hidden+prefs), side by side, each its own page
//     in this one browser: a list of their reports, in that order.)
//   node web_dashboard_browser.js URL --weather PART: the page at 1024x768, its data altered on the way, a part of the weather behind the
//     tiles (WEATHER): each reading at, and just short of, the mark that calls up its effect; the canvas behind the tiles and the tiles'
//     red frames; "stop animation"; the temporary test links; every effect at once (?fx=all: the frame time, the light, the particles);
//     a flash flood;
//     ?fx= as the page opens, a phone, reduced motion; prints {step: what was seen}.
//   node web_dashboard_browser.js URL --fill CASES: for each case {name: [width, height, hidden titles]}, the page with those hidden;
//     prints {name: {rects, columns, scroll}}.
"use strict";
require("module").enableCompileCache?.();   // (Playwright's code compiled once, kept in the temp folder: most of a run's start, each part's)
const { chromium } = require("playwright");
const readline = require("readline");

// The gauges drawn at their readings as the page opens (and each time it's loaded), not rising there from their floor: the page reads
// as hidden (document.hidden, as a tab in the background) till it has drawn its first data, as it then skips every move. For the pages
// that drive the tiles, not their rise (a second of every gauge redrawn each frame, waited out); the rise's own tests, and the weather's
// pages, see it as ever
const RISEN = () => {
  let drawn = false;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => !drawn });
  const get = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await get(...args);
    if (!drawn && /\/dashboard\/data$/.test(String(args[0]))) {
      const json = response.json.bind(response);
      response.json = async () => { const data = await json(); setTimeout(() => { drawn = true; }); return data; };   // (drawn by then)
    }
    return response;
  };
};

async function open(browser, url, width, height, options = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, ...(options.context || {}) });
  if (options.risen) await context.addInitScript(RISEN);
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
      glass: [...document.querySelectorAll(".tile")].map((t) => [t.dataset.kind, [...t.querySelectorAll("svg .glass")].map((e) => e.getAttribute("d"))]),
      // each tile's gauge, in its own px (480 wide): its kind and title, how far its drawing's top is below the title's bottom, the
      // lowest of its drawing and its words, its words (svg text) but its reading, and any band word's element
      gauges: [...document.querySelectorAll(".tile")].map((t) => {
        const box = t.getBoundingClientRect(), k = 480 / box.width, g = t.querySelector("svg g.gauge").getBoundingClientRect();
        const texts = [...t.querySelectorAll("svg text")];
        return { kind: t.dataset.kind, title: t.querySelector(".title").textContent, gap: (g.top - t.querySelector(".title").getBoundingClientRect().bottom) * k,
                 bottom: Math.max(g.bottom, ...texts.map((e) => e.getBoundingClientRect().bottom)) * k - box.top * k,
                 words: texts.filter((e) => !e.classList.contains("reading")).map((e) => e.textContent), bands: t.querySelectorAll("svg .band").length };
      }),
    })), errors };
    await handsSettled(page);
    report[name].hands = await handsLook(page);   // the dials' thin red hands: the past hour's highest
    await compassSettled(page);
    report[name].compass = await compassLook(page);   // the wind's direction in the grid: its compass, no table
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
               glass: [...t.querySelectorAll("svg .glass")].map(rect),
               table: [...new Set(table.map((c) => `${c.visibility} ${c.opacity}`))] };
    });
    // back, then the compass zoomed (Enter): its look as the first's, and a screenshot (SHOTS/dashboard-NAME-compass.png)
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.body.classList.contains("zoomed") && !document.querySelector(".tile.zooming, .tile.fading"));
    await page.focus('.tile[data-kind="compass"]');
    await page.keyboard.press("Enter");
    await calm(page, 400);
    await page.screenshot({ path: `${shots}/dashboard-${name}-compass.png` });
    report[name].compassZoomed = await compassLook(page);
    await context.close();
  }));
  process.stdout.write(JSON.stringify(Object.fromEntries(Object.keys(wanted).map((name) => [name, report[name]]))));
}

// Each tile's red hand (null without one): its turn, shown or not, its colour and width, where it is among the dial's parts (after the
// needle, before the hub: [needle, hand, hub] indexes), its drawn box's narrower side against the needle's; with the Wind tile's table
const handsLook = (page) => page.evaluate(() => Object.fromEntries([...document.querySelectorAll("#grid .tile")].map((t) => {
  const hand = t.querySelector("svg .hour"), title = t.querySelector(".title").textContent;
  if (!hand) return [title, null];
  const parts = [...t.querySelector("svg > g.gauge").children], line = hand.querySelector("path");
  const hub = parts.filter((e) => e.tagName === "circle").pop(), needle = t.querySelector("svg > g.gauge > polygon");
  return [title, { turn: hand.getAttribute("transform"), shown: hand.style.display !== "none", colour: line.getAttribute("stroke"),
                   width: Number(line.getAttribute("stroke-width")), needleWidth: needle.getBBox().width,
                   order: [parts.indexOf(needle), parts.indexOf(hand), parts.indexOf(hub)],
                   heads: title === "Wind" ? [...t.querySelectorAll(".chip.head")].map((e) => e.textContent) : undefined,
                   daily: title === "Wind" ? [...t.querySelectorAll(".figure")].slice(0, 2).map((e) => e.textContent) : undefined }];
})));
// The red hands at rest (the opening's rise or a move over): none turned since the last look, 150 ms before, rather than a fixed wait
const handsSettled = (page) => page.waitForFunction(() => {
  const now = [...document.querySelectorAll("#grid .tile svg .hour")].map((h) => h.getAttribute("transform")).join("|");
  const still = now && now === window.__hands;
  window.__hands = now;
  return still;
}, null, { timeout: 5000, polling: 150 }).catch(() => { /* reported as it is */ });

// The compass tile's look: its label, reading, needle's turn, the points named, any table, where its parts are (the window's px) and
// its needle's head and tail
const compassLook = (page) => page.evaluate(() => {
  const t = document.querySelector('.tile[data-kind="compass"]'), rect = (e) => e.getBoundingClientRect().toJSON();
  return { label: t.getAttribute("aria-label"), reading: t.querySelector("svg text.reading").textContent, faded: t.classList.contains("faded"),
           turn: t.querySelector(".needle").getAttribute("transform"), points: [...t.querySelectorAll("svg text.point")].map((e) => e.textContent),
           table: t.querySelectorAll(".unit, .chip, .figure, .tablearea").length, zoomed: document.body.classList.contains("zoomed"),
           shown: !t.inert, box: rect(t), title: rect(t.querySelector(".title")), texts: [...t.querySelectorAll("svg text")].map(rect),
           ring: rect(t.querySelector("svg circle")), head: rect(t.querySelector(".needle .head")),
           // (its needle's two ends: the head's and the tail's colour, how opaque, their size (the drawn box's area, the window's px²))
           ends: [".head", ".tail"].map((k) => {
             const e = t.querySelector(`.needle ${k}`), c = getComputedStyle(e), r = rect(e);
             return { colour: k === ".head" ? c.fill : c.stroke, opacity: Number(k === ".head" ? c.fillOpacity : c.strokeOpacity) * Number(c.opacity),
                      area: r.width * r.height, length: Math.hypot(r.width, r.height) };
           }) };
});

// The compass at rest: its needle turned all the way to the reading its label gives (north with none), rather than a fixed wait
const compassSettled = (page) => page.waitForFunction(() => {
  const t = document.querySelector('.tile[data-kind="compass"]'), turn = t.querySelector(".needle").getAttribute("transform");
  const m = /(\d+)° /.exec(t.getAttribute("aria-label"));
  return turn.startsWith(`rotate(${m ? m[1] : 0}.00 `);
}, null, { timeout: 5000 }).catch(() => { /* reported as it is */ });

async function live(browser, url) {
  const { page, errors } = await open(browser, url, 800, 600);
  const label = () => page.evaluate(() => document.querySelector(".tile").getAttribute("aria-label"));
  await page.evaluate(() => { window.__mark = 1; document.querySelector(".tile").__mark = 1; });
  // (the compass's needle, every frame from now, the opening's rise over: the wind's direction changes with the temperature)
  await compassSettled(page);
  await handsSettled(page);
  await page.evaluate(() => {
    window.__turns = [];
    window.__hand = [];   // (and the Wind's red hand, every frame: its past hour's highest gust rises)
    const needle = document.querySelector('.tile[data-kind="compass"] .needle');
    const wind = [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === "Wind").querySelector("svg .hour");
    const sample = () => {
      window.__turns.push(Number(/rotate\(([-\d.]+)/.exec(needle.getAttribute("transform"))[1]));
      window.__hand.push(Number(/rotate\(([-\d.]+)/.exec(wind.getAttribute("transform"))[1]));
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
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
  await compassSettled(page);
  const compass = { ...await compassLook(page), turns: await page.evaluate(() => window.__turns) };
  await handsSettled(page);
  const hands = { ...await handsLook(page), frames: await page.evaluate(() => window.__hand) };
  process.stdout.write(JSON.stringify({ label: await label(), same_page: same, same_tile: sameTile, shown, compass, hands, errors }) + "\n");
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
  await compassSettled(page);
  const before = await state(), compassBefore = await compassLook(page);
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
  await compassSettled(page);
  process.stdout.write(JSON.stringify({ before, after: await state(), frames, same_tile: sameTile, compass: [compassBefore, await compassLook(page)],
                                        errors }) + "\n");
}

// ---------- arranging ----------
const KEY = "ecobot.dashboard.v1";
const titles = (page) => page.evaluate(() => [...document.querySelectorAll("#grid .tile .title")].map((t) => t.textContent));
const tileOf = (page, title) => page.locator(".tile", { has: page.locator(".title", { hasText: new RegExp(`^${title}$`) }) });
// The page at rest, rather than a fixed wait: nothing in its document changed (a gauge's needle, a tile's transform, a class) for 50 ms
// and two frames (a busy machine's frames come far apart: a gauge moving changes something every frame), and no animation or transition
// running (an endless one aside); at most `cap` ms: the fixed wait it stands for where that was a margin, longer for the gauges' rise
// and moves (which the frames counted after them must not include, however slow the machine is just then). (Not for what a timer
// starts later, a long press or a swipe's end: a wait for that.)
const calm = (page, cap = 450) => page.evaluate((cap) => new Promise((resolve) => {
  const start = performance.now();
  let changed = start, changes = 0, was = -1, quiet = 0;
  const seen = new MutationObserver(() => { changed = performance.now(); changes++; });
  seen.observe(document.documentElement, { subtree: true, attributes: true, childList: true, characterData: true });
  const moving = () => document.getAnimations().some((a) => a.playState === "running" && a.effect.getComputedTiming().endTime !== Infinity);
  const check = (now) => {
    if (moving()) changed = Math.max(changed, now);
    quiet = moving() || changes !== was ? 0 : quiet + 1;
    was = changes;
    if ((quiet >= 2 && performance.now() - changed >= 50) || now - start >= cap) { seen.disconnect(); resolve(); } else requestAnimationFrame(check);
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

// In three parts (each its own page: the tests run them side by side): "dragged" (dragged, and drags on the text), "hidden" (dragged,
// then hidden, reloaded and reset) and "prefs" (preferences unreadable or from elsewhere, a click soon after a drop and the × from the
// keyboard; then storage blocked, on a page of its own beside it), and "stale" (the strip with a device's reading over an hour old).
async function arrangeMode(browser, url, part) {
  const out = {}, errors = [];
  if (part === "dragged") await arranged(browser, url, out, errors);
  else if (part === "hidden") await arrangedHidden(browser, url, out, errors);
  else if (part === "stale") await arrangedStale(browser, url, out, errors);
  else await Promise.all([arrangedFromPrefs(browser, url, out, errors), arrangedBlocked(browser, url, out)]);
  out.errors = errors;
  return out;
}
async function arranged(browser, url, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768, { risen: true });
  await calm(page);   // (at rest: drawn at their readings, risen)
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
  allErrors.push(...errors);
  await context.close();
}
async function arrangedHidden(browser, url, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768, { risen: true });
  await calm(page);   // (at rest: drawn at their readings, risen)
  // (dragged as on the other page: Temperature to Atmospheric pressure's place)
  await mouseDrag(page, await at(page, "Temperature", 0.85, 0.3), await at(page, "Atmospheric pressure", 0.5, 0.4));
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
  const { context, page, errors } = await open(browser, url, 1024, 768, { risen: true });
  const initial = await titles(page);
  // preferences corrupt: the defaults; a tile new to the saved order: in its usual place (after the tile before it by default)
  await page.evaluate((key) => localStorage.setItem(key, "{not json"), KEY);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  out.corrupt = await titles(page);
  const saved = initial.filter((t) => !["Rain", "Temperature", "Wind direction"].includes(t)).concat(["Temperature", "Gone sensor"]);
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
// The strip's reading time as the devices' readings age: the data's `devices` replaced on the way in by readings this old (ms; null: none
// held; a device left out: not configured), fetched again (as the page does when it is shown again); each step's {text, grey (the
// greyed-out parts), sent (the devices as sent)}. Last, a device turning stale with no new data: {before, after, ms (till it did)}.
const STALE_STEPS = { fresh: { station: 0, air: 60e3 }, airStale: { station: 0, air: 2 * 3600e3 }, recovered: { station: 0, air: 0 },
                      stationStale: { station: 3 * 3600e3, air: 30e3 }, bothStale: { station: 3 * 3600e3, air: 2 * 3600e3 },
                      noReading: { station: null, air: 0 }, only: { station: 2 * 3600e3 }, onlyFresh: { station: 0 } };
async function arrangedStale(browser, url, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768, { init: { fn: () => {
    window.__fetched = 0;
    const get = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await get(...args);
      if (/\/dashboard\/data$/.test(String(args[0]))) {
        const json = response.json.bind(response);
        response.json = async () => {
          const data = await json();
          if (window.__ages) data.devices = window.__sent = Object.fromEntries(Object.entries(window.__ages).map(([d, age]) =>
            [d, age === null ? null : new Date(Date.now() - age).toISOString()]));
          window.__fetched++;
          return data;
        };
      }
      return response;
    };
  } } });
  const strip = () => page.evaluate(() => ({ text: document.getElementById("status").textContent, sent: window.__sent || null,
                                             grey: [...document.querySelectorAll("#status .stale")].map((e) => e.textContent) }));
  const send = async (ages) => {
    const n = await page.evaluate((ages) => { window.__ages = ages; document.dispatchEvent(new Event("visibilitychange")); return window.__fetched; }, ages);
    await page.waitForFunction((n) => window.__fetched > n, n);
    return strip();
  };
  out.stale = { real: await strip() };
  for (const [name, ages] of Object.entries(STALE_STEPS)) {
    out.stale[name] = await send(ages);
    if (name === "airStale") out.stale.colours = await page.evaluate(() => [getComputedStyle(document.getElementById("status")).color,
                                                                            getComputedStyle(document.querySelector("#status .stale")).color]);
  }
  // the station's reading turning an hour old 1.5 s from now, the air sensor's already stale: no new data, the strip greys out by itself
  const turning = { before: await send({ station: 3600e3 - 1500, air: 2 * 3600e3 }) };
  const t0 = Date.now();
  await page.waitForFunction(() => [...document.querySelectorAll("#status .stale")].some((e) => /^last reading/.test(e.textContent)), null, { timeout: 6000 });
  turning.ms = Date.now() - t0;
  turning.after = await strip();
  out.stale.turning = turning;
  allErrors.push(...errors);
  await context.close();
}
async function arrangedBlocked(browser, url, out) {
  // storage blocked: the page works, nothing saved, no error
  const blocked = await open(browser, url, 1024, 768, { risen: true, init: { fn: () => {
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

// Touch through the browser's own input (CDP), as a finger: the browser decides on scrolling, long presses and taps. In three parts (each
// its own page: the tests run them side by side): "taps" (a tap and a swipe), "edge" (a long press at the bottom edge) and "held" (a
// tile picked up, moved and dropped, then one held in place and hidden by its ×).
async function touchMode(browser, url, part) {
  const out = {}, errors = [];
  await touchPage(browser, url, part, out, errors);
  out.errors = errors;
  return out;
}
async function touchPage(browser, url, part, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 390, 844, { risen: true,
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
  await calm(page);   // (at rest: drawn at their readings, risen)
  if (part === "held") await touchHeld(page, touch, state, out);
  else if (part === "edge") await touchEdge(page, touch, out);
  else {
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
}
async function touchEdge(page, touch, out) {
  // held near the bottom edge: the page scrolls to its end and no further (the lifted tile mustn't stretch it without end)
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
  // every frame for `ms`, and on till no animation is left to finish (one held paused too: at most 5 s): the tile's rect (title given)
  // or the strip's offset; when the last click or key came; and the animations it started there and then, as it was handled
  // (window.__timed): how long they run on their own timeline, and whether they finished, by the end
  const going = () => document.getAnimations().some((a) => !["finished", "idle"].includes(a.playState) && a.effect && a.effect.getComputedTiming().endTime !== Infinity);
  window.__record = (title, ms) => new Promise((resolve) => {
    const frames = [], start = performance.now();
    window.__timed = null;
    const step = (now) => {
      if (title) {   // (and its title's place, and its table's opacity: a figure's, as drawn)
        const t = window.__tile(title), r = t.getBoundingClientRect(), h = t.querySelector(".title").getBoundingClientRect();
        const f = getComputedStyle(t.querySelector(".figure"));
        frames.push({ t: now, left: r.left, top: r.top, width: r.width, height: r.height, titleTop: h.top, titleSize: h.height,
                      table: f.visibility === "hidden" ? 0 : +f.opacity });
      }
      else frames.push({ t: now, x: window.__track() });
      window.__drawn = now;
      if (now - start < ms || (going() && now - start < 5000)) return requestAnimationFrame(step);
      const timed = window.__timed && { end: window.__timed.end, finished: window.__timed.anims.every((a) => a.playState === "finished") };
      resolve({ frames, at: window.__at, timed });
    };
    requestAnimationFrame(step);
  });
  // (the animations a click or a key started as it was handled, in its own task (not a frame or a timer later), and how long they run
  // on their own timeline (Animation's: their delay and duration), from their start)
  window.__started = (anims) => {
    window.__timed = { end: Math.max(...anims.map((a) => a.effect.getComputedTiming().endTime)), anims };
  };
  addEventListener("click", () => { window.__at = performance.now(); }, true);
  addEventListener("click", () => {   // (after the page's own: the zoom's animations started)
    window.__anims = document.getAnimations().filter((a) => !(a instanceof CSSTransition)).map((a) => a.effect.getTiming().duration);
    const zoom = document.getAnimations().filter((a) => a.id === "zoom" && a.playState !== "finished");
    if (zoom.length) window.__started(zoom);
  });
  addEventListener("keydown", (e) => { window.__at = performance.now(); setTimeout(() => window.__keys.push([e.key, e.defaultPrevented])); });
  addEventListener("wheel", (e) => { setTimeout(() => window.__wheels.push(e.defaultPrevented)); }, { passive: true });
  window.__keys = []; window.__wheels = [];
  // A tile's look: its box; its title, reading and the gauge's other words, each on the screen; its table's visibility and opacity
  window.__look = (title) => {
    const t = window.__tile(title), rect = (e) => e && e.getBoundingClientRect().toJSON();
    const table = [...t.querySelectorAll(".unit, .chip, .figure, .tablearea")].map((e) => getComputedStyle(e));
    return { box: rect(t), title: rect(t.querySelector(".title")), reading: rect(t.querySelector("svg text.reading")),
             texts: [...t.querySelectorAll("svg text")].map(rect).filter((r) => r.width),
             table: [...new Set(table.map((c) => `${c.visibility} ${c.opacity}`))] };
  };
};
// A recording summed up: where it started and ended, whether it passed through between, and how long after the click or key it came to
// rest: on the page's own clock, the animations the click or key started as it was handled, their length on their own timeline, once
// they finished there (null if none started then, they didn't finish, or it never came to rest) - not the frames' times, which a busy
// machine spaces out as it likes, nor the handler's, which it slows as it likes
function motion(rec, key) {
  const f = rec.frames, last = f[f.length - 1], same = (a, b) => key.every((k) => Math.abs(a[k] - b[k]) < 0.5);
  const after = f.filter((x) => x.t >= rec.at);
  const rest = after.find((x, i) => after.slice(i).every((y) => same(y, last)));
  const { timed } = rec;
  return { first: after[0], last, between: after.some((x) => !same(x, after[0]) && !same(x, last)),
           settledMs: rest && timed && timed.finished ? Math.round(timed.end) : null,
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
  await page.evaluate(() => addEventListener("keydown", () => {   // (after the page's own: the slide or nudge it started)
    getComputedStyle(document.getElementById("grid")).transform;   // (its transition made now, as the next frame would)
    const anims = document.getElementById("grid").getAnimations().filter((a) => a.playState !== "finished");
    if (anims.length) window.__started(anims);
  }, { once: true }));
  await page.waitForTimeout(50);
  await page.keyboard.press(key);
  const rec = await recording;
  await page.waitForTimeout(Math.max(0, wait - 400));
  const m = motion(rec, ["x"]);
  return { ...await page.evaluate(() => window.__state()), settledMs: m.settledMs, between: m.between, extreme: Math.round(m.extreme) };
}
// Input on a clock of its own (CDP's timestamps, the events' timeStamp in the page): each event `gap` ms after the one before as the
// page sees it, however late it reaches the page on a busy machine, so a quick flick stays quick and a slow drag slow (the page's
// velocities are of its events' times); a gesture starting at the time now
async function clocked(page) {
  const cdp = page.__cdp || (page.__cdp = await page.context().newCDPSession(page));
  let t = Math.max(Date.now(), page.__inputT || 0);
  const at = (gap) => { t += gap; page.__inputT = t; return t / 1000; };
  return {
    mouse: (type, [x, y], gap, more = {}) => cdp.send("Input.dispatchMouseEvent", { type, x, y, timestamp: at(gap), ...more }),
    wheel: ([x, y], deltaX, gap) => cdp.send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX, deltaY: 0, timestamp: at(gap) }),
    touch: (type, points, gap) => cdp.send("Input.dispatchTouchEvent", { type, touchPoints: points.map(([x, y]) => ({ x, y })), timestamp: at(gap) }),
  };
}
async function pagerDrag(page, [x0, y], dx, { steps = 12, pause = 300 } = {}) {   // a mouse drag sideways; the strip's offset midway
  const input = await clocked(page), held = { button: "left", buttons: 1 };
  await input.mouse("mouseMoved", [x0, y], 0);
  await input.mouse("mousePressed", [x0, y], 8, { ...held, clickCount: 1 });
  for (let i = 1; i <= steps; i++) await input.mouse("mouseMoved", [x0 + dx * i / steps, y], 8, held);   // (one after another, quickly)
  const during = await page.evaluate(() => window.__track());
  await page.waitForTimeout(pause);
  await input.mouse("mouseReleased", [x0 + dx, y], Math.max(pause, 8), { button: "left", buttons: 0, clickCount: 1 });
  await calm(page);
  return { during: Math.round(during), ...await page.evaluate(() => window.__state()) };
}
// a trackpad's swipe: n wheel events of deltaX dx, `gap` ms apart (on the input's clock, and waited); then (no wheel event for
// WHEEL_IDLE_MS, the strip settling) at rest
async function wheelSwipe(page, n, dx, gap) {
  const input = await clocked(page);
  for (let i = 0; i < n; i++) { await input.wheel([800, 300], dx, i ? gap : 0); await page.waitForTimeout(gap); }
  await swiped(page);
}
const swiped = async (page) => { await page.waitForTimeout(160); await calm(page); };
const cursorAt = (page, [x, y]) => page.evaluate(([x, y]) => getComputedStyle(document.elementFromPoint(x, y)).cursor, [x, y]);

// The zoom in nine parts, each its own page (the tests run them side by side): "in" (the cursors, a click zooming a tile, the live
// reading beside it, the keys), "pager" (zoomed: mouse drags), "wheel" (zoomed, the first: a trackpad's swipes), "last" (zoomed: the last tile, paging past it),
// "text" (zoomed, the last tile: a drag across the text, a click back out), "single" (a single tile zoomed, the keys and drags only
// bouncing), "lone" (a single tile zoomed: a trackpad's swipe bouncing, a click back, Esc back), "out" (a double-click, Esc, the
// bounce cut short), "keys" (the keyboard, Esc mid-way through a zoom-in)
async function zoomMode(browser, url, part) {
  const out = {}, errors = [];
  await zoomPage(browser, url, part, out, errors);
  out.errors = errors;
  return out;
}
async function zoomPage(browser, url, part, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768, { risen: true, init: { fn: ZOOM_HELPERS } });
  await calm(page);   // (at rest: drawn at their readings, risen)
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
  } else if (part === "wheel") {
    // (on a page of its own:) zoomed on Humidity, by a click on its gauge, then ← to the first; a trackpad's two-finger swipe: wheel
    // deltaX, kept from the browser
    await page.mouse.click(...await at(page, "Humidity", 0.85, 0.3));
    await calm(page);
    await page.keyboard.press("ArrowLeft");
    await calm(page, 500);
    out.wheel = { from: await page.evaluate(() => window.__state()) };
    await wheelSwipe(page, 8, 50, 16);
    out.wheel.far = await page.evaluate(() => window.__state());
    await wheelSwipe(page, 6, 10, 60);   // (slowly: no flick)
    out.wheel.short = await page.evaluate(() => window.__state());
    await wheelSwipe(page, 8, -50, 16);
    out.wheel.back = await page.evaluate(() => window.__state());
    out.wheel.prevented = await page.evaluate(() => window.__wheels);
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
  } else if (part === "last" || part === "text") {
    // zoomed on Humidity, by a click on its gauge
    await page.mouse.click(...await at(page, "Humidity", 0.85, 0.3));
    await calm(page);
    // the last tile: → past it bounces, as does a swipe on
    const n = (await titles(page)).length;
    for (let i = 1; i < n; i++) { await page.keyboard.press("ArrowRight"); await page.waitForTimeout(30); }   // (from the second: once past the last)
    await calm(page, 500);
    const reached = await page.evaluate(() => window.__state());
    if (part === "last") {
      out.last = { reached, pastLast: await keyStep(page, "ArrowRight") };
      await wheelSwipe(page, 8, 50, 16);
      out.last.wheeled = await page.evaluate(() => window.__state());
      out.last.dragged = await pagerDrag(page, [800, 300], -400);
      allErrors.push(...errors);
      await context.close();
      return;
    }
    // (on a page of its own, beside that one, the last reached the same way:) a drag on the text selects it: no paging, still zoomed
    const shown = reached.shown;
    out.textOn = shown;
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
  } else if (part === "single" || part === "lone") {
    // one tile only: the zoom cursor, a click zooms it (title and gauge, no table); the keys, a drag and a trackpad's swipe only
    // bounce (a gallery of one); a click goes back, Esc too
    await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: [...document.querySelectorAll(".tile .title")].map((t) => t.textContent).filter((t) => t !== "Humidity") })), KEY);
    await page.reload({ waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll(".tile").length === 1);
    await calm(page);   // (at rest: drawn at its reading, risen)
    const one = await at(page, "Humidity", 0.85, 0.3);
    const gridRect = await tileOf(page, "Humidity").boundingBox();
    const single = part === "single" ? (out.single = { cursor: await cursorAt(page, one), gridRect }) : (out.lone = { gridRect });
    await page.mouse.click(...one);
    await calm(page);
    if (part === "single") {
      single.zoomed = { ...await page.evaluate(() => window.__state()), cursor: await cursorAt(page, one),
                        look: await page.evaluate(() => window.__look("Humidity")) };
      single.keys = { right: await keyStep(page, "ArrowRight"), left: await keyStep(page, "ArrowLeft"), space: await keyStep(page, " ") };
      single.dragged = { left: await pagerDrag(page, [800, 300], -400), right: await pagerDrag(page, [200, 300], 400) };
      allErrors.push(...errors);
      await context.close();
      return;
    }
    // (on a page of its own, beside that one, zoomed the same way:) a trackpad's swipe; a click back; zoomed again, Esc back
    await wheelSwipe(page, 8, 50, 16);
    single.wheeled = { ...await page.evaluate(() => window.__state()), prevented: await page.evaluate(() => window.__wheels) };
    await page.mouse.click(...one);
    await calm(page);
    single.back = { ...await page.evaluate(() => window.__state()), rect: await tileOf(page, "Humidity").boundingBox() };
    await page.mouse.click(...one);
    await calm(page);
    await page.keyboard.press("Escape");
    await calm(page);
    single.esc = await page.evaluate(() => window.__state());
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
    // (on the animations' own timelines, not by waits around round trips to the page: the bounce held 30 ms in till the next key
    // comes; the step that key starts, where it is 90-130 ms in, read as it starts)
    const holdBounce = () => page.evaluate(() => addEventListener("keydown", () => {
      for (const a of document.getElementById("grid").getAnimations()) if (!(a instanceof CSSTransition)) { a.pause(); a.currentTime = 30; }
    }, { once: true }));
    await holdBounce();
    await page.keyboard.press("ArrowRight");   // (past the last: the bounce)
    await page.evaluate(() => addEventListener("keydown", () => {
      const grid = document.getElementById("grid");
      getComputedStyle(grid).transform;   // (its transition made now, as the next frame would)
      const step = grid.getAnimations().find((a) => a instanceof CSSTransition);
      window.__mid = [];
      if (!step) return;
      for (const t of [90, 100, 110, 120, 130]) { step.currentTime = t; window.__mid.push(Math.round(window.__track())); }
      step.currentTime = 0;
    }, { once: true }));
    await page.keyboard.press("ArrowLeft");
    const near100 = await page.evaluate(() => window.__mid);
    await calm(page);
    out.bounceCut = { step: { mid: near100, ...await page.evaluate(() => window.__state()) } };
    await page.keyboard.press("ArrowRight"); await calm(page);
    await holdBounce();
    await page.keyboard.press("ArrowRight");   // (the bounce again)
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
      // (on the zoom's own timeline, not by waits around round trips to the page: as the click is handled, its animations 30 ms in,
      // the others' tables then; then 100 ms in, held there till the Esc comes, however long that takes)
      await page.evaluate(() => addEventListener("click", () => {
        const zoom = document.getAnimations().filter((a) => a.id === "zoom");
        for (const a of zoom) { a.pause(); a.currentTime = 30; }
        window.__others = window.__look("Temperature").table;
        for (const a of zoom) a.currentTime = 100;
      }, { once: true }));
      await page.waitForTimeout(50);
      await page.mouse.click(...await at(page, "Humidity", 0.85, 0.3));
      const others = await page.evaluate(() => window.__others);
      await page.waitForFunction(() => window.__drawn > window.__at);   // (recorded there, drawn)
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
// In four parts (each its own page: the tests run them side by side): "back" (a tap zooming the tile, a tap straight back), "swipes"
// (the same tile zoomed by a tap, then the swipes), "later" (the same tile zoomed by a tap and two on, then a vertical swipe and a long
// press) and "return" (the same tile zoomed by a tap and two on, then three swipes back and a tap back from there).
async function zoomTouchMode(browser, url, part) {
  const out = {};
  const { context, page, errors } = await open(browser, url, 390, 844, { risen: true, context: { hasTouch: true, isMobile: true }, init: { fn: () => {
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
  const swipe = async ([x, y], dx, { steps = 10, pause = 250, dy = 0 } = {}) => {   // (on the input's clock: a flick stays quick)
    const input = await clocked(page), gap = pause ? 16 : 8;
    await input.touch("touchStart", [[x, y]], 0);
    for (let i = 1; i <= steps; i++) { await input.touch("touchMove", [[x + dx * i / steps, y + dy * i / steps]], gap); if (pause) await page.waitForTimeout(16); }
    await page.waitForTimeout(pause);
    await input.touch("touchEnd", [], Math.max(pause, 8));
    await calm(page, 500);
    return page.evaluate(() => window.__state());
  };
  await calm(page);   // (at rest: drawn at their readings, risen)
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
  } else if (part === "swipes") await zoomTouchSwipes(page, touch, tap, swipe, out);
  else await zoomTouchLater(page, touch, tap, swipe, part, out);
  out.errors = errors;
  await context.close();
  return out;
}
async function zoomTouchSwipes(page, touch, tap, swipe, out) {
  out.swipesTapped = out.tapped;
  out.next = await swipe([300, 300], -250);
  out.previous = await swipe([90, 300], 250);
  out.flick = await swipe([300, 300], -80, { steps: 4, pause: 0 });
  const title = await centreOf(page, tileOf(page, out.flick.shown).locator(".title"));
  out.fromTitle = await swipe([title[0] + 100, title[1]], -250);
}
async function zoomTouchLater(page, touch, tap, swipe, part, out) {
  // (on a page of its own, beside that one: the same tile tapped, then two on, as there; by the keys, quicker than swipes)
  out[`${part}Tapped`] = out.tapped;
  for (let i = 0; i < 2; i++) { await page.keyboard.press("ArrowRight"); await calm(page, 500); }
  if (part === "later") {
    out.vertical = await swipe([200, 400], 10, { dy: -250 });
    // held still: nothing picked up while zoomed
    await touch("touchStart", [[200, 300]]);
    await page.waitForTimeout(700);
    await touch("touchEnd", []);
    await calm(page, 300);
    out.held = { ...await page.evaluate(() => window.__state()), buzz: await page.evaluate(() => window.__buzz),
                 zone: await page.evaluate(() => document.getElementById("remove").classList.contains("on")) };
    return;
  }
  // (on a third page:) swiped back three, then a tap
  out.returnFrom = await page.evaluate(() => window.__state());
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
    const { context, page, errors } = await open(browser, url, width, height, { risen: true, init: { fn: ([key, hidden]) => {
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
// The weather's own clock (window.__skyTest, see the page's loop): every frame of it exactly a 30th of a second on (0.1 s with
// `pace`: as much as a frame of the page's fast time is, Sky's longest step), however long a busy machine took to draw it, so what a
// test sees is counted in the weather's frames and time, never the machine's. `driven`: the weather makes no frames of its own; the
// test makes them (drive(), driveUntil() below), one after another as fast as they're drawn: the same frames from the same start,
// whatever the load. Its watchers go frame by frame with window.__next(f) (f called once the next frame is drawn: on a driven page
// made at once, else the page's next).
// The page with its data altered on the way: `readings` {title: value} for the tiles' readings, rain_rate as it is; the US AQI Good
// unless given (the stand-in's is Moderate)
async function weatherPage(browser, url, options = {}) {
  const state = { readings: {} };
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 }, deviceScaleFactor: 1, ...(options.context || {}) });
  await context.addInitScript(([pace, driven, seed]) => {   // (every frame the page asks for, counted; and every fetch's data read)
    window.__skyTest = { step: pace === 1 ? 1 / 30 : 0.1, driven, seed };
    const port = new MessageChannel(), queued = [];
    port.port1.onmessage = () => queued.shift()();
    window.__next = driven ? (f) => { Sky.advance(1); queued.push(f); port.port2.postMessage(0); } : (f) => requestAnimationFrame(f);
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
  }, [options.pace || 1, !!options.driven, Number(process.env.SKY_SEED) || 0]);   // (SKY_SEED: the weather's randomness seeded otherwise)
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
    if ("gust" in readings) data.gust = readings.gust;              // (the gust now, km/h)
    if (readings.asOf) data.as_of = { ...data.as_of, time: readings.asOf };   // (a new reading: its time)
    if (readings.hour) Object.assign(data.hour_highs, readings.hour);   // (the red hands' past hour)
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
// On a driven page (weatherPage's `driven`): the script's time (the main thread's own CPU time, not the clock's: a busy machine taking
// turns doesn't count) n of the weather's frames take, ms a frame: the median of five runs of n / 5 (a run's mean: the drawing's cost;
// the median: never the run a garbage collection fell in)
async function frameCost(page, n = 60) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable", { timeDomain: "threadTicks" });
  const script = async () => (await cdp.send("Performance.getMetrics")).metrics.find((m) => m.name === "ScriptDuration").value;
  const runs = [];
  for (let i = 0; i < 5; i++) {
    const before = await script();   // (made in a task of the page's own: what the page's script time counts)
    const made = await page.evaluate((n) => new Promise((resolve) => setTimeout(() => { const made = Sky.advance(n); setTimeout(() => resolve(made)); })), Math.ceil(n / 5));
    const after = await script();
    runs.push((after - before) * 1000 / Math.max(made, 1));
  }
  await cdp.detach();
  return runs.sort((a, b) => a - b)[2];
}
// ... n of the weather's frames now (how many made: fewer once nothing is left to draw); or frames till until(arg) holds in the page
// (at most `most`, else an error): how many
const drive = (page, n) => page.evaluate((n) => Sky.advance(n), n);
const driveUntil = (page, until, arg, most = 3000) => page.evaluate(([until, arg, most]) => {
  const holds = (0, eval)(`(${until})`);
  let n = 0;
  while (!holds(arg) && n < most && Sky.advance(1)) n++;
  if (!holds(arg)) throw new Error(`not so after ${n} frames: ${until}`);
  return n;
}, [until.toString(), arg ?? null, most]);
const weatherSeen = (page) => page.evaluate(() => {
  const s = Sky.state(), link = document.querySelector("#motion .animations");
  return { effects: s.effects, levels: s.levels, running: s.running,
           framed: [...document.querySelectorAll("#grid .tile.alert .title")].map((t) => t.textContent),
           link: link ? link.textContent : null };
});
// The bottom corners: "stop animation" (its words, its box), the test row's and its last link's boxes (null without them), the strip's;
// the row's links' words, whether its "test:" shows, whether all of it fits unscrolled, the room left right of the link; whether the
// link is clear of the strip, and on the screen
const cornerSeen = (page) => page.evaluate(() => {
  const box = (e) => e ? e.getBoundingClientRect().toJSON() : null, link = document.querySelector("#motion .animations");
  const row = document.getElementById("fxtest"), buttons = row ? [...row.querySelectorAll("button")] : [];
  const m = box(document.getElementById("motion")), s = box(document.getElementById("status"));
  return { link: link ? link.textContent : null, motion: m, row: box(row), last: box(buttons.at(-1)), strip: s,
           buttons: buttons.map((b) => b.textContent), tag: row ? row.querySelector(".tag").checkVisibility() : null,
           fits: row ? row.scrollWidth <= row.clientWidth && row.scrollLeft === 0 : null, spare: innerWidth - m.right,
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
// The canvas's strongest regular pattern (an ordered dither's grid shows as one): in `rect` of it (power-of-two sides; the largest
// square from its top left if none), what one sees (each pixel's luminance by its alpha) less its 5x5 neighbourhood's mean (the fine
// texture only: the dithering), windowed (Hann) and through a 2D FFT; of its frequencies of wavelengths 8 px or shorter, the
// strongest's power over their median's ({ratio, at: [u, v]}). Irregular dithering: under a hundred or so; an ordered 4x4 one, thousands
const gridPeak = (page, rect) => page.evaluate((rect) => {
  const c = document.querySelector("#sky canvas"), side = 1 << Math.floor(Math.log2(Math.min(c.width, c.height)));
  const [X, Y, w, h] = rect || [0, 0, side, side];
  const d = c.getContext("2d").getImageData(X, Y, w, h).data, v = new Float64Array(w * h);
  for (let i = 0; i < w * h; i++) v[i] = (0.2126 * d[i * 4] + 0.7152 * d[i * 4 + 1] + 0.0722 * d[i * 4 + 2]) * d[i * 4 + 3] / 255;
  const re = new Float64Array(w * h), im = new Float64Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let sum = 0, n = 0;
    for (let yy = Math.max(0, y - 2); yy <= Math.min(h - 1, y + 2); yy++) for (let xx = Math.max(0, x - 2); xx <= Math.min(w - 1, x + 2); xx++) { sum += v[yy * w + xx]; n++; }
    re[y * w + x] = (v[y * w + x] - sum / n) * (0.5 - 0.5 * Math.cos(2 * Math.PI * (x + 0.5) / w)) * (0.5 - 0.5 * Math.cos(2 * Math.PI * (y + 0.5) / h));
  }
  const fft = (n, off, stride) => {   // (in place, radix 2)
    for (let i = 1, j = 0; i < n; i++) {
      let bit = n >> 1;
      for (; j & bit; bit >>= 1) j ^= bit;
      j ^= bit;
      if (i < j) { const a = off + i * stride, b = off + j * stride; [re[a], re[b]] = [re[b], re[a]]; [im[a], im[b]] = [im[b], im[a]]; }
    }
    for (let len = 2; len <= n; len <<= 1) {
      const wr = Math.cos(-2 * Math.PI / len), wi = Math.sin(-2 * Math.PI / len);
      for (let i = 0; i < n; i += len) {
        let cr = 1, ci = 0;
        for (let k = 0; k < len / 2; k++) {
          const a = off + (i + k) * stride, b = off + (i + k + len / 2) * stride;
          const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
          re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
          const nr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = nr;
        }
      }
    }
  };
  for (let y = 0; y < h; y++) fft(w, y * w, 1);
  for (let x = 0; x < w; x++) fft(h, x, w);
  const powers = [];
  let most = 0, at = null;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const u = Math.min(x, w - x), t = Math.min(y, h - y);
    if (u < w / 8 && t < h / 8) continue;   // (wavelengths over 8 px: the shapes, not their dithering)
    const p = re[y * w + x] ** 2 + im[y * w + x] ** 2;
    powers.push(p);
    if (p > most) { most = p; at = [u, t]; }
  }
  powers.sort((a, b) => a - b);
  return { ratio: most / Math.max(powers[powers.length >> 1], 1e-9), at };
}, rect || null);
// How regularly the canvas's commonest colour (RGBA) sits on a 4 px grid: the share of its pixels in their 4 commonest places of the
// 16 (x mod 4, y mod 4), and how many there are (an ordered 4x4 dither's shade: near 1; irregular dithering: about 0.3)
const gridShare = (page) => page.evaluate(() => {
  const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data, by = new Map();
  for (let y = 0; y < c.height; y++) for (let x = 0; x < c.width; x++) {
    const i = (y * c.width + x) * 4;
    if (!d[i + 3]) continue;
    const key = ((d[i] << 24) | (d[i + 1] << 16) | (d[i + 2] << 8) | d[i + 3]) >>> 0;
    if (!by.has(key)) by.set(key, new Uint32Array(16));
    by.get(key)[(y & 3) * 4 + (x & 3)]++;
  }
  let bins = new Uint32Array(16), n = 0;
  for (const b of by.values()) { const all = b.reduce((s, v) => s + v, 0); if (all > n) { n = all; bins = b; } }
  return { share: [...bins].sort((a, b) => b - a).slice(0, 4).reduce((s, v) => s + v, 0) / Math.max(n, 1), n };
});
// The haze alone (faded in), at each band in turn, 1 to 5, once its clouds are lit in the band's shades and done fading in or out:
// the share of the canvas drawn (its cover) and its mean alpha (0-255), its strongest regular pattern (gridPeak), and each cloud's
// look as lit (on show: the mean light of its upper half and of its lower). Then on a clock 4 times as fast, the haze 5 with no wind,
// then with it (faded in): over 20 frames its motes' moves and its clouds' (each the same one, not come in again at the right), how
// many went left, right, their mean speed leftward (px/s of the weather's time); and its drift's (slide), the same
const hazeSeen = async (page) => {
  const out = { levels: [] };
  for (let level = 1; level <= 5; level++) {
    await page.evaluate((level) => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { haze: level } })), level);
    await driveUntil(page, (level) => { const h = Sky.state().haze; return h.level === level && !h.pending && !h.settling; }, level);
    const seen = await page.evaluate(() => {
      const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data, h = Sky.state().haze;
      let drawn = 0, alpha = 0;
      for (let i = 3; i < d.length; i += 4) { drawn += d[i] > 0; alpha += d[i]; }
      const looks = h.clouds.filter(([, , x, y, w, hh]) => x < c.width && x + w > 0 && y < c.height && y + hh > 0).map(([id]) => h.look(id));
      return { level: h.level, cover: drawn / (d.length / 4), mean: alpha / (d.length / 4), looks: looks.filter(Boolean).map((l) => [l.upper, l.lower]) };
    });
    seen.grid = (await gridPeak(page)).ratio;
    out.levels.push(seen);
  }
  return page.evaluate(async (out) => {
    const frames = (n) => new Promise((resolve) => { const end = Sky.state().frames + n; const on = () => Sky.state().frames >= end ? resolve() : __next(on); on(); });
    const fx = (detail) => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail }));
    Sky.timeScale(4);
    const moved = (a, b, dt, key, at) => {   // (at: where its x is in each entry)
      const was = new Map(a[key].map((m) => [m[0], m[at]])), dx = [];
      for (const m of b[key]) if (was.has(m[0])) dx.push(m[at] - was.get(m[0]));
      return { n: dx.length, left: dx.filter((v) => v < 0).length, right: dx.filter((v) => v > 0).length,
               speed: -dx.reduce((s, v) => s + v, 0) / Math.max(dx.length, 1) / dt };
    };
    const moves = async () => {
      const a = Sky.state();
      await frames(20);
      const b = Sky.state(), dt = b.clock - a.clock;
      return { ...moved(a.haze, b.haze, dt, "motes", 1), clouds: moved(a.haze, b.haze, dt, "clouds", 2), slide: (b.haze.slide - a.haze.slide) / dt };
    };
    out.calm = await moves();
    fx({ haze: 5, wind: 1 });
    await frames(15);   // (the wind faded in: 1.5 s of the weather's time)
    out.windy = await moves();
    Sky.timeScale(1);
    return out;
  }, out);
};
// The wind's leaves for `ms` of the weather's own time (Sky's clock: as much of it, however long a busy machine takes to draw it), every
// frame: their mean speed across on screen (negative: leftward), how many at most went against the wind (rightward: in a
// loop); and where each was drawn on the canvas (with the wind alone, every sprite drawn is a leaf: its middle, so a leaf turning
// over, its sprite narrower, isn't a step), frame to frame: how many steps it made leftward and how many rightward. From 45 frames in, as rainSeen (1.5 s of the weather's time: the wind faded in, under way)
const windSeen = (page, ms) => page.evaluate((ms) => new Promise((resolve) => {
  const g = document.querySelector("#sky canvas").getContext("2d"), drawImage = g.drawImage, clearRect = g.clearRect, drawn = [];
  const vx = [];
  let against = 0, end = 0;
  const begun = () => {
    if (Sky.clock().frames < 45) return __next(begun);
    g.clearRect = function (...args) { drawn.push([]); return clearRect.apply(this, args); };   // (a frame begins)
    g.drawImage = function (...args) { if (drawn.length) drawn[drawn.length - 1].push(args[1] + args[0].width / 2); return drawImage.apply(this, args); };
    end = Sky.clock().clock + ms / 1000;
    __next(step);
  };
  const step = () => {
    const w = Sky.state().wind;
    vx.push(w.vx);
    against = Math.max(against, w.against);
    if (Sky.state().clock < end) return __next(step);
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
  begun();
}), ms);
// The rain's drops for `ms` of the weather's own time (Sky's clock: as much of it, however long a busy machine takes to draw it), a look
// every 100 ms of it: where they are across in the window's lower half, counted in columns 32 art px wide; how uneven that is
// across (each look's columns: their variance over their mean, averaged over the looks) and over time (each column's counts, look to
// look: the same, averaged over the columns); scattered evenly at random both are about 1. And the drops' mean speed across (px/s,
// negative leftward), averaged.
const rainSeen = (page, ms) => page.evaluate((ms) => new Promise((resolve) => {
  const looks = [], vx = [];
  let start = 0, next = 0;
  const spread = (counts) => {   // variance over mean
    const mean = counts.reduce((a, b) => a + b, 0) / counts.length;
    return mean ? counts.reduce((a, b) => a + (b - mean) ** 2, 0) / counts.length / mean : 0;
  };
  const look = () => {
    const clock = Sky.state().clock;
    if (clock < next) return __next(look);
    start = start || clock;
    next = clock + 0.1 - 1e-6;
    const { xs, width, vx: v } = Sky.state().rain, counts = new Array(Math.floor(width / 32)).fill(0);
    for (const x of xs) if (x < counts.length * 32) counts[Math.floor(x / 32)]++;
    looks.push(counts);
    vx.push(v);
    if (clock - start < ms / 1000) return __next(look);
    const columns = looks[0].map((_, i) => looks.map((c) => c[i]));
    resolve({ looks: looks.length, drops: looks.reduce((a, c) => a + c.reduce((x, y) => x + y, 0), 0) / looks.length,
              across: looks.reduce((a, c) => a + spread(c), 0) / looks.length,
              overTime: columns.reduce((a, c) => a + spread(c), 0) / columns.length, vx: vx.reduce((a, b) => a + b, 0) / vx.length });
  };
  // (from 50 frames in, 1.5 s or more of the page's time: the effects faded in, the wind's gusts under way)
  const begun = () => Sky.clock().frames >= 50 ? look() : __next(begun);
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
    if (drawn.length < 400) return __next(step);
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
  __next(step);
}));
// humidity's beads condensing on the glass and running down it, every frame over `frames` of Sky's (each 0.1 s of the page's time,
// its clock run on fast: `pace`; times Sky.timeScale's, if set): how many ran (moved 10 px or more); any that went up (between two
// frames, by more than nothing); the furthest one went down from where it showed; the furthest one wandered either side of where it
// showed, and how many wandered 2 px or more; their mean offset from where they began (px, negative leftward); the lowest any sprite
// reached (its bottom row + 1, of the window's height); how many ran off the bottom edge, and any seen with its top at or below that
// edge (a frame's worth: gone at once); how often a bead that had moved had its trail behind it; the most trail pixels at once; the
// beads condensed ({r as it showed, fog: the fog shown there, y: its height, of the window's}); merges ({vol: the two's water, into:
// the one's}); px run and s taken, and water gained and lost, on each glass (fog, trail, dry; the px and s of beads of r 1.8-3.2), and
// on fog by size; how many pixels showed on the fog's canvas where no trail is any more, at the end; how many beads vanished (gone
// at a radius of 1 or more, not faded, not merged into another, not run off the bottom: shrinking to nothing, a bead goes small); the
// beads at the start and at the end; any tiny bead's place changing between two frames (not snapping then: crept); every 10th
// frame, how many tiny beads (not drying away, not snapping) and the most there may be (cap); how many snaps; every frame, the most pixels drawn in the canvas's top two rows, and the still beads drawn
// whole (r 2 or more, under 1 px/s, not drying): of the canvas's pixels in their sprites, the alpha (0-255) of the bead's own drawn
// there (the fog under it taken out), mean and most; the still beads (under 1 px/s) and the running ones, each's mean count; and of each bead's trail (through the fog, off another
// trail) as it passed 20 px down, a pixel of it: how many px across around it were wiped clear (crisp) as it was (its own, not
// another beside it), the bead's width as it wiped it, its fog as shown (and not wiped) then, the fog shown beside the trail (a few px either
// side of it), and at the end its fog as shown and how fresh
// A bead's trail as it looks (Sky.state().humidity h, a bead b of h.beads), its fresh rows (fresher than 0.88): on clear glass (no
// fog there), each row's streak (its own pixels shown, from a px left of the bead's sprite to a px right of it); through the fog (fog
// there, not wiped, 0.1 or more at its middle), each row's wiped pixels (its own: the fog shown under half what it'd be): how many, the first
// and the last against the sprite's left and right; each kind with 12 rows or more (a streak on clear glass 16: a thin one's mean
// width needs them): {id, kind ("clear", "fog"), w: the bead's mean
// width, widths, lefts, rights, ys, and through the fog inside: what shows inside it, see inside()}. (Put in the page as
// window.__trailLook, see lookable())
const trailLook = (h, b) => {
  const kinds = { clear: [], fog: [] };
  for (const r of h.trailOf(b.id)) {
    if (!(r.wet > 0.88) || !r.mine) continue;   // (fresh, its own: not another bead's since, over it)
    const mid = 3 + ((r.w - 1) >> 1), fogs = r.fog.slice(2, r.w + 4);
    if (fogs.every(([, f]) => f < 0.06)) {
      kinds.clear.push({ y: r.y, w: r.w, width: r.alpha.filter((a, k) => k >= 2 && k < r.w + 4 && a > 0 && r.life[k] === r.life[mid]).length });
    } else if (r.fog[mid][1] >= 0.1) {
      const wiped = r.fog.map(([s, f], k) => (f > 0 && s < 0.5 * f && r.life[k] === r.life[mid] ? k : -1)).filter((k) => k >= 0);
      kinds.fog.push({ y: r.y, w: r.w, width: wiped.length, left: wiped.length ? wiped[0] - 3 : null,
                       right: wiped.length ? wiped[wiped.length - 1] - 3 - (r.w - 1) : null,
                       // (each px across, at x: wiped (its own), what it shows)
                       px: r.fog.map((_, k) => ({ x: r.left - 3 + k, wiped: wiped.includes(k), shows: r.shows[k], residue: r.residue[k] > 0 })),
                       shades: r.fogShades });
    }
  }
  // (through the fog, what shows inside the wiped streak: each patch of it (8-connected) not touching the fog beside it (not
  // wiped: the ragged edge, its fog dithered as ever) and wholly within the rows looked at: {px, tall, wide, shows: what its
  // pixels show, at: a pixel of it}; anything else than a fog shade shown in the wiped streak; its residue shown (px) and its px
  // wiped)
  const inside = (rows) => {
    const byY = new Map(rows.map((r) => [r.y, new Map(r.px.map((p) => [p.x, p]))]));
    const patches = [], done = new Set(), odd = [];
    for (const r of rows) for (const p of r.px) if (p.wiped && p.shows > r.shades) odd.push(p.shows);
    for (const r of rows) for (const p of r.px) {
      if (!p.wiped || !p.shows || done.has(`${p.x},${r.y}`)) continue;
      const todo = [[p.x, r.y]], got = [];
      let whole = true;
      done.add(`${p.x},${r.y}`);
      while (todo.length) {
        const [x, y] = todo.pop();
        got.push([x, y]);
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          if (!dx && !dy) continue;
          const q = byY.get(y + dy)?.get(x + dx);
          if (!q) { whole = false; continue; }   // (beyond the rows or columns looked at: can't tell)
          if (!q.wiped) { whole = false; continue; }   // (at the ragged edge: the fog's own dither coming back)
          if (!q.shows) continue;
          if (!done.has(`${x + dx},${y + dy}`)) { done.add(`${x + dx},${y + dy}`); todo.push([x + dx, y + dy]); }
        }
      }
      if (!whole) continue;
      const gx = got.map(([x]) => x), gy = got.map(([, y]) => y);
      patches.push({ px: got.length, tall: Math.max(...gy) - Math.min(...gy) + 1, wide: Math.max(...gx) - Math.min(...gx) + 1,
                     shows: got.map(([x, y]) => byY.get(y).get(x).shows), at: got[0] });
    }
    const residue = rows.reduce((n, r) => n + r.px.filter((p) => p.wiped && p.residue && p.shows).length, 0);
    return { patches, odd, residue, px: rows.reduce((n, r) => n + r.px.filter((p) => p.wiped).length, 0) };
  };
  return Object.entries(kinds).filter(([kind, rows]) => rows.length >= (kind === "clear" ? 16 : 12)).map(([kind, rows]) => ({
    id: b.id, kind, w: rows.reduce((s, r) => s + r.w, 0) / rows.length, widths: rows.map((r) => r.width), ys: rows.map((r) => r.y),
    lefts: rows.map((r) => r.left), rights: rows.map((r) => r.right), ...(kind === "fog" ? { inside: inside(rows) } : {}) }));
};
const lookable = (page) => page.evaluate(`window.__trailLook = ${trailLook.toString()}`);
const humiditySeen = (page, frames) => page.evaluate((frames) => new Promise((resolve) => {
  const last = new Map(), first = Sky.state().frames, count = Sky.state().particles.humidity, h0 = Sky.state().humidity;
  const from = { ranOff: h0.ranOff, merges: h0.merges, glass: h0.glass, sizes: h0.sizes };
  let up = 0, down = 0, wander = 0, offset = 0, samples = 0, lowest = 0, moved = 0, behind = 0, trails = 0, popped = 0, under = 0;
  let top = 0, inked = 0, alphas = 0, most = 0, looked = 0, still = 0, running = 0, counts = 0, crept = 0, tinies = [];
  const snaps = h0.snaps;
  const ran = new Set(), wandered = new Set(), wiped = new Map(), looks = [], traced = new Set();
  const canvas = document.querySelector("#sky canvas"), g = canvas.getContext("2d");
  const step = () => {
    const state = Sky.state(), h = state.humidity, now = new Map(h.beads.map((b) => [b.id, b])), absorbed = new Set(h.absorbed), left = new Set(h.left);
    for (const [id, b] of last) if (!now.has(id) && !absorbed.has(id) && !left.has(id) && b.r >= 1 && b.alpha > 0.15) popped++;
    for (const b of h.beads) {
      const before = last.get(b.id);
      if (before && b.y < before.y - 1e-9) up++;
      if (before && before.tiny && b.tiny && !before.snapping && !b.snapping && (b.x !== before.x || b.y !== before.y)) crept++;
      if (b.fading) continue;
      down = Math.max(down, b.y - b.y0);
      if (b.y - b.y0 >= 10) ran.add(b.id);
      wander = Math.max(wander, Math.abs(b.x - b.x0));
      if (Math.abs(b.x - b.x0) >= 2) wandered.add(b.id);
      if (b.y - b.y0 > 5) { offset += b.x - b.x0; samples++; }
      lowest = Math.max(lowest, b.bottom / h.height);
      if (Math.floor(b.y) >= h.height) under++;
      if (b.y - b.y0 > 10) { moved++; if (b.behind) behind++; }
      if (b.y - b.y0 > 30 && !traced.has(b.id) && traced.size < 16 && window.__trailLook) {   // (its trail's looks, once)
        traced.add(b.id);
        looks.push(...window.__trailLook(h, b));
      }
      if (b.y - b.y0 > 20 && b.trail && !wiped.has(b.id) && b.glass === "fog") {
        // (its own trail: wiped clear as it passed, as this pixel was, not another's close beside it, wiped at another time)
        // (or a bead's since, close by, wiping it again: fresher)
        const [x, y] = b.trail, own = h.wetAt(x, y);
        const crisp = (x) => h.wetAt(x, y) > h.crisp && (h.lifeAt(x, y) === h.lifeAt(...b.trail) || h.wetAt(x, y) > own + 1e-6);
        if (crisp(x)) {
          let a = x, c = x;
          while (crisp(a - 1)) a--;
          while (crisp(c + 1)) c++;
          const side = [a - 3, a - 2, c + 2, c + 3].map((x) => h.fogAt(x, y)[0]);
          wiped.set(b.id, { across: c - a + 1, w: b.laidW, at: [x, y], shown: h.fogAt(x, y)[0], fog: h.fogAt(x, y)[1], wet: h.wetAt(x, y),
                            beside: side.reduce((s, v) => s + v, 0) / side.length });
        }
      }
    }
    trails = Math.max(trails, h.trails);
    if ((state.frames - first) % 10 === 0) tinies.push({ n: h.beads.filter((b) => b.tiny && !b.fading && !b.snapping).length, cap: h.cap });
    last.clear();
    for (const [id, b] of now) last.set(id, b);
    {   // (the canvas as drawn this frame: read back once)
      const cw = canvas.width, ch = canvas.height, all = g.getImageData(0, 0, cw, ch).data;
      let k = 0;
      for (let i = 3; i < cw * 2 * 4; i += 4) if (all[i]) k++;
      top = Math.max(top, k);
      still += h.beads.filter((b) => b.v < 1).length; running += h.beads.filter((b) => b.v >= 1).length; counts++;
      for (const b of h.beads) if (b.r >= 2 && b.v < 1 && b.alpha === 1 && b.drawn) {
        const [x0, y0, w, hh, drawn] = b.drawn;
        for (let y = Math.max(0, y0); y < Math.min(ch, y0 + hh); y++) for (let x = Math.max(0, x0); x < Math.min(cw, x0 + w); x++) {
          // (the bead's own alpha, the fog drawn under it taken out: the canvas's alpha over it is 1 - (1 - fog)(1 - bead))
          const under = h.shownAt(x, y)[3] / 255 * drawn, a = Math.round(255 * (1 - (1 - all[(y * cw + x) * 4 + 3] / 255) / (1 - under)));
          if (a > 2) { inked++; alphas += a; most = Math.max(most, a); }
        }
        looked++;
      }
    }
    if (state.frames - first < frames) return __next(step);
    const glass = Object.fromEntries(Object.entries(h.glass).map(([k, o]) => [k, Object.fromEntries(Object.entries(o).map(([q, v]) => [q, v - from.glass[k][q]]))]));
    resolve({ runners: ran.size, up, down, wander, meander: h.meander, wandered: wandered.size, offset: offset / Math.max(samples, 1), lowest,
              ranOff: h.ranOff - from.ranOff, under, behind: behind / Math.max(moved, 1), trails, popped, count: [count, Sky.state().particles.humidity],
              born: h.born.filter((b) => b.kind === "fog"), merges: h.merges - from.merges, merged: h.merged, largest: h.largest, top,
              glass, sizes: h.sizes.map(([px, s], i) => [px - from.sizes[i][0], s - from.sizes[i][1]]), bands: h.bands, stray: h.stray(),
              look: { beads: looked, mean: alphas / Math.max(inked, 1), most },
              showing: { still: still / Math.max(counts, 1), running: running / Math.max(counts, 1) },
              wiped: [...wiped.values()].map((t) => ({ ...t, end: { shown: h.fogAt(...t.at)[0], wet: h.wetAt(...t.at) } })),
              fog: h.fog(), first: h.first, colours: h.colours(), aged: h.beads.filter((b) => !b.fading).map((b) => b.age),
              landed: h.landed, looks, crept, tinies, snaps: h.snaps - snaps, tiny: h.tiny });
  };
  __next(step);
}), frames);
// Tiny beads put on the glass in the fog, a clear spot (no bead within 6 px): two close (2 px across and 1 down: not touching,
// within the snap) and, 20 px on, two further apart (5 px across); then 6 frames: each placed one as it is (gone, or {x, y, r, vol,
// tiny}), and every bead then within 3 px of the close two's
const snapped = (page) => page.evaluate(() => {
  const h0 = Sky.state().humidity, [W, H] = [h0.width, h0.height];
  let spot = null;
  for (let y = Math.round(H * 0.3); y < H * 0.7 && !spot; y += 3) for (let x = 20; x < W - 40 && !spot; x += 3) {
    if (h0.glassAt(x, y) !== "fog" || h0.glassAt(x + 25, y) !== "fog") continue;
    if (h0.beads.every((b) => Math.hypot(b.x - x - 1, b.cy - y) > 6 && Math.hypot(b.x - x - 22, b.cy - y) > 7)) spot = [x, y];
  }
  if (!spot) return { spot };
  const [x, y] = spot, placed = h0.place([{ x, y }, { x: x + 2, y: y + 1 }, { x: x + 20, y }, { x: x + 25, y }]);
  const at = Sky.state().humidity.beads.filter((b) => placed.includes(b.id)).map((b) => ({ x: b.x, y: b.y, r: b.r, vol: b.vol }));
  Sky.advance(6);
  const h = Sky.state().humidity, byId = new Map(h.beads.map((b) => [b.id, b]));
  return { spot, at, placed: placed.map((id) => { const b = byId.get(id); return b ? { x: b.x, y: b.y, r: b.r, vol: b.vol, tiny: b.tiny } : null; }),
           near: h.beads.filter((b) => Math.hypot(b.x - x - 1, b.cy - y - 0.5) <= 3).map((b) => ({ id: b.id, r: b.r, vol: b.vol, tiny: b.tiny })),
           snap: h.snap, tinyR: h.tiny };
});
// humidity off (the humidity below 90 %), every frame after till the fog has cleared and no bead is left (at most `frames`):
// the fog's mean thickness in the window's middle and at its edges as it clears (the first frame each is gone, and the edges' then);
// the slow beads (under 1 px/s) showing at the start; each slow one as it went (gone at a radius of 1 or more, not faded: popped;
// else: where it was, its distance from the middle, 0 there to 1 at the edges, and the frame); the frames taken and what was left
const humidityClears = (page, frames) => page.evaluate((frames) => new Promise((resolve) => {
  document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { wind: 1 } }));
  const last = new Map(), gone = [], start = Sky.state().humidity.beads.filter((b) => b.v < 1).length, [W, H] = Sky.state().size;
  let n = 0, popped = 0, middleGone = null, edgeGone = null, edgeThen = null, shrank = 0;
  const step = () => {
    const h = Sky.state().humidity, fog = h.fog(), beads = new Map(h.beads.map((b) => [b.id, b])), absorbed = new Set(h.absorbed), left = new Set(h.left);
    for (const [id, b] of last) if (!beads.has(id) && !absorbed.has(id) && !left.has(id)) {
      if (b.r >= 1 && b.alpha > 0.15) popped++;
      if (b.v < 1) gone.push({ far: Math.max(Math.abs(2 * b.x / W - 1), Math.abs(2 * b.cy / H - 1)), frame: n });
    }
    for (const [id, b] of beads) if (last.has(id) && b.r < last.get(id).r - 1e-9) shrank++;
    last.clear();
    for (const [id, b] of beads) last.set(id, b);
    if (middleGone === null && fog.middle < 0.005) { middleGone = n; edgeThen = fog.edge; }
    if (edgeGone === null && fog.edge < 0.005) edgeGone = n;
    n++;
    if (n < frames && (fog.cover > 0 || beads.size)) return __next(step);
    resolve({ start, middleGone, edgeGone, edgeThen, gone, popped, shrank, frames: n, left: { cover: fog.cover, thickness: fog.thickness,
              beads: beads.size }, effects: Sky.state().effects });
  };
  __next(step);
}), frames);
// The trails as their time runs on fast (Sky.state().humidity.hasten(k): k times as fast; each frame 0.1 s of the page's): up to
// `most` trail pixels, each of a trail of its own (on clear glass, fresh still, on its streak as shown, a few px apart down it; or through the fog, `through`,
// still crisp, inside it, in the middle of its row there: its own on either side), each followed till it's gone (fogged over again
// all the way, or dry) or a bead wipes it afresh (dropped): how fresh it was at the start, how long it lasted all told, on the trails' clock (from how fresh it was),
// how long it stayed crisp (no fog shown) from the start, how long it showed at all (hidden: shown nothing from then on, as a fraction
// of how long it lasted from the start), and at the end its fog shown against the fog not wiped there; and its look at the start
// ([r, g, b, alpha] on the fog's canvas)
const trailsAge = (page, k, through, most) => page.evaluate(([k, through, most]) => new Promise((resolve) => {
  const h0 = Sky.state().humidity, [W, H] = Sky.state().size, picked = [];
  for (let y = 4; y < H && picked.length < most; y += 1) for (let x0 = 2; x0 < W && picked.length < most; x0 += 1) {
    // (through the fog: the middle of the trail's row there, not the first of it met from the left, a px in from its edge: like
    // with like, wherever the trail is)
    let x = x0;
    if (through && h0.lifeAt(x0, y) > 0) {
      let a = x0, c = x0;
      while (h0.lifeAt(a - 1, y) === h0.lifeAt(x0, y)) a--;
      while (h0.lifeAt(c + 1, y) === h0.lifeAt(x0, y)) c++;
      x = (a + c) >> 1;
    }
    const wet = h0.wetAt(x, y), fog = h0.fogAt(x, y)[1], life = h0.lifeAt(x, y);
    // (through the fog: inside the trail, its own on either side; on clear glass: on its streak, shown)
    const inside = through ? [x - 1, x + 1].every((x) => h0.lifeAt(x, y) === life) : h0.shownAt(x, y)[3] > 0;
    if (wet > (through ? h0.crisp + 0.03 : 0.3) && (through ? fog > 0.1 : fog === 0) && inside &&
        !picked.some((p) => Math.abs(p.life - life) < 1 && (through || Math.abs(p.at[1] - y) < 4))) {   // (on clear glass: a few down a streak)
      picked.push({ at: [x, y], wet, life, start: h0.clock(), last: wet, look: h0.shownAt(x, y), crispTill: null, shownTill: 0 });
    }
  }
  h0.hasten(k);
  let n = 0;
  const step = () => {
    const h = Sky.state().humidity, t = h.clock();
    for (const p of picked) if (!p.done && !p.dropped) {
      const wet = h.wetAt(...p.at);
      if (wet > p.last + 1e-6) { p.dropped = true; continue; }
      p.last = wet;
      if (p.crispTill === null && h.fogAt(...p.at)[0] > 0) p.crispTill = t - p.start;
      if (h.shownAt(...p.at)[3] > 0) p.shownTill = t - p.start;
      if (wet <= 0) { p.done = true; p.lasted = (t - p.start) / p.wet; p.fog = h.fogAt(...p.at); p.hidden = p.shownTill / (t - p.start); }
    }
    n++;
    if (n < 400 && picked.some((p) => !p.done && !p.dropped)) return __next(step);
    h.hasten(1);
    resolve(picked.filter((p) => p.done).map((p) => ({ lasted: p.lasted, crispTill: p.crispTill, wet: p.wet, fog: p.fog, look: p.look,
                                                       hidden: p.hidden })));
  };
  __next(step);
}), [k, through, most]);
// Heavy rain's beads: light rain first, `light` frames (none sticking), then heavy, `heavy` frames: the drops that stuck (each as it
// first showed: its radius, its alpha, its splash's droplets), the most on the glass at once, the beads' water gained and lost on each
// glass and their px run and s taken there (r 1.8-3.2), any bead vanishing (as humiditySeen), the trails at the end; then (`after`
// frames, the weather's time `scale` times as fast; unless `phase` "heavy": from there) the rain light again: no new one sticking,
// and those there run off or shrunk away
const rainBeads = (page, light, heavy, after, scale, from = "light") => page.evaluate(([light, heavy, after, scale, from]) => new Promise((resolve) => {
  const out = { stuck: [], looks: [] }, seenIds = new Set(), last = new Map(), looked = new Set();
  let n = 0, phase = from, popped = 0, start = Sky.state().frames;
  const step = () => {
    const state = Sky.state(), h = state.humidity, absorbed = new Set(h.absorbed), left = new Set(h.left), now = new Map(h.beads.map((b) => [b.id, b]));
    for (const [id, b] of last) if (!now.has(id) && !absorbed.has(id) && !left.has(id) && b.r >= 1 && b.alpha > 0.15) popped++;
    last.clear();
    for (const b of h.beads) {
      last.set(b.id, b);
      if (b.kind === "rain" && b.y - b.y0 > 20 && !looked.has(b.id) && window.__trailLook) {   // (its trail's looks, once)
        looked.add(b.id);
        out.looks.push(...window.__trailLook(h, b));
      }
      if (b.kind === "rain" && !seenIds.has(b.id)) {
        seenIds.add(b.id);
        const s = h.splashes.find((s) => s.id === b.id);
        out.stuck.push({ r: s ? s.r : 0, alpha: b.alpha, dots: s ? s.dots : 0, phase });   // (r: as it landed)
      }
    }
    n = state.frames - start;
    if (phase === "light" && n >= light) {
      out.light = { landed: h.landed, levels: state.levels.rain };
      document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1 } }));
      phase = "heavy"; start = state.frames;
    } else if (phase === "heavy" && n >= heavy) {
      out.heavy = { landed: h.landed, rainMost: h.rainMost, glass: h.glass, trails: h.trails, beads: h.beads.length, popped };
      if (!after) return resolve(out);
      document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 0.35 } }));
      Sky.timeScale(scale);
      out.eased = { landed: h.landed, beads: h.beads.length, ranOff: h.ranOff, shrunk: h.shrunk, faded: h.faded };
      phase = "after"; start = state.frames;
    } else if (phase === "after" && (n >= after || !h.beads.length)) {
      Sky.timeScale(1);
      out.after = { landed: h.landed, beads: h.beads.length, frames: n, ranOff: h.ranOff, shrunk: h.shrunk, faded: h.faded, popped };
      return resolve(out);
    }
    __next(step);
  };
  __next(step);
}), [light, heavy, after, scale, from]);
// "stop animation" (an effect on: the link shows; it ends the test links' effects; the link gone), then `detail` on (as a test link
// clicked after it: the animations on again), all at once (no frame between): the loop, the link and the glass (humidity's marks) as
// stopped, and once started again, the glass, the frost and the rain's drops then
const restarted = (page, detail) => page.evaluate((detail) => {
  const linkNow = () => { const a = document.querySelector("#motion .animations"); return a ? a.textContent : null; }, seen = {};
  seen.link = linkNow();
  document.querySelector("#motion .animations").click();
  seen.stopped = { running: Sky.state().running, link: linkNow(), marks: Sky.state().humidity.marks() };
  document.dispatchEvent(new CustomEvent("ecobot-fx", { detail }));
  seen.again = { running: Sky.state().running, link: linkNow() };
  const f = Sky.frost();
  return { ...seen, marks: Sky.state().humidity.marks(), stray: Sky.state().humidity.stray(), drops: Sky.state().particles.rain,
           frost: f && { extent: f.extent, ink: f.ink, sparkles: f.sparkles }, effects: Sky.state().effects };
}, detail);
const framesIn = async (page, ms) => {   // the frames the page asked for in this long
  const before = await page.evaluate(() => window.__frames);
  await page.waitForTimeout(ms);
  return (await page.evaluate(() => window.__frames)) - before;
};

// MCP_DASHBOARD_ANIMATIONS off (the page as ecobot-mcp serves it then: data-animations="off" on its <html>): every effect asked for
// and called for, nothing drawn; the rest as ever
async function animationsOff(browser, url, out) {
  const { context, page, errors, reads } = await weatherPage(browser, url, { query: "?fx=all,gust", animations: "off" });
  await reads({ "UV index": 11, Wind: 60, Temperature: -3, rain_rate: 2, Humidity: 97, "US AQI": 160, gust: 130 });
  const seen = async () => ({ ...await weatherSeen(page), frames: await page.evaluate(() => Sky.state().frames), ink: await canvasInk(page),
                              gust: await page.evaluate(() => Sky.state().gust.sent),   // (a gust over 99 km/h, and ?fx=gust: none sent)
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
// its mark and just short of it, haze's bands, rain's strength, the ice's depth), "several" (several at once, the canvas, the tiles see-
// through, their look, zoomed), "stop" ("stop animation", the readings, a reload, a test link after it, the reset), "links" (the temporary test links;
// and beside them, MCP_DASHBOARD_ANIMATIONS off: nothing drawn),
// "all" (?fx=all: its frame time, its brightness against Hazardous haze alone, its particles), "opened" (?fx= when the page opens, uv
// and the wind alone, heavy rain alone and in the wind (on a phone too), uv's beams over 40 s, humidity's beads running down the
// glass alone and in the wind, a phone's strip, reduced motion), "flood" (a flash flood: its mark, its rise, its surface and waves,
// what ends at it, the test link's rain3, its leaves and its frog, its cost), "gust" (something big blown across in a gust over 99 km/h)
const WEATHER = {
  async marks(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url);
    await calm(page, 5000);   // (the gauges' rise)
    out.nothing = { ...await weatherSeen(page), frames: await framesIn(page, 800), ink: await canvasInk(page) };
    // each mark: just short of it, then at it
    const marks = { rain: [{ rain_rate: 0 }, { rain_rate: 0.1 }], haze: [{ "US AQI": 50 }, { "US AQI": 51 }],
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
    out.hazeBands = [];
    for (const aqi of [101, 151, 201, 301, 500]) { await reads({ "US AQI": aqi }); out.hazeBands.push((await weatherSeen(page)).levels.haze); }
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
    // the tiles see-through: a tile's blank margin shows what's drawn behind it (haze, Hazardous, over the bottom row)
    await reads({ "US AQI": 500 });
    await page.waitForTimeout(1800);
    const margin = async () => {
      const r = await tileOf(page, "PM10").boundingBox();
      return page.screenshot({ clip: { x: r.x + r.width * 0.04, y: r.y + r.height * 0.45, width: r.width * 0.12, height: r.height * 0.15 } });
    };
    const hazegy = await margin();
    await reads({});
    await page.waitForFunction(() => !Sky.state().running);   // (faded out: the loop ended)
    await calm(page);
    const clear1 = await margin(), clear2 = await margin();
    out.through = { differs: !hazegy.equals(clear1), still: clear1.equals(clear2) };
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
    // "stop animation" (a test link's effect on too): nothing drawn, no loop, the test link's effect ended, the frames stay, the link
    // gone (no "start animation"), nothing saved
    await page.locator("#fxtest button[data-fx='wind']").click();
    out.simulating = { ...await weatherSeen(page), title: await page.evaluate(() => document.querySelector("#motion .animations").title) };
    await page.locator("#motion .animations").click();
    await page.waitForTimeout(200);
    out.stopped = { ...await weatherSeen(page), frames: await framesIn(page, 600), ink: await canvasInk(page), stored: await stored(page),
                    url: await page.evaluate(() => location.search),
                    pressed: await page.evaluate(() => document.querySelectorAll("#fxtest button.on").length) };
    // the readings calling for more (wind too) and a tile hidden and "reset preferences": still stopped (the reset not about them)
    await reads({ "UV index": 11, Wind: 20 });
    await calm(page, 5000);
    await page.mouse.move(...await at(page, "Rain", 0.5, 0.4));
    await calm(page);
    await tileOf(page, "Rain").locator(".close").click();
    await calm(page);
    await page.getByText("reset preferences").click();
    await page.waitForTimeout(100);
    out.reset = { ...await weatherSeen(page), frames: await framesIn(page, 500), ink: await canvasInk(page) };
    // a reload (an earlier version's saved {animations: false} kept beside the arrangement): running as normal, that value dropped
    await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: ["Rain"], animations: false })), KEY);
    await page.reload({ waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll(".tile.alert").length > 0);
    await calm(page, 5000);   // (the gauges' rise)
    out.reloaded = { ...await weatherSeen(page), frames: await framesIn(page, 500), stored: JSON.parse(await stored(page)) };
    // stopped again, then a test link: the animations on again, its effect and the readings' with it, the link back
    await page.locator("#motion .animations").click();
    await page.waitForFunction(() => !document.querySelector("#motion .animations"));
    await page.locator("#fxtest button[data-fx='heat']").click();
    await page.waitForTimeout(100);
    out.resumed = { ...await weatherSeen(page), frames: await framesIn(page, 500), url: await page.evaluate(() => location.search) };
    // from the keyboard: Enter on the link stops them; the link gone, the focus with it (not left on a removed element)
    await page.focus("#motion .animations");
    await page.keyboard.press("Enter");
    out.keyboard = { ...await weatherSeen(page), focused: await page.evaluate(() => document.activeElement.tagName) };
    out.errors = errors;
    await context.close();
  },
  async links(browser, url, out) {
    const off = animationsOff(browser, url, out.off = {});   // (beside the rest)
    // phones (360x740, 390x844), every effect on (their links' longest): the row without "test:", all of it and "stop animation" in
    // sight, unscrolled (beside the rest, as the off page)
    const phones = Promise.all([[360, 740], [390, 844]].map(async ([width, height]) => {
      const p = await weatherPage(browser, url, { query: "?fx=all", driven: true, context: { viewport: { width, height }, hasTouch: true, isMobile: true } });
      const seen = { ...await cornerSeen(p.page), labels: await p.page.evaluate(() => [...document.querySelectorAll("#fxtest button")]
        .map((b) => b.getAttribute("aria-label"))), errors: p.errors };
      await p.page.locator("#fxtest button[data-fx='rain']").click();   // (rain's third step, the flood: its link's figure and label)
      seen.flood = await p.page.evaluate(() => { const b = document.querySelector("#fxtest button[data-fx='rain']"); return [b.textContent, b.getAttribute("aria-label")]; });
      await p.context.close();
      return seen;
    }));
    // the temporary test links: each turns its effect and frame on (the gauges keep their readings), "stop animation" ends them (and
    // stops the animations); the URL follows
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
    out.links.haze = [];
    for (let i = 0; i < 6; i++) { await fx("haze"); out.links.haze.push((await weatherSeen(page)).levels.haze); }
    out.links.rain = [];   // (rain: light, heavy, heavy with a flash flood (the test's own flood level), off: [rain, flood])
    for (let i = 0; i < 4; i++) { await fx("rain"); const { levels } = await weatherSeen(page); out.links.rain.push([levels.rain, levels.flood]); }
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
    await page.locator("#motion .animations").click();
    await page.waitForFunction(() => !document.querySelector("#motion .animations"));
    out.links.cleared = { ...await weatherSeen(page), url: await page.evaluate(() => location.search),
                          pressed: await page.evaluate(() => document.querySelectorAll("#fxtest button.on").length),
                          stored: await stored(page) };
    // every effect at once: ?fx=all (no link of its own)
    await page.goto(url + "?fx=all", { waitUntil: "load" });
    await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0 && /last reading/.test(document.getElementById("status").textContent));
    out.links.all = { ...await weatherSeen(page), url: await page.evaluate(() => location.search) };
    await off;
    [out.phone360, out.phone390] = await phones;
    out.errors = [...errors, ...out.off.errors];
    await context.close();
  },
  async all(browser, url, out) {
    const particles = (p) => p.evaluate(() => ({ ...Sky.state().particles, budget: Sky.state().budget }));
    const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=all,gust", driven: true });
    await drive(page, 90);   // (3 s of the weather's time)
    out.cost = await frameCost(page, 60);   // (ms of script a frame, with every effect on, and something blown across)
    out.flying = await page.evaluate(() => Sky.state().gust.flyers.length);
    // the strongest single effect (Hazardous haze), as bright as "all" may be: faded in while the ice grows on
    const strongest = (async () => {
      const haze = await weatherPage(browser, url, { query: "?fx=haze5", driven: true });
      await drive(haze.page, 56);   // (faded in: 1.5 s)
      const light = await canvasLight(haze.page);
      out.haze = await hazeSeen(haze.page);   // (meanwhile, the ice growing on the other page)
      await haze.context.close();
      return light;
    })();
    // (the ice grown all the way in, over some minutes: the weather's time 100 times as fast meanwhile; then as ever, the cost again
    // 45 frames on, the frost across the glass)
    await page.evaluate(() => Sky.timeScale(100));
    await driveUntil(page, () => { const f = Sky.frost(true); return f && f.target > 0 && f.extent === f.target; });
    await page.evaluate(() => Sky.timeScale(1));
    await drive(page, 45);
    out.costFrosted = { cost: await frameCost(page, 60), crystals: await page.evaluate(() => Sky.frost().crystals) };
    out.light = { all: await canvasLight(page) };
    out.particles = { all: await particles(page) };
    out.light.haze5 = await strongest;
    out.errors = errors;
    await context.close();
  },
  // A flash flood: the station's rain rate over 30 mm/h (not at 30): the water rising steadily to a quarter of the window's height,
  // its surface choppy all across and higher at the sides, the rain ending at it in ripples and splashes; its waves alone (one
  // push: travelling both ways, reflecting off a side); draining at 30, its chop calming slowly. Beside it, every other effect on
  // and the test link's rain3 (rain light, heavy, flood, off): the frame time and the particles with the flood full; then on a fast
  // clock, leaves landing on it, floating, sinking and resting on the bottom; the drips joining it (each as it did), and where the
  // surface is down in a trough, what's drawn just under its level against what's just above; its first frog 30 s after the water
  // starts rising, drifting right to left on the waves; off, draining.
  async flood(browser, url, out) {
    const linked = (async () => {
      const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=haze3,uv,humidity2,wind,heat,ice", driven: true });
      const seen = { steps: [] };
      const rain = () => page.evaluate(() => {
        const b = document.querySelector("#fxtest button[data-fx='rain']");
        return { url: location.search, label: b.getAttribute("aria-label"), text: b.textContent, rain: Sky.state().levels.rain,
                 flood: Sky.state().levels.flood };
      });
      const click = () => page.locator("#fxtest button[data-fx='rain']").click();
      for (let i = 0; i < 3; i++) { await click(); seen.steps.push(await rain()); }
      await page.evaluate(() => Sky.timeScale(4));   // (full, then 80 frames as ever: the last 60's cost, the page's start long past)
      await driveUntil(page, () => Sky.state().flood.depth >= Sky.state().flood.full);
      await page.evaluate(() => Sky.timeScale(1));
      await drive(page, 20);
      seen.costly = { cost: await frameCost(page, 60), ...await page.evaluate(() => ({ effects: Sky.state().effects,
                                                                                         particles: Sky.state().particles, budget: Sky.state().budget })) };
      await page.evaluate(() => Sky.timeScale(10));
      // every frame, until the first frog has crossed (or 100 s of the page's time): the frog, the leaves in the water, the drips
      Object.assign(seen, await page.evaluate(() => new Promise((resolve) => {
        const r = { before: null, first: null, track: [], leaves: { float: 0, sink: 0, rest: 0 }, gaps: [], resting: [], wet: 0, most: 0,
                    over: 0, under: 0, joined: 0, joins: [], frogs: 0, fullAt: null, fewest: Infinity, flown: [],
                    // (every leaf it took, by id: still in it at the end, or not; its count of them, those come to rest, those resting
                    // shown on the bottom (and its px) each 5 frames (each 5 s of its time here): any not shown, any look with fewer px than the last)
                    kept: { ids: new Set(), missing: [], took: 0, rested: 0, float: 0, sink: 0, looks: [], hidden: 0, shrank: 0 },
                    // (the leaves and the water, every frame: flying ones drawn touching it or under it; those stuck in it, and of them
                    // the ones seen flying just before (and how near the water they were then); any flying again, any above the surface
                    // or going back a state; flying ones vanishing other than into it, last seen near the water and away from the
                    // window's edges (further than it went in a frame); flying ones seen within 3 px of it; the rain's hits on leaves meanwhile)
                    stuck: { touching: 0, wet: 0, seen: 0, lastGaps: [], back: 0, above: 0, backwards: 0, lost: [], near: 0, hits0: null, hits: 0 } };
        const flyers = new Map(), states = new Map(), ORDER = ["float", "sink", "rest"];
        let then = null;   // (the clock the frame before)
        const step = () => {
          const s = Sky.state(), f = s.flood, w = s.wind, k = r.stuck;
          if (f.depth >= f.full) {
            if (k.hits0 === null) k.hits0 = w.hits;
            k.hits = w.hits - k.hits0;
            const now = new Map(w.flyers.map(([id, gap, x, y]) => [id, [gap, x, y]]));
            for (const [id, [gap]] of now) {
              if (gap !== null && gap <= 0) k.touching++;
              if (gap !== null && gap <= 3) k.near++;
              if (states.has(id)) k.back++;
            }
            for (const [id, state, gap] of w.wet) {
              if (gap < 0) k.above++;
              if (!states.has(id)) {
                k.wet++;
                if (flyers.has(id)) { k.seen++; k.lastGaps.push(flyers.get(id)[0]); }
              } else if (ORDER.indexOf(state) < ORDER.indexOf(states.get(id))) k.backwards++;
              states.set(id, state);
            }
            // (away from the edges: 16 px, or further than it could have gone this frame, at 1.5 times its last frame's speed)
            const dt = then === null ? 0 : s.clock - then;
            for (const [id, [gap, x, y, speed]] of flyers) {
              if (now.has(id) || states.has(id)) continue;
              const edge = Math.max(16, 1.5 * speed * dt + 4);
              if (gap !== null && gap <= 8 && x > edge && x < f.width - edge) k.lost.push([id, gap, x, y]);
            }
            for (const [id, v] of now) v.push(flyers.has(id) && dt > 0 ? Math.abs(v[1] - flyers.get(id)[1]) / dt : 400);
            then = s.clock;
            flyers.clear();
            for (const [id, v] of now) flyers.set(id, v);
          }
          if (f.depth >= f.full && r.fullAt === null) r.fullAt = f.since;
          if (!f.frog && !r.first) r.before = f.since;
          if (f.frog) {
            if (!r.first) r.first = f.since;
            const x = Math.round(f.frog.x), under = [-6, -3, 0, 3, 6].map((dx) => f.surface[Math.min(Math.max(x + dx, 0), f.width - 1)]);
            if (f.frog.y !== null && x >= 0 && x < f.width) r.track.push([f.frog.x, f.frog.y, under.reduce((a, b) => a + b) / 5]);   // (the surface under its pad)
          }
          for (const k of ["float", "sink", "rest"]) r.leaves[k] = Math.max(r.leaves[k], w.water[k]);
          r.gaps.push(...w.floating);
          r.resting.push(...w.resting);
          const wet = w.water.float + w.water.sink;   // (those resting: drawn on the bottom, no particles)
          r.wet = Math.max(r.wet, wet);
          r.most = Math.max(r.most, wet / w.leaves);
          if (f.depth >= f.full) { r.fewest = Math.min(r.fewest, w.flyers.length / w.leaves); r.flown.push(w.flyers.length / w.leaves); }
          for (const [id] of w.wet) r.kept.ids.add(id);
          if (s.frames % 5 === 0 && w.rested) {
            const shows = w.shows(), last = r.kept.looks.at(-1);
            if (shows.shown < shows.of) r.kept.hidden++;
            if (last && shows.px < last.px) r.kept.shrank++;
            r.kept.looks.push(shows);
          }
          const used = Object.values(s.particles).reduce((a, b) => a + b, 0);
          r.over = Math.max(r.over, used - s.budget);
          if (f.depth >= f.full) {   // (the drips: none below the surface, beyond a crest's rise in a frame on this fast clock)
            r.under = Math.max(r.under, s.humidity.beads.filter((b) => b.bottom > f.surface[Math.round(Math.min(Math.max(b.x, 0), f.width - 1))] + 3).length);
          }
          r.joined = s.humidity.joined;
          r.joins = s.humidity.joins;
          r.frogs = f.frogs;
          if ((r.first && !f.frog) || f.since > 100) {
            r.gaps = r.gaps.slice(-400); r.resting = r.resting.slice(-400); r.stuck.lastGaps = r.stuck.lastGaps.slice(-200);
            r.flying = r.flown.reduce((a, b) => a + b, 0) / Math.max(r.flown.length, 1);
            delete r.flown;
            const now = new Set(w.wet.map(([id]) => id)), k = r.kept;
            k.missing = [...k.ids].filter((id) => !now.has(id));
            Object.assign(k, { ids: k.ids.size, took: w.took, rested: w.rested, float: w.water.float, sink: w.water.sink,
                               looks: k.looks.length, last: k.looks.at(-1), first: k.looks[0] });
            return resolve(r);
          }
          __next(step);
        };
        __next(step);
      })));
      // (still full: where the surface is down in a trough, 3 px or more below its level, the canvas's alpha in the two rows just
      // under the level beside two rows a little above it, those columns all summed, frame by frame till 400 of them; last, as reading
      // the canvas back can slow its drawing)
      seen.cut = await page.evaluate(() => new Promise((resolve) => {
        const g = document.querySelector("#sky canvas").getContext("2d"), cut = { n: 0, above: 0, at: 0 };
        let frames = 0;
        const step = () => {
          const f = Sky.state().flood, level = f.height - f.depth, d = g.getImageData(0, level - 4, f.width, 6).data;
          const alpha = (row, x) => d[(row * f.width + x) * 4 + 3];
          for (let x = 0; x < f.width; x++) if (Math.round(f.surface[x]) >= level + 3) {
            cut.n++; cut.above += alpha(0, x) + alpha(1, x); cut.at += alpha(4, x) + alpha(5, x);
          }
          if (cut.n >= 400 || ++frames >= 150) resolve(cut); else __next(step);
        };
        __next(step);
      }));
      // (still full: its body's dithering, in its bottom 32 rows, the middle 256 px across)
      const [cw, ch] = await page.evaluate(() => Sky.state().size);
      seen.grid = (await gridPeak(page, [Math.floor((cw - 256) / 2), ch - 32, 256, 32])).ratio;
      // the wind off, some leaves resting on the bottom and some floating: those in the water staying, the floating ones sinking
      // (once the wind is gone: then the weather's time 20 times as fast for 16 s of it, and on till every one has come to rest)
      await driveUntil(page, () => { const w = Sky.state().wind.water; return w.rest >= 1 && w.float >= 2; });
      await page.locator("#fxtest button[data-fx='wind']").click();
      await driveUntil(page, () => !Sky.state().effects.includes("wind") && Sky.state().particles.wind === 0);
      seen.windOff = { before: await page.evaluate(() => Sky.state().flood.leaves.wet) };
      await page.evaluate(() => Sky.timeScale(20));
      const gone = await page.evaluate(() => Sky.state().clock);
      await driveUntil(page, (c) => Sky.clock().clock >= c + 16, gone);
      Object.assign(seen.windOff, await page.evaluate(() => ({ after: Sky.state().flood.leaves.wet, flyers: Sky.state().wind.flyers.length,
                                                               effects: Sky.state().effects })));
      await driveUntil(page, () => { const w = Sky.state().flood.leaves.water; return w.float + w.sink === 0; });
      seen.windOff.settled = await page.evaluate(() => { const l = Sky.state().flood.leaves; return { took: l.took, rested: l.rested, all: l.all, shows: l.shows(), particles: Sky.state().particles.flood ?? 0 }; });
      // off: draining, the leaves in it staying till it's nearly gone, then gone with it (the weather's time four times as fast)
      await page.evaluate(() => Sky.timeScale(4));
      const n0 = await page.evaluate(() => Sky.state().flood.leaves.all);
      await click();
      seen.off = await rain();
      seen.drained = await page.evaluate((n0) => new Promise((resolve) => {
        const c0 = Sky.state().clock, depth0 = Sky.state().flood.depth;
        let low = null;   // (how many leaves in it as it got down to its last 8 px)
        const step = () => {
          const s = Sky.state();
          if (s.flood.depth > 0) {
            if (s.flood.depth <= 8 && low === null) low = s.flood.leaves.all;
            return __next(step);
          }
          __next(() => __next(() => {
            const w = Sky.state().wind;
            resolve({ took: s.clock - c0, depth0, effects: Sky.state().effects, wet: w.water.float + w.water.sink + w.water.rest,
                      all: Sky.state().flood.leaves.all, frog: Sky.state().flood.frog, n0, low });
          }));
        };
        __next(step);
      }), n0);
      // a new flood (rain3 again): none of the leaves from before in it
      for (let i = 0; i < 3; i++) await click();
      await driveUntil(page, () => Sky.state().flood.depth > 0);
      seen.again = await page.evaluate(() => ({ url: location.search, leaves: Sky.state().flood.leaves }));
      seen.errors = errors;
      await context.close();
      return seen;
    })();
    // the station's rain rate: 30 mm/h, then just over it (the weather's time four times as fast)
    const { context, page, errors, reads } = await weatherPage(browser, url, { driven: true });
    await reads({ rain_rate: 30 });
    out.at30 = await weatherSeen(page);
    await page.evaluate(() => Sky.timeScale(4));
    await reads({ rain_rate: 30.1 });
    out.over = await weatherSeen(page);
    out.rising = await page.evaluate(() => new Promise((resolve) => {
      const rise = [], looks = [], splashes = [];
      let under = 0, hits = null;
      const mean = (a) => a.reduce((s, v) => s + v, 0) / Math.max(a.length, 1);
      const step = () => {
        const s = Sky.state(), f = s.flood;
        under = Math.max(under, s.rain.under);
        if (f.depth < f.full) { if (!rise.length || s.clock > rise.at(-1)[0]) rise.push([s.clock, f.depth]); return __next(step); }
        if (hits === null) hits = f.hits;
        splashes.push(f.splashes);
        if (looks.length < 90) { looks.push(f.surface.map((y) => y - (f.height - f.depth))); return __next(step); }
        // the rise: its speed (art px/s of the weather's time) frame to frame, while it rose (not its first frame or its last)
        const speeds = rise.slice(1).map(([c, d], i) => (d - rise[i][1]) / (c - rise[i][0])).slice(1, -1);
        const W = looks[0].length, rms = (xs) => Math.sqrt(mean(looks.flatMap((l) => xs.map((x) => l[x] ** 2))));
        const middle = Array.from({ length: Math.floor(W / 3) }, (_, i) => i + Math.floor(W / 3));
        // (the waves alone: a still surface 200 px wide pushed down 4 px at 60, each 0.1 s for 3 s: its troughs' places at 1 s and
        // 3 s, and its deepest then)
        const alone = f.waves(200, 60, 4, 3, 0.1), troughs = (h) => h.flatMap((v, x) => (x > 0 && x < h.length - 1 && v < -0.15 &&
          v <= h[x - 1] && v <= h[x + 1] ? [x] : []));
        resolve({ full: f.full, height: f.height, width: W, speeds, test: f.test, nextFrog: f.nextFrog, frog: f.frog, under, hits: f.hits - hits,
                  splashes: Math.max(...splashes), chop: f.chop,
                  crests: mean(looks.map((l) => l.filter((v, x) => x > 0 && x < W - 1 && v < l[x - 1] && v <= l[x + 1]).length)),
                  middle: rms(middle), edges: rms([0, 1, 2, W - 3, W - 2, W - 1]),
                  // (the chop in each third of the width: the surface against its own mean over 9 px about each column, so the
                  // longer sloshes passing don't count)
                  thirds: [0, 1, 2].map((k) => Math.sqrt(mean(looks.flatMap((l) => Array.from({ length: Math.floor(W / 3) - 8 }, (_, i) => {
                    const x = i + 4 + k * Math.floor(W / 3);
                    return (l[x] - mean(l.slice(x - 4, x + 5))) ** 2;
                  }))))),
                  waves: { speed: f.wave, at1: troughs(alone[10]), at3: troughs(alone[30]), deepest3: Math.min(...alone[30]) } });
      };
      __next(step);
    }));
    await reads({ rain_rate: 30 });
    out.draining = await page.evaluate(() => new Promise((resolve) => {
      const f0 = Sky.state().flood, c0 = Sky.state().clock, effects = Sky.state().effects;
      let half = null;
      const step = () => {
        const s = Sky.state(), f = s.flood;
        if (half === null && f.depth <= f.full / 2) half = { chop: f.chop, after: s.clock - c0 };
        if (f.depth > 0) return __next(step);
        resolve({ effects, took: s.clock - c0, chop0: f0.chop, half, surface: f.surface.length });
      };
      __next(step);
    }));
    out.drained = await weatherSeen(page);
    out.errors = errors;
    await context.close();
    out.linked = await linked;
    out.errors.push(...out.linked.errors);
  },
  async opened(browser, url, out) {
    const particles = (p) => p.evaluate(() => ({ ...Sky.state().particles, budget: Sky.state().budget }));
    // uv's beams over 40 s of the page's time, on a page of their own beside the rest: each swaying about its place
    // ... then, on that page, the frost (its page's time 10 times as fast and its weather's 40 times that: 4 s a frame): just below
    // 0 °C (-0.5) as far as it goes, then melted; then -5 °C from nothing, a minute in and as far as it goes; then melting, half a
    // minute in, and gone. Each: Sky.frost() and the weather's time it took; as far as it goes, the canvas's pixels: their alphas
    // and colours
    const frostSeen = async (p) => {
      const fx = (detail) => p.page.evaluate((d) => { document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: d })); return Sky.state().clock; }, detail);
      const frost = () => p.page.evaluate(() => ({ ...Sky.frost(), clock: Sky.state().clock, running: Sky.state().running }));
      const until = (test, arg) => driveUntil(p.page, test, arg);
      const settled = () => until(() => { const f = Sky.frost(true); return f && f.extent === f.target; });
      // the canvas: the share of it drawn, its pixels' mean alpha (0-1), the share over 100 (of 255); of those drawn, their mean
      // light (luminance, 0-255) and the share as blue as red or bluer
      const pixels = () => p.page.evaluate(() => {
        const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
        let drawn = 0, alpha = 0, strong = 0, light = 0, bluer = 0;
        for (let i = 0; i < d.length; i += 4) {
          if (!d[i + 3]) continue;
          drawn++; alpha += d[i + 3]; strong += d[i + 3] > 100;
          light += 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
          bluer += d[i + 2] >= d[i] && d[i + 1] >= d[i];
        }
        const n = d.length / 4;
        return { drawn: drawn / n, alpha: alpha / n / 255, strong: strong / n, light: light / Math.max(drawn, 1), bluer: bluer / Math.max(drawn, 1) };
      });
      const seen = {};
      await p.page.evaluate(() => Sky.timeScale(40));
      let from = await fx({ ice: 0.5 });
      await settled();
      seen.slight = await frost();
      seen.slight.took = seen.slight.clock - from;
      await fx({});
      await until(() => !Sky.state().running);
      seen.slightGone = await frost();
      from = await fx({ ice: 5 });
      await until((t) => Sky.state().clock >= t, from + 60);
      seen.minute = await frost();
      const sparkles = p.page.evaluate(() => new Promise((resolve) => {   // (the most sparkles at once, while it grows on)
        let most = 0;
        const look = () => { const f = Sky.frost(true); most = Math.max(most, f.sparkles); if (f.extent === f.target) resolve(most); else __next(look); };
        look();
      }));
      seen.sparkles = await sparkles;
      await settled();
      seen.cold = await frost();
      seen.cold.took = seen.cold.clock - from;
      seen.cold.pixels = await pixels();
      seen.cold.grid = await gridShare(p.page);   // (its commonest shade: its haze's faint one)
      seen.cold.size = await p.page.evaluate(() => { const c = document.querySelector("#sky canvas"); return [c.width, c.height]; });
      from = await fx({});
      await until((t) => Sky.state().clock >= t, from + 30);
      seen.melting = await frost();
      await until(() => !Sky.state().running);
      seen.gone = await frost();
      // (on again: nothing from before, its canvas clear, no sparkle; then grown half a minute, and "stop animation", "start
      // animation" and on again: the same)
      seen.again = await p.page.evaluate(() => {
        document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { ice: 5 } }));
        const f = Sky.frost();
        return { extent: f.extent, ink: f.ink, sparkles: f.sparkles };
      });
      from = await p.page.evaluate(() => Sky.state().clock);
      await until((t) => Sky.state().clock >= t, from + 30);
      seen.grown = await frost();
      seen.restarted = await restarted(p.page, { ice: 5 });
      return seen;
    };
    const swaying = (async () => {
      const uv = await weatherPage(browser, url, { query: "?fx=uv", pace: 10, driven: true, context: { viewport: { width: 640, height: 480 } } });   // (small: cheap)
      const seen = await uvSeen(uv.page);
      seen.grid = (await gridPeak(uv.page)).ratio;   // (its glow's dithering)
      const frost = await frostSeen(uv);
      seen.errors = frost.errors = uv.errors;
      await uv.context.close();
      return [seen, frost];
    })();
    // humidity's beads condensing on the glass and running down it over 3.6 minutes of the page's time (its weather's time 6 times as
    // fast), alone and with the wind, beside the rest (small: cheap) (heavy: 100 %); and light (91 %), watched for 2 minutes
    const beading = Promise.all(["humidity2", "humidity2,wind", "humidity"].map(async (name) => {
      const p = await weatherPage(browser, url, { query: `?fx=${name}`, pace: 10, driven: true, context: { viewport: { width: 640, height: 480 } } });
      await p.page.evaluate(() => Sky.timeScale(6));
      await lookable(p.page);
      const seen = await humiditySeen(p.page, name === "humidity" ? 200 : 360);
      if (name === "humidity2") seen.grid = (await gridPeak(p.page)).ratio;   // (its fog's dithering)
      await p.page.evaluate((k) => Sky.timeScale(k), name === "humidity2,wind" ? 1 : 2);
      if (name === "humidity2") seen.snapped = await snapped(p.page);
      if (name === "humidity2,wind") {   // (then its trails' time 60 times as fast, through the fog; then humidity off: the fog clearing)
        seen.refog = await trailsAge(p.page, 60, true, 60);
        await p.page.evaluate(() => Sky.timeScale(2));
        seen.clears = await humidityClears(p.page, 300);
      }
      if (name === "humidity2") {   // (then every other effect on too, its share of the budget shrinking: watched 8 s more)
        await p.page.evaluate(() => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1, haze: 3, uv: 1, humidity: 1, wind: 1, heat: 1, ice: 5 } })));
        seen.squeezed = await humiditySeen(p.page, 40);
        // (then all off, faded out all the way, and humidity on again: the glass fresh, nothing left from before)
        const before = await p.page.evaluate(() => {   // (the trails as it's turned off)
          const trails = Sky.state().humidity.trails;
          document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: {} }));
          return trails;
        });
        await driveUntil(p.page, () => !Sky.clock().running);
        seen.again = await p.page.evaluate((before) => {
          const gone = Sky.state().humidity.marks();   // (faded out all the way: forgotten)
          document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { humidity: 1 } }));
          const h = Sky.state().humidity;
          return { before, trails: h.trails, beads: h.beads.length, stray: h.stray(), gone, marks: h.marks() };
        }, before);
        // (then its beads running and leaving trails (the weather's time 30 times as fast), and "stop animation", then the effect on again
        // and humidity on again: the glass fresh too)
        await p.page.evaluate(() => Sky.timeScale(30));
        await driveUntil(p.page, () => Sky.state().humidity.trails > 30);
        seen.marked = await p.page.evaluate(() => { Sky.timeScale(1); return Sky.state().humidity.marks(); });
        seen.restarted = await restarted(p.page, { humidity: 1 });
        // (and the physics the beads run by: a bead's mean speed on each glass, by size; its size after running 20 px in 5 s there)
        seen.physics = await p.page.evaluate(() => {
          const { speed, after, gone } = Sky.state().humidity.physics, sizes = [0.6, 1, 1.4, 2, 2.8, 4, 6, 10];
          return { sizes, gone, speed: Object.fromEntries(["dry", "trail", "fog"].map((g) => [g, sizes.map((r) => speed(r, g))])),
                   after: Object.fromEntries(["dry", "trail", "fog"].map((g) => [g, sizes.map((r) => after(r, g, 20, 5))])) };
        });
      }
      seen.errors = p.errors;
      await p.context.close();
      return seen;
    }));
    // heavy rain's beads (rain alone, light first; 30 s, then heavy, 75 s: its time 30 times as fast, small: cheap), and on clear
    // glass their trails drying (their time 120 times as fast), beside the rest
    const sticking = (async () => {
      const p = await weatherPage(browser, url, { query: "?fx=rain", pace: 10, driven: true, context: { viewport: { width: 480, height: 360 } } });
      await p.page.evaluate(() => Sky.timeScale(3));
      await lookable(p.page);
      const seen = await rainBeads(p.page, 100, 250, 0, 1);
      await p.page.evaluate(() => Sky.timeScale(1));
      let looks;
      ({ eased: seen.eased, after: seen.after, looks } = await rainBeads(p.page, 0, 0, 300, 6, "heavy"));
      seen.looks.push(...looks);   // (the trails' looks, those running on too)
      // the rain easing, still on (light): its streaks drying on their own time, there still 6 s (of its weather's time) after
      // (the heavy rain's streaks: a minute old at most, none dried yet)
      seen.easing = await p.page.evaluate(() => new Promise((resolve) => {
        const at = Sky.state().humidity.marks(), from = Sky.state().clock;
        Sky.timeScale(6);
        const look = () => {
          if (Sky.state().clock < from + 6) return __next(look);
          Sky.timeScale(1);
          resolve({ at, after: Sky.state().humidity.marks() });
        };
        __next(look);
      }));
      seen.drying = await trailsAge(p.page, 120, false, 30);   // (those streaks, still fresh: their time 120 times as fast)
      // heavy again (its weather's time 10 times as fast) till more streaks; off, faded out all the way (the loop over, its time 30
      // times as fast); heavy
      // again: the glass clean, nothing from before (its trails, its canvas, its beads; the rain's drops)
      const heavyTill = (more) => p.page.evaluate((more) => new Promise((resolve) => {
        const from = Sky.state().humidity.marks().trails;
        document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1 } }));
        Sky.timeScale(10);
        let n = 0;
        const look = () => {
          const m = Sky.state().humidity.marks();
          if (m.trails < from + more && n++ < 900) return __next(look);
          Sky.timeScale(1);
          resolve(m);
        };
        __next(look);
      }), more);
      seen.streaked = await heavyTill(30);
      await p.page.evaluate(() => { document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: {} })); Sky.timeScale(30); });
      await driveUntil(p.page, () => !Sky.clock().running);
      seen.cleared = await p.page.evaluate(() => {
        Sky.timeScale(1);
        const gone = Sky.state().humidity.marks();
        document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1 } }));
        const s = Sky.state();
        return { gone, again: s.humidity.marks(), stray: s.humidity.stray(), drops: s.particles.rain };
      });
      // ... and the same with "stop animation" between, then the effect on again
      seen.streakedAgain = await heavyTill(30);
      seen.restarted = await restarted(p.page, { rain: 1 });
      seen.errors = p.errors;
      await p.context.close();
      return seen;
    })();
    // heavy rain, alone and in the wind (and in the wind on a phone), its drops watched for 3 s, beside the rest; with the wind, the
    // leaves the drops knocked. (In the wind, the window's size: first light rain, as it fades in, its first few hits on the leaves.)
    const phoneSize = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };
    const raining = Promise.all([["rain2"], ["rain2,wind"], ["rain2,wind", phoneSize]].map(async ([name, context], i) => {
      const p = await weatherPage(browser, url, { query: `?fx=${i === 1 ? "rain,wind" : name}`, context, driven: true });
      let light = null;
      if (i === 1) {
        await driveUntil(p.page, () => Sky.state().wind.hits >= 3);
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
    // without the temporary test links (the page as it will be), at 1024x768 and on a phone with a tile hidden: "stop animation" at
    // the bottom left on its own
    const bare = Promise.all([{}, { viewport: { width: 360, height: 740 }, hasTouch: true, isMobile: true }].map(async (context) => {
      const p = await weatherPage(browser, url, { bare: true, driven: true, context,
        init: { fn: (key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: ["Rain"] })), arg: KEY } });
      await p.reads({ "UV index": 11 });
      const seen = { ...await cornerSeen(p.page), errors: p.errors };
      await p.context.close();
      return seen;
    }));
    // ?fx= when the page opens (each a page of its own, side by side, beside the rest; small: cheap)
    const params = Promise.all([["param", "?fx=haze3,wind,ice"], ["paramAll", "?fx=all"], ["paramRain", "?fx=rain2"],
                                ["paramOld", "?fx=leaves,radiation,drips,smog4"]].map(async ([name, query]) => {
      const opened = await weatherPage(browser, url, { query, driven: true, context: { viewport: { width: 480, height: 360 } } });
      await opened.page.waitForTimeout(200);
      out[name] = { ...await weatherSeen(opened.page), errors: opened.errors };
      await opened.context.close();
    }));
    // uv alone: its beams, no particles; the wind alone: its leaves within the budget, blowing right to left (and looping), watched
    // for 20 s of its time (several gusts and lulls: 2.5 s could fall in a lull, its eddies' loops then much of what moves) beside
    // the rest
    out.particles = {};
    out.rain = {};
    out.humidity = {};
    const alone = Promise.all(["uv", "wind"].map(async (name) => {
      const alone = await weatherPage(browser, url, { query: `?fx=${name}`, driven: true });
      if (name === "wind") out.wind = await windSeen(alone.page, 20000);
      else await drive(alone.page, 20);   // (under way)
      const p = await particles(alone.page);
      out.particles[name] = { [name]: p[name], budget: p.budget };
      await alone.context.close();
    }));
    // a phone, a tile hidden, an effect on (the wind, its leaves looping there too): the strip (reset preferences · last reading) at
    // the bottom right, the time last; the test row and "stop animation" not over it (beside the rest)
    const phoned = (async () => {
      const phone = await weatherPage(browser, url, { query: "?fx=wind", driven: true, context: { viewport: { width: 360, height: 740 }, hasTouch: true, isMobile: true },
        init: { fn: (key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: ["Rain"] })), arg: KEY } });
      out.phoneWind = await windSeen(phone.page, 20000);
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
    await still.reads({ "UV index": 11, Temperature: -4, hour: { "UV index": 9 } });
    const stillHand = await still.page.evaluate(() => {   // (the UV's red hand at once at its new highest: no frames for it)
      const t = [...document.querySelectorAll(".tile")].find((e) => e.querySelector(".title").textContent === "UV index");
      return t.querySelector("svg .hour").getAttribute("transform");
    });
    await still.page.waitForTimeout(500);
    out.still = { ...await weatherSeen(still.page), frames: await framesIn(still.page, 800), ink: await canvasInk(still.page), hand: stillHand,
                  stateFrames: await still.page.evaluate(() => Sky.state().frames) };
    out.still.humidity = await still.page.evaluate(() => {   // (humidity on too: its still frame some minutes in)
      document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { humidity: 1 } }));
      const h = Sky.state().humidity;
      return { beads: h.beads.length, ran: h.beads.filter((b) => b.y - b.y0 >= 10).length + h.ranOff, trails: h.trails, stray: h.stray(),
               fog: h.fog().cover, running: Sky.state().running, frames: Sky.state().frames };
    });
    out.still.flood = await still.page.evaluate(() => {   // (a flood: full at once, still; no frog)
      document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1, flood: 1 } }));
      const f = Sky.state().flood;
      return { depth: f.depth, full: f.full, frog: f.frog, running: Sky.state().running, frames: Sky.state().frames };
    });
    out.still.gust = await still.page.evaluate(() => {   // (a gust: nothing sent, by the page or the test link)
      document.dispatchEvent(new Event("ecobot-gust"));
      document.querySelector("#fxtest button[data-gust]").click();
      return { fling: Sky.fling("cow"), sent: Sky.state().gust.sent, running: Sky.state().running, frames: Sky.state().frames };
    });
    out.still.errors = still.errors;
    await still.context.close();
    await Promise.all([params, alone, phoned]);
    [out.uv, out.frost] = await swaying;
    [out.humidity.alone, out.humidity.windy, out.humidityLight] = await beading;
    out.rainBeads = await sticking;
    [out.rain.alone, out.rain.windy, out.rain.phone] = await raining;
    [out.bare, out.barePhone] = await bare;
    out.errors = [];
  },
  // A gust over 99 km/h: the live gust at 99 (nothing), just over (one sent), the same reading fetched again (no more), a new one over
  // (another), one under (none); then a cow, a barn and a chicken sent at once, watched every frame as they cross (the weather's time
  // 5 times as fast): where each is, its angle, the chicken's frames, the canvas's pixels while they cross (alone: theirs and their
  // bits'), the loop ending once they're gone; the test link (five clicks, its URL, never pressed), "stop animation" (the wind's
// link on too, so it shows: they're gone at once),
  // ?fx=gust as the page opens, and on a phone the row (every link, "gust" too, and "stop animation" in sight unscrolled). A frame
  // sequence of the three crossing kept as screenshots (SHOTS, when given)
  async gust(browser, url, out) {
    const opened = (async () => {   // (beside the rest)
      const p = await weatherPage(browser, url, { query: "?fx=gust", driven: true });
      const seen = { ...await p.page.evaluate(() => ({ sent: Sky.state().gust.sent, kinds: Sky.state().gust.kinds, effects: Sky.state().effects })),
                     url: await p.page.evaluate(() => location.search), errors: p.errors };
      await p.context.close();
      return seen;
    })();
    const phone = (async () => {
      const p = await weatherPage(browser, url, { query: "?fx=all", driven: true, context: { viewport: { width: 360, height: 740 }, hasTouch: true, isMobile: true } });
      await p.page.locator("#fxtest button[data-gust]").click();
      const seen = { ...await cornerSeen(p.page), sent: await p.page.evaluate(() => Sky.state().gust.sent), errors: p.errors };
      await p.context.close();
      return seen;
    })();
    const { context, page, errors, reads } = await weatherPage(browser, url, { driven: true });
    const sent = () => page.evaluate(() => Sky.state().gust.sent);
    out.live = [];
    for (const [gust, asOf] of [[99, "2026-11-16T10:00:00+11:00"], [99.1, "2026-11-16T10:01:00+11:00"], [99.1, "2026-11-16T10:01:00+11:00"],
                                [140, "2026-11-16T10:02:00+11:00"], [60, "2026-11-16T10:03:00+11:00"]]) {
      await reads({ gust, asOf });
      out.live.push(await sent());
    }
    out.liveKinds = await page.evaluate(() => Sky.state().gust.kinds);
    out.liveEffects = await page.evaluate(() => Sky.state().effects);
    // (the two on their way off first: the time on fast till they're gone)
    await page.evaluate(() => Sky.timeScale(8));
    await driveUntil(page, () => !Sky.clock().running);
    // a cow, a barn and a chicken at once, every frame till they're gone
    out.crossing = await page.evaluate(() => new Promise((resolve) => {
      Sky.timeScale(5);
      const kinds = ["cow", "barn", "chicken"].map((k) => Sky.fling(k)), c = document.querySelector("#sky canvas"), g = c.getContext("2d");
      const tracks = { cow: [], barn: [], chicken: [] }, size = Sky.state().size;
      const alphas = new Array(256).fill(0);   // (the canvas's pixels by their alpha, while all three cross: how faint)
      let frames = 0, inked = 0, effects = null, first = Sky.state().frames, last = first;
      const look = () => {
        const s = Sky.state();
        if (s.frames > last) {   // (a frame drawn since the last look)
          last = s.frames;
          frames++;
          for (const f of s.gust.flyers) tracks[f.kind].push([f.x, f.y, f.angle, f.turn, f.legs, f.size, s.clock]);
          if (s.gust.flyers.length === 3 && frames % 8 === 1) {   // (the canvas read now and then: reading it is slow)
            effects = effects || s.effects;
            const d = g.getImageData(0, 0, c.width, c.height).data;
            let n = 0;
            for (let i = 3; i < d.length; i += 4) if (d[i]) { n++; alphas[d[i]]++; }
            inked = Math.max(inked, n);
          }
        }
        if (s.running || frames === 0) return __next(look);
        Sky.timeScale(1);
        const all = alphas.reduce((a, b) => a + b, 0);
        let median = 0;
        for (let k = 0, n = 0; k < 256; k++) if ((n += alphas[k]) >= all / 2) { median = k; break; }
        resolve({ kinds, tracks, size, frames, median, strong: alphas.slice(160).reduce((a, b) => a + b, 0) / all, inked, effects, after: s.gust, running: s.running,
                  ink: (() => { const d = g.getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; })() });
      };
      __next(look);
    }));
    // (a frame's cost with the three crossing: sent again, a second on, its script's time; till they're gone)
    await page.evaluate(() => ["cow", "barn", "chicken"].forEach((k) => Sky.fling(k)));
    await drive(page, 30);
    out.crossing.cost = await frameCost(page, 30);
    await driveUntil(page, () => !Sky.clock().running);
    // the test link: each click one, the three kinds in three clicks; ?fx=gust; never pressed; "stop animation": gone at once
    const before = await sent();
    for (let i = 0; i < 5; i++) await page.locator("#fxtest button[data-gust]").click();
    out.link = { sent: await sent() - before, kinds: (await page.evaluate(() => Sky.state().gust.kinds)).slice(-5),
                 flying: await page.evaluate(() => Sky.state().gust.flyers.length), url: await page.evaluate(() => location.search),
                 pressed: await page.evaluate(() => document.querySelector("#fxtest button[data-gust]").getAttribute("aria-pressed")),
                 classes: await page.evaluate(() => document.querySelector("#fxtest button[data-gust]").className) };
    await page.locator("#fxtest button[data-fx='wind']").click();   // (an effect on: "stop animation" shows)
    await page.locator("#motion .animations").click();
    out.stopped = { ...await page.evaluate(() => ({ flyers: Sky.state().gust.flyers.length, running: Sky.state().running })),
                    url: await page.evaluate(() => location.search), ink: await canvasInk(page) };
    await reads({ gust: 150, asOf: "2026-11-16T10:30:00+11:00" });   // (stopped: a live gust over 99 km/h sends none)
    out.stopped.sent = await sent() - before;
    await page.locator("#fxtest button[data-gust]").click();   // (the test link after a stop: on again, one sent)
    out.resumed = { sent: await sent() - before, running: await page.evaluate(() => Sky.state().running) };
    out.errors = errors;
    await context.close();
    out.opened = await opened;
    out.phone = await phone;
  },
};

const PARTED = { "--arrange": arrangeMode, "--touch": touchMode, "--zoom": zoomMode, "--zoom-touch": zoomTouchMode };

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
    else if (PARTED[second]) {   // (PART, or several joined by "+": side by side, each its own page, a report each, in a list)
      const reports = await Promise.all(third.split("+").map((part) => PARTED[second](browser, url, part)));
      process.stdout.write(JSON.stringify(third.includes("+") ? reports : reports[0]) + "\n");
    }
    else if (second === "--fill") await fillMode(browser, url, JSON.parse(third));
    else if (second === "--weather") await weatherMode(browser, url, third);
    else await sizes(browser, url, second, JSON.parse(third));
  } finally {
    await browser.close();
  }
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
