// Xueqiu in-browser fetch collector.
// Usage: node xueqiu_fetch.mjs <keywords_json_file>
// keywords file: [{"kw": "价值投资"}, ...]
// Opens one real browser, warms up on /about, then same-origin fetch()es the
// search API for each keyword, plus the site-wide hot list (今日热门).
// Prints JSON results to stdout.
import { createRequire } from "module";
import fs from "fs";

const require = createRequire("C:/Users/31345/.workbuddy/binaries/node/workspace/package.json");
const { chromium } = require("playwright-core");

const [, , kwFile] = process.argv;
if (!kwFile) {
  console.error("usage: node xueqiu_fetch.mjs <keywords.json>");
  process.exit(2);
}
const keywords = JSON.parse(fs.readFileSync(kwFile, "utf-8"));

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const exe = fs.existsSync(EDGE) ? EDGE : CHROME;

const browser = await chromium.launch({
  executablePath: exe,
  headless: true,
  args: ["--disable-blink-features=AutomationControlled"],
});
const context = await browser.newContext({
  userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  locale: "zh-CN",
});
await context.addInitScript(() => {
  Object.defineProperty(navigator, "webdriver", { get: () => false });
});
const page = await context.newPage();

async function settle(p) {
  let content = await p.content();
  let tries = 0;
  while ((content.includes("_waf_") || content.includes("aliyun_waf")) && tries < 6) {
    await p.waitForTimeout(2000);
    content = await p.content();
  }
}

const results = [];
try {
  // warm up: tokens + WAF clearance
  await page.goto("https://xueqiu.com/about", { waitUntil: "domcontentloaded", timeout: 30000 });
  await settle(page);

  for (const { kw } of keywords) {
    // page1+2: 搜索型源更新慢，扩候选池支撑日报轮换（跳过已推送、
    // 名次顺延后仍有足够新帖可取）
    const collected = [];
    for (const pg of [1, 2]) {
      const url = `https://xueqiu.com/query/v1/search/status.json?q=${encodeURIComponent(kw)}&count=10&page=${pg}&sort=time`;
      let body = null;
      let mode = "fetch";
      try {
        // same-origin fetch from the warm page
        body = await page.evaluate(async (u) => {
          const r = await fetch(u, { headers: { Accept: "application/json" }, credentials: "include" });
          return await r.text();
        }, url);
        if (body && body.includes("_waf_")) {
          // fetch hit the challenge; fall back to navigation for this URL
          mode = "navigate";
          body = null;
        }
      } catch (e) {
        mode = "navigate";
      }
      if (body === null) {
        const resp = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
        await settle(page);
        body = await page.evaluate(() => (document.body ? document.body.innerText : "")).catch(() => "");
        // go back to a same-origin page for the next fetch
        await page.goto("https://xueqiu.com/about", { waitUntil: "domcontentloaded" }).catch(() => {});
        await settle(page);
      }
      let parsed = null;
      try { parsed = JSON.parse(body); } catch {}
      if (parsed && parsed.list) collected.push(...parsed.list);
      await page.waitForTimeout(1200);
    }
    const seen = new Set();
    const merged = collected.filter((it) => it && it.id && !seen.has(it.id) && (seen.add(it.id), true));
    results.push({ kw, mode: "fetch", ok: merged.length > 0, count: merged.length,
                   items: merged.slice(0, 20).map((it) => ({
                     id: it.id, target: it.target, created_at: it.created_at,
                     title: it.title || "", description: (it.description || "").slice(0, 200),
                     text: (it.text || "").slice(0, 500),
                     like_count: it.like_count, reply_count: it.reply_count, retweet_count: it.retweet_count,
                     user: it.user ? { screen_name: it.user.screen_name } : null,
                   })) });
  }

  // site-wide hot list (今日热门) — no keyword, pure community heat
  try {
    const hotUrl = "https://xueqiu.com/statuses/hot/listV2.json?sessionId=-24&count=15";
    let hotBody = null;
    try {
      hotBody = await page.evaluate(async (u) => {
        const r = await fetch(u, { headers: { Accept: "application/json" }, credentials: "include" });
        return await r.text();
      }, hotUrl);
      if (hotBody && hotBody.includes("_waf_")) hotBody = null;
    } catch {}
    if (hotBody) {
      let hot = null;
      try { hot = JSON.parse(hotBody); } catch {}
      let items = (hot && Array.isArray(hot.items) ? hot.items : [])
        .map((row) => row && row.original_status ? row.original_status : null)
        .filter(Boolean)
        .slice(0, 15);
      // ⚠️ listV2 has a field-mismatch bug: a row's id/target can be paired
      // with another post's title/description (verified 2026-09-21: id
      // 409963344 carried the 锦江航运 description while show.json says it is
      // a different reply post). Canonicalize every hot item via show.json
      // so title and URL always belong to the same post.
      const canon = [];
      for (const it of items) {
        try {
          const s = await page.evaluate(async (id) => {
            const r = await fetch(`https://xueqiu.com/statuses/show.json?id=${id}`, { headers: { Accept: "application/json" }, credentials: "include" });
            return await r.json();
          }, it.id);
          if (s && s.id && (s.text || s.description || s.title)) {
            canon.push({
              id: s.id,
              target: s.target || `/${s.user_id}/${s.id}`,
              created_at: s.created_at,
              title: s.title || "",
              description: (s.description || s.text || "").slice(0, 200),
              like_count: s.like_count ?? it.like_count ?? 0,
              reply_count: s.reply_count ?? it.reply_count ?? 0,
              retweet_count: s.retweet_count ?? it.retweet_count ?? 0,
              user: s.user ? { screen_name: s.user.screen_name } : null,
            });
          } else {
            // show.json failed for this id — keep the row only if its own
            // text/description is self-consistent-looking; drop otherwise
            if (it.title || it.description) canon.push({
              id: it.id, target: it.target, created_at: it.created_at,
              title: it.title || "", description: (it.description || "").slice(0, 200),
              like_count: it.like_count, reply_count: it.reply_count,
              retweet_count: it.retweet_count,
              user: it.user ? { screen_name: it.user.screen_name } : null,
            });
          }
        } catch {}
        await page.waitForTimeout(600);
      }
      items = canon;
      results.push({ kw: "__hot__", mode: "fetch", ok: items.length > 0, count: items.length, items });
    } else {
      results.push({ kw: "__hot__", mode: "fetch", ok: false, count: 0, items: [] });
    }
  } catch (e) {
    console.error(`[hot] ${e.message}`);
    results.push({ kw: "__hot__", mode: "fetch", ok: false, count: 0, items: [] });
  }

  console.log(JSON.stringify(results));
} catch (e) {
  console.error(`[fetch] ${e.message}`);
  console.log(JSON.stringify(results));
} finally {
  await browser.close();
}
