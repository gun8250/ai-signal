# Content Sources

Central feed is updated daily at 6am Beijing time (UTC 22:00) with:

### CN investor forums (subscriber-side local source, not central)
`generate_cn_forums.py` runs locally before `prepare_digest.py` and writes
`feeds/feed-cn-forums.json`. Sources and anti-bot notes:

- **NGA 杂谈 (fid=510567, 大时代话题集合)** — guest access blocked by a JS
  challenge; the cookie value is embedded in the 403 body's script, extracted
  and retried with `&rand=` plus cleared `lastpath`/`ngaPassportUid` cookies.
  A `Referer: https://bbs.nga.cn/` header is required.
- **集思录** — `/explore/` is a server-rendered stream sorted by last activity;
  plain GET works. (The `/topic/{category}` pages sort by all-time hotness and
  mostly surface old threads — not used.)
- **虎扑股票区** (`bbs.hupu.com/stock`) — server-rendered list page, plain GET.
- **雪球** (`xueqiu.com`) — hardest of the four. Main domain sits behind an
  Aliyun WAF: a 75KB obfuscated JS challenge plus a per-URL `md5__1038`
  signature parameter, and the app layer needs `xq_a_token` cookies (seeded by
  GET `/about`). Cookie reuse across requests does NOT work (signature is
  per-URL), so the only stable channel is a real browser: `xueqiu_fetch.mjs`
  (playwright-core + system Edge/Chrome, headless) warms up on `/about`, lets
  the challenge self-solve, then same-origin `fetch()`es the search API
  `/query/v1/search/status.json?q=<kw>&sort=time` per keyword. Quotes APIs on
  `stock.xueqiu.com` do not sit behind the WAF but carry no discussions.
  Falls back gracefully: if node/playwright/browser is missing, the xueqiu
  board reports an error and the other three boards still ship.
  **Hot-list caveat (found 2026-09-21):** `statuses/hot/listV2.json` rows can
  pair one post's id/target with ANOTHER post's title/description (server-side
  field mismatch). The collector therefore canonicalizes every hot item via
  `statuses/show.json?id=<id>` in the same browser session before emitting it.

Filtering (mirrors the central Twitter tier philosophy): theme keyword gates
for commodities / cyclicals / value investing, a hard exclude list for ads /
recruitment / emotional spam, standing mega-thread patterns (超短楼/技术分析
大楼) dropped, engagement floors (NGA ≥5 replies, jisilu/hupu ≥3 replies or
≥500 views, xueqiu like+2×reply ≥10), and a 14-day activity window. Threads
are ranked by the communities' own voting (replies + views/100).

**当日热榜 (per-board hot TOP5, grouped)**: besides theme-filtered topics,
each board reports its own hot list (NGA by replies, jisilu homepage 热门
block, hupu `/stock-hot` = the site's 24h rank, xueqiu
`statuses/hot/listV2.json`). The feed keeps them **grouped per board** in
`hot_boards: [{source, source_name, board_label, items}]` with per-board
`hot_rank` — NO cross-board merging or scoring, because the heat metrics are
not comparable (NGA/jisilu/hupu numbers are all-time accumulated replies;
xueqiu's hot list only carries a few hours of same-day likes/replies).
Guards: standing mega-threads with >3000 replies are dropped; xueqiu items
need like+reply+retweet ≥10 and use the post text head as title (hot posts
often carry no title field). Cap per board is `--hot-per-board` (default 5).

### Podcasts (14 channels)
Dwarkesh Patel, Lex Fridman, Latent Space, All-In Podcast, a16z, Naval, No Priors,
SemiAnalysis (Dylan Patel), Google DeepMind, Y Combinator Startup Podcast, Lenny's Podcast,
Invest Like the Best, Capital Allocators, The Acquirers Podcast

### People tracking (28 people, YouTube-wide guest search)
Beyond the fixed channels, the central feed searches YouTube daily for these
people appearing as podcast/interview **guests** anywhere, limited server-side
to videos uploaded in the past week. Channels under 50k subscribers are
rejected (small re-upload accounts), and for overseas people, channels or
titles in a non-Latin script are rejected too (large foreign-language
dub/reaction channels carry no English transcript and aren't real interviews).
As a definitive backstop, an overseas-person video with no English caption
track at all is rejected — this catches foreign shows that use an English title
(e.g. Jensen Huang on the Korean variety show You Quiz on the Block, captions
only in Korean). Only English originals get through. Videos that merely talk
ABOUT the person are rejected too — a title whose grammar puts the name in
topic position ("Journalist Karen Hao on Sam Altman...", "the truth about X")
is coverage, not an appearance; only videos where the person actually speaks
count. Hits merge into the same
podcast feed with a `person` field (and `region: "cn"` for China AI voices,
which are exempt from both filters).

**Overseas:** Sundar Pichai, Greg Brockman, Sam Altman, Demis Hassabis, Jensen Huang,
Satya Nadella, Mark Zuckerberg; Anthropic (Dario/Daniela Amodei, Krishna Rao,
Mike Krieger, Sholto Douglas, Amanda Askell, Boris Cherny, Cat Wu, Alex Albert);
Kevin Weil (OpenAI), Ivan Zhao (Notion), Dylan Patel (SemiAnalysis), Gavin Baker (Atreides),
Naval Ravikant

**China AI:** 闫俊杰 (MiniMax), 杨植麟 (Moonshot), 梁文锋 (DeepSeek), 唐杰 (智谱),
罗福莉, 李广密 (拾象), 肖弘 (Manus)

### Twitter/X (18 accounts)
**Analysts:** Karpathy, Swyx, Dylan Patel (SemiAnalysis), Irrational Analysis, Naval Ravikant,
Jim Keller, Gavin Baker (Atreides Management)
**Executives:** Sam Altman, Dario Amodei, Demis Hassabis (Google DeepMind), Tang Jie (Z.ai),
Jensen Huang (NVIDIA CEO)
**Builders:** Amanda Askell, Boris Cherny (Claude Code), Cat Wu, Alex Albert, Guillermo Rauch (Vercel), Josh Woodward (Google Labs)

Analysts and Executives are *judgment tiers* (`judgment_tiers` in
`config/sources.json`): their posts skip the topic keyword gate and only social
noise is dropped, because judgment is written in plain language. Builders keep
the keyword gate — they post product announcements, which always name a product.
Quote tweets are fetched together with the post they quote, and the quoted text
participates in both filtering and rendering.

### arXiv Papers (daily, up to 30)
cs.AI (Artificial Intelligence), cs.CL (Computation and Language), cs.LG (Machine Learning)

All feeds are fetched centrally. **No API keys needed for content.**
