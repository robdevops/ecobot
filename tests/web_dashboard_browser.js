// The dashboard page in Playwright's Chromium, for tests/test_web_dashboard.py.
//   node web_dashboard_browser.js URL SHOTS SIZES: the page at each size ({name: [width, height]}): a screenshot of each (SHOTS/dashboard-
//     NAME.png, device scale factor 2) and, on stdout, {name: {tiles, rects, columns, scroll, scrollWidth, scrollHeight, status, glass (each
//     tile's [kind, its glass highlights' paths]), errors,
//     zoomed, compass, compassZoomed}}; then the first tile zoomed (Enter): its look, and a screenshot (SHOTS/dashboard-NAME-zoomed.png);
//     then the compass (the wind's direction) zoomed: its look (compassLook), and a screenshot (SHOTS/dashboard-NAME-compass.png);
//     every dial's hub and pivot and the pressure's arrow (dials: dialsLook), and the pressure zoomed (pressureZoomed, and a screenshot:
//     SHOTS/dashboard-NAME-pressure.png).
//   node web_dashboard_browser.js URL --live: the page open at 800x600; prints {label} (the first tile's), waits for a line on stdin
//     (the test has changed the reading, and the wind's direction), then prints {label, same_page, compass (its look, and its needle's
//     turn every frame since), pressure (dialsLook's, and whether its arrow is the same element), errors} once the tile shows it, or
//     after 15 s.
//   node web_dashboard_browser.js URL --arrive TITLE: the page open at 1024x768; prints the tile's state ({label, faded, opacity, shown,
//     needle}), waits for a line on stdin (the test has given it a reading, or taken it away), then, once its label changes, samples it
//     every frame for 1.5 s and prints {before, after, frames: [{opacity, number}], same_tile, compass: [before, after] (compassLook),
//     pressure (dialsLook's), errors}.
//   node web_dashboard_browser.js URL --arrows JSON: the page at 1024x768 once for each tendency in the JSON list, the Atmospheric pressure
//     tile given it; prints [{tendency, hpa, inhg (dialsLook's pressure, in hPa and with inHg pressed), errors}].
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
//     red frames; "stop"; the temporary test links (beside them, MCP_DASHBOARD_ANIMATIONS off); on, low, off and reduced motion; the haze alone, heavy humidity's trails, the ice grown in, and rain with wind and haze (frame time, particle budget);
//     a flash flood;
//     ?fx= as the page opens, a phone; prints {step: what was seen}.
//   node web_dashboard_browser.js URL --rotate: a phone (412x839) and a tablet (820x1180), touch, scale 2: the page as a fresh load in each
//     orientation, then portrait, landscape, portrait, landscape (orientationchange, the viewport turned, orientationchange, 400 ms);
//     prints {device: {fresh: {portrait, landscape}, steps: [{orientation, now (what the screen shows), snapshot (what the fresh load
//     of that orientation shows)}]}}.
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

// No weather behind the tiles, the rest animated as served (MCP_DASHBOARD_ANIMATIONS on): the environment's DASHBOARD_NO_WEATHER set
// (tests/test_web_dashboard.py's run_page, weather False), for the pages that drive the tiles and time their moves, not the weather, so
// the machine's time goes to those moves. Once the page's scripts have run, Sky never set going nor sent anything across, and its
// canvas, "stop" and the test links not shown: as low motion's weather, without its zoom at once
const NO_WEATHER = () => document.addEventListener("DOMContentLoaded", () => {
  const set = Sky.set;
  Object.assign(Sky, { set: (want) => set(want, false), fling: () => null });
  document.head.append(Object.assign(document.createElement("style"), { textContent: "#sky, #corner { display: none !important; }" }));
});

async function open(browser, url, width, height, options = {}) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, ...(options.context || {}) });
  if (process.env.DASHBOARD_NO_WEATHER) await context.addInitScript(NO_WEATHER);
  if (options.risen) await context.addInitScript(RISEN);
  if (options.init) await context.addInitScript(options.init.fn, options.init.arg);
  const page = await context.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(url, { waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0 && /last reading/.test(document.getElementById("status").textContent),
                             null, { timeout: 15000 }).catch(async (e) => { throw new Error(`${e.message} ${errors} ${await page.evaluate(() => document.body.innerText.slice(0, 300))}`); });
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
        const box = t.getBoundingClientRect(), k = 480 / box.width;
        const drawn = [...t.querySelector("svg g.gauge").children].filter((e) => !e.matches(".limit, defs")).map((e) => e.getBoundingClientRect())
          .filter((r) => r.width || r.height);   // (the gauge's drawing, but a limit mark outside its ring: handsLook's, clear of the title)
        const g = { top: Math.min(...drawn.map((r) => r.top)), bottom: Math.max(...drawn.map((r) => r.bottom)) };
        const texts = [...t.querySelectorAll("svg text")];
        return { kind: t.dataset.kind, title: t.querySelector(".title").textContent, gap: (g.top - t.querySelector(".title").getBoundingClientRect().bottom) * k,
                 bottom: Math.max(g.bottom, ...texts.map((e) => e.getBoundingClientRect().bottom)) * k - box.top * k,
                 words: texts.filter((e) => !e.classList.contains("reading")).map((e) => e.textContent), bands: t.querySelectorAll("svg .band").length };
      }),
    })), errors };
    await handsSettled(page);
    report[name].hands = await handsLook(page);   // the dials' thin red hands: today's highest
    report[name].headColours = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll("#grid .tile")].map((t) =>   // every
      [t.querySelector(".title").textContent, [...t.querySelectorAll(".chip.head")].map((e) => [e.textContent, getComputedStyle(e).backgroundColor])])));   // table's heads
    await compassSettled(page);
    report[name].compass = await compassLook(page);   // the wind's direction in the grid: its compass, no table
    report[name].dials = await dialsLook(page);       // every round dial's hub and pivot, and the pressure's arrow
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
    // back, then the UV index zoomed: its limit mark as in the grid, scaled with it (SHOTS/dashboard-NAME-uv.png)
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.body.classList.contains("zoomed") && !document.querySelector(".tile.zooming, .tile.fading"));
    await page.evaluate(() => [...document.querySelectorAll("#grid .tile")].find((t) => t.querySelector(".title").textContent === "UV index").focus());
    await page.keyboard.press("Enter");
    await calm(page, 400);
    await page.screenshot({ path: `${shots}/dashboard-${name}-uv.png` });
    report[name].uvZoomed = { zoomed: await page.evaluate(() => document.body.classList.contains("zoomed")), ...(await handsLook(page))["UV index"] };
    // back, then the pressure zoomed: its hub and arrow as in the grid, scaled with it (SHOTS/dashboard-NAME-pressure.png)
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.body.classList.contains("zoomed") && !document.querySelector(".tile.zooming, .tile.fading"));
    await page.evaluate(() => [...document.querySelectorAll("#grid .tile")].find((t) => t.querySelector(".title").textContent === "Atmospheric pressure").focus());
    await page.keyboard.press("Enter");
    await calm(page, 400);
    await page.screenshot({ path: `${shots}/dashboard-${name}-pressure.png` });
    report[name].pressureZoomed = { zoomed: await page.evaluate(() => document.body.classList.contains("zoomed")), ...(await dialsLook(page))["Atmospheric pressure"] };
    await page.evaluate(() => document.querySelector("#units button[data-unit='inHg']").click());   // (in inHg: its arrow after the wider reading)
    await calm(page, 1200);
    report[name].pressureInHg = (await dialsLook(page))["Atmospheric pressure"];
    await context.close();
  }));
  process.stdout.write(JSON.stringify(Object.fromEntries(Object.keys(wanted).map((name) => [name, report[name]]))));
}

// Each tile's red mark at today's highest (null without one): its turn, shown or not, where it is among the dial's parts (after the
// needle, before the hub: [needle, mark, hub] indexes). A hand (the Wind's): its colour and width, its drawn box's narrower side against
// the needle's. A limit mark (a triangle: the others'): its fill, its corners' distances from the dial's centre (its own px, unturned),
// whether its tip points at the centre (its first corner, on the line through the centre it is turned on), the ring's radius, its own
// box; whether, drawn, it is clear of the title's text and of every tick number; its drawn width and its tile's (window px). With the
// Wind tile's table
const handsLook = (page) => page.evaluate(() => Object.fromEntries([...document.querySelectorAll("#grid .tile")].map((t) => {
  const hand = t.querySelector("svg .high"), title = t.querySelector(".title").textContent;
  if (!hand) return [title, null];
  const parts = [...t.querySelector("svg > g.gauge").children], line = hand.querySelector("path"), mark = hand.querySelector("polygon");
  const hub = parts.filter((e) => e.tagName === "circle").pop(), needle = t.querySelector("svg > g.gauge > polygon");
  const apart = (a, b) => a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top;
  const range = document.createRange();
  range.selectNodeContents(t.querySelector(".title"));
  const ring = parts.find((e) => e.tagName === "circle"), [cx, cy] = [Number(ring.getAttribute("cx")), Number(ring.getAttribute("cy"))];
  const corners = mark && mark.getAttribute("points").split(" ").map((p) => p.split(",").map(Number));
  const limit = mark && { fill: mark.getAttribute("fill"), radii: corners.map(([x, y]) => Math.hypot(x - cx, y - cy)),
                          inward: Math.abs(corners[0][1] - cy) < 0.01 && corners[0][0] > cx,
                          ring: Number(ring.getAttribute("r")), size: (({ width, height }) => ({ width, height }))(mark.getBBox()),
                          clearOfTitle: apart(mark.getBoundingClientRect(), range.getBoundingClientRect()),
                          clearOfTicks: [...t.querySelectorAll("svg > g.gauge > text")].filter((e) => /^[\d.]+$/.test(e.textContent))
                            .every((e) => apart(mark.getBoundingClientRect(), e.getBoundingClientRect())),
                          tileWidth: t.getBoundingClientRect().width, drawnWidth: mark.getBoundingClientRect().width };
  return [title, { turn: hand.getAttribute("transform"), shown: hand.style.display !== "none", kind: mark ? "limit" : "hand",
                   colour: line ? line.getAttribute("stroke") : null, width: line ? Number(line.getAttribute("stroke-width")) : null,
                   limit, needleWidth: needle.getBBox().width,
                   order: [parts.indexOf(needle), parts.indexOf(hand), parts.indexOf(hub)],
                   heads: title === "Wind" ? [...t.querySelectorAll(".chip.head")].map((e) => e.textContent) : undefined,
                   headColours: title === "Wind" ? [...t.querySelectorAll(".chip.head")].map((e) => getComputedStyle(e).backgroundColor) : undefined,
                   daily: title === "Wind" ? [...t.querySelectorAll(".figure")].slice(0, 2).map((e) => e.textContent) : undefined }];
})));
// Every round dial's (and the compass's) hub and its needle's pivot, in its own px: the hub's centre, radius and outline width; the
// needle's pivot (a dial's: the midpoint of its polygon's two side corners; the compass's: the centre its turn is about). Its arrow
// after the reading (the pressure's way over the past hour; null without one: its group's absence): whether it shows, its way
// (data-way), its path (d, the stroke's colour and width, its box (getBBox, the gauge's px), points along it every px), the reading's box (getBBox: its advance width; the font's line box), its last digit's ink's right edge and its digits' ink (the
// reading's text measured on a canvas in its own font, its middle baseline at the reading's y: [top, bottom]), the dial's outline
// ([cx, cy, r, width]), the hub's, the unit's and the tick numbers' boxes, and in window px the arrow's box and those of the tile's
// title and table (its chips and figures), the tile's width
const dialsLook = (page) => page.evaluate(() => Object.fromEntries([...document.querySelectorAll("#grid .tile")]
  .filter((t) => t.dataset.kind === "dial" || t.dataset.kind === "compass").map((t) => {
    const title = t.querySelector(".title").textContent, gauge = t.querySelector("svg > g.gauge"), parts = [...gauge.children];
    const hub = t.dataset.kind === "compass" ? gauge.querySelector(":scope > .hub") : parts.filter((e) => e.tagName === "circle").pop();
    const n = (e, k) => Number(e.getAttribute(k));
    let pivot;
    if (t.dataset.kind === "compass") pivot = /rotate\([-\d.]+ ([-\d.]+) ([-\d.]+)\)/.exec(gauge.querySelector(".needle").getAttribute("transform")).slice(1).map(Number);
    else {
      const pts = gauge.querySelector(":scope > polygon").getAttribute("points").split(" ").map((p) => p.split(",").map(Number));
      pivot = [(pts[1][0] + pts[3][0]) / 2, (pts[1][1] + pts[3][1]) / 2];
    }
    const g = gauge.querySelector(":scope > g.tendency");
    let arrow = null;
    if (g) {
      const path = g.querySelector("path.arrow"), shown = getComputedStyle(g).display !== "none";
      const box = (e) => { const b = e.getBBox(); return [b.x, b.y, b.x + b.width, b.y + b.height]; };
      const rect = (e) => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; };
      const reading = gauge.querySelector(":scope > text.reading"), ring = parts.find((e) => e.tagName === "circle");
      const c = document.createElement("canvas").getContext("2d"), style = getComputedStyle(reading);
      c.font = `${style.fontWeight} ${reading.getAttribute("font-size")}px ${style.fontFamily}`;
      c.textBaseline = "middle"; c.textAlign = "center";
      const m = c.measureText(reading.textContent), y = n(reading, "y"), inkRight = n(reading, "x") + m.actualBoundingBoxRight;
      const along = [];
      if (shown && path.getAttribute("d")) for (let s = 0, len = path.getTotalLength(); s <= len; s += 1) { const q = path.getPointAtLength(s); along.push([q.x, q.y]); }
      arrow = { shown, way: g.dataset.way, cls: g.dataset.class, d: path.getAttribute("d"), stroke: getComputedStyle(path).stroke, fill: getComputedStyle(g).fill,
                width: parseFloat(getComputedStyle(path).strokeWidth), box: shown ? box(path) : null, along,
                hubWidth: n(hub, "stroke-width"), reading: box(reading), inkRight, ink: [y - m.actualBoundingBoxAscent, y + m.actualBoundingBoxDescent],
                ring: [n(ring, "cx"), n(ring, "cy"), n(ring, "r"), n(ring, "stroke-width")], hub: box(hub),
                unit: box(parts.find((e) => e.tagName === "text" && /^(hPa|inHg)$/.test(e.textContent))),
                ticks: parts.filter((e) => e.tagName === "text" && e !== reading && /^[\d.]+$/.test(e.textContent)).map(box),
                rect: shown ? rect(path) : null, words: [t.querySelector(".title"), ...t.querySelectorAll(".chip, .figure, .unit")].map(rect),
                tileWidth: t.getBoundingClientRect().width };
    }
    return [title, { kind: t.dataset.kind, hub: [n(hub, "cx"), n(hub, "cy"), n(hub, "r"), n(hub, "stroke-width")], pivot, arrow,
                     reading: (gauge.querySelector(":scope > text.reading") || {}).textContent,
                     figures: title === "Atmospheric pressure" ? [...t.querySelectorAll(".figure")].map((e) => e.textContent) : undefined }];
  })));
// The red hands at rest (the opening's rise or a move over): none turned since the last look, 150 ms before, rather than a fixed wait
const handsSettled = (page) => page.waitForFunction(() => {
  const now = [...document.querySelectorAll("#grid .tile svg .high")].map((h) => h.getAttribute("transform")).join("|");
  const still = now && now === window.__hands;
  window.__hands = now;
  return still;
}, null, { timeout: 5000, polling: 150 }).catch(() => { /* reported as it is */ });

