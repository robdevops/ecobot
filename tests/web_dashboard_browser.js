// The dashboard page in Playwright's Chromium, for tests/test_web_dashboard.py.
//   node web_dashboard_browser.js URL SHOTS SIZES: the page at each size ({name: [width, height]}): a screenshot of each (SHOTS/dashboard-
//     NAME.png, device scale factor 2) and, on stdout, {name: {tiles, rects, columns, scroll, scrollWidth, scrollHeight, status, errors}}.
//   node web_dashboard_browser.js URL --live: the page open at 800x600; prints {label} (the first tile's), waits for a line on stdin
//     (the test has changed the reading), then prints {label, same_page, errors} once the tile shows it, or after 15 s.
//   node web_dashboard_browser.js URL --arrive TITLE: the page open at 1024x768; prints the tile's state ({label, faded, opacity, shown,
//     needle}), waits for a line on stdin (the test has given it a reading, or taken it away), then, once its label changes, samples it
//     every frame for 1.5 s and prints {before, after, frames: [{opacity, number}], same_tile, errors}.
"use strict";
const { chromium } = require("playwright");
const readline = require("readline");

async function open(browser, url, width, height) {
  const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 });
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

(async () => {
  const [url, second, third] = process.argv.slice(2);
  const browser = await chromium.launch();
  try {
    if (second === "--live") await live(browser, url);
    else if (second === "--arrive") await arrive(browser, url, third);
    else await sizes(browser, url, second, JSON.parse(third));
  } finally {
    await browser.close();
  }
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