// The compass tile's look: its label, reading, needle's turn, the points named, any table, where its parts are (the window's px) and
// its needle's head and tail; its ring's centre and radius in its own px beside a dial's (the first dial tile's ring); its layers, bottom
// to top (the gauge's children's classes); its rose's petals (compass point, share, reach in its own px, colour, opacity); its reading's
// centre's distance from the ring's centre, in its own px
const compassLook = (page) => page.evaluate(() => {
  const t = document.querySelector('.tile[data-kind="compass"]'), rect = (e) => e.getBoundingClientRect().toJSON();
  const ring = t.querySelector("svg .ring"), dialRing = document.querySelector('.tile[data-kind="dial"] svg g.gauge > circle');
  const circle = (c) => c && ["cx", "cy", "r"].map((k) => Number(c.getAttribute(k)));
  const [cx, cy] = circle(ring), reading = t.querySelector("svg text.reading");
  return { label: t.getAttribute("aria-label"), reading: reading.textContent, faded: t.classList.contains("faded"),
           turn: t.querySelector(".needle").getAttribute("transform"), points: [...t.querySelectorAll("svg text.point")].map((e) => e.textContent),
           pointColours: [...t.querySelectorAll("svg text.point")].map((e) => e.getAttribute("fill")),
           table: t.querySelectorAll(".unit, .chip, .figure, .tablearea").length, zoomed: document.body.classList.contains("zoomed"),
           shown: !t.inert, box: rect(t), title: rect(t.querySelector(".title")), texts: [...t.querySelectorAll("svg text")].map(rect),
           ring: rect(ring), head: rect(t.querySelector(".needle .head")), hubColour: getComputedStyle(t.querySelector("svg .hub")).stroke, circle: circle(ring), dialCircle: circle(dialRing),
           dialRing: dialRing && rect(dialRing), ringStroke: Number(ring.getAttribute("stroke-width")),
           layers: [...t.querySelector("svg g.gauge").children].map((e) => e.getAttribute("class") || e.tagName),
           readingAt: Math.hypot(Number(reading.getAttribute("x")) - cx, Number(reading.getAttribute("y")) - cy),
           readingBelow: Number(reading.getAttribute("y")) - cy,
           petals: [...t.querySelectorAll("svg .rose .petal")].map((e) => {
             const [x, y] = e.getAttribute("d").match(/L([-\d.]+),([-\d.]+)/).slice(1).map(Number), c = getComputedStyle(e);
             return { point: e.dataset.point, share: Number(e.dataset.share), reach: Math.hypot(x - cx, y - cy), colour: c.fill,
                      opacity: Number(c.fillOpacity), fill: e.getAttribute("fill"), stroke: e.getAttribute("stroke") };
           }),
           // (the rose's gradients: id, units, centre and radius in its own px, stops [offset, colour, opacity]; any pattern in it)
           fades: [...t.querySelectorAll("svg .rose radialGradient")].map((g) => ({
             id: g.id, units: g.getAttribute("gradientUnits"), cx: Number(g.getAttribute("cx")), cy: Number(g.getAttribute("cy")),
             r: Number(g.getAttribute("r")), stops: [...g.querySelectorAll("stop")].map((s) => [Number(s.getAttribute("offset")),
               getComputedStyle(s).stopColor, Number(s.getAttribute("stop-opacity"))]) })),
           patterns: t.querySelectorAll("svg pattern, svg .rose [fill*=pattern]").length,
           ringCentre: [cx, cy],
           // (its needle's two ends: their fill and how opaque, their size in its own px (the shape's box, not turned) and on the window)
           ends: [".head", ".tail"].map((k) => {
             const e = t.querySelector(`.needle ${k}`), c = getComputedStyle(e), b = e.getBBox();
             return { colour: c.fill, opacity: Number(c.fillOpacity) * Number(c.opacity), area: b.width * b.height, length: b.height,
                      width: b.width };
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
  await page.evaluate(() => {
    window.__mark = 1; document.querySelector(".tile").__mark = 1;
    window.__pressureTile = () => [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === "Atmospheric pressure");
    window.__pressure = window.__pressureTile().querySelector("g.tendency");   // (the pressure's arrow: the same group after)
  });
  const pressureBefore = (await dialsLook(page))["Atmospheric pressure"];
  // (the compass's needle, every frame from now, the opening's rise over: the wind's direction changes with the temperature)
  await compassSettled(page);
  await handsSettled(page);
  await page.evaluate(() => {
    window.__turns = [];
    window.__hand = [];   // (and the Wind's red hand, every frame: today's highest gust rises; and the UV's limit mark: its highest rises)
    window.__limit = [];
    const needle = document.querySelector('.tile[data-kind="compass"] .needle');
    const high = (title) => [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === title).querySelector("svg .high");
    const wind = high("Wind"), uv = high("UV index");
    const sample = () => {
      window.__turns.push(Number(/rotate\(([-\d.]+)/.exec(needle.getAttribute("transform"))[1]));
      window.__hand.push(Number(/rotate\(([-\d.]+)/.exec(wind.getAttribute("transform"))[1]));
      window.__limit.push(Number(/rotate\(([-\d.]+)/.exec(uv.getAttribute("transform"))[1]));
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  const before = await label();
  process.stdout.write(JSON.stringify({ label: before, pressure: pressureBefore }) + "\n");
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
  const hands = { ...await handsLook(page), frames: await page.evaluate(() => window.__hand), limitFrames: await page.evaluate(() => window.__limit) };
  const pressureSame = await page.evaluate(() => window.__pressure.isConnected && window.__pressure === window.__pressureTile().querySelector("g.tendency"));
  const pressure = { ...(await dialsLook(page))["Atmospheric pressure"], same: pressureSame };   // (its arrow turned in place)
  process.stdout.write(JSON.stringify({ label: await label(), same_page: same, same_tile: sameTile, shown, compass, hands, pressure, errors }) + "\n");
}

// --arrows JSON: for each tendency given (null or {change, way, class}), a fresh page whose data's Atmospheric pressure tile carries it (the
// page's fetch altered on the way: the server's own is tested apart), the pressure's dialsLook in hPa and again with inHg pressed.
async function arrowsMode(browser, url, wanted) {
  const out = [];
  for (const tendency of JSON.parse(wanted)) {
    const init = { fn: (t) => {
      const real = window.fetch.bind(window);
      window.fetch = async (u, o) => {
        const r = await real(u, o);
        if (!/\/data(\?|$)/.test(String(u))) return r;
        const j = await r.clone().json();
        for (const tile of j.tiles || []) if (tile.title === "Atmospheric pressure") tile.tendency = t;
        return new Response(JSON.stringify(j), { status: r.status, headers: { "content-type": "application/json" } });
      };
    }, arg: tendency };
    const { context, page, errors } = await open(browser, url, 1024, 768, { init });
    await page.evaluate(() => document.fonts.ready);
    await calm(page, 3000);
    const hpa = (await dialsLook(page))["Atmospheric pressure"];
    await page.evaluate(() => document.querySelector("#units button[data-unit='inHg']").click());
    await calm(page, 500);
    const inhg = (await dialsLook(page))["Atmospheric pressure"];
    out.push({ tendency, hpa, inhg, errors });
    await context.close();
  }
  process.stdout.write(JSON.stringify(out));
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
                                        pressure: (await dialsLook(page))["Atmospheric pressure"], errors }) + "\n");
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
  else if (part === "reset") await arrangedReset(browser, url, out, errors);
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
  // mouseover: no frame, the tile under the mouse too (shown only picked up, or with the keyboard's focus)
  out.hoverFrames = await page.evaluate(() => [...document.querySelectorAll(".tile")].slice(0, 3).map((t) => getComputedStyle(t, "::before").borderTopColor));
  out.hover = await page.evaluate(() => [...document.querySelectorAll(".tile")].slice(0, 3).map((t) => ({
    grip: getComputedStyle(t.querySelector(".grip")).opacity, close: getComputedStyle(t.querySelector(".close")).opacity })));
  // the grip under the mouse: its size and place in its tile (px), its dots' spacing
  out.grip = await page.evaluate(() => {
    const t = [...document.querySelectorAll(".tile")][1], g = t.querySelector(".grip"), r = g.getBoundingClientRect(), b = t.getBoundingClientRect();
    return { width: r.width, height: r.height, left: r.left - b.left, top: r.top - b.top, dots: getComputedStyle(g).backgroundSize };
  });
  // the frame shown on the tile under the mouse picked up, then (the mouse gone) with the keyboard's focus, then neither
  const frameOf = (title) => page.evaluate((title) => getComputedStyle([...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === title), "::before").borderTopColor, title);
  const humidity = (how) => page.evaluate((how) => [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === "Humidity").classList[how]("lifted"), how);
  out.shownFrames = {};
  await humidity("add");
  out.shownFrames.lifted = await frameOf("Humidity");
  await humidity("remove");
  await page.mouse.move(1, 1);
  await page.keyboard.press("Shift");   // (the keyboard in use: the focus seen)
  await page.evaluate(() => [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".title").textContent === "Humidity").focus());
  out.shownFrames.focused = await frameOf("Humidity");
  await page.evaluate(() => document.activeElement.blur());
  out.shownFrames.after = await frameOf("Humidity");
  await page.mouse.move(...await at(page, "Humidity", 0.5, 0.4));
  await calm(page);
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
// "reset preferences" left of the units' switches, in the bar with them (reset | switches | strip): the switches' boxes the same with the
// link shown (a unit switched) and gone (reset), at a desktop's, a tablet's and two phones' widths; the link left of the first
// switch, on screen, over nothing; clicking it puts the tiles, the units and the order back.
async function arrangedReset(browser, url, all, allErrors) {
  const out = all.resetBar = {};
  out.widths = {};
  for (const [width, height] of [[1440, 900], [1024, 768], [768, 1024], [390, 800], [320, 600]]) {
    const { context, page, errors } = await open(browser, url, width, height, { risen: true });
    await calm(page);
    const look = () => page.evaluate(() => {
      const box = (e) => { const r = e.getBoundingClientRect(); return { left: r.left, top: r.top, right: r.right, bottom: r.bottom }; };
      const units = [...document.querySelectorAll("#units button")].map(box), link = document.querySelector("#reset .link");
      const apart = (a, b) => a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top;
      const strip = box(document.getElementById("status")), bar = box(document.getElementById("units"));
      return { units, bar, strip, link: link && box(link), linkInStrip: !!document.querySelector("#status .reset"), name: link && link.textContent,
               layout: document.body.dataset.units, reset: document.body.dataset.reset, status: document.getElementById("status").textContent,
               onScreen: !link || (link.getBoundingClientRect().left >= 0 && link.getBoundingClientRect().right <= innerWidth && link.getBoundingClientRect().bottom <= innerHeight),
               clear: !link || [...units, strip].every((u) => apart(box(link), u)), width: innerWidth };
    });
    const first = await look();
    await page.locator("#units button", { hasText: "°F" }).click();
    await calm(page);
    const shown = await look();
    if (process.env.RESET_SHOTS) await page.screenshot({ path: `${process.env.RESET_SHOTS}/reset-${width}.png`, clip: { x: 0, y: height - 90, width, height: 90 } });
    const order = await titles(page);
    await page.getByText("reset preferences").click();
    await calm(page);
    const gone = await look();
    out.widths[width] = { first, shown, gone, restored: JSON.stringify(order) === JSON.stringify(await titles(page)), stored: await stored(page) };
    allErrors.push(...errors);
    await context.close();
  }
  // a tile hidden: the link, the tiles back on reset
  const { context, page, errors } = await open(browser, url, 1024, 768, { risen: true });
  await calm(page);
  const before = await titles(page);
  await page.mouse.move(...await at(page, "Rain", 0.5, 0.4));
  await calm(page);
  await tileOf(page, "Rain").locator(".close").click();
  await calm(page);
  out.hidden = { titles: await titles(page), link: await page.locator("#reset .link").count() };
  await page.getByText("reset preferences").click();
  await calm(page);
  out.back = { titles: await titles(page), link: await page.locator("#reset .link").count(), same: JSON.stringify(before) === JSON.stringify(await titles(page)) };
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
  await unitsSwitched(page, out);
  allErrors.push(...errors);
  await context.close();
}
// The units' switches (on the "hidden" part's page, after its reset): the temperature tiles, the Wind, Atmospheric pressure, Rain,
// Rain rate and Humidity (untouched) as they look (unitsLook) in metric, then °F (clicked), then mph too (the Wind's needle sampled
// each frame as it eases), then inHg, then in; reloaded; km/h again from the keyboard; then "reset preferences" (metric again). Each:
// the looks (the pressure's arrow's way too), the switches' pressed state and the strip
const UNIT_TILES = ["Temperature", "Dew point", "Feels like", "Wind", "Humidity", "Atmospheric pressure", "Rain", "Rain rate"];
const unitsLook = (page) => page.evaluate((names) => {
  const look = {};
  for (const t of document.querySelectorAll("#grid .tile")) {
    const title = t.querySelector(".title").textContent;
    if (!names.includes(title)) continue;
    const high = t.querySelector("svg .high"), arrow = t.querySelector("svg g.tendency");
    look[title] = { arrow: arrow ? arrow.dataset.way : null, label: t.getAttribute("aria-label"), reading: t.querySelector("svg text.reading").textContent,
                    words: [...t.querySelectorAll("svg text:not(.reading)")].map((e) => e.textContent),
                    unit: t.querySelector(".unit").textContent, figures: [...t.querySelectorAll(".figure")].map((e) => e.textContent),
                    hand: high ? high.getAttribute("transform") : null };
  }
  return { tiles: look, pressed: [...document.querySelectorAll("#units button")].map((b) => [b.textContent, b.getAttribute("aria-pressed")]),
           status: document.getElementById("status").textContent, stored: JSON.parse(localStorage.getItem("ecobot.dashboard.v1")),
           focused: document.activeElement && document.activeElement.closest("#units") ? document.activeElement.textContent : null };
}, UNIT_TILES);
async function unitsSwitched(page, out) {
  const seen = out.units = {};
  await calm(page, 2000);
  seen.metric = await unitsLook(page);
  await page.click("#units button[data-unit='°F']");
  await calm(page, 2000);
  seen.fahrenheit = await unitsLook(page);
  const needle = () => page.evaluate(() => new Promise((resolve) => {   // (the Wind's needle each frame for 1.2 s: its points)
    const all = [], start = performance.now();
    const tile = () => [...document.querySelectorAll("#grid .tile")].find((t) => t.querySelector(".title").textContent === "Wind");
    const step = (now) => { all.push(tile().querySelector("svg polygon").getAttribute("points")); if (now - start < 1200) requestAnimationFrame(step); else resolve(all); };
    requestAnimationFrame(step);
  }));
  const [, frames] = await Promise.all([page.click("#units button[data-unit='mph']"), needle()]);
  seen.needleSteps = new Set(frames).size;
  await calm(page, 2000);
  seen.mph = await unitsLook(page);
  await page.click("#units button[data-unit='inHg']");
  await calm(page, 2000);
  seen.inHg = await unitsLook(page);
  await page.click("#units button[data-unit='in']");
  await calm(page, 2000);
  seen.inches = await unitsLook(page);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0 && /last reading/.test(document.getElementById("status").textContent));
  await calm(page, 2000);
  seen.reloaded = await unitsLook(page);
  await page.focus("#units button[data-unit='km/h']");   // the keyboard: km/h again (Enter)
  await page.keyboard.press("Enter");
  await calm(page, 2000);
  seen.keyboard = await unitsLook(page);
  await page.getByText("reset preferences").click();
  await calm(page, 2000);
  seen.reset = await unitsLook(page);
}
async function arrangedFromPrefs(browser, url, out, allErrors) {
  const { context, page, errors } = await open(browser, url, 1024, 768, { risen: true });
  const initial = await titles(page);
  // preferences corrupt: the defaults; a tile new to the saved order: in its usual place (after the tile before it by default)
  await page.evaluate((key) => localStorage.setItem(key, "{not json"), KEY);
  await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
  out.corrupt = await titles(page);
  // a layout saved while the AQI tile was titled "US AQI": that tile moved first, then hidden, as it was left
  const renamed = async (prefs) => {
    await page.evaluate(([key, prefs]) => localStorage.setItem(key, JSON.stringify(prefs)), [KEY, prefs]);
    await page.reload({ waitUntil: "load" });
  await page.waitForFunction(() => document.querySelectorAll(".tile").length > 0);
    return titles(page);
  };
  out.renamedMoved = await renamed({ order: ["US AQI", ...initial.filter((t) => t !== "AQI")], hidden: [] });
  out.renamedHidden = await renamed({ order: [], hidden: ["US AQI", "Rain"] });
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

// ---------- a rotated screen ----------
// What the page shows: the grid's columns and rows, the tiles' rounded rects, the weather's canvas, and the page's own fit to the screen
const shown = () => {
  const el = document.documentElement, g = document.getElementById("grid"), c = document.querySelector("#sky canvas"), vv = window.visualViewport;
  const round = (r) => [r.left, r.top, r.width, r.height].map(Math.round);
  return {
    snapshot: { columns: getComputedStyle(g).gridTemplateColumns, rows: getComputedStyle(g).gridAutoRows, bodyColumns: document.body.dataset.columns,
                tiles: [...g.children].map((t) => round(t.getBoundingClientRect())), sky: [c.width, c.height, c.style.width, c.style.height] },
    fit: { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, innerWidth: window.innerWidth, scale: vv ? vv.scale : 1, scrollX: window.scrollX,
           inside: [...g.children].every((t) => { const r = t.getBoundingClientRect(); return r.left >= 0 && r.right <= el.clientWidth + 0.5; }),
           skyCovers: parseFloat(c.style.width) >= el.clientWidth && parseFloat(c.style.height) >= el.clientHeight } };
};

async function rotateMode(browser, url) {
  const out = {};
  const devices = { pixel7: [412, 839], ipadAir: [820, 1180], ipadPro13: [1032, 1376] };
  const sized = (name, landscape) => { const [w, h] = devices[name]; return landscape ? { width: h, height: w } : { width: w, height: h }; };
  const settle = async (page) => { await page.evaluate(() => new Promise((ok) => requestAnimationFrame(() => requestAnimationFrame(ok)))); await page.waitForTimeout(400); };
  for (const name of Object.keys(devices)) {
    const fresh = {}, steps = [], errors = [];
    for (const orientation of ["portrait", "landscape"]) {   // (a fresh load of each)
      const opened = await open(browser, url, 1, 1, { context: { viewport: sized(name, orientation === "landscape"), isMobile: true, hasTouch: true } });
      await settle(opened.page);
      fresh[orientation] = await opened.page.evaluate(shown);
      errors.push(...opened.errors);
      await opened.context.close();
    }
    const { context, page, errors: more } = await open(browser, url, 1, 1, { context: { viewport: sized(name, false), isMobile: true, hasTouch: true } });
    await settle(page);
    for (const orientation of ["portrait", "landscape", "portrait", "landscape"]) {
      const landscape = orientation === "landscape";
      await page.evaluate(() => window.dispatchEvent(new Event("orientationchange")));
      await page.setViewportSize(sized(name, landscape));
      await page.evaluate(() => window.dispatchEvent(new Event("orientationchange")));
      await settle(page);
      steps.push({ orientation, now: await page.evaluate(shown) });
    }
    errors.push(...more);
    // Turned into landscape with the size reported late: the events come while the page still reads the portrait size (and the page
    // has been shifted sideways, as a browser can), the real size arriving only some time after
    const stale = {};
    await page.setViewportSize(sized(name, false)); await settle(page);
    await page.evaluate(() => { window.dispatchEvent(new Event("orientationchange")); window.dispatchEvent(new Event("resize")); });
    await page.waitForTimeout(150);
    await page.setViewportSize(sized(name, true));
    await page.evaluate(() => { document.body.style.overflowX = "scroll"; window.scrollTo(40, 0); document.body.style.overflowX = ""; });
    await page.waitForTimeout(1200);
    stale.landscape = await page.evaluate(shown);
    stale.sideways = await page.evaluate(() => window.scrollX);
    // a grid wider than the screen starts at its left edge (never overflows to the unreachable left)
    stale.wide = await page.evaluate(() => { const g = document.getElementById("grid"); g.style.gridTemplateColumns = "repeat(4, 600px)";
      return g.firstElementChild.getBoundingClientRect().left; });
    out[name] = { fresh, steps, errors, stale };
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
    window.__skyTest = { step: pace === 1 ? 1 / 30 : 0.1, driven, seed, time: 720 };   // (time: midday, beams straight down, whatever the clock)
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
  }, [options.pace || 1, !!options.driven, (Number(process.env.SKY_SEED) || (options.seed ? 1115 : 0)) + (options.seed || 0)]);   // (SKY_SEED: the
  // weather's randomness seeded otherwise; `seed`: this page's seeded that much further on, another sample of the same weather)
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
    const readings = { "AQI": 40, ...state.readings };
    for (const t of data.tiles) if (t.title in readings) t.value = readings[t.title];
    if ("rain_rate" in readings) data.rain_rate = readings.rain_rate;
    data.pm25 = "pm25" in readings ? readings.pm25 : null;                      // (the air sensor's PM2.5: none unless given)
    data.flood = "flood" in readings ? { depth: readings.flood } : null;        // (the server's leaky bucket: none unless given)
    if ("gust" in readings) data.gust = readings.gust;              // (the gust now, km/h)
    if (readings.asOf) data.as_of = { ...data.as_of, time: readings.asOf };   // (a new reading: its time)
    if (readings.high) Object.assign(data.today_highs, readings.high);   // (the red hands': today's highest)
    for (const t of data.tiles) if (readings.scales && t.title in readings.scales) Object.assign(t, readings.scales[t.title]);   // (as the server
                                                                                                    // widens a thermometer's scale)
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
// The bottom corners: "stop" (its words, its box), the test row's and its last link's boxes (null without them), the strip's;
// the row's links' words, whether its "test:" shows, whether all of it fits unscrolled, the room left right of the link; whether the
// link is clear of the strip (and of the units' switches: their box), and on the screen
const cornerSeen = (page) => page.evaluate(() => {
  const box = (e) => e ? e.getBoundingClientRect().toJSON() : null, link = document.querySelector("#motion .animations");
  const row = document.getElementById("fxtest"), buttons = row ? [...row.querySelectorAll("button")] : [];
  const m = box(document.getElementById("motion")), s = box(document.getElementById("status")), u = box(document.getElementById("units")), rs = box(document.getElementById("reset"));
  const clear = (s) => m.right <= s.left || s.right <= m.left || m.bottom <= s.top || s.bottom <= m.top;
  return { units: u, reset: rs, link: link ? link.textContent : null, motion: m, row: box(row), last: box(buttons.at(-1)), strip: s,
           buttons: buttons.map((b) => b.textContent), tag: row ? !!row.querySelector(".tag") : null,   // (no "test:" label any more)
           fits: row ? row.scrollWidth <= row.clientWidth && row.scrollLeft === 0 : null, spare: innerWidth - m.right,
           window: [innerWidth, innerHeight], apart: clear(s) && clear(u),
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
// The haze alone (faded in), at each band in turn, 1 to 5, once it is baked: of the canvas drawn, the share covered (its cover) and its
// mean alpha (0-255), its strongest regular pattern (gridPeak), the mean colour of what is drawn (by its alpha) and its luminance, the
// luminance (by alpha) of the upper half and of the lower, and how far the density runs along the canvas against across it: the
// autocorrelation's sum over lags 1 to 24 of the alpha (its mean taken out) along each row, and down each column. Then on a clock 4 times
// as fast, Hazardous: with no wind (still: the picture and slide unchanged 2 s apart, and again after a minute), with the live wind at 3, 40
// and 25 (gusting 40) km/h (each: the pan's px/s and speed, the picture's best sideways shift, rows down, and the share of pixels
// matching exactly there, the motes' mean move and spread, the bakes before and after), a minute of it in steps, the wind turned (mirrored),
// a band change (the bakes, the cross-fade), the baked image's seam; and Hazardous's cost a frame (ms)
const hazeSeen = async (page) => {
  const out = { levels: [] };
  for (let level = 1; level <= 5; level++) {
    await page.evaluate((level) => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { haze: level } })), level);
    await driveUntil(page, (level) => { const h = Sky.state().haze; return h.level === level && !h.pending && !h.settling; }, level);
    await drive(page, 12);
    const seen = await page.evaluate(() => {
      const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data, h = Sky.state().haze;
      const w = c.width, rows = c.height;
      let drawn = 0, alpha = 0;
      const sum = [0, 0, 0], half = [[0, 0], [0, 0]];
      const a = new Float32Array(w * rows);
      for (let y = 0; y < rows; y++) for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4, v = d[i + 3];
        a[y * w + x] = v;
        drawn += v > 0; alpha += v;
        const lum = 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
        for (let k = 0; k < 3; k++) sum[k] += d[i + k] * v;
        const part = half[y < rows / 2 ? 0 : 1];
        part[0] += lum * v; part[1] += v;
      }
      const all = Math.max(alpha, 1e-6), mean = alpha / a.length;
      let variance = 0;
      for (let i = 0; i < a.length; i++) variance += (a[i] - mean) ** 2;
      const along = (dx, dy) => {   // (the autocorrelation summed over lags 1-24 in that direction, over the variance)
        let total = 0;
        for (let lag = 1; lag <= 24; lag++) {
          let s = 0;
          for (let y = 0; y < rows - lag * dy; y++) for (let x = 0; x < w - lag * dx; x++) s += (a[y * w + x] - mean) * (a[(y + lag * dy) * w + x + lag * dx] - mean);
          total += s / variance;
        }
        return total;
      };
      const rgb = sum.map((v) => v / all);
      return { level: h.level, cover: drawn / a.length, mean, rgb, lum: 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2],
               upper: half[0][0] / Math.max(half[0][1], 1e-6), lower: half[1][0] / Math.max(half[1][1], 1e-6),
               acrossX: along(1, 0), acrossY: along(0, 1), tint: h.tint };
    });
    seen.grid = (await gridPeak(page)).ratio;
    out.levels.push(seen);
  }
  out.cost = await frameCost(page);   // (Hazardous, alone: the script's ms a frame)
  return page.evaluate((out) => {
    Sky.timeScale(4);
    const fx = (detail) => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail }));
    const picture = () => { const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data; return { w: c.width, h: c.height, d: Uint8ClampedArray.from(d) }; };
    const same = (p, q) => p.d.length === q.d.length && p.d.every((v, i) => v === q.d[i]);
    // (the shift, leftward positive, and the down-shift at which q matches p best, over lags up to `reach` across and 3 down: q(x, y) = p(x + s, y + t); with the share of the pixels that match exactly there)
    const shifted = (p, q, reach) => {
      let best = null;
      for (let t = -3; t <= 3; t++) for (let s = -reach; s <= reach; s++) {
        let n = 0, hit = 0;
        for (let y = Math.max(0, -t); y < Math.min(p.h, p.h - t); y += 2) for (let x = Math.max(0, -s); x < Math.min(p.w, p.w - s); x += 2) {
          const i = ((y + t) * p.w + x + s) * 4, j = (y * p.w + x) * 4;
          n++; if (p.d[i + 3] === q.d[j + 3] && p.d[i] === q.d[j] && p.d[i + 1] === q.d[j + 1]) hit++;
        }
        if (!best || hit / n > best.share) best = { s, t, share: hit / n };
      }
      return best;
    };
    // (the weather's time: so many seconds on, in frames of 4 x 1/30 s)
    const run = (seconds) => { const end = Sky.state().clock + seconds; let n = 0; while (Sky.state().clock < end && n++ < 20000) Sky.advance(1); };
    const rows = (p) => { const r = new Float32Array(p.h); for (let y = 0; y < p.h; y++) for (let x = 0; x < p.w; x++) r[y] += p.d[(y * p.w + x) * 4 + 3] / p.w; return r; };
    const correlation = (a, b) => {
      const m = (v) => v.reduce((s, x) => s + x, 0) / v.length, ma = m(a), mb = m(b);
      let sab = 0, saa = 0, sbb = 0;
      for (let i = 0; i < a.length; i++) { sab += (a[i] - ma) * (b[i] - mb); saa += (a[i] - ma) ** 2; sbb += (b[i] - mb) ** 2; }
      return sab / Math.sqrt(saa * sbb);
    };
    const motion = (seconds, reach) => {   // the weather's `seconds`: its slide, picture, motes and bakes before and after
      const a = Sky.state(), was = picture();
      run(seconds);
      const b = Sky.state(), now = picture(), dt = b.clock - a.clock;
      const kept = new Map(a.haze.motes.map((m) => [m[0], m])), dx = [], dy = [];
      for (const m of b.haze.motes) if (kept.has(m[0])) { dx.push(m[1] - kept.get(m[0])[1]); dy.push(m[2] - kept.get(m[0])[2]); }
      const shift = shifted(was, now, reach);
      return { dt, slide: b.haze.slide - a.haze.slide, rate: (b.haze.slide - a.haze.slide) / dt, speed: b.haze.speed, renders: [a.haze.renders, b.haze.renders],
               shift: shift.s, down: shift.t, match: shift.share, identical: same(was, now), motes: dx.length,
               moteDx: dx.length ? dx.reduce((s, v) => s + v, 0) / dx.length : 0, moteSpread: dx.length ? Math.max(...dx) - Math.min(...dx) : 0, moteDy: dy.length ? Math.max(...dy.map(Math.abs)) : 0 };
    };
    // no wind at all (the live speed 0): perfectly still, 2 s apart, the motes too
    Sky.setWindSpeed(0, 0);
    run(40);   // (whatever wind the page had, low-passed away)
    out.still = motion(2, 3);
    run(60);
    out.stillLong = motion(2, 3);
    // wind at 3 km/h (gusting a little): a slow pan; and at 40 km/h; a pure sideways shift (the shift's best match at no rows down), the bake count unchanged
    for (const [speed, gust, key, seconds] of [[3, 3, "light", 30], [40, 40, "strong", 12], [25, 40, "gusty", 12]]) {
      Sky.setWindSpeed(speed, gust);
      run(30);   // (the low-pass settled)
      out[key] = motion(seconds, key === "light" ? 20 : 45);
    }
    // a minute of it at 40 km/h in steps: each step a pure shift again, and the rows' profile as it was
    Sky.setWindSpeed(40, 40);
    run(20);
    const first = picture(), steps = [];
    for (let i = 0; i < 6; i++) steps.push(motion(10, 45));
    out.minute = { steps: steps.map((m) => ({ down: m.down, match: m.match, renders: m.renders })), rows: correlation(rows(first), rows(picture())) };
    // the wind's other side: the pan reverses (the speed through 0 as the direction turns), no new bake; mirrored
    const before = Sky.state().haze.renders;
    Sky.setWind(1);
    run(8);
    out.mirrored = motion(10, 45);
    out.mirrored.baked = Sky.state().haze.renders - before;
    Sky.setWind(-1);
    run(8);
    // a new band: baked once more, cross-fading over about a second
    Sky.setWindSpeed(0, 0);
    run(30);
    const baked = Sky.state().haze.renders;
    fx({ haze: 3 });
    let n = 0;
    while (Sky.state().haze.level !== 3 && n++ < 100) Sky.advance(1);
    Sky.advance(1);
    const mid = Sky.state().haze;
    n = 0;
    while (Sky.state().haze.settling && n++ < 600) Sky.advance(1);
    out.band = { baked: Sky.state().haze.renders - baked, mid: { settling: mid.settling, pending: mid.pending }, settled: Sky.state().haze.settling };
    out.seam = Sky.state().haze.seam;
    out.width = [Sky.state().haze.width, Sky.state().size[0]];
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
              profile: columns.map((c) => Math.round(c.reduce((x, y) => x + y, 0) / c.length * 10) / 10),
              overTime: columns.reduce((a, c) => a + spread(c), 0) / columns.length, vx: vx.reduce((a, b) => a + b, 0) / vx.length });
  };
  // (from 50 frames in, 1.5 s or more of the page's time: the effects faded in, the wind's gusts under way)
  const begun = () => Sky.clock().frames >= 50 ? look() : __next(begun);
  begun();
}), ms);
// The sun (heat: the disc with its halo and flare; UV: the beams round it) on a driven page (?fx=heat,uv, 640x480). Prints, for the
// tests: `arcs` (the sun's place every 5 minutes over the solstice and equinox days, from half an hour before sunrise to half an hour
// after sunset: {m, day, x, y, r, t} and the window's size), `night` (the canvas's ink and the sun's day flag at 01:00 and 21:40),
// `discs` (at heat 0.3 and 1, the disc's mean colour at its centre and near its limb, from the canvas), `beams` (the probe's beams at
// UV 11: count, widths against the sun's diameter), `amounts` (UV 5.9 to 12: the probe's amount, the violet pixels drawn), `lobes`
// (at five places of the sun: the lean, the beams' angles and lengths), `ends` (each beam's brightness along it from 0.5 to 1 of its
// length, and the violet along the longest in the picture), `edge` (violet pixels in the window's outer ring), `flash` (the brightness
// over 24 s: its dominant frequency, heat alone and with the UV), `grid` (the dither's grid power), `bakes` and `renders`
const sunLook = async (page) => {
  const fx = (detail) => page.evaluate((detail) => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail })), detail);
  const inPage = (f, arg) => page.evaluate(f, arg);
  await page.evaluate(() => {
    window.__c = document.querySelector("#sky canvas"); window.__g = window.__c.getContext("2d");
    window.__grab = () => window.__g.getImageData(0, 0, window.__c.width, window.__c.height).data;
    window.__day = (rise, set, noon, m) => { Sky.setSun({ rise, set, now: m, noon_el: noon }); Sky.setTime(m); };
    window.__violet = (d) => { let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 0) n += Math.max(0, d[i + 2] - d[i] - 10); return n; };   // (the violet drawn: how much bluer than red, summed)
  });
  const out = {};
  await fx({ heat: 1, uv: 1 });
  const DAYS = { summer: [354.36, 1241.24, 75.61], winter: [455.21, 1027.59, 28.74], equinox: [442.31, 1174.58, 52.81] };
  out.arcs = await inPage((DAYS) => {
    const run = {};
    for (const [name, [rise, set, noon]] of Object.entries(DAYS)) {
      run[name] = [];
      for (let m = Math.floor(rise) - 30; m <= set + 30; m += 5) {
        __day(rise, set, noon, m); Sky.advance(1);
        const st = Sky.state(), s = st.heat.sun;
        run[name].push({ m, day: s.day, x: s.x, y: s.y, r: s.r, t: s.t, size: st.heat.size, noon });
      }
    }
    return run;
  }, DAYS);
  // nothing at night (no ink on the canvas; no beams), the sun up at midday
  out.night = await inPage(() => {
    const seen = {};
    for (const [name, m] of [["early", 60], ["late", 1300], ["noon", 798]]) {
      __day(354.36, 1241.24, 75.61, m); Sky.setSunPosition(null, null); Sky.advance(60);
      const d = __grab(); let ink = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) ink++;
      seen[name] = { ink, day: Sky.state().heat.sun.day };
    }
    return seen;
  });
  // the disc: mean colour at its centre (inside 0.35 R) and near its limb (0.7 to 0.9 R), heat 0.3 and 1, the UV off
  out.discs = [];
  for (const level of [0.3, 1]) {
    await fx({ heat: level, uv: 0 });
    out.discs.push(await inPage(() => {
      __day(354.36, 1241.24, 75.61, 798); Sky.advance(150);
      const st = Sky.state(), s = st.heat.sun, d = __grab(), W = __c.width, mean = (lo, hi) => {
        let n = 0; const sum = [0, 0, 0];
        for (let y = Math.floor(s.y - s.r); y <= Math.ceil(s.y + s.r); y++) for (let x = Math.floor(s.x - s.r); x <= Math.ceil(s.x + s.r); x++) {
          const q = Math.hypot(x + 0.5 - s.x, y + 0.5 - s.y) / s.r; if (q < lo || q > hi) continue;
          const i = (y * W + x) * 4; for (let k = 0; k < 3; k++) sum[k] += d[i + k]; n++;
        }
        return sum.map((v) => v / Math.max(n, 1));
      };
      return { level: st.heat.level, centre: mean(0, 0.35), limb: mean(0.7, 0.9), drawn: st.drawn.heat, cap: st.cap };
    }));
  }
  // the beams at UV 11
  await fx({ heat: 1, uv: 1 });
  out.beams = await inPage(() => {
    __day(354.36, 1241.24, 75.61, 600); Sky.setSunPosition(80, 40); Sky.advance(150);
    const b = Sky.state().beam;
    return { count: b.count, diameter: b.diameter, widths: b.beams.map((x) => x.width), lengths: b.beams.map((x) => x.length), lobe: b.lobe, edge: b.edge };
  });
  // the UV's amount from its index: the probe's amount and the violet drawn
  out.amounts = [];
  for (const index of [5.9, 6, 6.5, 7, 7.5, 8, 9, 10, 11, 12]) {
    await fx({ heat: 1, uv: Math.max(0, Math.min(1, (index - 6) / 5)) });
    out.amounts.push(await inPage((index) => {
      Sky.advance(120);
      const b = Sky.state().beam;
      return { index, amp: b.amp, violet: __violet(__grab()), beams: b.beams.length };
    }, index));
  }
  await fx({ heat: 1, uv: 1 });
  // the lobe: the longest and brightest beams nearest the sun's real direction; the lean the page works out
  out.lobes = await inPage(() => {
    __day(354.36, 1241.24, 75.61, 600);
    return [[80, 40], [300, 30], [0, 70], [120, 10], [240, 60], [100, 20]].map(([az, el]) => {
      Sky.setSunPosition(az, el); Sky.advance(60);
      const b = Sky.state().beam;
      return { az, el, lean: b.lean, beams: b.beams.map((x) => ({ angle: x.angle, length: x.length, bright: x.bright })) };
    });
  });
  // the ends: each beam's profile from half its length to its end (steady, then in the picture along the longest)
  out.ends = await inPage(() => {
    __day(354.36, 1241.24, 75.61, 798); Sky.setSunPosition(0, 70); Sky.advance(150);
    const st = Sky.state(), b = st.beam, s = st.heat.sun, W = __c.width, d = __grab();
    const profiles = b.beams.map((_, i) => Array.from({ length: 21 }, (_, k) => b.at(i, 0.4 + 0.03 * k)));
    const long = b.beams.reduce((a, x, i) => x.length > b.beams[a].length ? i : a, 0), L = b.beams[long], ang = L.angle * Math.PI / 180;
    const violet = [0.3, 0.6, 0.8, 0.95, 1].map((f) => {   // (the mean of a 5 x 5 patch on its axis, the channel above the page's own)
      const x = Math.round(s.x + Math.sin(ang) * f * L.length), y = Math.round(s.y + Math.cos(ang) * f * L.length); let sum = 0, n = 0;
      for (let yy = y - 2; yy <= y + 2; yy++) for (let xx = x - 2; xx <= x + 2; xx++) { if (xx < 0 || yy < 0 || xx >= W || yy >= __c.height) continue; sum += Math.max(0, d[(yy * W + xx) * 4 + 2] - 29) * (d[(yy * W + xx) * 4 + 3] > 0); n++; }
      return { f, x, y, v: sum / Math.max(n, 1), inside: x >= 0 && y >= 0 && x < W && y < __c.height };
    });
    return { profiles, violet, ends: b.beams.map((_, i) => b.at(i, 1)), start: b.beams.map((_, i) => b.at(i, 0.3)) };
  });
  // the window's outer ring (one pixel): no violet there (the beams fade out over 34 px short of the edge)
  out.edge = await inPage(() => {
    __day(354.36, 1241.24, 75.61, 450); Sky.setSunPosition(90, 5); Sky.advance(150);
    const d = __grab(), W = __c.width, H = __c.height; let n = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (x === 0 || y === 0 || x === W - 1 || y === H - 1) { const i = (y * W + x) * 4; if (d[i + 3] > 0 && d[i + 2] > d[i] + 30) n++; }
    return { violet: n, whole: __violet(d) };
  });
  // the dither's grid power at three moments of the day
  out.grid = [];
  for (const m of [372, 798, 1225]) {
    await inPage((m) => { __day(354.36, 1241.24, 75.61, m); Sky.setSunPosition(m < 700 ? 90 : m > 1000 ? 270 : 0, 40); Sky.advance(120); }, m);
    out.grid.push((await gridPeak(page)).ratio);
  }
  // the brightness over 24 s: its dominant frequency, heat alone and with the UV
  out.flash = [];
  for (const detail of [{ heat: 1, uv: 0 }, { heat: 1, uv: 1 }]) {
    await fx(detail);
    out.flash.push(await inPage(() => {
      __day(354.36, 1241.24, 75.61, 700); Sky.setSunPosition(60, 50); Sky.advance(90);
      const n = 720, series = [];
      for (let k = 0; k < n; k += 2) {
        Sky.advance(2);
        const d = __grab(); let s = 0; for (let i = 0; i < d.length; i += 4) s += (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) * d[i + 3] / 255;
        series.push(s);
      }
      const m = series.length, mean = series.reduce((a, b) => a + b, 0) / m, x = series.map((v) => v - mean);
      let best = 0, at = 0;
      for (let k = 1; k <= m / 2; k++) {
        let re = 0, im = 0; for (let i = 0; i < m; i++) { re += x[i] * Math.cos(2 * Math.PI * k * i / m); im -= x[i] * Math.sin(2 * Math.PI * k * i / m); }
        const p = re * re + im * im; if (p > best) { best = p; at = k; }
      }
      const st = Sky.state();
      let mx = 0; const dd = __grab(); for (let i = 3; i < dd.length; i += 4) mx = Math.max(mx, dd[i]);
      return { dominant: at * 15 / m, swing: (Math.max(...series) - Math.min(...series)) / mean, maxAlpha: mx / 255, drawn: st.drawn.heat, cap: st.cap };
    }));
  }
  // the picture made again every 0.1 s of the weather's time, and baked again only for a new size or a sun moved a quarter pixel
  await fx({ heat: 1, uv: 1 });
  out.bakes = await inPage(() => {
    __day(354.36, 1241.24, 75.61, 700); Sky.advance(30);
    const h = () => Sky.state().heat, a = h(); Sky.advance(90); const b = h();
    __day(354.36, 1241.24, 75.61, 701); Sky.advance(6); const c = h();   // (a minute on: a few pixels along the arc)
    __day(354.36, 1241.24, 75.61, 701.001); Sky.advance(6); const d = h();   // (a thousandth of a minute: under the step)
    return { start: a.bakes, after: b.bakes, moved: c.bakes, tiny: d.bakes, renders: b.renders - a.renders, every: a.every, size: h().size };
  });
  out.bakes.beforeResize = await inPage(() => { Sky.advance(3); return Sky.state().heat.bakes; });
  await page.setViewportSize({ width: 560, height: 420 });
  await page.waitForTimeout(700);
  out.bakes.resized = await inPage(() => { Sky.advance(3); return { baked: Sky.state().heat.bakes, size: Sky.state().heat.size }; });
  await page.setViewportSize({ width: 640, height: 480 });
  await page.waitForTimeout(700);
  // a very wide window (1600 x 400, W / H about 4): the summer day's arc, its disc whole all day
  await page.setViewportSize({ width: 1600, height: 400 });
  await page.waitForTimeout(700);
  out.wide = await inPage((DAY) => {
    const [rise, set, noon] = DAY, rows = [];
    for (let m = Math.floor(rise) - 30; m <= set + 30; m += 5) {
      __day(rise, set, noon, m); Sky.advance(1);
      const st = Sky.state(), s = st.heat.sun;
      rows.push({ m, day: s.day, x: s.x, y: s.y, r: s.r, t: s.t, size: st.heat.size });
    }
    return rows;
  }, DAYS.summer);
  await page.setViewportSize({ width: 640, height: 480 });
  await page.waitForTimeout(700);
  await inPage(() => { Sky.setSunPosition(null, null); Sky.setSun({ rise: 360, set: 1080, now: 720 }); Sky.setTime(720); Sky.advance(3); });   // (the page's own day again)
  return out;
};
// The cost of the sun on the real window's size (1024 x 768): the weather's frame cost (Sky.state().cost, the page script's own time,
// eased) after 300 frames of heat and UV together, and the picture's own cost (ms) when it is made
const sunCost = async (browser, url) => {
  const p = await weatherPage(browser, url, { query: "?fx=heat,uv", driven: true });
  const seen = await p.page.evaluate(() => {
    Sky.setSun({ rise: 354.36, set: 1241.24, now: 600, noon_el: 75.61 }); Sky.setTime(600); Sky.setSunPosition(80, 40); Sky.advance(150);
    const mean = [], t0 = performance.now();
    for (let k = 0; k < 300; k++) { const s = performance.now(); Sky.advance(1); mean.push(performance.now() - s); }
    mean.sort((a, b) => a - b);
    const st = Sky.state();
    return { cost: st.cost, mean: (performance.now() - t0) / 300, p50: mean[150], max: mean[299], render: st.heat.ms, size: st.heat.size };
  });
  seen.errors = p.errors;
  await p.context.close();
  return seen;
};
// The beams' lean through the day (Sky.setTime, no frames needed): `curve`, every minute of the day on the page's fallback day
// (06:00-18:00), then a given day's (07:00-19:00, Sky.setSun): the lean at sunrise, at its midday, and 60 s of the weather's time after
// (the beams on: heat) the page's own clock going on
const sunSeen = (page) => page.evaluate(() => {
  const curve = [];
  for (let m = 0; m <= 1440; m++) { Sky.setTime(m); curve.push(Sky.state().beam.lean); }
  Sky.setTime(null);
  Sky.setSun({ rise: 420, set: 1140, now: 420 });
  const sunrise = Sky.state().beam;
  Sky.setSun({ rise: 420, set: 1140, now: 780 });
  const midday = Sky.state().beam;
  Sky.advance(1800);
  const later = Sky.state().beam;
  Sky.setSun({ rise: 900, set: 800, now: 850 });   // (not a day: ignored)
  const ignored = Sky.state().beam;
  Sky.setTime(720);
  return { curve, sunrise, midday, later, ignored };
});
// The beams' lean by the sun's real place (Sky.setSunPosition, tests/sun_days.json: Melbourne's sun every 10 minutes on the summer and
// winter solstices, from lib/solar.py): `points` (az, el) -> lean; each day's lean through it pinned; then each day run on by the page's
// own clock from the data's samples (Sky.setSun with az and el: a sample a minute after the first, then every 10 minutes, the page
// advancing 60 s between): the lean each minute; then with no place (setSun without az, el) the curve again
const placeSeen = (page, days) => page.evaluate((days) => {
  const lean = (az, el) => { Sky.setSunPosition(az, el); const b = Sky.state().beam; return { lean: b.lean, az: b.az, el: b.el, up: b.up, real: b.real }; };
  const points = {};
  for (const [name, az, el] of [["noon", 0, 70], ["east", 90, 40], ["west", 270, 40], ["lowEast", 90, 2], ["lowWest", 270, 2], ["south", 180, 40], ["off20high", 20, 70], ["off20low", 20, 30], ["overhead", 90, 89], ["down", 90, -5]]) points[name] = lean(az, el);
  const through = {};
  for (const [name, [, rows]] of Object.entries(days)) through[name] = rows.map(([m, az, el]) => ({ m, az, el, ...lean(az, el) }));
  Sky.setSunPosition(null, null);
  Sky.setTime(null);
  const run = {};
  Sky.timeScale(60);   // (a frame: a second of the weather's time at 2 s: 30 frames, a minute)
  for (const [name, [, rows]] of Object.entries(days)) {
    const leans = [], jumps = [];
    const sample = ([m, az, el]) => Sky.setSun({ rise: 300, set: 1200, now: m, az, el });
    const [m0, az0, el0] = rows[0], [m1, az1, el1] = rows[1];
    sample(rows[0]);
    Sky.advance(30);
    sample([m0 + 1, az0 + (az1 - az0) / 10, el0 + (el1 - el0) / 10]);   // (a minute on: the page has its rate)
    let at = m0 + 1;
    for (let i = 1; i < rows.length; i++) {
      while (at < rows[i][0]) { Sky.advance(30); at++; leans.push(Sky.state().beam.lean); }   // (the page's own run, a minute at a time)
      const before = Sky.state().beam.lean;
      sample(rows[i]);
      jumps.push(Math.abs(Sky.state().beam.lean - before));   // (what the new sample moved it)
    }
    run[name] = { leans, jumps, ends: Sky.state().beam };
  }
  Sky.timeScale(1);
  Sky.setSun({ rise: 360, set: 1080, now: 720 });   // (no az, el: the curve's place again)
  const curve = Sky.state().beam;
  Sky.setTime(720);
  return { points, through, run, curve };
}, days);
// uv's beams alone over 40 s of the page's time (on a page whose clock runs on fast: `pace`, below; each frame 0.1 s on, Sky's longest
// step): the canvas's brightness every frame from 60 frames in (the beams faded in): its dominant frequency (Hz) and swing, and the beams'
// angles at the start and the end of it (they sway slowly, each way a few degrees at most)
const uvSeen = (page) => page.evaluate(() => {
  Sky.setSun({ rise: 354.36, set: 1241.24, now: 600, noon_el: 75.61 }); Sky.setTime(600); Sky.setSunPosition(80, 40);
  Sky.advance(60);
  const c = document.querySelector("#sky canvas"), g = c.getContext("2d"), series = [], angles = [];
  for (let k = 0; k < 400; k++) {
    Sky.advance(1);
    const d = g.getImageData(0, 0, c.width, c.height).data; let s = 0;
    for (let i = 0; i < d.length; i += 4) s += (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) * d[i + 3] / 255;
    series.push(s);
    if (k % 100 === 0) angles.push(Sky.state().beam.beams.map((b) => b.angle));
  }
  const m = series.length, mean = series.reduce((a, b) => a + b, 0) / m, x = series.map((v) => v - mean);
  let best = 0, at = 0;
  for (let k = 1; k <= m / 2; k++) {
    let re = 0, im = 0; for (let i = 0; i < m; i++) { re += x[i] * Math.cos(2 * Math.PI * k * i / m); im -= x[i] * Math.sin(2 * Math.PI * k * i / m); }
    const p = re * re + im * im; if (p > best) { best = p; at = k; }
  }
  const sway = angles[0].map((a, i) => Math.max(...angles.map((v) => Math.abs(v[i] - a))));
  return { frames: Sky.clock().frames, dominant: at * 10 / m, swing: (Math.max(...series) - Math.min(...series)) / mean, sway: Math.max(...sway) };
});
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
// frame, how many tiny beads (not drying away, not snapping) and the most there may be (cap); how many snaps; the tiny beads grown
// on the fog into small ones since the page opened (swelled: how many; swells: the latest, see the probe's; swell: the rate); every frame, the most pixels drawn in the canvas's top two rows, and the still beads drawn
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
// width, widths, paused: each row where it had paused (a blob there), lefts, rights, ys, and through the fog inside: what shows inside it, see inside(),
// and soft: each row's px partly wiped (its soft edges), edges: each row's first and last wiped px, on the glass (null: none)}.
// (Put in the page as window.__trailLook, see lookable())
const trailLook = (h, b) => {
  const kinds = { clear: [], fog: [] };
  for (const r of h.trailOf(b.id)) {
    if (!(r.wet > 0.88) || !r.mine) continue;   // (fresh, its own: not another bead's since, over it)
    const mid = 3 + ((r.w - 1) >> 1), fogs = r.fog.slice(2, r.w + 4);
    if (fogs.every(([, f]) => f < 0.06)) {
      kinds.clear.push({ y: r.y, w: r.w, paused: r.paused, width: r.alpha.filter((a, k) => k >= 2 && k < r.w + 4 && a > 0 && r.life[k] === r.life[mid]).length });
    } else if (r.fog[mid][1] >= 0.1) {
      const wiped = r.fog.map(([s, f], k) => (f > 0 && s < 0.5 * f && r.life[k] === r.life[mid] ? k : -1)).filter((k) => k >= 0);
      // (its soft edges: the px its own trail partly wiped, the fog shown there between a tenth and nine tenths of what it'd be)
      const soft = r.fog.filter(([s, f], k) => f > 0 && s > 0.1 * f && s < 0.9 * f && r.life[k] === r.life[mid]).length;
      kinds.fog.push({ y: r.y, w: r.w, at: r.left, width: wiped.length, soft, left: wiped.length ? wiped[0] - 3 : null,
                       right: wiped.length ? wiped[wiped.length - 1] - 3 - (r.w - 1) : null,
                       // (each px across, at x: wiped (its own), what it shows)
                       px: r.fog.map((_, k) => ({ x: r.left - 3 + k, wiped: wiped.includes(k), shows: r.shows[k], residue: r.residue[k] > 0 })),
                       shades: r.fogShades, residueShades: r.residueShades });
    }
  }
  // (through the fog, what shows inside the wiped streak: each patch of it (8-connected) not touching the fog beside it (not
  // wiped: the soft edge, its fog partly there) and wholly within the rows looked at: {px, tall, wide, shows: what its
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
          if (!q.wiped) { whole = false; continue; }   // (at the soft edge: the fog partly there, or coming back)
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
    return { patches, odd, residue, px: rows.reduce((n, r) => n + r.px.filter((p) => p.wiped).length, 0), residueShades: rows[0].residueShades };
  };
  return Object.entries(kinds).filter(([kind, rows]) => rows.length >= (kind === "clear" ? 16 : 12)).map(([kind, rows]) => ({
    id: b.id, kind, w: rows.reduce((s, r) => s + r.w, 0) / rows.length, widths: rows.map((r) => r.width), ys: rows.map((r) => r.y),
    paused: rows.map((r) => !!r.paused), lefts: rows.map((r) => r.left), rights: rows.map((r) => r.right),
    ...(kind === "fog" ? { inside: inside(rows), soft: rows.map((r) => r.soft),
                             edges: rows.map((r) => (r.width ? [r.at + r.left, r.at + r.w - 1 + r.right] : null)) } : {}) }));
};
// The beads followed frame by frame (window.__beadWatch(), see lookable(); each frame .step(Sky.state()), then .result()):
//   stretches: each time one elongated (its stretch from 0 to more), {r: its radius then, most: its stretch at most (0-1), rows: the
//     most rows its sprite was drawn taller than its own, cols: the most columns narrower, low: the most its drawn lower edge was off
//     its own (px), lasted: s (the weather's) till round again (null: gone first)}, small: frames a bead under 2.5 showed elongated
//     with none set going while it was 2.5 or more;
//   splashes: each heavy-rain splash's droplet left on the glass {r: its radius at most, tiny: tiny always while it was one, moved:
//     frames it moved (not snapping), glass: the glass under it as it last sat (before fading), fogged: s on fog, aged: s shown in
//     all, fadedAt: its age as it began drying away (null: never), thrown: [dx, dy] from where its drop struck, end: "faded" (dried away), "taken" (merged into another: snapped
//     or swept up by a runner), "on" (still there at the end) or "other"};
//   locks: the beads locked on another's run (lock), each {id, rows: frames it was locked in a row it could see the run under it,
//     off: the most px its middle was from the run's middle there, dropped: it let go (not gone, not merged) with the run still
//     under it or a few rows on (within 2 px), end: "gone" (merged or run off), "dropped", "on"}
const beadWatch = () => {
  const stretching = new Map(), stretches = [], splashes = new Map(), last = new Map(), locks = new Map();
  let small = 0, clock = null;
  return {
    step(state) {
      const h = state.humidity, t = state.clock, dt = clock === null ? 0 : t - clock, absorbed = new Set(h.absorbed), now = new Set();
      clock = t;
      for (const b of h.beads) {
        now.add(b.id);
        if (b.lock) {
          let k = locks.get(b.id);
          if (!k) { k = { id: b.id, rows: 0, off: 0, dropped: false, end: "on", bottom: 0 }; locks.set(b.id, k); }
          const x = Math.round(b.x), y = b.bottom, wet = (xx, yy) => h.glassAt(xx, yy) === "trail";
          if (y !== k.bottom && wet(x, y)) {
            let l = x, r = x;
            while (x - l < 6 && wet(l - 1, y)) l--;
            while (r - x < 6 && wet(r + 1, y)) r++;
            k.rows++;
            if (x - l < 6 && r - x < 6) k.off = Math.max(k.off, Math.abs(b.x - (l + r) / 2));   // (a run of its own: not two joined)
          }
          k.bottom = y;
        }
        let e = stretching.get(b.id);
        if (b.stretch > 0 && !b.fading) {
          if (!e) { e = { r: b.r, most: 0, rows: 0, cols: 0, low: 0, from: t, lasted: null }; stretching.set(b.id, e); stretches.push(e); }
          e.most = Math.max(e.most, b.stretch);
          if (b.drawn && b.drawn[2] <= b.w) {   // (as drawn: not its pop, a size bigger, after a merge)
            const [, y, w, hh] = b.drawn;
            e.rows = Math.max(e.rows, hh - (b.bottom - Math.floor(b.y)));
            e.cols = Math.max(e.cols, b.w - w);
            e.low = Math.max(e.low, Math.abs(y + hh - b.bottom));
          }
          if (b.r < 2.5 && e.r < 2.5) small++;
        } else if (e) { e.lasted = t - e.from; stretching.delete(b.id); }
        if (b.kind === "splash") {
          let d = splashes.get(b.id);
          const before = last.get(b.id);
          if (!d) { d = { id: b.id, r: b.r, tiny: true, moved: 0, glass: null, fogged: 0, aged: 0, fadedAt: null, end: "other", thrown: b.thrown }; splashes.set(b.id, d); }
          d.r = Math.max(d.r, b.r);
          if (!b.tiny) d.tiny = false;
          if (before && b.tiny && !b.snapping && !before.snapping && (b.x !== before.x || b.y !== before.y)) d.moved++;
          if (b.fading) { if (d.fadedAt === null) d.fadedAt = b.age; }
          else { d.glass = b.glass; if (b.glass === "fog") d.fogged += dt; }
          d.aged = b.age;
        }
      }
      for (const [id, b] of last) {
        const k = locks.get(id);
        if (!k || k.end !== "on") continue;
        if (!now.has(id)) k.end = "gone";
        else {
          const c = h.beads.find((o) => o.id === id);
          if (c.lock) continue;
          k.end = "dropped";   // (looked at where it is now, the rows ahead of it: its own trail is behind)
          const x = Math.round(c.x), lead = h.beads.some((o) => o.id !== id && Math.abs(o.x - x) <= c.r + o.r + 2 && o.cy - c.bottom > -1 && o.cy - c.bottom < 7);   // (a bead ahead on it: its body there)
          for (let dy = 0; dy <= 5; dy++) for (let dx = -2; dx <= 2; dx++) if (!lead && h.glassAt(x + dx, c.bottom + dy) === "trail") k.dropped = true;
        }
      }
      for (const id of last.keys()) if (!now.has(id)) {
        stretching.delete(id);
        const d = splashes.get(id);
        if (d && d.end === "other") d.end = absorbed.has(id) ? "taken" : d.fadedAt !== null ? "faded" : "other";
      }
      last.clear();
      for (const b of h.beads) last.set(b.id, b);
    },
    result() {
      for (const id of last.keys()) { const d = splashes.get(id); if (d && d.end === "other") d.end = "on"; }
      return { stretches: stretches.map(({ from, ...e }) => e), small, splashes: [...splashes.values()], locks: [...locks.values()].map(({ bottom, ...k }) => k) };
    },
  };
};
// The beads followed (window.__beadWatch()) for `frames` frames: what it saw, the weather's s taken, the drops landed, and the
// biggest beads' restarts after a pause and their stretches set going the while (begun)
const beadsWatched = (page, frames) => page.evaluate((frames) => new Promise((resolve) => {
  const watch = window.__beadWatch(), from = Sky.state();
  let n = 0;
  const step = () => {
    const state = Sky.state();
    watch.step(state);
    if (++n < frames) return __next(step);
    const h = state.humidity, h0 = from.humidity;
    resolve({ ...watch.result(), s: state.clock - from.clock, landed: h.landed - h0.landed, restarts: h.restarts - h0.restarts,
              begun: h.stretches - h0.stretches });   // (begun: the stretches set going)
  };
  __next(step);
}), frames);
const lookable = (page) => page.evaluate(`window.__trailLook = ${trailLook.toString()}; window.__beadWatch = ${beadWatch.toString()}`);
const humiditySeen = (page, frames) => page.evaluate((frames) => new Promise((resolve) => {
  const last = new Map(), first = Sky.state().frames, count = Sky.state().particles.humidity, h0 = Sky.state().humidity;
  const from = { ranOff: h0.ranOff, faded: h0.faded, shrunk: h0.shrunk, merges: h0.merges, glass: h0.glass, sizes: h0.sizes, restarts: h0.restarts, stretches: h0.stretches };
  const watch = window.__beadWatch ? window.__beadWatch() : null;
  let up = 0, down = 0, wander = 0, offset = 0, samples = 0, lowest = 0, moved = 0, behind = 0, trails = 0, popped = 0, under = 0;
  let top = 0, inked = 0, alphas = 0, most = 0, looked = 0, still = 0, running = 0, counts = 0, crept = 0, tinies = [];
  const snaps = h0.snaps;
  const ran = new Set(), wandered = new Set(), wiped = new Map(), looks = [], traced = new Set();
  const canvas = document.querySelector("#sky canvas"), g = canvas.getContext("2d");
  const step = () => {
    const state = Sky.state(), h = state.humidity, now = new Map(h.beads.map((b) => [b.id, b])), absorbed = new Set(h.absorbed), left = new Set(h.left);
    for (const [id, b] of last) if (!now.has(id) && !absorbed.has(id) && !left.has(id) && b.r >= 1 && b.alpha > 0.15) popped++;
    if (watch) watch.step(state);
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
      for (const b of h.beads) if (b.r >= 1.5 && b.v < 1 && b.alpha === 1 && b.drawn) {   // (big enough to look at: fewer reach r 2 now they run from about large)
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
              ranOff: h.ranOff - from.ranOff, faded: h.faded - from.faded, shrunk: h.shrunk - from.shrunk, under, behind: behind / Math.max(moved, 1), trails, popped, count: [count, Sky.state().particles.humidity],
              born: h.born.filter((b) => b.kind === "fog"), merges: h.merges - from.merges, merged: h.merged, largest: h.largest, top,
              glass, sizes: h.sizes.map(([px, s], i) => [px - from.sizes[i][0], s - from.sizes[i][1]]), bands: h.bands, stray: h.stray(),
              look: { beads: looked, mean: alphas / Math.max(inked, 1), most },
              showing: { still: still / Math.max(counts, 1), running: running / Math.max(counts, 1) },
              wiped: [...wiped.values()].map((t) => ({ ...t, end: { shown: h.fogAt(...t.at)[0], wet: h.wetAt(...t.at) } })),
              fog: h.fog(true), first: h.first, colours: h.colours(), aged: h.beads.filter((b) => !b.fading).map((b) => b.age),
              landed: h.landed, looks, crept, tinies, snaps: h.snaps - snaps, tiny: h.tiny,
              restarts: h.restarts - from.restarts, stretches: h.stretches - from.stretches, watched: watch && watch.result(),
              since: { restarts: h.restarts, stretches: h.stretches, big: h.big },   // (since the page opened: its beading up first too)
              swelled: h.swelled, swells: h.swells, swell: h.swell });
  };
  __next(step);
}), frames);
// Tiny beads put on the glass in the fog, a clear spot (no bead within 6 px): two close (1.6 px across and 1 down: not touching,
// their edges under a px apart, within the snap) and, 20 px on, two just beyond it (2.5 px across: their edges about 1.4 px apart);
// then 6 frames: each placed one as it is (gone, or {x, y, r, vol,
// tiny}), and every bead then within 3 px of the close two's
const snapped = (page) => page.evaluate(() => {
  const h0 = Sky.state().humidity, [W, H] = [h0.width, h0.height];
  let spot = null;
  for (let y = Math.round(H * 0.3); y < H * 0.7 && !spot; y += 3) for (let x = 20; x < W - 40 && !spot; x += 3) {
    if (h0.glassAt(x, y) !== "fog" || h0.glassAt(x + 25, y) !== "fog") continue;
    if (h0.beads.every((b) => Math.hypot(b.x - x - 1, b.cy - y) > 6 && Math.hypot(b.x - x - 21, b.cy - y) > 6)) spot = [x, y];
  }
  if (!spot) return { spot };
  const [x, y] = spot, placed = h0.place([{ x, y }, { x: x + 1.6, y: y + 1 }, { x: x + 20, y }, { x: x + 22.5, y }]);
  const at = Sky.state().humidity.beads.filter((b) => placed.includes(b.id)).map((b) => ({ x: b.x, y: b.y, r: b.r, vol: b.vol }));
  Sky.advance(6);
  const h = Sky.state().humidity, byId = new Map(h.beads.map((b) => [b.id, b]));
  return { spot, at, placed: placed.map((id) => { const b = byId.get(id); return b ? { x: b.x, y: b.y, r: b.r, vol: b.vol, tiny: b.tiny } : null; }),
           near: h.beads.filter((b) => Math.hypot(b.x - x - 1, b.cy - y - 0.5) <= 3).map((b) => ({ id: b.id, r: b.r, vol: b.vol, tiny: b.tiny })),
           snap: h.snap, tinyR: h.tiny };
});
// A big bead (r 4.2) and a smaller one (r 3.4), each with a tiny one a px off its edge (placed on fog, 30 px apart), then 40 frames one
// by one: for each pair, the big one's (the one left, the water of both) frames {gulp (0-1 swell), w, h (as drawn), rw, rh (its own
// sprite's), bottom (its own lower edge), low (the drawn lower edge), vol, stretch}, and the two's water before
const gulped = (page) => page.evaluate(() => {
  const h0 = Sky.state().humidity, [W, H] = [h0.width, h0.height];
  let spot = null;
  for (let y = 12; y < H - 30 && !spot; y += 2) for (let x = 12; x < W - 50 && !spot; x += 2) {
    if (h0.beads.every((b) => Math.hypot(b.x - x - 3, b.cy - y) > 11 && Math.hypot(b.x - x - 33, b.cy - y) > 11)) spot = [x, y];
  }
  if (!spot) return { spot, W, H, n: h0.beads.length };
  const [x, y] = spot, out = { spot, pairs: [] };
  for (const [i, r] of [4.2, 3.4].entries()) {
    const ids = h0.place([{ x: x + 30 * i, y, r }, { x: x + 30 * i + r + 1.2, y, r: 0.55 }]);
    out.pairs.push({ r, ids, before: Sky.state().humidity.beads.filter((b) => ids.includes(b.id)).map((b) => b.vol), frames: [] });
  }
  for (let n = 0; n < 40; n++) {
    Sky.advance(1);
    const h = Sky.state().humidity, byId = new Map(h.beads.map((b) => [b.id, b]));
    for (const p of out.pairs) {
      const b = byId.get(p.ids[0]);
      if (!b || !b.drawn) continue;
      p.frames.push({ gulp: b.gulp, w: b.drawn[2], h: b.drawn[3], rw: b.w, rh: b.bottom - Math.floor(b.y), bottom: b.bottom,
                      low: b.drawn[1] + b.drawn[3], vol: b.vol, r: b.r, stretch: b.stretch, t: n });
    }
  }
  return out;
});
// humidity off (the humidity below 90 %), every frame after till the fog has cleared and no bead is left (at most `frames`):
// the fog's mean thickness in the window's middle and at its edges as it clears (the first frame each is gone, and the edges' then);
// the slow beads (under 1 px/s) showing at the start; each slow one as it went (gone at a radius of 1 or more, not faded: popped;
// else: where it was, its distance from the middle, 0 there to 1 at the edges, and the frame); the frames taken and what was left;
// every 10th frame while it clears, the most pixels showing a trail's look on clear glass (looks: the condensed beads' trails none);
// and the glass as the fog had just cleared (cleared: its marks, the trails still listed) and at the end (end)
const humidityClears = (page, frames) => page.evaluate((frames) => new Promise((resolve) => {
  document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { wind: 1 } }));
  const last = new Map(), gone = [], start = Sky.state().humidity.beads.filter((b) => b.v < 1).length, [W, H] = Sky.state().size;
  let n = 0, popped = 0, middleGone = null, edgeGone = null, edgeThen = null, shrank = 0, looks = 0, cleared = null;
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
    if (n % 10 === 0 && fog.cover > 0) looks = Math.max(looks, h.marks().looks);
    if (!cleared && fog.cover === 0) cleared = h.marks();
    n++;
    if (n < frames && (fog.cover > 0 || beads.size)) return __next(step);
    resolve({ start, middleGone, edgeGone, edgeThen, gone, popped, shrank, frames: n, left: { cover: fog.cover, thickness: fog.thickness,
              beads: beads.size }, effects: Sky.state().effects, looks, cleared, end: h.marks() });
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
// frames, the weather's time `scale` times as fast; unless `phase` "heavy": from there, once one is on the glass (the drops landing
// at their own pace, the beads outside the shared budget: those there may all have run off or shrunk away already; the trails'
// clock held the while, so they're no older for it)) the rain light again: no new one sticking, and those there run off or
// shrunk away
const rainBeads = (page, light, heavy, after, scale, from = "light") => page.evaluate(([light, heavy, after, scale, from]) => new Promise((resolve) => {
  const out = { stuck: [], looks: [] }, seenIds = new Set(), last = new Map(), looked = new Map(), h0 = Sky.state().humidity;
  const watch = window.__beadWatch ? window.__beadWatch() : null, counts = { restarts: h0.restarts, stretches: h0.stretches };
  let n = 0, phase = from, popped = 0, start = Sky.state().frames;
  const end = () => {   // (the beads followed, and the biggest's restarts after a pause and their stretches the while)
    const h = Sky.state().humidity;
    return { watched: watch && watch.result(), restarts: h.restarts - counts.restarts, stretches: h.stretches - counts.stretches };
  };
  const step = () => {
    const state = Sky.state(), h = state.humidity, absorbed = new Set(h.absorbed), left = new Set(h.left), now = new Map(h.beads.map((b) => [b.id, b]));
    for (const [id, b] of last) if (!now.has(id) && !absorbed.has(id) && !left.has(id) && b.r >= 1 && b.alpha > 0.15) popped++;
    if (watch) watch.step(state);
    last.clear();
    for (const b of h.beads) {
      last.set(b.id, b);
      // (its trail's looks: once it has run 20 px, and again each 40 px further on (its latest rows, up to 80): all its trail, as far
      // as it runs, looked at)
      if (b.kind === "rain" && b.y - b.y0 > 20 && b.y - (looked.has(b.id) ? looked.get(b.id) : -Infinity) >= 40 && window.__trailLook) {
        looked.set(b.id, b.y);
        out.looks.push(...window.__trailLook(h, b));
      }
      if (b.kind === "rain" && !seenIds.has(b.id)) {
        seenIds.add(b.id);
        const s = h.splashes.find((s) => s.id === b.id);
        out.stuck.push({ r: s ? s.r : 0, alpha: b.alpha, dots: s ? s.dots : 0, stay: s ? s.stay : 0, meant: s ? s.meant : 0, cut: s ? s.cut : 0,
                         off: s ? s.off : 0, room: s ? s.room : 0, squash: s ? s.squash : 0, thrown: s ? s.thrown : [], phase });   // (r: as it landed)
      }
    }
    for (const s of h.splashes) if (!seenIds.has(s.id) && absorbed.has(s.id)) {   // (one landing on a bead there, merged at once: never
      seenIds.add(s.id);                                                          // shown on its own)
      out.stuck.push({ r: s.r, alpha: null, dots: s.dots, stay: s.stay, meant: s.meant, cut: s.cut, off: s.off, room: s.room, squash: s.squash,
                       thrown: s.thrown, phase, merged: true });
    }
    n = state.frames - start;
    if (phase === "light" && n >= light) {
      out.light = { landed: h.landed, levels: state.levels.rain };
      document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 1 } }));
      phase = "heavy"; start = state.frames;
    } else if (phase === "heavy" && n >= heavy && from === "heavy" && !h.beads.some((b) => b.kind === "rain") && n < 900) {
      h.hasten(0);   // (waiting for one to land: the trails' clock held)
    } else if (phase === "heavy" && n >= heavy) {
      h.hasten(1);
      out.heavy = { landed: h.landed, rainMost: h.rainMost, glass: h.glass, trails: h.trails, beads: h.beads.length, popped };
      if (!after) return resolve({ ...out, ...end() });
      document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { rain: 0.35 } }));
      Sky.timeScale(scale);
      out.eased = { landed: h.landed, beads: h.beads.length, ranOff: h.ranOff, shrunk: h.shrunk, faded: h.faded };
      phase = "after"; start = state.frames;
    } else if (phase === "after" && (n >= after || !h.beads.length)) {
      Sky.timeScale(1);
      out.after = { landed: h.landed, beads: h.beads.length, frames: n, ranOff: h.ranOff, shrunk: h.shrunk, faded: h.faded, popped };
      return resolve({ ...out, ...end() });
    }
    __next(step);
  };
  __next(step);
}), [light, heavy, after, scale, from]);
// Heavy rain's drops landing, each followed frame by frame (the weather's time a 30th of a second a frame) from the frame it landed
// in till 0.35 s on: `landings` of them (up to `most` frames): each {id, frames: [{impact: s since it landed, squash, w, h: as drawn,
// rw, rh: its round sprite's, r, vol, stretch: elongated as it sets off (see beadWatch)}]} (till it merges or goes)
const landingsWatched = (page, landings, most) => page.evaluate(([landings, most]) => new Promise((resolve) => {
  const followed = new Map(), done = [];
  let n = 0;
  Sky.timeScale(1 / 3);
  const step = () => {
    const h = Sky.state().humidity;
    for (const b of h.beads) if (b.kind === "rain" && b.impact !== null && b.impact <= 0.35 && b.drawn) {
      if (!followed.has(b.id)) { if (b.impact > 0 || followed.size + done.length >= landings) continue; followed.set(b.id, { id: b.id, frames: [] }); }
      followed.get(b.id).frames.push({ impact: b.impact, squash: b.squash, w: b.drawn[2], h: b.drawn[3], rw: b.w, rh: b.bottom - Math.floor(b.y), r: b.r, vol: b.vol,
                                     stretch: b.stretch });
    }
    for (const [id, f] of followed) if (f.frames.at(-1).impact >= 0.33 || !h.beads.some((b) => b.id === id)) { done.push(f); followed.delete(id); }
    if ((done.length < landings || followed.size) && ++n < most) return __next(step);
    Sky.timeScale(1);
    resolve(done);
  };
  __next(step);
}), [landings, most]);
// "stop" (an effect on: the link shows; it ends the test links' effects; the link gone), then `detail` on (as a test link
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
  await reads({ "UV index": 11, Wind: 60, Temperature: -3, rain_rate: 2, Humidity: 97, "AQI": 160, gust: 130 });
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

// MCP_DASHBOARD_ANIMATIONS on, low and off (the page as ecobot-mcp serves it with each: data-animations on its <html>), and a browser
// asking for reduced motion (served with on): each its own page, side by side. In each, what moves: a gauge's new reading (its reading's
// text as it goes: every value it shows, in order: swept, or straight there), the weather called for (every effect, a gust over 99 km/h
// and the test link's: drawn, its loop, or nothing at all: no canvas shown, no frame, no ink, nothing blown across), "stop" and the test
// links (shown or not), a zoom and the gallery's step (animated, or at once), and a tile hidden (the others gliding to their places, or
// at once)
const MOTIONS = { on: { animations: "on" }, low: { animations: "low" }, off: { animations: "off" },
                  reduced: { animations: "on", context: { reducedMotion: "reduce" } } };
async function motionSeen(browser, url, options) {
  const { context, page, errors, reads } = await weatherPage(browser, url, options);
  await calm(page, 5000);   // (the gauges' rise, where they rise)
  const out = {};
  // a new reading: Humidity's reading's text, every value it shows from now (a MutationObserver: each change, however quick)
  await page.evaluate(() => {
    const t = [...document.querySelectorAll(".tile")].find((e) => e.querySelector(".title").textContent === "Humidity");
    const reading = () => t.querySelector("svg text.reading").textContent;
    window.__shown = [];
    new MutationObserver(() => { const r = reading(); if (window.__shown.at(-1) !== r) window.__shown.push(r); })
      .observe(t, { subtree: true, attributes: true, childList: true, characterData: true });
  });
  await reads({ "UV index": 11, Wind: 60, Temperature: -3, rain_rate: 2, Humidity: 97, "AQI": 160, gust: 130 });
  await page.waitForTimeout(1500);   // (the sweep over: MOVE_MS; and time for the weather's frames, were any drawn)
  out.gauge = await page.evaluate(() => window.__shown);
  await page.evaluate(() => {   // (the test link's gust, and the page's own event for it)
    document.dispatchEvent(new Event("ecobot-gust"));
    const b = document.querySelector("#fxtest button[data-gust]");
    if (b && b.checkVisibility()) b.click();
  });
  out.weather = { ...await weatherSeen(page), frames: await page.evaluate(() => Sky.state().frames), ink: await canvasInk(page),
                  gust: await page.evaluate(() => Sky.state().gust.sent),
                  ...await page.evaluate(() => ({ sky: getComputedStyle(document.getElementById("sky")).display,
                                                  corner: getComputedStyle(document.getElementById("corner")).display,
                                                  shownLink: !!document.querySelector("#motion .animations") && document.querySelector("#motion .animations").checkVisibility(),
                                                  row: document.getElementById("fxtest").checkVisibility() })) };
  // zoomed (a click on Humidity), a step to the next (→), back (Esc): the zoom's animations running just after, #grid's transition
  const zoomAnimations = () => page.evaluate(() => document.getAnimations().filter((a) => a.id === "zoom" && a.playState === "running").length);
  await page.mouse.click(...await at(page, "Humidity", 0.5, 0.4));
  out.zoomed = { zoomed: await page.evaluate(() => document.body.classList.contains("zoomed")), animations: await zoomAnimations() };
  await calm(page);
  await page.keyboard.press("ArrowRight");
  out.paged = { transition: await page.evaluate(() => getComputedStyle(document.getElementById("grid")).transitionDuration) };
  await calm(page);
  await page.keyboard.press("Escape");
  out.back = { zoomed: await page.evaluate(() => document.body.classList.contains("zoomed")), animations: await zoomAnimations() };
  await calm(page);
  // Rain hidden (its ×): the next frame, the tiles still on their way to their places (a transform), and the transform transitions running
  out.hidden = await page.evaluate(() => new Promise((resolve) => {
    const t = [...document.querySelectorAll(".tile")].find((e) => e.querySelector(".title").textContent === "Rain");
    t.querySelector(".close").click();
    requestAnimationFrame(() => resolve({
      moved: [...document.querySelectorAll(".tile")].filter((b) => getComputedStyle(b).transform !== "none").length,
      gliding: document.getAnimations().filter((a) => a.transitionProperty === "transform" && a.playState === "running").length,
      tiles: document.querySelectorAll(".tile").length }));
  }));
  out.errors = errors;
  await context.close();
  return out;
}

// The weather in parts, each its own page (and process: the tests run them side by side): "marks" (nothing called for, each effect at
// its mark and just short of it, haze's bands, rain's strength, the ice's depth), "several" (several at once, the canvas, the tiles see-
// through, their look, zoomed), "stop" ("stop", the readings, a reload, a test link after it, the reset), "links" (the temporary test links;
// and beside them, MCP_DASHBOARD_ANIMATIONS off: nothing drawn), "motion" (MCP_DASHBOARD_ANIMATIONS on, low and off, and reduced
// motion: what moves in each),
// "haze" (Hazardous haze alone), "trails" (heavy humidity alone: its cost as the trails build up), "frost" (the ice grown all the way in:
// its cost), "budget" (heavy rain, wind and haze: the shared particle budget), "opened" (?fx= when the page opens, uv
// and the wind alone, heavy rain alone and in the wind (on a phone too), uv's beams over 40 s, humidity's beads running down the
// glass alone and in the wind, a phone's strip), "flood" (a flash flood: its mark, its rise, its surface and waves,
// what ends at it, the test link's rain3, its leaves and its frog, its cost), "gust" (something big blown across in a gust over 99 km/h),
// "tilt" (a phone's gravity: the humidity's beads running downhill, by synthetic devicemotion at screen angles)
const WEATHER = {
  async marks(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url);
    await calm(page, 5000);   // (the gauges' rise)
    out.nothing = { ...await weatherSeen(page), frames: await framesIn(page, 800), ink: await canvasInk(page) };
    // each mark: just short of it, then at it
    const marks = { rain: [{ rain_rate: 0 }, { rain_rate: 0.1 }], haze: [{ "AQI": 50 }, { "AQI": 51 }],
                    uv: [{ "UV index": 6.2 }, { "UV index": 6.3 }], humidity: [{ Humidity: 89.9 }, { Humidity: 90 }],
                    wind: [{ Wind: 15 }, { Wind: 15.1 }], heat: [{ Temperature: 34.9 }, { Temperature: 35 }],
                    ice: [{ Temperature: 0.1 }, { Temperature: -0.1 }], iceAtZero: [{ Temperature: 0 }, { Temperature: -0.1 }],
                    flood: [{ flood: 0.049 }, { flood: 0.05 }] };
    out.marks = {};
    for (const [name, [below, at]] of Object.entries(marks)) {
      await reads(below);
      const short = await weatherSeen(page);
      await reads(at);
      out.marks[name] = { below: short, at: await weatherSeen(page) };
    }
    out.hazeBands = [];
    for (const aqi of [101, 151, 201, 301, 500]) { await reads({ "AQI": aqi }); out.hazeBands.push((await weatherSeen(page)).levels.haze); }
    out.rainStrength = [];
    for (const rate of [0.5, 8, 20]) { await reads({ rain_rate: rate }); out.rainStrength.push((await weatherSeen(page)).levels.rain); }
    out.humidityStrength = [];
    for (const rh of [91, 95, 100]) { await reads({ Humidity: rh }); out.humidityStrength.push((await weatherSeen(page)).levels.humidity); }
    out.heatStrength = [];
    for (const t of [35, 39, 43, 48]) { await reads({ Temperature: t }); out.heatStrength.push((await weatherSeen(page)).levels.heat); }
    out.uvStrength = [];
    for (const index of [7, 8.5, 11, 12]) { await reads({ "UV index": index }); out.uvStrength.push((await weatherSeen(page)).levels.uv); }
    out.iceDepth = [];
    for (const t of [-0.5, -5]) { await reads({ Temperature: t }); out.iceDepth.push((await weatherSeen(page)).levels.ice); }
    // in °F, mph, inHg and in (the switches): the marks the same, of the readings in °C, km/h and mm/h (the rain above 0, heavy from
    // 8 mm/h, the flood above 30 mm/h); the thermometer's scale widened as in °C
    for (const unit of ["°F", "mph", "inHg", "in"]) await page.click(`#units button[data-unit='${unit}']`);
    out.unitsRain = [];
    for (const rate of [0.5, 8, 30, 31]) {
      await reads({ rain_rate: rate });   // (the rate alone no longer floods: the bucket's depth does)
      const { levels } = await weatherSeen(page);
      out.unitsRain.push([levels.rain, levels.flood]);
    }
    out.unitsMarks = {};
    for (const name of ["heat", "wind", "ice", "rain"]) {
      const [below, at] = marks[name];
      await reads(below);
      const short = await weatherSeen(page);
      await reads(at);
      out.unitsMarks[name] = { below: short, at: await weatherSeen(page) };
    }
    const temperature = () => page.evaluate(() => {
      const t = [...document.querySelectorAll("#grid .tile")].find((e) => e.querySelector(".title").textContent === "Temperature");
      return [t.getAttribute("aria-label"), [...t.querySelectorAll("svg text:not(.reading)")].map((e) => e.textContent)];
    });
    const widened = (low, high) => ({ low, high, ticks: Array.from({ length: (high - low) / 10 + 1 }, (_, i) => high - 10 * i),
                                      colours: Array((high - low) / 10).fill("#888888") });
    await reads({ Temperature: -5, scales: { Temperature: widened(-10, 40) } });
    out.widened = await temperature();
    await reads({ Temperature: 43, scales: { Temperature: widened(0, 50) } });
    out.widenedHot = await temperature();
    out.errors = errors;
    await context.close();
  },
  async several(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url);
    await calm(page, 5000);   // (the gauges' rise)
    // several at once: one canvas, one loop (at most 30 frames a second)
    await reads({ "UV index": 11, Wind: 60, Temperature: -3, rain_rate: 2, Humidity: 97, "AQI": 160 });
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
    await reads({ "AQI": 500 });
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
    // "stop" (a test link's effect on too): nothing drawn, no loop, the test link's effect ended, the frames stay, the link
    // gone (no "start animation"), nothing saved
    await page.locator("#fxtest button[data-fx='wind']").click();
    out.simulating = { ...await weatherSeen(page), ...await page.evaluate(() => { const a = document.querySelector("#motion .animations"); return { title: a.title, label: a.getAttribute("aria-label") }; }) };
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
    // phones (360x740, 390x844), every effect on (their links' longest): the row without "test:", all of it and "stop" in
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
    // the temporary test links: each turns its effect and frame on (the gauges keep their readings), "stop" ends them (and
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
    const grown = await page.evaluate(() => {   // (as far as it had grown as it's turned off (one step: none growing between, the
      const reach = Sky.state().iceReach;       // machine however slow)); off: it melts back, the loop running on until it's gone
      [...document.querySelectorAll("#fxtest button")].find((b) => /^ice/.test(b.textContent)).click();
      return reach;
    });
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
  async motion(browser, url, out) {
    const seen = await Promise.all(Object.values(MOTIONS).map((options) => motionSeen(browser, url, options)));
    Object.keys(MOTIONS).forEach((k, i) => { out[k] = seen[i]; });
    out.errors = seen.flatMap((m) => m.errors);
  },
  // The haze alone (each band, the veil's look and drift; Hazardous haze: the wind's way)
  async haze(browser, url, out) {
    const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=haze5", driven: true });
    await drive(page, 56);   // (faded in: 1.5 s)
    out.haze = await hazeSeen(page);
    // the window resized: baked again at its size (once, after the debounce), the pan unchanged
    const baked = await page.evaluate(() => Sky.state().haze.renders);
    const size = page.viewportSize();
    await page.setViewportSize({ width: size.width + 120, height: size.height });
    await page.waitForTimeout(400);   // (the page's own debounce: 150 ms)
    await driveUntil(page, (was) => { const h = Sky.state().haze; return h.level === 3 && !h.pending && !h.settling && h.renders > was; }, baked);
    out.resized = await page.evaluate((baked) => ({ baked: Sky.state().haze.renders - baked, width: Sky.state().haze.width, art: Sky.state().size[0] }), baked);
    out.errors = errors;
    await context.close();
  },
  // Heavy humidity alone: the frame cost early, after minutes of the weather's time (the trails and beads built up), and with its
  // time 20 times as fast (the sweep of the trails capped)
  async trails(browser, url, out) {
    const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=humidity2", driven: true });
    await drive(page, 90);   // (3 s of the weather's time)
    out.cost = await frameCost(page, 60);
    await page.evaluate(() => Sky.timeScale(100));
    await driveUntil(page, () => { const s = Sky.state(); return s.clock > 220 && s.humidity.trails > 16000; });
    await page.evaluate(() => Sky.timeScale(1));
    await drive(page, 45);
    out.late = { cost: await frameCost(page, 60), ...await page.evaluate(() => ({ clock: Sky.state().clock, trails: Sky.state().humidity.trails,
                                                                                  beads: Sky.state().humidity.beads.length })) };
    await page.evaluate(() => Sky.timeScale(20));
    out.fast = { cost: await frameCost(page, 60), ...await page.evaluate(() => { const h = Sky.state().humidity; return { trails: h.trails, swept: h.swept, most: h.sweepMost, beads: h.beads.length }; }) };
    await page.evaluate(() => Sky.timeScale(1));
    out.errors = errors;
    await context.close();
  },
  // The ice alone, grown all the way in: its frame cost and its frost across the glass
  async frost(browser, url, out) {
    const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=ice", driven: true });
    await page.evaluate(() => Sky.timeScale(100));
    await driveUntil(page, () => { const f = Sky.frost(true); return f && f.target > 0 && f.extent === f.target; });
    await page.evaluate(() => Sky.timeScale(1));
    await drive(page, 45);
    out.frosted = { cost: await frameCost(page, 60), crystals: await page.evaluate(() => Sky.frost().crystals) };
    out.errors = errors;
    await context.close();
  },
  // Heavy rain, wind and haze together (what can occur together in practice): the shared particle budget
  async budget(browser, url, out) {
    const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=rain2,wind,haze3", driven: true });
    await drive(page, 90);
    out.cost = await frameCost(page, 60);
    out.particles = await page.evaluate(() => ({ ...Sky.state().particles, budget: Sky.state().budget }));
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
  // The weather past the window's top and bottom (a phone's address bar sliding away shows more than the layout viewport was): the
  // canvas and its two margins reach at least OVER (100 px) past each edge; the viewport made taller (the bar hidden), with the
  // window's resize event or without, still covered before any relayout; the weather's own world (the flood's level, the gust's
  // height) is still the window's size, not the margins' too; the margins carry on the first and last rows, mirrored
  async over(browser, url, out) {
    const { context, page, errors } = await weatherPage(browser, url, { query: "?fx=haze3,humidity2,ice,heat,uv", driven: true,
                                                                       context: { viewport: { width: 390, height: 700 }, hasTouch: true, isMobile: true } });
    await drive(page, 40);
    const look = () => page.evaluate(() => {
      const rect = (e) => { const r = e.getBoundingClientRect(); return [r.top, r.bottom, r.left, r.right]; };
      const [c, a, b] = document.querySelectorAll("#sky canvas"), s = Sky.state(), h = document.documentElement.clientHeight;
      const rows = (cv, ys) => { const g = cv.getContext("2d"); return ys.map((y) => [...g.getImageData(0, y, cv.width, 1).data]); };
      const same = (x, y) => x.length === y.length && x.every((v, i) => v === y[i]);
      const ink = (cv) => { const d = cv.getContext("2d").getImageData(0, 0, cv.width, cv.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; };
      return { inner: [innerWidth, innerHeight], client: [document.documentElement.clientWidth, h], canvas: rect(c), above: rect(a), below: rect(b),
               sky: rect(document.getElementById("sky")), size: s.size, canvases: document.querySelectorAll("canvas").length,
               mirrored: { top: same(rows(a, [a.height - 1])[0], rows(c, [0])[0]), bottom: same(rows(b, [0])[0], rows(c, [c.height - 1])[0]),
                           top2: same(rows(a, [a.height - 2])[0], rows(c, [1])[0]) },
               ink: [ink(a), ink(c), ink(b)], flood: s.flood.height, gustFrom: s.gust ? null : null,
               bg: [getComputedStyle(document.documentElement).backgroundColor, getComputedStyle(document.body).backgroundColor],
               theme: document.querySelector("meta[name=theme-color]").content };
    });
    out.first = await look();
    await page.setViewportSize({ width: 390, height: 790 });   // (the bar hidden: 90 px more, no event, no relayout yet)
    await page.evaluate(() => new Promise((ok) => requestAnimationFrame(() => requestAnimationFrame(ok))));
    out.grown = await look();
    await page.evaluate(() => window.dispatchEvent(new Event("resize")));
    await page.waitForTimeout(500);
    await drive(page, 5);
    out.relaid = await look();
    out.errors = errors;
    await context.close();
  },
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
                    over: 0, under: 0, strokes: [], joined: 0, joins: [], frogs: 0, fullAt: null, fewest: Infinity, flown: [],
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
            if (f.frog.stroke !== null && !r.strokes.includes(f.frog.stroke)) r.strokes.push(f.frog.stroke);
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
          const used = Object.entries(s.particles).filter(([k]) => !(s.own || []).includes(k)).reduce((a, [, b]) => a + b, 0);   // (humidity's beads their own)
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
    // the server's bucket: 0.04 of full (a heavy rain rate with it: no flood), then full (the weather's time four times as fast)
    const { context, page, errors, reads } = await weatherPage(browser, url, { driven: true });
    await reads({ rain_rate: 60, flood: 0.04 });
    out.at30 = await weatherSeen(page);
    await page.evaluate(() => Sky.timeScale(4));
    await reads({ rain_rate: 60, flood: 1 });
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
    await reads({ rain_rate: 23, flood: 0.04 });   // (the bucket under 0.05: off, the rain on)
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
      // (on again: nothing from before, its canvas clear, no sparkle; then grown half a minute, and "stop", "start
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
    // the sun's limb (the heat) with the UV's beams rooted on it, at each lean; the sun's lean through the day and by its real place (small: cheap)
    const hot = (async () => {
      const p = await weatherPage(browser, url, { query: "?fx=heat,uv", driven: true, context: { viewport: { width: 640, height: 480 } } });
      const seen = await sunLook(p.page);
      seen.cost = await sunCost(browser, url);
      seen.sun = await sunSeen(p.page); seen.place = await placeSeen(p.page, require("./sun_days.json").days);
      seen.errors = p.errors;
      await p.context.close();
      return seen;
    })();
    // humidity's beads condensing on the glass and running down it over 3.6 minutes of the page's time (its weather's time 6 times as
    // fast), alone and with the wind, beside the rest (small: cheap) (heavy: 100 %); and light (91 %), watched for 2 minutes
    // light (91 %) and heavy (100 %) humidity's still beads after the same time (1500 frames, then the mean of ten counts 20 frames
    // apart): heavy's more (a grown bead costs a whole particle of the budget, a tiny one a quarter; heavy's budget the larger)
    const counting = Promise.all(["humidity", "humidity2"].map(async (name) => {
      const p = await weatherPage(browser, url, { query: `?fx=${name}`, pace: 10, driven: true, context: { viewport: { width: 640, height: 480 } } });
      await p.page.evaluate(() => Sky.timeScale(6));
      await drive(p.page, 1500);
      let sum = 0;
      for (let k = 0; k < 10; k++) { await drive(p.page, 20); sum += await p.page.evaluate(() => Sky.state().humidity.beads.filter((b) => b.v < 1).length); }
      await p.context.close();
      return sum / 10;
    }));
    const beading = Promise.all(["humidity2", "humidity2,wind", "humidity"].map(async (name) => {
      // (the wind's page another glass than humidity's alone, its own seed: the wind blows no water on the glass, so seeded alike it
      // would be the very same beads, nothing more seen)
      const p = await weatherPage(browser, url, { query: `?fx=${name}`, pace: 10, driven: true, seed: name.includes("wind") ? 1 : 0,
                                                  context: { viewport: { width: 640, height: 480 } } });
      await p.page.evaluate(() => Sky.timeScale(6));
      await lookable(p.page);
      if (name !== "humidity") await drive(p.page, 150);   // (beaded up a while first: beads run only from about large)
      const seen = await humiditySeen(p.page, name === "humidity" ? 200 : 540);   // (beads run only from about large: watched longer)
      if (name === "humidity2") seen.grid = (await gridPeak(p.page)).ratio;   // (its fog's dithering)
      if (name === "humidity") {   // (then heavy rain too, its weather's time as fast: its splashes' droplets left on the fog)
        await p.page.evaluate(() => document.dispatchEvent(new CustomEvent("ecobot-fx", { detail: { humidity: 1, rain: 1 } })));
        seen.splashed = await beadsWatched(p.page, 300);
      }
      await p.page.evaluate((k) => Sky.timeScale(k), name === "humidity2,wind" ? 1 : 2);
      if (name === "humidity2") seen.snapped = await snapped(p.page);
      if (name === "humidity2") {   // (a big bead swallowing a tiny one: its time as it is, a frame a 30th of a second)
        await p.page.evaluate(() => Sky.timeScale(1));
        seen.gulped = await gulped(p.page);
        await p.page.evaluate(() => Sky.timeScale(2));
      }
      if (name === "humidity2,wind") {   // (then its trails' time 60 times as fast, through the fog; then humidity off: the fog clearing)
        // (beads running only from about large, a few at a time: the fresh trails then, a few, and again after the beads have run on
        // a while (their weather's time 6 times as fast), till a dozen or so)
        seen.refog = [];
        for (let k = 0; k < 4 && seen.refog.length < 12; k++) {
          if (k) { await p.page.evaluate(() => Sky.timeScale(6)); await drive(p.page, 60); await p.page.evaluate(() => Sky.timeScale(1)); }
          seen.refog.push(...await trailsAge(p.page, 60, true, 60));
        }
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
        // (then its beads running and leaving trails (the weather's time 30 times as fast), and "stop", then the effect on again
        // and humidity on again: the glass fresh too)
        await p.page.evaluate(() => Sky.timeScale(30));
        await driveUntil(p.page, () => Sky.state().humidity.trails > 30);
        seen.marked = await p.page.evaluate(() => { Sky.timeScale(1); return Sky.state().humidity.marks(); });
        seen.restarted = await restarted(p.page, { humidity: 1 });
        // (and the physics the beads run by: a bead's mean speed on each glass, by size; its size after running 20 px in 5 s there)
        seen.physics = await p.page.evaluate(() => {
          const { speed, after, gone, run } = Sky.state().humidity.physics, sizes = [0.6, 1, 1.1, 1.3, 1.4, 2, 2.8, 3, 3.4, 4, 6, 10];
          return { sizes, gone, run, speed: Object.fromEntries(["dry", "trail", "fog"].map((g) => [g, sizes.map((r) => speed(r, g))])),
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
      // (how far, px, each rain bead (of the radii they landed with) would run down dry glass before it is too small to run: the physics' own
      // steps of a px, a rain bead's)
      seen.runs = await p.page.evaluate((radii) => {
        const { after, run } = Sky.state().humidity.physics;
        return radii.map((r) => { let px = 0; while (r >= run && px < 5000) { r = after(r, "dry", 1, 0, 0.5, "rain"); px++; } return px; });
      }, seen.stuck.map((s) => s.r));
      await p.page.evaluate(() => Sky.timeScale(1));
      // (heavy still, its weather's own time (a frame 0.1 s: a stretch over a few), 30 s: the biggest setting off again after pauses)
      seen.stretching = await beadsWatched(p.page, 300);
      // (a few drops landing, frame by frame: squashed and back to round)
      seen.landings = await landingsWatched(p.page, 4, 900);
      let looks;
      // (the rain eased: watched till the last bead has gone, up to 1100 frames (11 minutes of its weather's time): one shrunk on
      // dry glass under the run size and sitting on a wet trail keeps its size till that trail dries, 3 to 10 minutes)
      ({ eased: seen.eased, after: seen.after, looks, watched: seen.afterWatched } = await rainBeads(p.page, 0, 0, 1100, 6, "heavy"));
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
      // ... and the same with "stop" between, then the effect on again
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
      // (the rain alone: slanted by a breeze's reading, 6 km/h (about 18 degrees), under the 15 the wind effect comes on at; with no wind at all it
      // falls straight down, which the slant tests check)
      if (i === 0) await p.reads({ "Wind": 6 });
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
    // without the temporary test links (the page as it will be), at 1024x768 and on a phone with a tile hidden: "stop" at
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
    // the bottom right, the time last; the test row and "stop" not over it (beside the rest)
    const phoned = (async () => {
      const phone = await weatherPage(browser, url, { query: "?fx=wind", driven: true, context: { viewport: { width: 360, height: 740 }, hasTouch: true, isMobile: true },
        init: { fn: (key) => localStorage.setItem(key, JSON.stringify({ order: [], hidden: ["Rain"] })), arg: KEY } });
      out.phoneWind = await windSeen(phone.page, 20000);
      out.phoneCorner = await cornerSeen(phone.page);
      out.phone = await phone.page.evaluate(() => {
        const a = document.getElementById("fxtest").getBoundingClientRect(), b = document.getElementById("status").getBoundingClientRect();
        const r = document.querySelector("#reset").getBoundingClientRect(), u = document.getElementById("units").getBoundingClientRect();
        return { strip: document.getElementById("status").textContent, resetRight: r.right <= innerWidth && r.left >= 0 && r.width > 0 && (r.bottom <= u.top || r.right <= u.left),
                 resetAfterRow: r.bottom <= a.top || r.left >= a.right, right: Math.abs(b.right - innerWidth) < 1 && Math.abs(b.bottom - innerHeight) < 1,
                 apart: a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top, onScreen: a.left >= 0 && a.bottom <= innerHeight };
      });
      await phone.context.close();
    })();
    await Promise.all([params, alone, phoned]);
    [out.uv, out.frost] = await swaying;
    [out.humidity.alone, out.humidity.windy, out.humidityLight] = await beading;
    out.stillBeads = await counting;   // ([light, heavy])
    out.rainBeads = await sticking;
    out.heat = await hot;
    [out.rain.alone, out.rain.windy, out.rain.phone] = await raining;
    [out.bare, out.barePhone] = await bare;
    out.errors = [];
  },
  // A gust over 99 km/h: the live gust at 99 (nothing), just over (one sent), the same reading fetched again (no more), a new one over
  // (another), one under (none); then a cow, a barn and a chicken sent at once, watched every frame as they cross (the weather's time
  // 5 times as fast): where each is, its angle, the chicken's frames, the canvas's pixels while they cross (alone: theirs and their
  // bits'), the loop ending once they're gone; the test link (five clicks, its URL, never pressed), "stop" (the wind's
// link on too, so it shows: they're gone at once),
  // ?fx=gust as the page opens (ignored: nothing sent, the word dropped from the URL; and beside "wind"), and on a phone the row
  // (every link, "gust" too, and "stop" in sight unscrolled). A frame
  // sequence of the three crossing kept as screenshots (SHOTS, when given)
  // push: a strong wind pushing the humidity's beads' run downwind (?windspeed=, the test links' own effective speed): the push ratio at
  // 0, 15, 30, 45 and 60 km/h, both sides; the mean way the beads run at 45 km/h from the west and the east, at 10 (vertical); a flat phone
  // (pooled: no push), a phone tilted 30 degrees left in a rightward push (the vectors add); the trails laid along the slanted run (rows,
  // no gaps), and a screenshot of the beads in a 40 km/h west wind (SHOTS, when given)
  async push(browser, url, out) {
    const shake = (page, x, y, z) => page.evaluate(([x, y, z]) => window.dispatchEvent(new DeviceMotionEvent("devicemotion", { accelerationIncludingGravity: { x, y, z } })), [x, y, z]);
    const page = async (speed, way, extra = {}) => weatherPage(browser, url, { query: `?fx=humidity2,wind-${way}&windspeed=${speed}`, pace: 10, driven: true, context: { viewport: { width: 400, height: 300 } }, ...extra });
    // the beads' run: the sum of each running bead's move (dx, dy), the way it was (degrees off straight down, positive rightward), and what the state said
    const run = (p, frames = 400) => p.page.evaluate((frames) => {
      Sky.timeScale(30);
      const first = new Map(), last = new Map(), steps = new Map(), angles = [];
      let n = 0;
      while (n++ < frames && Sky.advance(1)) {
        for (const b of Sky.state().humidity.beads) if (b.kind === "fog" && b.v > 0) {
          if (!first.has(b.id)) first.set(b.id, [b.x, b.cy]);
          last.set(b.id, [b.x, b.cy]);
          steps.set(b.id, (steps.get(b.id) || 0) + 1);
          angles.push(b.angle);
        }
      }
      const moves = [...last].filter(([id]) => steps.get(id) >= 8).map(([id, [x, y]]) => [x - first.get(id)[0], y - first.get(id)[1]]);
      const dx = moves.reduce((t, m) => t + m[0], 0), dy = moves.reduce((t, m) => t + m[1], 0);
      const h = Sky.state().humidity;
      return { running: moves.length, dx: dx / Math.max(moves.length, 1), dy: dy / Math.max(moves.length, 1), degrees: Math.atan2(dx, dy) * 180 / Math.PI,
               ranOff: h.ranOff, push: h.windPush, tilt: Sky.state().tilt, angles: [Math.min(...angles), Math.max(...angles)], nAngles: angles.length };
    }, frames);
    out.errors = [];
    out.ratio = {};
    for (const way of ["west", "east"]) for (const speed of [0, 15, 30, 45, 60]) {
      const p = await page(speed, way);
      await p.page.evaluate(() => Sky.advance(5));
      const w = await p.page.evaluate(() => Sky.state().humidity.windPush);
      out.ratio[`${way}/${speed}`] = { p: w.p, side: w.side, factor: w.factor, push: w.push, angle: w.angle, speed: w.speed };
      out.errors.push(...p.errors);
      await p.context.close();
    }
    out.runs = {};
    for (const [name, speed, way] of [["west45", 45, "west"], ["east45", 45, "east"], ["west10", 10, "west"], ["west0", 0, "west"], ["west25", 25, "west"]]) {
      const p = await page(speed, way);
      out.runs[name] = await run(p);
      out.errors.push(...p.errors);
      await p.context.close();
    }
    // the speeds' own, the same beads as vertical ones (sized(), PACE unchanged): the mean path length per running bead
    // flat: pooled, nothing runs, no push
    {
      const p = await page(45, "west");
      await shake(p.page, 0, 0, 9.8);
      out.flat = await run(p);
      out.errors.push(...p.errors);
      await p.context.close();
    }
    // tilted 30 degrees to the left (the pull toward the left edge: the reading is minus the pull) in a rightward push: the unit pull
    // (-0.5, 0.866) plus (0.6, 0): (0.1, 0.866), 6.6 degrees; in a leftward one 30 + 31 = 55.8 degrees leftward... both, and the tilt alone
    out.tilted = {};
    for (const [name, speed, way] of [["alone", 0, "west"], ["against", 45, "west"], ["with", 45, "east"]]) {
      const p = await page(speed, way);
      await shake(p.page, 4.9, 8.487, 0);
      out.tilted[name] = await run(p);
      out.errors.push(...p.errors);
      await p.context.close();
    }
    // the trails: each running bead's rows (the latest 80 laid) one after another: none missing, their spans touching row to row (now and
    // then a bead's one hop of a few px sideways onto a neighbour's trail, the lock-on's, aside); at 45 km/h, and at 0 for comparison
    for (const [key, speed] of [["trails", 45], ["trailsUpright", 0]])
    {
      const p = await page(speed, "west");
      out[key] = await p.page.evaluate(() => {
        Sky.timeScale(30);
        const h = () => Sky.state().humidity;
        const seen = new Map();   // (each bead's trail's longest look, while it still runs)
        for (let n = 0; n < 600 && Sky.advance(1); n++) if (n % 3 === 0) for (const b of h().beads) if (b.kind === "fog" && b.v > 0) {
          const rows = h().trailOf(b.id);
          if (rows.length >= (seen.get(b.id) || []).length) seen.set(b.id, rows);
        }
        const out = [];
        for (const [id, rows] of seen) {
          if (rows.length < 40) continue;
          let gaps = 0, jumps = 0, wide = 0;
          for (let i = 1; i < rows.length; i++) {
            const a = rows[i - 1], b = rows[i];
            if (Math.abs(b.y - a.y) > 1) jumps++;
            if (b.left > a.left + a.w || a.left > b.left + b.w) gaps++;   // (a row's span not touching the last's)
            wide = Math.max(wide, Math.abs(b.left - a.left));
          }
          // the rows' own run: x against y over them, degrees off straight down
          const first = rows[0], last = rows[rows.length - 1];
          out.push({ id, rows: rows.length, gaps, jumps, wide, ys: [first.y, last.y], degrees: Math.atan2((last.left + last.w / 2) - (first.left + first.w / 2), last.y - first.y) * 180 / Math.PI });
        }
        return out;
      });
      out.errors.push(...p.errors);
      await p.context.close();
    }
    // the screenshot: 1024 x 768, humidity heavy with the push (40 km/h from the west), the glass wiped by a few trails
    if (process.env.SHOTS) {
      const p = await weatherPage(browser, url, { query: "?fx=humidity2,wind-west&windspeed=40", pace: 10, driven: true });
      await p.page.evaluate(() => { Sky.timeScale(30); for (let n = 0; n < 1500 && Sky.advance(1); n++); });
      await p.page.waitForTimeout(300);
      await p.page.screenshot({ path: process.env.SHOTS });
      out.shot = process.env.SHOTS;
      out.errors.push(...p.errors);
      await p.context.close();
    }
  },
  // tilt: a phone's gravity turning the humidity's beads' way (the page's own devicemotion listener, Sky.tilt): synthetic readings at
  // screen angles (the spec's convention, as Android's: at rest upright, y +9.8), where the beads run (the mean of their moves while
  // running: dx, dy; which edge, how many ran off), a phone flat (pooling: nothing runs, the beads merge into ever larger blobs) and the speed at angles from flat
  // through upright to face down, no reading (as ever), the animations low and off
  // (ignored), an iPhone's link (its permission asked from a tap: granted, denied; the sign turned), and the low-pass and dead zone
  async tilt(browser, url, out) {
    const turned = (angle) => ({ fn: (a) => {
      Object.defineProperty(window.screen.orientation, "angle", { configurable: true, get: () => window.__angle });
      window.__angle = a;
    }, arg: angle });
    const shake = (page, x, y, z) => page.evaluate(([x, y, z]) => window.dispatchEvent(new DeviceMotionEvent("devicemotion", { accelerationIncludingGravity: { x, y, z } })), [x, y, z]);
    // the beads' mean move while running, over a stretch of the weather's time (30 times as fast)
    const run = async (page, frames = 400) => page.evaluate((frames) => {
      Sky.timeScale(30);
      const first = new Map(), last = new Map(), steps = new Map();
      let n = 0;
      while (n++ < frames && Sky.advance(1)) {
        for (const b of Sky.state().humidity.beads) if (b.kind === "fog" && b.v > 0) {
          if (!first.has(b.id)) first.set(b.id, [b.x, b.cy]);
          last.set(b.id, [b.x, b.cy]);
          steps.set(b.id, (steps.get(b.id) || 0) + 1);
        }
      }
      const moves = [...last].filter(([id]) => steps.get(id) >= 8).map(([id, [x, y]]) => [x - first.get(id)[0], y - first.get(id)[1]]);
      const sum = (i) => moves.reduce((t, m) => t + m[i], 0) / Math.max(moves.length, 1);
      const h = Sky.state().humidity, t = Sky.state().tilt;
      return { running: moves.length, dx: sum(0), dy: sum(1), ranOff: h.ranOff, tilt: t, size: Sky.state().size };
    }, frames);
    const opts = (angle, extra = {}) => ({ query: "?fx=humidity2", pace: 10, driven: true, context: { viewport: { width: 400, height: 300 } }, init: turned(angle), ...extra });
    // (readings: [angle, x, y, z], the phone held so)
    const cases = { upright: [0, 0, 9.8, 0], rolledRight: [0, -6, 7.7, 0], rolledLeft: [0, 6, 7.7, 0], onItsRight: [0, -9.8, 0, 0], onItsTop: [0, 0, -9.8, 0],
                    landscape: [90, 9.8, 0, 0], landscapeLeaning: [90, 9, 5.7, 0], landscapeFlipped: [270, -9.8, 0, 0], landscapeFlippedLeaning: [270, -9, -5.7, 0],
                    upsideDown: [180, 0, -9.8, 0], upsideDownLeaning: [180, 6, -7.7, 0] };
    out.cases = {};
    out.errors = [];
    await Promise.all(Object.entries(cases).map(async ([name, [angle, x, y, z]]) => {
      const p = await weatherPage(browser, url, opts(angle));
      await shake(p.page, x, y, z);
      out.cases[name] = await run(p.page);
      out.errors.push(...p.errors);
      await p.context.close();
    }));
    // the pure turn (the page's screenGravity(ax, ay, angle, ios) -> the pull in the screen's x right, y down), against readings worked out
    // here from the physics, not from the page's table: the device turned a quarter COUNTER-clockwise `angle` degrees (screen.orientation.angle:
    // 90 is the device's top to the viewer's left), its axes in the viewer's world (x right, y up) x = (cos a, sin a), y = (-sin a, cos a);
    // the screen's own edge `dir` (left, right, top, bottom) lowered, the pull in the world that way; the reading by the spec is minus the
    // pull in the device's axes, iOS's the pull itself
    const pure = await (await weatherPage(browser, url, opts(0))).page.evaluate(() => {
      const world = { left: [-1, 0], right: [1, 0], top: [0, 1], bottom: [0, -1] }, got = {};
      for (const angle of [0, 90, 180, 270, -90]) for (const ios of [false, true]) for (const [edge, [wx, wy]] of Object.entries(world)) {
        const t = angle * Math.PI / 180, dx = Math.cos(t) * wx + Math.sin(t) * wy, dy = -Math.sin(t) * wx + Math.cos(t) * wy;   // (the pull in the device's axes)
        const k = ios ? 1 : -1, [x, y] = window.__screenGravity(k * 9.8 * dx, k * 9.8 * dy, angle, ios);
        got[`${angle}/${ios ? "ios" : "spec"}/${edge}`] = [Math.round(x * 100) / 100 + 0, Math.round(y * 100) / 100 + 0];
      }
      return got;
    });
    out.pure = pure;
    // end to end on an iPad (its user agent: the sign turned), the window shaped as the screen is. iPadOS Safari's screen.orientation.angle
    // is relative to the iPad's natural LANDSCAPE (0 for landscape-primary, as a real iPad's ?tiltdebug=1 showed), so the page must use
    // window.orientation (0, 90, -90, 180: 90 is the device turned counter-clockwise) and ignore that angle; the readings are the pull itself
    // in the PORTRAIT device frame (x to its right edge, y to its top, z out of the screen).
    const ipad = (w, h) => ({ viewport: { width: w, height: h }, hasTouch: true, isMobile: true,
                              userAgent: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1" });
    const reported = (angle, extra) => ({ fn: ([angle, extra]) => {
      Object.defineProperty(window.screen.orientation, "angle", { configurable: true, get: () => angle });
      if ("windowOrientation" in extra) Object.defineProperty(window, "orientation", { configurable: true, value: extra.windowOrientation });
      if (extra.type) Object.defineProperty(window.screen.orientation, "type", { configurable: true, get: () => extra.type });
    }, arg: [angle, extra] });
    // the iOS reading worked out from the physics: the iPad turned `wo` degrees counter-clockwise from upright (the device's x and y axes in
    // the viewer's world (x right, y up): x = (cos, sin), y = (-sin, cos)); the screen's `edge` down, the pull that way; the reading is the
    // pull's share along each device axis
    const world = { left: [-1, 0], right: [1, 0], top: [0, 1], bottom: [0, -1] };
    const iosReading = (wo, edge) => {
      const t = wo * Math.PI / 180, [wx, wy] = world[edge], r = (v) => Math.round(v * 98) / 10 + 0;
      return [r(Math.cos(t) * wx + Math.sin(t) * wy), r(-Math.sin(t) * wx + Math.cos(t) * wy), 0];
    };
    const ipads = {};
    for (const wo of [0, 90, -90, 180]) for (const edge of Object.keys(world)) {
      // (what iPadOS likely reports: its angle 0 in landscape, 90 in portrait; the type from the way up)
      ipads[`wo${wo}/${edge}`] = { sa: wo === 0 || wo === 180 ? 90 : 0, wo, type: { 0: "portrait-primary", 90: "landscape-primary", "-90": "landscape-secondary", 180: "portrait-secondary" }[wo],
                                   w: wo === 0 || wo === 180 ? 300 : 400, h: wo === 0 || wo === 180 ? 400 : 300, reading: iosReading(wo, edge), edge };
    }
    // the user's three real readings (iPad, iPadOS 18.7, landscape: angle 0, landscape-primary, window.orientation 90)
    const real = { flat: [-0.47, 0.44, -9.56], leftDown: [-0.18, 1.81, -9.48], topRaised: [-6.56, 0.06, -6.64] };
    for (const [name, reading] of Object.entries(real)) ipads[`real/${name}`] = { sa: 0, wo: 90, type: "landscape-primary", w: 400, h: 300, reading, edge: { flat: "flat", leftDown: "left", topRaised: "bottom" }[name] };
    // portrait with the angle iPadOS likely reports (90), window.orientation 0: the top raised must give down
    ipads["portrait/topRaised"] = { sa: 90, wo: 0, type: "portrait-primary", w: 300, h: 400, reading: [0, -9.8, 0], edge: "bottom" };
    // no window.orientation: the orientation type (landscape-primary is 90 here, whatever the angle says)
    ipads["type/leftDown"] = { sa: 0, wo: null, type: "landscape-primary", w: 400, h: 300, reading: iosReading(90, "left"), edge: "left" };
    ipads["type/secondaryLeftDown"] = { sa: 0, wo: null, type: "landscape-secondary", w: 400, h: 300, reading: iosReading(-90, "left"), edge: "left" };
    out.ipads = {};
    await Promise.all(Object.entries(ipads).map(async ([name, c]) => {
      const p = await weatherPage(browser, url, { ...opts(c.sa), context: ipad(c.w, c.h), init: reported(c.sa, { windowOrientation: c.wo, type: c.type }) });
      await shake(p.page, ...c.reading);
      out.ipads[name] = { edge: c.edge, tilt: await p.page.evaluate(() => Sky.state().tilt) };
      out.errors.push(...p.errors);
      await p.context.close();
    }));
    // ?tiltdebug=1: the readout of the raw numbers appears with a reading; without it, none
    {
      const dbg = await weatherPage(browser, url, { ...opts(90), query: (opts(90).query || "") + "?tiltdebug=1" });
      const off = await weatherPage(browser, url, opts(90));
      await shake(dbg.page, 1.5, 9.5, 0.5); await shake(off.page, 1.5, 9.5, 0.5);
      out.debugText = await dbg.page.evaluate(() => (document.getElementById("tiltdebug") || {}).textContent || null);
      const dbgIos = await weatherPage(browser, url, { ...opts(0), query: "?fx=humidity2&tiltdebug=1", context: ipad(400, 300), init: reported(0, { windowOrientation: 90, type: "landscape-primary" }) });
      await shake(dbgIos.page, -0.18, 1.81, -9.48);
      out.debugIos = await dbgIos.page.evaluate(() => (document.getElementById("tiltdebug") || {}).textContent || null);
      await dbgIos.context.close();
      out.debugOff = await off.page.evaluate(() => !document.getElementById("tiltdebug"));
      await dbg.context.close(); await off.context.close();
    }
    // no sensor reading: as ever (every bead straight down, no reading taken)
    const none = await weatherPage(browser, url, opts(0));
    out.none = await run(none.page);
    out.errors.push(...none.errors);
    // (a reading with nothing in it, as a desktop browser's one empty event: ignored)
    await none.page.evaluate(() => window.dispatchEvent(new DeviceMotionEvent("devicemotion", {})));
    out.empty = await none.page.evaluate(() => Sky.state().tilt);
    await none.context.close();
    // the window's own rotation (window.orientation, the old way) when screen.orientation says nothing
    const old = await weatherPage(browser, url, { ...opts(0), init: { fn: () => {
      Object.defineProperty(window.screen, "orientation", { configurable: true, value: undefined });
      Object.defineProperty(window, "orientation", { configurable: true, value: 90 });
    }, arg: null } });
    await shake(old.page, 9.8, 0, 0);
    out.oldWay = await run(old.page);
    out.errors.push(...old.errors);
    await old.context.close();
    // the animations low and off: no listener, nothing taken
    out.ignored = {};
    for (const mode of ["low", "off"]) {
      const p = await weatherPage(browser, url, { ...opts(0), query: "", animations: mode });
      await shake(p.page, -9.8, 0, 0);
      out.ignored[mode] = await p.page.evaluate(() => ({ tilt: Sky.state().tilt, link: document.getElementById("tilt").textContent, running: Sky.state().running }));
      out.errors.push(...p.errors);
      await p.context.close();
    }
    // ... and after "stop": ignored too, straight down again; a test link after it, the readings taken again
    const stopped = await weatherPage(browser, url, opts(0));
    await shake(stopped.page, -9.8, 0, 0);
    const before = await stopped.page.evaluate(() => Sky.state().tilt);
    await stopped.page.click("#motion .animations");
    await shake(stopped.page, -9.8, 0, 0);
    const after = await stopped.page.evaluate(() => Sky.state().tilt);
    out.stopped = { before, after };
    out.errors.push(...stopped.errors);
    await stopped.context.close();
    // an iPhone's (a touch screen, permission asked first, nothing sent till it's granted): the link a moment after, its permission
    // asked from the tap, gone at once; granted, denied, an error; its sign the other way about (upright: y -9.8)
    const ios = (answer) => ({ fn: ([answer]) => {
      Object.defineProperty(window.screen.orientation, "angle", { configurable: true, get: () => 0 });
      Object.defineProperty(window, "orientation", { configurable: true, value: 0 });   // (iOS's rotation is window.orientation: upright)
      const asked = window.__asked = [];
      DeviceMotionEvent.requestPermission = () => { asked.push(1); return answer === "error" ? Promise.reject(new Error("no")) : Promise.resolve(answer); };
    }, arg: [answer] });
    const phone = { viewport: { width: 400, height: 300 }, hasTouch: true, isMobile: true,
                    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1" };
    const seen = (page) => page.evaluate(() => ({ link: document.getElementById("tilt").textContent, asked: window.__asked.length, tilt: Sky.state().tilt }));
    out.ios = {};
    for (const answer of ["granted", "denied", "error"]) {
      const p = await weatherPage(browser, url, { ...opts(0), context: phone, init: ios(answer) });
      const early = await seen(p.page);   // (not yet: it may be sent without asking)
      await p.page.waitForSelector("#tilt .link");
      const was = await seen(p.page);
      await p.page.click("#tilt .link");
      await p.page.waitForFunction(() => document.getElementById("tilt").textContent === "");
      await p.page.evaluate(() => new Promise((r) => setTimeout(r, 50)));
      if (answer === "granted") await shake(p.page, 6, -7.7, 0);   // (as sent: the pull to the device's right edge, upright)
      out.ios[answer] = { early, was, now: await seen(p.page) };
      out.errors.push(...p.errors);
      await p.context.close();
    }
    // ... no link once readings came by themselves (it asks nothing, as Chrome's), nor with the animations low, nor on a desktop
    const free = await weatherPage(browser, url, { ...opts(0), context: phone, init: ios("granted") });
    await shake(free.page, 0, 9.8, 0);
    await free.page.evaluate(() => new Promise((r) => setTimeout(r, 1800)));
    out.ios.free = await seen(free.page);
    await free.context.close();
    const lowIos = await weatherPage(browser, url, { ...opts(0), query: "", animations: "low", context: phone, init: ios("granted") });
    await lowIos.page.evaluate(() => new Promise((r) => setTimeout(r, 1800)));
    out.ios.low = await lowIos.page.evaluate(() => document.getElementById("tilt").textContent);
    await lowIos.context.close();
    const desk = await weatherPage(browser, url, { ...opts(0), init: ios("granted") });
    await desk.page.evaluate(() => new Promise((r) => setTimeout(r, 1800)));
    out.ios.desktop = await desk.page.evaluate(() => document.getElementById("tilt").textContent);
    await desk.context.close();
    // the low-pass and the dead zone, in the weather's own terms (Sky.tilt(x, y, z, dt)): a turn eased in over time and held a
    // moment, noise inside a few degrees (no change of epoch), 4 degrees of straight down kept straight, flat blended
    const sky = await weatherPage(browser, url, opts(0));
    out.filter = await sky.page.evaluate(() => {
      const t = () => Sky.state().tilt, seen = {};
      Sky.tilt(0, 9.8, 0);
      seen.start = t();
      Sky.tilt(9.8, 0, 0, 0.05);   // (a sudden 90 degree turn: a fifth of the way in 0.05 s at TAU 0.25)
      seen.eased = [t().angle];
      for (let i = 0; i < 30; i++) Sky.tilt(9.8, 0, 0, 0.1);
      seen.eased.push(t().angle);
      const epoch = t().epoch;
      for (let i = 0; i < 100; i++) Sky.tilt(9.8 + 0.1 * Math.sin(i), 0.1 * Math.cos(i * 1.7), 0, 0.03);   // (a hand's tremor)
      seen.still = { epoch: t().epoch - epoch, angle: t().angle };
      for (let i = 0; i < 30; i++) Sky.tilt(0.6, 9.8, 0, 0.1);   // (3.5 degrees off straight down: down)
      seen.snapped = t();
      for (let i = 0; i < 30; i++) Sky.tilt(9.8, 0, 0.1, 0.1);
      for (let i = 0; i < 30; i++) Sky.tilt(0.5, 0.5, 9.8, 0.1);   // (laid flat)
      seen.flat = t();
      // (hysteresis: from flat, 6 degrees is still pooling, 8 runs, and then 6 runs still; the readings' size aside)
      const at = (deg) => { const r = deg * Math.PI / 180; Sky.tilt(0, 3 * Math.sin(r), 3 * Math.cos(r), Infinity); return Sky.state().tilt.pool; };
      seen.hyst = [at(6), at(8), at(6), at(4)];
      seen.bad = [Sky.tilt(NaN, 1, 1), Sky.tilt(undefined, 1, 1), Sky.tilt(0, 0, 0)];
      Sky.level();
      seen.level = t();
      return seen;
    });
    out.errors.push(...sky.errors);
    await sky.context.close();
    // the screen's angle off level, theta (0 lying face up, 90 upright, 180 face down): the steepness sin(theta) is a speed factor (0 under
    // 5 degrees: the water pools, beads stay and merge; smoothstep to 1 at 40; the same through 90 and down again to 0 at 180). The readings are
    // (0, g sin, g cos) in any magnitude. Per angle: how fast the beads run (v against what the physics gives them: the factor), whether
    // any ran, the merges and the largest bead (against the largest early on), and whether any left the window
    const pools = {};
    out.pools = pools;
    await Promise.all([0, 3, 180, 177, 10, 30, 60, 90, 120, 150, 175].map(async (theta) => {
      const p = await weatherPage(browser, url, opts(0));
      pools[theta] = await p.page.evaluate((theta) => {
        const g = theta === 3 ? 3 : 9.8, t = theta * Math.PI / 180;   // (3: a weak reading of the same angle: only the ratio counts)
        Sky.tilt(0, g * Math.sin(t), g * Math.cos(t));
        Sky.timeScale(30);
        const out = { factor: 0, n: 0, ran: 0, early: 0, largest: 0, off: 0, trails: 0 };
        const stat = () => {
          const h = Sky.state().humidity, [W, H] = Sky.state().size;
          for (const b of h.beads) {
            if (b.kind !== "fog") continue;
            if (b.v > 0) { out.ran++; if (out.n < 400) { out.factor += b.v / h.physics.speed(b.r, b.glass); out.n++; } }
            if (b.x < 0 || b.x > W || b.y >= H) out.off++;
            out.largest = Math.max(out.largest, b.r);
          }
          return h;
        };
        let n = 0, h0;
        while (n++ < 600 && Sky.advance(1)) { h0 = stat(); if (n === 60) out.early = out.largest; }
        const h = Sky.state().humidity;
        return { ...out, factor: out.factor / Math.max(out.n, 1), merges: h.merges, ranOff: h.ranOff, beads: h.beads.length, tilt: Sky.state().tilt, trailsAfter: h.trails };
      }, theta);
      out.errors.push(...p.errors);
      await p.context.close();
    }));
    // stop-start: flat (pooled) then tilted, several times over: each time a bead runs again its trail begins at once, from its own back edge
    // (no gap, no half-length missing): within 2 px of travel its rows reach from where its top stood to its top now, with none missing
    const restarts = [];
    out.restarts = restarts;
    await Promise.all([0, 1, 2, 3].map(async (edge) => {
      const p = await weatherPage(browser, url, opts(0));
      const got = await p.page.evaluate((edge) => {
        const g = 9.8, flat = () => Sky.tilt(0, 0, g, Infinity);
        // (the downhill edge: 0 the bottom, 1 the top, 2 the left, 3 the right; the screen up, steepest, tipped that way)
        const tip = () => Sky.tilt(...[[0, g, 0], [0, -g, 0], [-g, 0, 0], [g, 0, 0]][edge], Infinity);
        Sky.timeScale(30);
        const rows = [];
        for (let cycle = 0; cycle < 5; cycle++) {
          flat();
          for (let n = 0; n < 40; n++) Sky.advance(1);
          const pooledTrails = Sky.state().humidity.trails;
          tip();
          Sky.timeScale(3);
          // (the beads that run: where each first ran from, then each step until one has moved 2 px along its way)
          const h0 = Sky.state().humidity;
          const start = new Map(), ids = new Set(h0.beads.filter((b) => b.kind === "fog" && !b.fading && b.r >= 1.5).map((b) => b.id));
          const done = new Set(), bad = [];
          for (let n = 0; n < 600 && done.size < 6; n++) {
            Sky.advance(1);
            const h = Sky.state().humidity;
            for (const b of h.beads) {
              if (!ids.has(b.id) || done.has(b.id) || !(b.v > 0)) continue;
              if (!start.has(b.id)) start.set(b.id, [b.x0, b.y0, Math.floor(b.y0) + b.bottom - Math.floor(b.y)]);   // (its first running frame: where it set off from)
              const [x0, y0, bottom0] = start.get(b.id), along = edge === 0 ? b.y - y0 : edge === 1 ? bottom0 - b.bottom : edge === 3 ? b.x - x0 : x0 - b.x;
              if (along < 2 || along > 6) { if (along > 6) done.add(b.id); continue; }
              done.add(b.id);
              const tr = h.laidOf(b.id);
              rows.push({ edge, cycle, id: b.id, along, rows: tr.length, ys: tr, y0, bottom0, x0, left0: Math.round(x0) - (b.w >> 1), right0: Math.round(x0) - (b.w >> 1) + b.w });
            }
          }
          Sky.timeScale(30);
          rows.push({ edge, cycle, pooledTrails, trails: Sky.state().humidity.trails, mode: Sky.state().tilt.mode, ran: done.size, seen: ids.size });
        }
        return rows;
      }, edge);
      restarts.push(...got);
      out.errors.push(...p.errors);
      await p.context.close();
    }));
  },
  // gives: a bead gives way by its size and the steepness (the pinning: sin(alpha_crit) ~ 1 / r^2, r_crit = RUN_R / sqrt(s)), on a device with
  // a tilt sensor (Sky.tilt at once, no smoothing): r_crit at 90, 30 and 10 degrees; at 10 and 30 degrees beads placed in clear fog,
  // 20 s of the page's time: each placed bead's {r0, r (largest it reached), moved, held, gone}; the lean at r / r_crit 0.8, 0.99 and
  // 1.03 (its offset, the drawn sprite against its own); the device levelled (flat; or 10 degrees from upright) with a bead running
  async gives(browser, url, out) {
    const pages = [];
    const open = async () => {
      const p = await weatherPage(browser, url, { query: "?fx=humidity2", pace: 10, driven: true, context: { viewport: { width: 400, height: 300 } } });
      pages.push(p);
      await p.page.evaluate(() => Sky.advance(150));
      return p;
    };
    // clear spots in the fog (no bead within 9 px, 30 px apart across), each {x, y: its middle}
    const place = (page, tilt, radii, frames) => page.evaluate(([tilt, radii, frames]) => {
      const h0 = Sky.state().humidity, [W, H] = [h0.width, h0.height], spots = [];
      for (let y = Math.round(H * 0.15); y < H * 0.8; y += 3) for (let x = 20; x < W - 20; x += 3) {
        if (spots.length >= radii.length) break;
        if (spots.some((s) => Math.abs(s.x - x) < 30)) continue;
        if (h0.glassAt(x, y) !== "fog" || h0.glassAt(x, y + 12) !== "fog") continue;
        if (h0.beads.every((b) => Math.hypot(b.x - x, b.cy - y) > 9)) spots.push({ x, y });
      }
      Sky.tilt(...tilt, Infinity);
      const ids = h0.place(spots.slice(0, radii.length).map((s, i) => ({ x: s.x, y: s.y, r: radii[i] })));
      const track = new Map(ids.map((id, i) => [id, { r0: radii[i], r: radii[i], x0: null, y0: null, moved: 0, held: 0, ran: 0, gone: false }]));
      for (let n = 0; n < frames; n++) {
        Sky.advance(1);
        const byId = new Map(Sky.state().humidity.beads.map((b) => [b.id, b]));
        for (const [id, t] of track) {
          const b = byId.get(id);
          if (!b) { t.gone = true; continue; }
          if (t.x0 === null) { t.x0 = b.x; t.y0 = b.y; }
          t.r = Math.max(t.r, b.r);
          if (b.held) t.held++;
          if (b.v > 0) t.ran++;
          t.moved = Math.max(t.moved, Math.hypot(b.x - t.x0, b.y - t.y0));
        }
      }
      return { spots: spots.length, tracked: [...track.values()], critical: Sky.state().humidity.critical };
    }, [tilt, radii, frames]);
    const at = (deg) => { const r = deg * Math.PI / 180; return [0, 9.8 * Math.sin(r), 9.8 * Math.cos(r)]; };
    out.critical = {};
    for (const deg of [90, 30, 10]) {
      const p = await open();
      await p.page.evaluate((t) => Sky.tilt(...t, Infinity), at(deg));
      const h = await p.page.evaluate(() => ({ critical: Sky.state().humidity.critical, of: [1, 0.5, 0.17].map((s) => Sky.state().humidity.physics.critical(s)) }));
      out.critical[deg] = h;
      await p.context.close();
    }
    // (no sensor: RUN_R, nothing leans)
    { const p = await open(); out.noSensor = await p.page.evaluate(() => ({ critical: Sky.state().humidity.critical, run: Sky.state().humidity.physics.run,
                                  lean: Sky.state().humidity.beads.reduce((m, b) => Math.max(m, b.lean), 0) })); await p.context.close(); }
    // (10 degrees: r_crit 3.36: none of 2.2-3.2 moves; a placed 3.6 runs. 30 degrees: r_crit 1.98: 2.2 runs, 1.8 stays)
    const radiiTen = [3.6, 3.2, 3.0, 2.6, 2.2], radiiThirty = [2.2, 2.6, 1.8, 1.6, 3.0];
    out.ten = [], out.thirty = [];
    for (const [deg, radii, into] of [[10, radiiTen, out.ten], [10, [3.3, 2.6, 2.0], out.ten], [30, radiiThirty, out.thirty], [30, [1.6, 1.9, 2.6], out.thirty]]) {
      const p = await open();
      into.push(await place(p.page, at(deg), radii, 200));
      await p.context.close();
    }
    // the lean: at 10 degrees (r_crit 3.36): r 2.69 (0.8), 3.33 (0.99), 3.46 (1.03), a few frames in (leaning in over 0.15 s)
    out.lean = {};
    for (const [name, r] of [["eight", 2.69], ["nearly", 3.33], ["over", 3.46]]) {
      const p = await open();
      out.lean[name] = await p.page.evaluate(([t, r]) => {
        const h0 = Sky.state().humidity, [W, H] = [h0.width, h0.height];
        let spot = null;
        for (let y = Math.round(H * 0.15); y < H * 0.8 && !spot; y += 3) for (let x = 20; x < W - 20 && !spot; x += 3) {
          if (h0.glassAt(x, y) === "fog" && h0.glassAt(x, y + 12) === "fog" && h0.beads.every((b) => Math.hypot(b.x - x, b.cy - y) > 9)) spot = [x, y];
        }
        Sky.tilt(...t, Infinity);
        const [id] = h0.place([{ x: spot[0], y: spot[1], r }]);
        const frames = [];
        for (let n = 0; n < 12; n++) {
          Sky.advance(1);
          const h = Sky.state().humidity, b = h.beads.find((q) => q.id === id);
          if (!b) break;
          frames.push({ r: b.r, ratio: b.r / h.critical, lean: b.lean, dx: b.leanDx, dy: b.leanDy, held: b.held, v: b.v, stretch: b.stretch,
                        drawnH: b.drawn && b.drawn[3], ownH: b.bottom - Math.floor(b.y), y: b.y, drawnTop: b.drawn && b.drawn[1], critical: h.critical });
        }
        return frames;
      }, [at(10), r]);
      await p.context.close();
    }
    // giving way: r 3.0 held at 10 degrees (leaning), then the glass steepened to 30 degrees (r_crit 1.98): each frame {lean, v, stretch, drawn
    // height against its own}
    { const p = await open();
      out.release = await p.page.evaluate(([ten, thirty]) => {
        const h0 = Sky.state().humidity, [W, H] = [h0.width, h0.height];
        let spot = null;
        for (let y = Math.round(H * 0.15); y < H * 0.8 && !spot; y += 3) for (let x = 20; x < W - 20 && !spot; x += 3) {
          if (h0.glassAt(x, y) === "fog" && h0.glassAt(x, y + 12) === "fog" && h0.beads.every((b) => Math.hypot(b.x - x, b.cy - y) > 9)) spot = [x, y];
        }
        Sky.tilt(...ten, Infinity);
        const [id] = h0.place([{ x: spot[0], y: spot[1], r: 3.0 }]);
        const frame = () => { const b = Sky.state().humidity.beads.find((q) => q.id === id); return { lean: b.lean, v: b.v, stretch: b.stretch, held: b.held, drawnH: b.drawn && b.drawn[3], ownH: b.bottom - Math.floor(b.y) }; };
        for (let n = 0; n < 12; n++) Sky.advance(1);
        const held = frame();
        Sky.tilt(...thirty, Infinity);
        const frames = [];
        for (let n = 0; n < 12; n++) { Sky.advance(1); frames.push(frame()); }
        return { held, frames };
      }, [at(10), at(30)]);
      await p.context.close(); }
    // levelling: a bead of r 3.0 running upright (r_crit 1.4); then flat, or 10 degrees from upright (r_crit 3.36): its run's move each
    // frame (px), 10 frames (1 s)
    out.level = {};
    for (const [name, to] of [["flat", [0, 0, 9.8]], ["gentle", at(10)]]) {
      const p = await open();
      out.level[name] = await p.page.evaluate(([to]) => {
        const h0 = Sky.state().humidity, [W, H] = [h0.width, h0.height];
        let spot = null;
        for (let y = Math.round(H * 0.15); y < H * 0.8 && !spot; y += 3) for (let x = 20; x < W - 20 && !spot; x += 3) {
          if (h0.glassAt(x, y) === "fog" && h0.glassAt(x, y + 12) === "fog" && h0.beads.every((b) => Math.hypot(b.x - x, b.cy - y) > 9)) spot = [x, y];
        }
        Sky.tilt(0, 9.8, 0, Infinity);
        const [id] = h0.place([{ x: spot[0], y: spot[1], r: 3.0 }]);
        const pos = () => { const b = Sky.state().humidity.beads.find((q) => q.id === id); return b ? [b.y, b.v] : null; };
        for (let n = 0; n < 20; n++) Sky.advance(1);
        const before = pos();
        Sky.tilt(...to, Infinity);
        const moves = [];
        let last = pos();
        for (let n = 0; n < 12; n++) { Sky.advance(1); const now = pos(); moves.push(now && last ? [Math.hypot(0, now[0] - last[0]), now[1]] : null); last = now; }
        return { before, moves };
      }, [to]);
      await p.context.close();
    }
    out.errors = pages.flatMap((p) => p.errors);
  },
  async gust(browser, url, out) {
    const opened = (async () => {   // (beside the rest)
      const p = await weatherPage(browser, url, { query: "?fx=gust", driven: true });
      await drive(p.page, 30);
      const seen = { ...await p.page.evaluate(() => ({ sent: Sky.state().gust.sent, kinds: Sky.state().gust.kinds, effects: Sky.state().effects })),
                     url: await p.page.evaluate(() => location.search), errors: p.errors };
      // ... and with an effect beside it (?fx=wind,gust): the wind on, nothing sent, only "wind" left in the URL
      const q = await weatherPage(browser, url, { query: "?fx=wind,gust", driven: true });
      await drive(q.page, 30);
      seen.beside = { ...await q.page.evaluate(() => ({ sent: Sky.state().gust.sent, effects: Sky.state().effects })),
                      url: await q.page.evaluate(() => location.search) };
      seen.errors = [...seen.errors, ...q.errors];
      await q.context.close();
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
    // the test link: each click one, the three kinds in three clicks; the URL untouched (a reload sends none); never pressed; with
    // an effect's link clicked after it, only that effect in the URL; "stop": gone at once
    const before = await sent();
    for (let i = 0; i < 5; i++) await page.locator("#fxtest button[data-gust]").click();
    out.link = { sent: await sent() - before, kinds: (await page.evaluate(() => Sky.state().gust.kinds)).slice(-5),
                 flying: await page.evaluate(() => Sky.state().gust.flyers.length), url: await page.evaluate(() => location.search),
                 pressed: await page.evaluate(() => document.querySelector("#fxtest button[data-gust]").getAttribute("aria-pressed")),
                 classes: await page.evaluate(() => document.querySelector("#fxtest button[data-gust]").className) };
    await page.locator("#fxtest button[data-fx='wind']").click();   // (an effect on: "stop" shows)
    out.link.afterWind = await page.evaluate(() => location.search);
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

  // The wind's direction (Sky.setWind; the page's: the Wind direction tile's reading, or the test link's ?fx=wind-east / wind-west):
  // which side each reading calls for and how a flip eases; then each effect with the wind blowing leftward (wind-east) and
  // rightward (wind-west), side by side on pages of their own (the same seed, so the same weather: the rightward mirrors the leftward)
  // The rain's slant: a continuous function of the live wind's speed (Sky.setWindSpeed), settled at each speed (no wind effect, then
  // with it), rightward too; eased in a few seconds, never a jump (at 60 frames a second); the streaks' tilts; its coverage of the width
  async slant(browser, url, out) {
    const p = await weatherPage(browser, url, { query: "?fx=rain2", driven: true });
    // (each speed held long enough to settle: frames of 0.1 s of the weather's time, 20 s)
    const settle = (kmh, gust = 0) => p.page.evaluate(([kmh, gust]) => {
      Sky.timeScale(3);
      Sky.setWindSpeed(kmh, gust);
      Sky.advance(200);
      Sky.timeScale(1);
      const r = Sky.state().rain;
      return { slantDeg: r.slantDeg, slant: r.slant, speed: r.speed, target: r.target, vxMax: r.vxMax, tilts: r.tilts, mirrored: r.mirrored, vx: r.vx, layers: r.layers };
    }, [kmh, gust]);
    out.speeds = {};
    for (const kmh of [0, 2, 5, 10, 20, 40, 60, 100]) out.speeds[kmh] = await settle(kmh);
    out.gust = await settle(10, 30);   // (speed 10, gust 30: 10 + 0.3 * 20 = 16 km/h)
    out.noGust = await settle(10, 4);   // (a gust under the speed adds nothing)
    // the tilts in use, speed by speed (0 to 40 km/h in steps of 2): every sprite step, drawn
    out.tilts = [];
    for (let kmh = 0; kmh <= 40; kmh += 2) out.tilts.push((await settle(kmh)).tilts);
    out.tiltSteps = await p.page.evaluate(() => Sky.state().rain.tiltSteps);
    // no wind, the wind effect on too (its leaves, its gust bands): every drop straight down; and with it on, wind: still the same
    out.calm = await settle(0);
    await p.page.evaluate(() => Sky.set({ rain: 1, wind: 1 }, true));
    await p.page.evaluate(() => Sky.advance(150));
    out.calmWind = await settle(0);
    out.calmWind.effects = await p.page.evaluate(() => Sky.state().effects);
    out.blowing = await settle(20);   // (the wind effect on: the bands push and bunch the drops, their mean still slanted that way)
    // a jump in the wind's speed at 60 frames a second: the slant's step each frame
    out.jump = await p.page.evaluate(() => {
      Sky.set({ rain: 1 }, true);
      Sky.advance(60);
      Sky.timeScale(0.5);   // (frames of a 60th of a second)
      Sky.setWindSpeed(0, 0);
      Sky.advance(600);
      const at = () => Sky.state().rain.slantDeg, up = [at()], down = [];
      Sky.setWindSpeed(40, 0);
      for (let i = 0; i < 600; i++) { Sky.advance(1); up.push(at()); }
      Sky.setWindSpeed(0, 0);
      for (let i = 0; i < 600; i++) { Sky.advance(1); down.push(at()); }
      Sky.timeScale(1);
      const steps = (a) => Math.max(...a.map((v, i) => i ? Math.abs(v - a[i - 1]) : 0));
      const to = (a, deg) => a.findIndex((v) => Math.abs(v) >= deg) / 60;   // (s until it has got that far)
      return { vt: Sky.state().rain.layers[1].vt, maxUp: steps(up), maxDown: steps(down), final: up[up.length - 1], upTo50: to(up, 50), upSecondsHalf: to(up, up[up.length - 1] * 0.5),
               downSecondsHalf: down.findIndex((v) => v <= up[up.length - 1] * 0.5) / 60, back: down[down.length - 1], monotonic: up.every((v, i) => i === 0 || v >= up[i - 1] - 1e-9) };
    });
    // rightward: the exact mirror (a negative angle, the streaks mirrored, the drops moving right)
    out.rightward = await p.page.evaluate(() => {
      Sky.setWind(1);
      Sky.setWindSpeed(20, 0);
      Sky.timeScale(3);
      Sky.advance(150);
      Sky.timeScale(1);
      const r = Sky.state().rain;
      return { slantDeg: r.slantDeg, vx: r.vx, mirrored: r.mirrored, drops: Sky.state().particles.rain, flips: Sky.state().windFlips };
    });
    // coverage across the window's width at the capped slant, each way: the drops on screen in the lower half, over eight columns, from
    // many looks: none bare
    out.coverage = {};
    for (const way of [-1, 1]) {
      out.coverage[way] = await p.page.evaluate((way) => {
        Sky.setWind(way);
        Sky.setWindSpeed(80, 0);
        Sky.timeScale(3);
        Sky.advance(150);
        Sky.timeScale(1);
        const cols = new Array(8).fill(0), top = new Array(8).fill(0);
        const { width } = Sky.state().rain;
        for (let i = 0; i < 120; i++) {
          Sky.advance(2);
          for (const x of Sky.state().rain.xs) cols[Math.min(7, Math.floor(x / width * 8))]++;
        }
        return { cols, deg: Sky.state().rain.slantDeg };
      }, way);
    }
    // the path speed, the streaks' length and the landing rate at 0, 11, 25 and 60 km/h (no wind effect: just the rain): the drops' speed
    // along their path over their layer's vertical speed (1 / cos(angle)), the downward speed over it (1), the streaks' mean length along theirs, and the drops
    // landed over 60 s of the weather's time (no wind: the page's gusts don't push)
    out.paths = {};
    for (const kmh of [0, 11, 25, 60]) {
      await settle(kmh);
      out.paths[kmh] = await p.page.evaluate(() => {
        Sky.set({ rain: 1, wind: 0 }, true);
        Sky.advance(50);
        const r0 = Sky.state().rain, from = r0.landed;
        let ratio = 0, down = 0, len = 0, count = 0, n = 0;
        for (let i = 0; i < 600; i++) {
          Sky.advance(1);
          const r = Sky.state().rain;
          ratio += r.pathRatio; down += r.downRatio; len += r.streakLen; count += Sky.state().particles.rain; n++;
        }
        const r = Sky.state().rain;
        return { path: ratio / n, down: down / n, streak: len / n, drops: count / n, landed: r.landed - from, deg: r.slantDeg, layers: r.layers.map((l) => l.angleDeg) };
      });
    }
    // the test link's wind (?fx=rain2,wind: simulated 11 km/h, no reading above it): the slant it gives
    const link = await weatherPage(browser, url, { query: "?fx=rain2,wind", driven: true });
    out.link = await link.page.evaluate(() => { Sky.timeScale(3); Sky.advance(200); Sky.timeScale(1); const r = Sky.state().rain; return { deg: r.slantDeg, speed: r.speed, layers: r.layers.map((l) => l.angleDeg), rate: r.rate }; });
    await link.context.close();
    // the splashes: sideways spread (their x as drawn against where the drop landed) isn't asserted; no error is
    out.errors = p.errors;
    await p.context.close();
    return out;
  },

  // The rain's drop sizes (Sky.setRainRate): at each rate R (mm/h) the median drop D0, each layer's drop size, terminal speed, lean at
  // 25 km/h and vertical speed, and the landing bead's radius, settled
  async drops(browser, url, out) {
    const p = await weatherPage(browser, url, { query: "?fx=rain2", driven: true });
    out.rates = {};
    for (const rate of [0, 0.2, 0.5, 1, 5, 10, 50, 100, 500]) {
      out.rates[rate] = await p.page.evaluate((rate) => {
        Sky.setRainRate(rate);
        Sky.setWindSpeed(25, 0);
        Sky.timeScale(3);
        Sky.advance(200);
        Sky.timeScale(1);
        const r = Sky.state().rain;
        return { rate: r.rate, d0: r.d0, beadR: r.beadR, layers: r.layers, slantDeg: r.slantDeg };
      }, rate);
    }
    out.errors = p.errors;
    await p.context.close();
  },

  // The wind's eddies: how often new ones rise, over a long stretch of the weather's time (each of the page's slots in turn: a life, a rest)
  async eddies(browser, url, out) {
    const p = await weatherPage(browser, url, { query: "?fx=wind", driven: true });
    out.run = await p.page.evaluate(() => {
      Sky.timeScale(3);
      Sky.advance(100);   // (past the opening's part-way cycles)
      const w0 = Sky.state().wind, t0 = Sky.clock().clock, from = w0.eddyRises;
      let live = 0, n = 0;
      for (let i = 0; i < 4000; i++) { Sky.advance(1); const w = Sky.state().wind; live += w.eddyStates.length; n++; }
      const w = Sky.state().wind;
      Sky.timeScale(1);
      return { rises: w.eddyRises - from, seconds: Sky.clock().clock - t0, slots: w.eddySlots, live: live / n };
    });
    out.errors = p.errors;
    await p.context.close();
  },

  async direction(browser, url, out) {
    const small = { viewport: { width: 640, height: 480 } };
    const errored = [];   // (every page's console errors, to the end)
    const open = async (query, extra = {}) => { const p = await weatherPage(browser, url, { query, driven: true, context: small, ...extra }); errored.push(p.errors); return p; };
    const dirs = () => ({ dir: Sky.state().windDir, target: Sky.state().windTarget, factor: Sky.state().windFactor, flips: Sky.state().windFlips });
    // each reading (degrees, where the wind comes from) and the way called for: the page opens leftward; 0, 180, 360 and none hold it
    const readings = async () => {
      const p = await open("?fx=wind");
      const seq = [];
      out.start = await p.page.evaluate(dirs);
      for (const v of [180, 90, 270, 0, 180, 360, 359.5, 0.5, 270, null, 180, 45, 315.5, 360, 179.5, 180.5]) {
        await p.reads({ "Wind direction": v });
        await drive(p.page, 1);
        seq.push([v, (await p.page.evaluate(dirs)).target]);
      }
      out.readings = seq;
      out.readingErrors = p.errors;
      await p.context.close();
    };
    // a flip: the factor across over seconds, no frame's jump; the wind's own at once with nothing drawn
    const flip = async () => {
      const p = await open("?fx=wind");
      await drive(p.page, 90);
      await p.reads({ "Wind direction": 270 });
      out.flip = await p.page.evaluate(() => {
        const f = [Sky.state().windFactor], sides = [Sky.state().windDir], vx = [Sky.state().wind.vx];
        for (let i = 0; i < 300; i++) { Sky.advance(1); const s = Sky.state(); f.push(s.windFactor); sides.push(s.windDir); vx.push(s.wind.vx); }
        const steps = f.slice(1).map((v, i) => Math.abs(v - f[i]));
        const crossed = f.findIndex((v) => v > 0);
        return { first: f[0], last: f[f.length - 1], maxStep: Math.max(...steps), crossedAt: crossed, flips: Sky.state().windFlips, sides: [sides[0], sides[sides.length - 1]],
                 monotonic: f.every((v, i) => i === 0 || v >= f[i - 1]), vx: [vx[0], vx[crossed], vx[vx.length - 1]], seconds: f.findIndex((v) => v >= 0.999) / 30 };
      });
      // flipped again, partway: back the other way without a jump
      await p.reads({ "Wind direction": 90 });
      out.flipBack = await p.page.evaluate(() => {
        const f = [];
        for (let i = 0; i < 400; i++) { Sky.advance(1); f.push(Sky.state().windFactor); }
        return { maxStep: Math.max(...f.slice(1).map((v, i) => Math.abs(v - f[i]))), last: f[f.length - 1] };
      });
      out.flipErrors = p.errors;
      await p.context.close();
    };
    // with the wind alone, rightward the exact mirror of leftward: the canvases after the same frames, one flipped
    const mirror = async (query, frames) => {
      const seen = {};
      for (const way of ["east", "west"]) {
        const p = await open(`${query},wind-${way}`);
        await drive(p.page, frames);
        seen[way] = await p.page.evaluate(() => {
          const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height);
          return { w: c.width, h: c.height, data: Array.from(d.data), state: Sky.state().wind, dir: Sky.state().windDir };
        });
        seen[way].errors = p.errors;
        await p.context.close();
      }
      const { w, h, data: a } = seen.east, b = seen.west.data;
      let wrong = 0, ink = 0;
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4, j = (y * w + (w - 1 - x)) * 4;
        if (a[i + 3]) ink++;
        if (a[i] !== b[j] || a[i + 1] !== b[j + 1] || a[i + 2] !== b[j + 2] || a[i + 3] !== b[j + 3]) wrong++;
      }
      return { dirs: [seen.east.dir, seen.west.dir], vx: [seen.east.state.vx, seen.west.state.vx], ink, wrong, size: [w, h],
               errors: [...seen.east.errors, ...seen.west.errors] };
    };
    // the haze: its pan over 6 s each way, and its motes' mean move
    const haze = async (way) => {
      const p = await open(`?fx=haze3,wind-${way}`);
      await drive(p.page, 60);
      const r = await p.page.evaluate(() => {
        const at = () => new Map(Sky.state().haze.motes.map((m) => [m[0], m[1]]));
        const a = at(), s0 = Sky.state().haze.slide;
        Sky.advance(180);
        const b = at(), moved = [...a.keys()].filter((k) => b.has(k) && Math.abs(b.get(k) - a.get(k)) < 300).map((k) => b.get(k) - a.get(k));
        const m = Sky.state().haze.motes;
        return { moved: moved.reduce((x, y) => x + y, 0) / Math.max(moved.length, 1), n: moved.length, slide: Sky.state().haze.slide - s0,
                 motes: m.length, size: Sky.state().size };
      });
      r.errors = p.errors;
      await p.context.close();
      return r;
    };
    // the rain: its drops' mean speed across, how many drawn mirrored, with and without the wind
    const rain = async (way, withWind) => {
      const p = await open(`?fx=rain2${withWind ? `,wind-${way}` : ""}`);
      if (!withWind) await p.reads({ "Wind direction": way === "east" ? 90 : 270, "Wind": 12 });   // (a breeze: the slant follows its speed)
      await drive(p.page, 120);
      const r = await rainSeen(p.page, 2000);
      r.mirrored = await p.page.evaluate(() => Sky.state().rain.mirrored);
      r.drops = await p.page.evaluate(() => Sky.state().particles.rain);
      r.errors = p.errors;
      await p.context.close();
      return r;
    };
    // the frog and the flood's waves: where the frog is as it comes and goes, its sprites (as drawn, each rows mirrored) against
    // the other way's; the waves' pulse
    const frog = async (way) => {
      const p = await open(`?fx=rain3,wind-${way}`, { seed: 0 });
      const got = await p.page.evaluate(() => {
        const g = document.querySelector("#sky canvas").getContext("2d"), draw = g.drawImage, seen = new Map(), at = [];
        g.drawImage = function (...args) {
          const img = args[0];
          if (img && img.width === 47 && img.height === 8) {   // (the frog's top)
            const d = img.getContext("2d").getImageData(0, 0, 47, 8).data;
            seen.set(Array.from(d).join(","), Array.from({ length: 8 }, (_, y) => Array.from({ length: 47 }, (_, x) => Array.from(d.slice((y * 47 + x) * 4, (y * 47 + x) * 4 + 4)).join("/")).join(" ")));
          }
          return draw.apply(this, args);
        };
        let n = 0, first = null, last = null;
        while (n++ < 3000 && Sky.advance(1)) {   // (the first frog, then 8 s of it)
          const fl = Sky.state().flood, f = fl.frog;
          if (!f) continue;
          if (!first) first = { x: f.x };
          last = f;
          if (at.push(f.x) >= 240) break;
        }
        delete g.drawImage;
        return { first, width: Sky.state().size[0], travelled: last ? last.x - first.x : 0, frames: at.length, flipped: last && last.flipped,
                 pics: [...seen.values()] };
      });
      got.errors = p.errors;
      await p.context.close();
      return got;
    };
    const waves = async () => {
      const p = await open("?fx=rain3,wind-west");
      await drive(p.page, 60);   // (the wind faded in)
      const r = await p.page.evaluate(() => {
        const f = Sky.state().flood, W = 400, every = 0.05, s = 3, ended = (drift) => {
          const h = f.waves(W, 200, 4, s, every, drift).pop();
          const best = (lo, hi) => { let at = lo; for (let i = lo; i < hi; i++) if (h[i] < h[at]) at = i; return at; };
          // the pulse pushed down splits, a dip running each way: how far each has gone
          return { right: best(201, W - 40) - 200, left: 200 - best(40, 199) };
        };
        return { still: ended(0), downRight: ended(f.PUSH), downLeft: ended(-f.PUSH), push: f.push, PUSH: f.PUSH, wave: f.wave };
      });
      r.errors = p.errors;
      await p.context.close();
      return r;
    };
    // the gust: something flung with each way blowing: its side, its path, its sprite
    const gust = async (way, kind) => {
      const p = await open(`?fx=wind-${way}`);
      await drive(p.page, 40);
      const r = await p.page.evaluate((kind) => {
        const g = Sky.fling(kind), a = Sky.state().gust.flyers[0], xs = [a.x];
        for (let i = 0; i < 60; i++) { Sky.advance(1); const f = Sky.state().gust.flyers[0]; if (f) xs.push(f.x); }
        return { kind: g, to: a.to, x0: xs[0], x1: xs[xs.length - 1], width: Sky.state().size[0] };
      }, kind);
      r.errors = p.errors;
      await p.context.close();
      return r;
    };
    await Promise.all([readings(), flip()]);
    out.mirrorWind = await mirror("?fx=wind", 240);
    out.haze = { east: await haze("east"), west: await haze("west") };
    out.rain = { east: await rain("east", true), west: await rain("west", true), eastRead: await rain("east", false), westRead: await rain("west", false) };
    out.frog = { east: await frog("east"), west: await frog("west") };
    out.waves = await waves();
    out.gust = { east: await gust("east", "cow"), west: await gust("west", "cow"), chickenWest: await gust("west", "chicken") };
    // everything on and the wind turning each way twice, a frog crossing, a gust flung: no error, every effect still going
    const p = await open("?fx=haze4,rain3,wind,humidity,ice");
    await drive(p.page, 900);
    await p.page.evaluate(() => Sky.fling("barn"));
    await p.reads({ "Wind direction": 270 });
    await drive(p.page, 400);
    await p.reads({ "Wind direction": 45 });
    await drive(p.page, 400);
    await p.reads({ "Wind direction": 300 });
    await drive(p.page, 400);
    out.busy = await p.page.evaluate(() => ({ flips: Sky.state().windFlips, dir: Sky.state().windDir, effects: Sky.state().effects, leaves: Sky.state().wind.leaves }));
    await p.context.close();
    out.errors = errored.flat();
  },

  // The wind turning, its eddies: the air set up by hand (Sky.lab: one eddy held still, one leaf, no random turbulence unless asked), the
  // wind turned (Sky.setWind) and each frame recorded: the eddy's swirl (Sky.air) undisturbed, a leaf in the eddy veering through up on
  // the first turn and down on the second, one outside any eddy reversing along its line, one in a weak eddy half and half
  // The leaves' pace follows the measured wind (the effective speed: the reading plus 0.3 of the gust over it, eased): k = clamp(0.4 + 0.024 s,
  // 0.5, 1.6), Sky.state().wind.k; the leaves' mean speed across scales with it; a test link's wind (11 km/h, forced): k = 1
  async pace(browser, url, out) {
    const p = await weatherPage(browser, url, { driven: true });
    await p.page.evaluate(() => Sky.set({ wind: 1 }, true));
    const run = (kmh, gust) => p.page.evaluate(([kmh, gust]) => {
      Sky.setWindSpeed(kmh, gust);
      Sky.advance(400);   // (settled: the speed eased in about 2 to 4 s of the weather's time)
      const k = Sky.state().wind.k, speed = Sky.state().wind.speed;
      let sum = 0, n = 0;
      for (let i = 0; i < 1500; i++) { Sky.advance(1); const w = Sky.state().wind; sum += Math.abs(w.vx); n++; }
      return { k, speed, mean: sum / n };
    }, [kmh, gust]);
    out.speeds = {};
    for (const kmh of [0, 15, 25, 40, 60]) out.speeds[kmh] = await run(kmh, kmh);
    out.gust = await run(10, 40);   // (10 + 0.3 * 30 = 19 km/h)
    // the factor eases: from 25 to 60 km/h it rises smoothly over a few seconds, never a jump
    out.eased = await p.page.evaluate(() => {
      Sky.setWindSpeed(25, 25); Sky.advance(300);
      const ks = [Sky.state().wind.k];
      Sky.setWindSpeed(60, 60);
      for (let i = 0; i < 100; i++) { Sky.advance(1); ks.push(Sky.state().wind.k); }
      return { first: ks[0], last: ks[ks.length - 1], maxStep: Math.max(...ks.slice(1).map((v, i) => Math.abs(v - ks[i]))), monotonic: ks.every((v, i) => !i || v >= ks[i - 1] - 1e-9) };
    });
    out.errors = p.errors;
    await p.context.close();
    // the test link's wind over the real calm (11 km/h simulated, the real 0): the pace it always had
    const link = await weatherPage(browser, url, { query: "?fx=wind", driven: true });
    out.link = await link.page.evaluate(() => { Sky.advance(300); const w = Sky.state().wind; return { k: w.k, speed: w.speed }; });
    // ... and over a real wind above its 11 km/h, the real one rules
    await link.reads({ Wind: 40 });
    out.linkOver = await link.page.evaluate(() => { Sky.advance(300); const w = Sky.state().wind; return { k: w.k, speed: w.speed }; });
    out.linkErrors = link.errors;
    await link.context.close();
  },

  // A reading hovering at a mark doesn't toggle the effect: the fog on from 90 % kept on down to 85 %; the flood has none (its depth, the
  // server's bucket, is slow itself): on from 0.05, off below it; a page load starts with the plain mark
  async hysteresis(browser, url, out) {
    const p = await weatherPage(browser, url, { driven: true });
    const seq = async (key, values, level) => {
      const seen = [];
      for (const v of values) { await p.reads({ [key]: v }); await drive(p.page, 5); seen.push([v, (await weatherSeen(p.page)).levels[level] > 0]); }
      return seen;
    };
    out.fog = await seq("Humidity", [91, 88, 86, 85, 84, 88, 91, 89.9, 85, 84.9], "humidity");
    await p.reads({ Humidity: 50 });
    out.flood = await seq("flood", [0.06, 0.05, 0.049, 0.06, 0.04, 0.2, 0.05, 0.04], "flood");   // (no hysteresis: at 0.05 and over)
    await p.reads({ flood: 0 });
    out.rateOnly = [];   // (the rain rate alone, however heavy, calls for no flood)
    for (const rate of [31, 60, 120]) { await p.reads({ rain_rate: rate }); await drive(p.page, 5); out.rateOnly.push((await weatherSeen(p.page)).levels.flood); }
    await p.reads({ rain_rate: 0 });
    out.errors = p.errors;
    await p.context.close();
    // a new page: 88 % is not enough to start it, nor 27 mm/h
    const q = await weatherPage(browser, url, { driven: true });
    await q.reads({ Humidity: 88, rain_rate: 27, flood: 0.04 });
    await drive(q.page, 5);
    const seen = await weatherSeen(q.page);
    out.fresh = { fog: seen.levels.humidity, flood: seen.levels.flood };
    out.freshErrors = q.errors;
    await q.context.close();
  },

  // The flood's water follows the server's bucket depth (data.flood.depth): its level a depth * 0.25 of the window's, easing; not below
  // 0.05; a frog only from 0.9 of full
  async bucket(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url, { driven: true });
    await page.evaluate(() => Sky.timeScale(4));
    const settle = async (depth) => {
      await reads({ flood: depth });
      await driveUntil(page, (want) => { const f = Sky.state().flood; return want < 0.05 ? f.depth === 0 : f.full > 0 && Math.abs(f.depth - want * f.full) < 0.01; }, depth, 4000);
      return page.evaluate(() => { const f = Sky.state().flood; return { depth: f.depth, full: f.full, frogs: f.frogs, nextFrog: f.nextFrog, since: f.since, level: Sky.state().levels.flood }; });
    };
    out.levels = [];
    for (const d of [0.04, 0.3, 0.6, 0.05, 0.2, 0.04, 0]) out.levels.push({ want: d, ...await settle(d) });
    // (rising: no faster than full / RISE s, draining no faster than full / DRAIN s; eased, not a jump)
    await reads({ flood: 0 });
    await driveUntil(page, () => Sky.state().flood.depth === 0);
    await reads({ flood: 0.5 });
    out.ease = await page.evaluate(() => { const f = Sky.state().flood, c0 = Sky.state().clock; Sky.advance(1); const s = Sky.state(); return { step: s.flood.depth, dt: s.clock - c0, full: f.full }; });
    // (the frog: not in water 0.85 of full, however long; some time at 0.95)
    await settle(0.85);
    await page.evaluate(() => Sky.timeScale(20));
    const t0 = await page.evaluate(() => Sky.state().clock);
    await driveUntil(page, (t0) => Sky.state().clock > t0 + 1500, t0, 20000);
    out.shallow = await page.evaluate(() => { const f = Sky.state().flood; return { frogs: f.frogs, depth: f.depth, full: f.full, nextFrog: f.nextFrog, since: f.since }; });
    await reads({ flood: 0.95 });
    await driveUntil(page, () => Sky.state().flood.frogs >= 1, null, 20000);
    out.deep = await page.evaluate(() => { const f = Sky.state().flood; return { frogs: f.frogs, depth: f.depth, full: f.full }; });
    out.errors = errors;
    await context.close();
  },
  // The haze's opacity from the air sensor's PM2.5 (data.pm25): the curve 1 - exp(-0.01457 pm), continuous across the bands' edges; the
  // band's pattern changing at them; the tint continuous; none below 9 µg/m3; a page's reading low-passed to it and drawn at it
  async pm25(browser, url, out) {
    const { context, page, errors, reads } = await weatherPage(browser, url, { driven: true });
    out.curve = await page.evaluate(() => {
      const of = Sky.state().haze.of, opacity = {}, pms = [0, 5, 8, 8.5, 9, 20, 35, 55, 100, 150, 250, 400, 1000];
      for (const pm of pms) opacity[pm] = of(pm).opacity;
      let worst = 0, tint = 0, last = of(9);
      for (let pm = 9.5; pm <= 400; pm += 0.5) {   // (a step of 0.5 ug/m3: per ug/m3, twice)
        const now = of(pm);
        worst = Math.max(worst, 2 * Math.abs(now.opacity - last.opacity));
        for (let k = 0; k < 2; k++) for (let c = 0; c < 3; c++) tint = Math.max(tint, 2 * Math.abs(now.rgb[k][c] - last.rgb[k][c]));
        last = now;
      }
      return { opacity, worst, tint, steady: [of(5).tint, of(22).tint, of(500).tint, of(900).tint], mids: [of(22.2).pos, of(45.4).pos, of(90.4).pos, of(175.4).pos, of(275.4).pos] };
    });
    const hazeNow = () => page.evaluate(() => { const h = Sky.state().haze, s = Sky.state(); return { effects: s.effects, level: h.level, pm25: h.pm25, opacity: h.opacity, drawn: h.drawn,
                                                                                            tint: h.tint, look: h.look, renders: h.renders, pending: h.pending, settling: h.settling }; });
    const settled = async (readings, want) => {   // (read, the weather's time on until the low-pass and any bake have settled)
      await reads(readings);
      await page.evaluate(() => Sky.timeScale(8));
      await driveUntil(page, (want) => { const h = Sky.state().haze; return (want === null ? h.level === 0 : h.level > 0 && Math.abs(h.pm25 - want) < 0.05) && !h.pending && !h.settling; }, want, 6000);
      await drive(page, 30);
      return hazeNow();
    };
    const meanAlpha = () => page.evaluate(() => { const c = document.querySelector("#sky canvas"), d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data; let a = 0; for (let i = 3; i < d.length; i += 4) a += d[i]; return a / (d.length / 4) / 255; });
    // below 9 none (the AQI Good), the reading in the band's middle: its design look; no sensor reading: the band's midpoint
    await reads({ AQI: 40, pm25: 5 });
    await drive(page, 60);
    out.below = { ...await hazeNow(), mean: await meanAlpha() };
    out.points = {};
    for (const [aqi, pm] of [[55, 9.5], [68, 20], [100, 35], [140, 50], [160, 60], [175, 100], [200, 125], [250, 175], [320, 250], [400, 300]]) {
      out.points[pm] = { ...await settled({ AQI: aqi, pm25: pm }, pm), mean: await meanAlpha() };
    }
    // (the same band, more PM2.5: more opaque, as the curve says; the pattern the same)
    out.sameBand = [];
    for (const [aqi, pm] of [[160, 60], [175, 100]]) out.sameBand.push({ ...await settled({ AQI: aqi, pm25: pm }, pm), mean: await meanAlpha() });
    // (across a band's edge: the pattern changes, the opacity does not jump)
    out.edge = [];
    for (const [aqi, pm] of [[99, 35.3], [101, 35.5]]) out.edge.push({ ...await settled({ AQI: aqi, pm25: pm }, pm), mean: await meanAlpha() });
    // (no PM2.5 reading: the band's midpoint's opacity, its design look)
    out.none = { ...await settled({ AQI: 160 }, 90.4), mean: await meanAlpha() };
    // (no grid in its dither: the least of its peak ratio over several pans; the pan puts a fine grid of its own, the canvas's, in and out of phase)
    out.grids = [];
    for (let i = 0; i < 6; i++) { out.grids.push((await gridPeak(page)).ratio); await drive(page, 300); }
    out.cost = await frameCost(page);
    out.errors = errors;
    await context.close();
  },
  async uturn(browser, url, out) {
    const p = await weatherPage(browser, url, { query: "?fx=wind", driven: true, context: { viewport: { width: 640, height: 480 } } });
    await drive(p.page, 60);
    const size = await p.page.evaluate(() => Sky.state().size);
    const [W, H] = size;
    out.size = size;
    // runs `turns` flips (each to the other way, `frames` frames after a flip), recording what each frame shows
    const run = (spec, turns, frames, watch) => p.page.evaluate(([spec, turns, frames, watch, W, H]) => {
      Sky.setWind(-1);
      Sky.advance(200);   // (any turn settled: leftward)
      const lab = {
        still: spec.still, eddy: spec.eddy && { x: spec.eddy.x * W, y: spec.eddy.y * H, r: spec.eddy.r, spin: spec.eddy.spin },
        leaf: spec.leaf && { x: spec.leaf.x * W, y: spec.leaf.y * H, layer: spec.leaf.layer, pinned: spec.leaf.pinned },
      };
      Sky.lab(lab);
      const frames_ = [];
      let way = -1;
      for (let turn = 0; turn < turns; turn++) {
        way = -way;
        Sky.setWind(way);
        const log = [];
        for (let i = 0; i < frames; i++) {
          Sky.advance(1);
          const s = Sky.state(), l = s.wind.leafStates[0];
          const rec = { f: s.windFactor, side: s.windDir, veer: s.windVeerAngle, veerSide: s.windVeerSide, leaf: l && { ...l } };
          for (const [name, x, y] of watch) { const a = Sky.air(x * W, y * H, 0); rec[name] = { eddy: a.eddy, curl: a.curl, strength: a.strength }; }
          log.push(rec);
        }
        frames_.push(log);
      }
      Sky.lab(null);
      return frames_;
    }, [spec, turns, frames, watch, W, H]);
    const norm = (v) => Math.hypot(v[0], v[1]);
    // 1. the eddy's swirl (turbulence off): at points round the eye, through the flip: the same sense, the same strength, the same velocity
    const swirl = await run({ still: true, eddy: { x: 0.5, y: 0.5, r: 36, spin: 180 } }, 2, 330,
                            [["east", 0.5 + 36 / W, 0.5], ["north", 0.5, 0.5 - 36 / H], ["eye", 0.5, 0.5]]);
    const all = swirl.flat();
    const first = all[0];
    out.swirl = {};
    for (const name of ["east", "north"]) {
      const v0 = norm(first[name].eddy);
      out.swirl[name] = { curls: [Math.min(...all.map((r) => Math.sign(r[name].curl))), Math.max(...all.map((r) => Math.sign(r[name].curl)))],
                          ratio: [Math.min(...all.map((r) => norm(r[name].eddy) / v0)), Math.max(...all.map((r) => norm(r[name].eddy) / v0))],
                          drift: Math.max(...all.map((r) => Math.hypot(r[name].eddy[0] - first[name].eddy[0], r[name].eddy[1] - first[name].eddy[1]))) / v0, v0 };
    }
    // (the eddy's sense of turning: the velocity east of the eye going down or up, north of it left or right, and the curl at the eye)
    out.swirl.sense = { east: [Math.min(...all.map((r) => Math.sign(r.east.eddy[1]))), Math.max(...all.map((r) => Math.sign(r.east.eddy[1])))],
                        north: [Math.min(...all.map((r) => Math.sign(r.north.eddy[0]))), Math.max(...all.map((r) => Math.sign(r.north.eddy[0])))],
                        eye: [Math.min(...all.map((r) => Math.sign(r.eye.curl))), Math.max(...all.map((r) => Math.sign(r.eye.curl)))],
                        eyeRatio: [Math.min(...all.map((r) => r.eye.curl / first.eye.curl)), Math.max(...all.map((r) => r.eye.curl / first.eye.curl))] };
    out.swirl.sides = [...new Set(all.map((r) => r.side))];
    out.swirl.factors = [Math.min(...all.map((r) => r.f)), Math.max(...all.map((r) => r.f))];
    // (and with the turbulence on, at the eye: the eddy still turns the way it turned, as the air's own turbulence is the lesser)
    const rough = await run({ eddy: { x: 0.5, y: 0.5, r: 36, spin: 180 } }, 2, 330, [["eye", 0.5, 0.5]]);
    out.swirl.rough = [Math.min(...rough.flat().map((r) => Math.sign(r.eye.curl))), Math.max(...rough.flat().map((r) => Math.sign(r.eye.curl)))];
    // 2. a leaf in the eddy, over four turns (left to right and back, twice): its mean wind's heading and speed, the path of the mean, up and down
    const leaf = await run({ still: true, eddy: { x: 0.5, y: 0.5, r: 36, spin: 180 }, leaf: { x: 0.5 + 30 / W, y: 0.5, layer: 0, pinned: true } }, 3, 330, []);
    const turned = leaf.map((log) => {
      const dt = 1 / 30, mean = log.map((r) => r.leaf.mean), speed = mean.map(norm);
      const heading = mean.map((m) => Math.atan2(-m[1], m[0]));
      let total = 0, signed = 0;
      for (let i = 1; i < heading.length; i++) { let d = heading[i] - heading[i - 1]; d = Math.atan2(Math.sin(d), Math.cos(d)); total += Math.abs(d); signed += d; }
      let y = 0, x = 0, upMost = 0;   // (the mean's own path: screen down positive)
      for (const m of mean) { x += m[0] * dt; y += m[1] * dt; upMost = Math.min(upMost, y); }
      return { veerSide: log[log.length - 1].veerSide, speeds: [Math.min(...speed), speed[0], Math.max(...speed)], total, signed,
               pathY: [upMost, y, Math.max(...log.map((r, i) => 0))], eddy: [Math.min(...log.map((r) => r.leaf.eddy)), Math.max(...log.map((r) => r.leaf.eddy))],
               headings: [heading[0], heading[heading.length - 1]], path: log.map((r) => [r.leaf.x, r.leaf.y]), meanXs: [mean[0][0], mean[mean.length - 1][0]],
               sides: [...new Set(log.map((r) => r.side))] };
    });
    out.leaf = turned;
    // 3. a leaf outside any eddy, going straight: its mean wind reverses along its line, through calm
    const straight = await run({ still: true, leaf: { x: 0.5, y: 0.5, layer: 1, pinned: true } }, 2, 330, []);
    out.straight = straight.map((log) => {
      const mean = log.map((r) => r.leaf.mean);
      return { eddy: Math.max(...log.map((r) => r.leaf.eddy)), ys: Math.max(...mean.map((m) => Math.abs(m[1]))), xs: [mean[0][0], Math.min(...mean.map((m) => Math.abs(m[0]))), mean[mean.length - 1][0]],
               monotonic: mean.every((m, i) => i === 0 || (mean[0][0] < 0 ? m[0] >= mean[i - 1][0] - 1e-9 : m[0] <= mean[i - 1][0] + 1e-9)) };
    });
    // a free straight leaf's path, for the picture
    const free = await run({ still: true, leaf: { x: 0.8, y: 0.5, layer: 1 } }, 2, 330, []);
    out.freePaths = free.map((log) => log.map((r) => [r.leaf.x, r.leaf.y]));
    // 4. a leaf in a weak eddy (pinned where its strength is about a half): its mean wind half way between the straight and the veer
    const half = await p.page.evaluate(([W, H]) => {
      Sky.setWind(-1); Sky.advance(200);
      Sky.lab({ still: true, eddy: { x: 0.5 * W, y: 0.5 * H, r: 36, spin: 180 } });
      let best = null;
      for (let d = 30; d < 160; d += 1) {
        const a = Sky.air(0.5 * W + d, 0.5 * H, 0);
        if (!best || Math.abs(a.strength - 0.5) < Math.abs(best.strength - 0.5)) best = { d, strength: a.strength };
      }
      Sky.lab(null);
      return best;
    }, [W, H]);
    out.half = half;
    const blend = await run({ still: true, eddy: { x: 0.5, y: 0.5, r: 36, spin: 180 }, leaf: { x: 0.5 + half.d / W, y: 0.5, layer: 0, pinned: true } }, 1, 330, []);
    out.blend = blend[0].map((r) => ({ w: r.leaf.eddy, f: r.f, mean: r.leaf.mean, straight: r.leaf.straight, veer: r.leaf.veer }));
    await p.context.close();
    // 5. the paths: on a wider window, a leaf in a drifting eddy through a turn (the first: up; and the second: down) and a leaf in none
    // (two turns), each frame's place, the eddy's too (for a picture of them)
    const q = await weatherPage(browser, url, { query: "?fx=wind", driven: true, context: { viewport: { width: 1400, height: 720 } } });
    await drive(q.page, 60);
    out.paths = await q.page.evaluate(() => {
      const [W, H] = Sky.state().size, r = { size: [W, H], turns: [], straight: [] };
      const settle = (way) => { Sky.setWind(way); Sky.advance(300); };
      const record = (frames) => {
        const log = [];
        for (let i = 0; i < frames; i++) {
          Sky.advance(1);
          const s = Sky.state(), l = s.wind.leafStates[0], e = s.wind.eddyStates[0];
          log.push({ x: l.x, y: l.y, eddy: e && [e.x, e.y], f: s.windFactor, veer: s.windVeerAngle, side: s.windVeerSide });
        }
        return log;
      };
      Sky.setWind(-1); Sky.advance(300);
      for (const way of [1, -1, 1]) {   // (the eddy drifting the way the wind was blowing, its leaf in the ring on its upwind side)
        const from = way > 0 ? 0.85 : 0.15;
        Sky.lab({ eddy: { x: from * W, y: 0.5 * H, r: 36, spin: 180, drift: true, u: 45 }, leaf: { x: from * W + (way > 0 ? 30 : -30), y: 0.5 * H, layer: 0 }, still: true });
        Sky.advance(20);
        Sky.setWind(way);
        r.turns.push(record(270));
        Sky.lab(null);
        settle(way);
      }
      for (const way of [-1, 1]) {   // (it ended rightward)
        Sky.lab({ leaf: { x: 0.5 * W, y: 0.5 * H, layer: 1 }, still: true });
        Sky.setWind(way);
        r.straight.push(record(240));
        Sky.lab(null);
        settle(way);
      }
      return r;
    });
    await q.context.close();
    out.errors = [...p.errors, ...q.errors];
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
    else if (second === "--arrows") await arrowsMode(browser, url, third);
    else if (PARTED[second]) {   // (PART, or several joined by "+": side by side, each its own page, a report each, in a list)
      const reports = await Promise.all(third.split("+").map((part) => PARTED[second](browser, url, part)));
      process.stdout.write(JSON.stringify(third.includes("+") ? reports : reports[0]) + "\n");
    }
    else if (second === "--rotate") await rotateMode(browser, url);
    else if (second === "--fill") await fillMode(browser, url, JSON.parse(third));
    else if (second === "--weather") await weatherMode(browser, url, third);
    else await sizes(browser, url, second, JSON.parse(third));
  } finally {
    await browser.close();
  }
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
