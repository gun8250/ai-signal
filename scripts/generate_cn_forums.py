# -*- coding: utf-8 -*-
"""CN forums feed generator — NGA 杂谈(大时代) + 集思录 + 虎扑股票区.

Local subscriber-side source (not central). Fetches three Chinese investor
communities and writes feeds/feed-cn-forums.json in the same shape as the
central feeds; the digest pipeline treats it like any other source.

Design mirrors generate_feed.py philosophy:
  - engagement gate (replies/views as the community's own voting)
  - keyword gates tuned per theme (commodities / cyclicals / value investing)
  - hard exclude for ads, recruitment, emotional chatter, standing mega-threads
  - rolling window snapshot; dedup happens client-side in prepare_digest.py

Anti-bot handling:
  - NGA: solve the guestJs JS-challenge embedded in the 403 body — the cookie
    value is printed in the challenge script; extract it, clear lastpath/
    ngaPassportUid cookies (as the JS does), retry with a &rand= cache-buster
  - jisilu / hupu: plain SSR pages, browser UA + Referer suffice

Usage:
    python scripts/generate_cn_forums.py [--boards nga,jisilu,hupu] [--max-per-board N]

Rotation (2026-09-22): reads ~/.ai-signal/seen.json and skips topics already
delivered in past digests — the next candidate in line is promoted, so each
board serves fresh content every day. When a board's fresh pool runs dry,
a backfill pool (same topic/time gates, relaxed engagement floor) tops it up.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
FEEDS_DIR = ROOT_DIR / "feeds"

# 已推送话题记录（mark_delivered.py 维护）——抓取阶段即感知，已推送的
# 直接跳过、名次顺延，保证每个板块每天给日报留的是没见过的新内容。
SEEN_PATH = Path.home() / ".ai-signal" / "seen.json"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

CST = timezone(timedelta(hours=8))

# ----------------------------------------------------------------------------
# Theme keyword gates — 用户关注：大宗商品、周期股、价值投资
# ----------------------------------------------------------------------------
THEME_KEYWORDS = {
    "commodities": [
        "原油", "石油", "油价", "天然气", "煤炭", "焦煤", "焦炭", "动力煤",
        "铁矿石", "铁矿", "铜价", "铜矿", "电解铜", "铝价", "电解铝", "锌", "镍",
        "黄金", "白银", "贵金属", "金价", "大宗商品", "商品期货", "期货", "豆粕",
        "豆油", "棕榈油", "棉花", "白糖", "橡胶", "螺纹钢", "钢材", "钢铁", "水泥",
        "玻璃", "纯碱", "尿素", "化肥", "磷化工", "碳酸锂", "稀土",
        "运价", "油运", "航运", "BDI", "VLCC", "集运", "散运", "乙烷", "LNG",
    ],
    "cyclical": [
        "周期股", "周期", "猪周期", "养殖", "生猪", "猪价", "白羽鸡", "水产",
        "化工", "石化", "炼化", "钛白粉", "有机硅", "农药", "万华",
        "有色", "紫金", "洛阳钼业", "江西铜业", "中国铝业", "云铝", "金铜",
        "船舶", "造船", "中远海控", "中远海能", "招商轮船", "招商南油", "油轮",
        "海螺水泥", "中国神华", "陕西煤业", "兖矿", "露天煤业", "中石油", "中石化", "中海油",
        "地产链", "房地产", "建材", "工程机械", "重卡", "挖掘机",
        "净息差", "银行股", "保险股", "券商股", "牧原", "温氏",
        "库存周期", "产能出清", "供给侧", "资本开支", "开工率", "产能利用率",
    ],
    "value": [
        "价值投资", "价投", "估值", "市盈率", "PE", "PB", "股息率", "分红", "股息",
        "自由现金流", "FCF", "护城河", "安全边际", "低估", "高估", "戴维斯双击",
        "格雷厄姆", "巴菲特", "芒格", "彼得林奇", "霍华德", "段永平", "施洛斯",
        "ROE", "净资产收益率", "商业模式", "现金流", "复利", "能力圈",
        "老登股", "红利", "中特估", "破净", "基本面", "财报", "年报",
        "业绩预告", "业绩快报", "回购", "注销式回购", "折价", "溢价", "套利",
    ],
}

# 广告 / 引流 / 情绪宣泄——无论命中什么主题词，直接排除
EXCLUDE_KEYWORDS = [
    "开户", "佣金", "万1", "万一免五", "开户福利", "邀请码", "返现", "返佣",
    "培训课", "荐股", "带单", "喊单", "群号", "加群", "加微信", "微信号",
    "招聘", "求职", "简历", "兼职", "一夜暴富", "小白学",
]

# 常驻大楼/直播贴——每天内容都在变，作为"信源"无增量，排除（想看随时可去）
STANDING_THREAD_PATTERNS = [
    r"\[股市\]技术分析", r"超短楼", r"大楼", r"每日复盘", r"今日话题",
    r"实盘楼", r"交易楼", r"讨论楼", r"期货.*楼$", r"楼\d{4}",
]

# 弱命中（单个主题词）需要确认词，避免「银行」两个字就进来一堆水贴
CONFIRM_WORDS = ["讨论", "分析", "逻辑", "看法", "观点", "记录", "研究", "思考",
                 "疑问", "请教", "为什么", "怎么看", "深度", "复盘", "总结",
                 "展望", "策略", "机会", "风险", "对比", "回测", "数据", "标的",
                 "买", "卖", "持仓", "仓位", "行情", "涨", "跌", "价格", "业绩",
                 "利好", "利空", "财报", "半年报", "年报", "选", "值不值", "还能"]

# 单个出现即算强信号的词
STRONG_WORDS = {"原油", "铁矿石", "黄金", "煤炭", "价值投资", "价投", "股息率", "ROE",
                "护城河", "安全边际", "戴维斯双击", "巴菲特", "库存周期", "供给侧",
                "大宗商品", "油运", "航运", "造船", "周期股", "猪周期", "招商轮船",
                "中远海能", "中国神华", "紫金", "破净", "中特估", "注销式回购"}


def configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def clean_text(text):
    return "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


def clean_data(value):
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_data(item) for item in value]
    if isinstance(value, dict):
        return {clean_data(k): clean_data(v) for k, v in value.items()}
    return value


# 递补保底线：板块新鲜量不足此数时用递补池补齐（不超 max_per_board）
BACKFILL_FLOOR = 5


def rotate_fresh(all_items, backfill_pool, delivered_ids, max_per_board, floor=BACKFILL_FLOOR):
    """轮换核心：已推送的剔除、名次顺延，板块不足保底线时递补。

    Returns (fresh_items, rotation_stats)。
    - 主池候选按原序（≈互动序）保留未推送项，每板截回 max_per_board
    - 递补池只在某板新鲜量 < floor 时启用，最多补到 floor
    - 完全不碰 hot 池（热榜轮换在 main 内单独处理）
    """
    per_board = {}
    skipped = 0
    for it in all_items:
        if it["id"] in delivered_ids:
            skipped += 1
            continue
        per_board.setdefault(it["source"], []).append(it)

    capped = {}
    for src, items in per_board.items():
        capped[src] = items[:max_per_board]

    fresh_count = {src: len(v) for src, v in capped.items()}
    topped_up = []
    for src, pool in backfill_pool.items():
        deficit = min(floor, max_per_board) - fresh_count.get(src, 0)
        if deficit <= 0:
            continue
        candidates = [b for b in pool if b["id"] not in delivered_ids]
        take = candidates[:deficit]
        if take:
            topped_up.extend(take)

    fresh_items = [it for items in capped.values() for it in items] + topped_up
    stats = {
        "seen_ids": len(delivered_ids),
        "skipped_delivered": skipped,
        "backfilled": len(topped_up),
        "fresh_per_board": {s: len(v) for s, v in capped.items()},
    }
    return fresh_items, stats


def log(msg):
    print(msg, file=sys.stderr)


def load_seen_forum_ids():
    """cn_forums 已推送 id 集合（seen.json 由 mark_delivered.py 写入）。"""
    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        return set(data.get("cn_forums", {}).keys())
    except Exception:
        return set()


def hit_keywords(text, words):
    return any(w in text for w in words)


def theme_score(title):
    scored = {}
    for theme, words in THEME_KEYWORDS.items():
        hits = sum(1 for w in words if w in title)
        if hits:
            scored[theme] = hits
    if not scored:
        return 0, None
    best = max(scored, key=scored.get)
    return scored[best], best


def classify(title):
    """Keep/drop one thread title. Returns (keep, theme, reason)."""
    t = title.strip()
    if len(t) < 6:
        return False, None, "too_short"
    if hit_keywords(t, EXCLUDE_KEYWORDS):
        return False, None, "excluded"
    if any(re.search(p, t) for p in STANDING_THREAD_PATTERNS):
        return False, None, "standing_thread"
    score, theme = theme_score(t)
    if score == 0:
        return False, None, "off_topic"
    if score == 1 and not hit_keywords(t, CONFIRM_WORDS) and not hit_keywords(t, STRONG_WORDS):
        return False, theme, "weak_signal"
    return True, theme, "ok"


def iso_from_unix(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=CST).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def parse_hupu_time(text, now):
    """Parse 'MM-DD HH:MM' with year inference (no DeprecationWarning)."""
    m = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2})$", text.strip())
    if not m:
        return None
    mon, day, hh, mm = (int(x) for x in m.groups())
    try:
        dt = datetime(now.year, mon, day, hh, mm, tzinfo=CST)
    except ValueError:
        return None
    if dt > now + timedelta(days=1):  # 12-31 帖子出现在 1 月初
        try:
            dt = datetime(now.year - 1, mon, day, hh, mm, tzinfo=CST)
        except ValueError:
            return None
    return dt.isoformat()


# 滚动时间窗：出现在日报里的帖子，最后活动时间不超过 14 天
# （集思录分类页按热度排序会翻出沉帖；虎扑/NGA 第一页本身即最新，
#  但虎扑的"最新回复"排序也会带出老帖，统一用时间窗兜底）
MAX_AGE_DAYS = 14


def within_window(published, now=None):
    if not published:
        return True  # 无法判定时间时保守放行，交给互动门槛把关
    now = now or datetime.now(CST)
    try:
        dt = datetime.fromisoformat(published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return (now - dt) <= timedelta(days=MAX_AGE_DAYS)
    except ValueError:
        return True


# ----------------------------------------------------------------------------
# NGA — guestJs JS-challenge + SSR board list
# ----------------------------------------------------------------------------

NGA_FID = 510567  # 杂谈——大时代话题集合所在版面

NGA_ROW_RE = re.compile(
    r"href='/read\.php\?tid=(\d+)' target='_blank' class='replies'>(\d+)</a></td>"
    r"\s*<td class='c2'>\s*<a [^>]*class='topic'>([^<]{4,100})</a>"
    r".*?class='silver postdate'[^>]*>(\d{9,10})<",
    re.S,
)


def nga_solve_and_fetch(client, url, retries=2):
    """Fetch an NGA URL, solving the guestJs JS-challenge when 403."""
    for attempt in range(retries + 1):
        r = client.get(url + ("&" if "?" in url else "?") + f"rand={int(time.time())}{attempt}")
        r.encoding = "gb18030"
        if r.status_code == 200:
            return r
        m = re.search(r"guestJs=([0-9a-zA-Z_]+);", r.text or "")
        if m:
            # mirror the challenge JS: clear lastpath, set guestJs, retry
            for k in ("lastpath", "lastvisit", "ngaPassportUid"):
                try:
                    client.cookies.delete(k, domain="bbs.nga.cn")
                except Exception:
                    pass
            client.cookies.set("guestJs", m.group(1), domain="bbs.nga.cn")
            log(f"  nga: solved challenge guestJs={m.group(1)}")
            continue
        break
    return r


def fetch_nga(client, max_items, include_hot=False, hot_limit=5):
    log("fetching NGA 杂谈 fid=510567 ...")
    r = nga_solve_and_fetch(client, f"https://bbs.nga.cn/thread.php?fid={NGA_FID}")
    if r.status_code != 200:
        raise RuntimeError(f"nga status={r.status_code}")
    items = []
    # 主池：热帖门槛回复 >= 5；递补池：回复 >= 2（同主题/同排除规则）
    backfill = []
    for tid, replies, title, ts in NGA_ROW_RE.findall(r.text):
        title = unescape(clean_text(title.strip()))
        keep, theme, reason = classify(title)
        if not keep:
            continue
        replies_i = int(replies)
        published = iso_from_unix(ts)
        entry = {
            "id": f"nga:{tid}",
            "source": "nga",
            "source_name": "NGA·杂谈(大时代)",
            "board": "大时代",
            "title": title,
            "url": f"https://bbs.nga.cn/read.php?tid={tid}",
            "published": published,
            "summary": "",
            "replies": replies_i,
            "views": None,
            "theme": theme,
        }
        if replies_i >= 5:
            items.append(entry)
        elif replies_i >= 2:
            backfill.append(entry)
    log(f"  nga: kept {len(items)} threads (+{len(backfill)} backfill)")
    # 热度榜：不设关键词门槛（用户点名要当日全站热度，不只投资相关），
    # 排除广告词，按互动取前 N
    if include_hot:
        hot = []
        for tid, replies, title, ts in NGA_ROW_RE.findall(r.text):
            title = unescape(clean_text(title.strip()))
            if len(title) < 6 or hit_keywords(title, EXCLUDE_KEYWORDS):
                continue
            if any(re.search(p, title) for p in STANDING_THREAD_PATTERNS):
                continue
            replies_i = int(replies)
            if replies_i < 5:
                continue
            hot.append({
                "id": f"nga:{tid}",
                "source": "nga",
                "source_name": "NGA·杂谈(大时代)",
                "board": "大时代·24h热榜",
                "title": title,
                "url": f"https://bbs.nga.cn/read.php?tid={tid}",
                "published": iso_from_unix(ts),
                "summary": "",
                "replies": replies_i,
                "views": None,
                "theme": "hot",
                "hot": True,
            })
        hot.sort(key=lambda x: x["replies"], reverse=True)
        log(f"  nga hot: {len(hot)} candidates")
        # 热榜候选全量上交（main 层做 seen 轮换后再按 hot_limit 截断）
        return items, backfill, hot
    return items, backfill, []


# ----------------------------------------------------------------------------
# jisilu — /explore/ real-time stream (sorted by last activity)
# ----------------------------------------------------------------------------

# 旧 /topic/{分类} 页按全时段热度排序，翻出的多是沉帖；/explore/ 是按
# 最后回复时间排序的实时流，第一页 ~27 条即当天活跃帖，足够日报使用。
JISILU_EXPLORE_URL = "https://www.jisilu.cn/explore/"

JISILU_ROW_RE = re.compile(
    r'<span class="aw-question-replay-count[^"]*">\s*<em>(\d+)</em>'
    r'.{0,400}?href="(?:https?://www\.jisilu\.cn)?/question/(\d+)"[^>]*>([^<]{4,80})</a>'
    r'.{0,900}?(\d{4}-\d{2}-\d{2} \d{2}:\d{2})[^<]*•\s*(\d+)\s*次浏览',
    re.S,
)


def fetch_jisilu(client, max_items, include_hot=False, hot_limit=5):
    log("fetching jisilu explore stream ...")
    r = client.get(JISILU_EXPLORE_URL)
    if r.status_code != 200:
        raise RuntimeError(f"jisilu status={r.status_code}")
    items = []
    backfill = []
    seen = set()
    rows = JISILU_ROW_RE.findall(r.text)
    log(f"  jisilu explore: {len(rows)} topics parsed")
    for replies, qid, title, last_reply, views in rows:
        if qid in seen:
            continue
        seen.add(qid)
        title = unescape(clean_text(title.strip()))
        # explore 流是全站混合（含新股/转债/闲聊），标题必须过主题词
        keep, theme, reason = classify(title)
        if not keep:
            continue
        replies_i, views_i = int(replies), int(views)
        try:
            published = datetime.strptime(last_reply, "%Y-%m-%d %H:%M").replace(tzinfo=CST).isoformat()
        except ValueError:
            published = None
        if not within_window(published):
            continue
        entry = {
            "id": f"jisilu:{qid}",
            "source": "jisilu",
            "source_name": "集思录",
            "board": "社区",
            "title": title,
            "url": f"https://www.jisilu.cn/question/{qid}",
            "published": published,
            "summary": "",
            "replies": replies_i,
            "views": views_i,
            "theme": theme,
        }
        # 主池：回复 >= 3 或浏览 >= 500（价投内容天然低频）；递补池：
        # 有任何回复或浏览 >= 100
        if replies_i >= 3 or views_i >= 500:
            items.append(entry)
        elif replies_i >= 1 or views_i >= 100:
            backfill.append(entry)
    log(f"  jisilu: kept {len(items)} topics (+{len(backfill)} backfill)")
    hot = []
    if include_hot:
        # 首页 SSR 的「热门」分栏（探测确认 5 行，带回复/浏览）
        try:
            hp = client.get("https://www.jisilu.cn/")
            idx = hp.text.find('class="category">热门')
            if idx < 0:
                idx = hp.text.find("热门")
            if idx > 0:
                seg = hp.text[idx : idx + 2000]
                for qid, title, rep, views in re.findall(
                    r'/question/(\d+)"[^>]*>([^<]{4,80})</a>'
                    r'.{0,600}?(\d+)个回复.{0,200}?(\d+)次浏览', seg, re.S):
                    title = unescape(clean_text(title.strip()))
                    if len(title) < 6 or hit_keywords(title, EXCLUDE_KEYWORDS):
                        continue
                    try:
                        rep_i, views_i = int(rep), int(views)
                    except ValueError:
                        continue
                    hot.append({
                        "id": f"jisilu:{qid}",
                        "source": "jisilu",
                        "source_name": "集思录",
                        "board": "社区·热榜",
                        "title": title,
                        "url": f"https://www.jisilu.cn/question/{qid}",
                        "published": None,
                        "summary": "",
                        "replies": rep_i,
                        "views": views_i,
                        "theme": "hot",
                        "hot": True,
                    })
            hot.sort(key=lambda x: x["replies"] + x["views"] / 100.0, reverse=True)
            log(f"  jisilu hot: {len(hot)} candidates")
        except Exception as e:
            log(f"  jisilu hot ERR: {e}")
    # 热榜候选全量上交（main 层做 seen 轮换后再按 hot_limit 截断）
    return items, backfill, hot


# ----------------------------------------------------------------------------
# hupu — SSR stock board
# ----------------------------------------------------------------------------

HUPU_ROW_RE = re.compile(
    r'<li class="bbs-sl-web-post-body">.*?href="(/\d+\.html)".*?class="p-title"[^>]*>([^<]+)</a>'
    r'.*?>(\d+)\s*/\s*(\d+)<.*?</li>',
    re.S,
)

HUPU_TIME_RE = re.compile(r'class="(?:post-)?time[^"]*"[^>]*>(\d{2}-\d{2} \d{2}:\d{2})<')


def fetch_hupu(client, max_items, include_hot=False, hot_limit=5):
    log("fetching hupu stock board ...")
    r = client.get("https://bbs.hupu.com/stock")
    if r.status_code != 200:
        raise RuntimeError(f"hupu status={r.status_code}")
    items = []
    backfill = []
    now = datetime.now(CST)
    blocks = re.findall(r'<li class="bbs-sl-web-post-body">.*?</li>', r.text, re.S)
    for block in blocks:
        m = HUPU_ROW_RE.match(block + "</li>") if not block.endswith("</li>") else HUPU_ROW_RE.match(block)
        if not m:
            m = HUPU_ROW_RE.search(block)
        if not m:
            continue
        href, title, replies, views = m.groups()
        tid = href.strip("/").replace(".html", "")
        title = unescape(clean_text(title.strip()))
        keep, theme, reason = classify(title)
        if not keep:
            continue
        replies_i, views_i = int(replies), int(views)
        tm = HUPU_TIME_RE.search(block)
        published = parse_hupu_time(tm.group(1), now) if tm else None
        # 时间窗：兜住「最新回复」排序带出的老帖
        if not within_window(published, now):
            continue
        entry = {
            "id": f"hupu:{tid}",
            "source": "hupu",
            "source_name": "虎扑·股票区",
            "board": "股票区",
            "title": title,
            "url": f"https://bbs.hupu.com/{tid}.html",
            "published": published,
            "summary": "",
            "replies": replies_i,
            "views": views_i,
            "theme": theme,
        }
        # 主池：回复 >= 3 或浏览 >= 500；递补池：回复 >= 1 或浏览 >= 100
        if replies_i >= 3 or views_i >= 500:
            items.append(entry)
        elif replies_i >= 1 or views_i >= 100:
            backfill.append(entry)
    log(f"  hupu: kept {len(items)} topics (+{len(backfill)} backfill)")
    hot = []
    if include_hot:
        # /stock-hot 即页面上的「24小时榜」tab（探测确认独立排序）
        try:
            hr = client.get("https://bbs.hupu.com/stock-hot")
            hblocks = re.findall(r'<li class="bbs-sl-web-post-body">.*?</li>', hr.text, re.S)
            for block in hblocks:
                m = HUPU_ROW_RE.search(block)
                if not m:
                    continue
                href, title, replies, views = m.groups()
                tid = href.strip("/").replace(".html", "")
                title = unescape(clean_text(title.strip()))
                if len(title) < 6 or hit_keywords(title, EXCLUDE_KEYWORDS):
                    continue
                try:
                    replies_i, views_i = int(replies), int(views)
                except ValueError:
                    continue
                tm = HUPU_TIME_RE.search(block)
                published = parse_hupu_time(tm.group(1), now) if tm else None
                if not within_window(published, now):
                    continue
                hot.append({
                    "id": f"hupu:{tid}",
                    "source": "hupu",
                    "source_name": "虎扑·股票区",
                    "board": "股票区·24h热榜",
                    "title": title,
                    "url": f"https://bbs.hupu.com/{tid}.html",
                    "published": published,
                    "summary": "",
                    "replies": replies_i,
                    "views": views_i,
                    "theme": "hot",
                    "hot": True,
                })
            hot.sort(key=lambda x: x["replies"] + x["views"] / 100.0, reverse=True)
            log(f"  hupu hot: {len(hot)} candidates")
        except Exception as e:
            log(f"  hupu hot ERR: {e}")
    # 热榜候选全量上交（main 层做 seen 轮换后再按 hot_limit 截断）
    return items, backfill, hot


# ----------------------------------------------------------------------------
# xueqiu — Aliyun WAF: real-browser collector via playwright-core
# ----------------------------------------------------------------------------

# 雪球主域套了阿里云 WAF（75KB 混淆 JS 挑战 + 每 URL 独立的 md5__1038 签名），
# cookie 无法跨请求复用；唯一稳定通道是真浏览器内跑完挑战后用同源 fetch 拿
# JSON。stock.xueqiu.com 的行情 API 不吃 WAF，但那是行情不是讨论内容。
XUEQIU_COLLECTOR = SCRIPT_DIR / "xueqiu_fetch.mjs"
NODE_EXE = r"C:\Users\31345\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

# 搜索词按主题分组；雪球搜索 API 按相关度+时间混合排序，300 条池子够筛
XUEQIU_KEYWORDS = [
    {"kw": "价值投资", "theme": "value"},
    {"kw": "股息率", "theme": "value"},
    {"kw": "大宗商品", "theme": "commodities"},
    {"kw": "原油", "theme": "commodities"},
    {"kw": "周期股", "theme": "cyclical"},
    {"kw": "油运", "theme": "commodities"},
]


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def fetch_xueqiu(max_items, include_hot=False, hot_limit=5):
    log("fetching xueqiu via real-browser collector ...")
    if not XUEQIU_COLLECTOR.exists() or not Path(NODE_EXE).exists():
        raise RuntimeError("xueqiu collector or node runtime missing")
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(XUEQIU_KEYWORDS, f, ensure_ascii=False)
        kw_path = f.name
    try:
        proc = subprocess.run(
            [NODE_EXE, str(XUEQIU_COLLECTOR), kw_path],
            capture_output=True, text=True, timeout=300,
        )
        if not proc.stdout.strip():
            raise RuntimeError(f"collector empty output; stderr={proc.stderr[-300:]}")
        results = json.loads(proc.stdout.strip().split("\n")[-1])
    finally:
        Path(kw_path).unlink(missing_ok=True)

    now = datetime.now(CST)
    items = []
    backfill = []
    seen = set()
    for block in results:
        theme = block.get("theme") or "value"
        kw = block.get("kw", "")
        for it in block.get("items", []):
            xid = str(it.get("id") or "")
            if not xid or xid in seen:
                continue
            seen.add(xid)
            # created_at 毫秒时间戳
            published = None
            ts = it.get("created_at")
            if ts:
                try:
                    published = datetime.fromtimestamp(int(ts) / 1000, tz=CST).isoformat()
                except (ValueError, OSError, OverflowError):
                    published = None
            # 时间窗：搜索池子混有旧帖，只要最近 14 天
            if published:
                dt = datetime.fromisoformat(published)
                if (now - dt) > timedelta(days=MAX_AGE_DAYS):
                    continue
            # 互动门槛（雪球社区自己的投票）：主池 like+2*reply+retweet >= 10；
            # 递补池 >= 3（搜索型源更新慢，留低互动新帖作轮换储备）
            like = int(it.get("like_count") or 0)
            reply = int(it.get("reply_count") or 0)
            retweet = int(it.get("retweet_count") or 0)
            engagement = like + reply * 2 + retweet
            target = it.get("target") or ""
            if not target:
                continue
            title = strip_html(it.get("title") or "")[:80]
            if not title:
                text_head = strip_html(it.get("text") or "")[:80]
                title = text_head or "(无标题)"
            entry = {
                "id": f"xueqiu:{xid}",
                "source": "xueqiu",
                "source_name": "雪球",
                "board": f"搜索:{kw}",
                "title": title,
                "url": f"https://xueqiu.com{target}",
                "published": published,
                "summary": strip_html(it.get("description") or "")[:300],
                "replies": reply,
                "views": None,
                "likes": like,
                "theme": theme,
            }
            if engagement >= 10:
                items.append(entry)
            elif engagement >= 3:
                backfill.append(entry)
    log(f"  xueqiu: kept {len(items)} items (+{len(backfill)} backfill)")

    hot = []
    if include_hot:
        for block in results:
            if block.get("kw") != "__hot__":
                continue
            for it in block.get("items", []):
                xid = str(it.get("id") or "")
                target = it.get("target") or ""
                if not xid or not target:
                    continue
                # 标题取值顺序（show.json 权威化后 title 可能仍为空，回退正文头）：
                # title > description 头部；均经 strip_html 净化
                title = strip_html(it.get("title") or "")[:80]
                if not title:
                    title = strip_html(it.get("description") or "")[:80] or "(无标题)"
                if len(title) < 6 or hit_keywords(title, EXCLUDE_KEYWORDS):
                    continue
                like = int(it.get("like_count") or 0)
                reply = int(it.get("reply_count") or 0)
                retweet = int(it.get("retweet_count") or 0)
                if like + reply + retweet < 10:
                    continue
                published = None
                ts = it.get("created_at")
                if ts:
                    try:
                        published = datetime.fromtimestamp(int(ts) / 1000, tz=CST).isoformat()
                    except (ValueError, OSError, OverflowError):
                        published = None
                hot.append({
                    "id": f"xueqiu:{xid}",
                    "source": "xueqiu",
                    "source_name": "雪球",
                    "board": "今日热门",
                    "title": title,
                    "url": f"https://xueqiu.com{target}",
                    "published": published,
                    "summary": strip_html(it.get("description") or "")[:300],
                    "replies": reply,
                    "views": None,
                    "likes": like,
                    "theme": "hot",
                    "hot": True,
                })
            # 官方热门榜顺序即热度顺序，保留原序
        log(f"  xueqiu hot: {len(hot)} candidates")
    # 热榜候选全量上交（main 层做 seen 轮换后再按 hot_limit 截断）
    return items, backfill, hot


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def write_json(path, data):
    path.write_text(json.dumps(clean_data(data), ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    configure_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", default="nga,jisilu,hupu,xueqiu")
    ap.add_argument("--max-per-board", type=int, default=15)
    ap.add_argument("--no-hot", action="store_true",
                    help="skip the per-board hot-rank collection")
    ap.add_argument("--hot-per-board", type=int, default=5,
                    help="how many hot items to keep per board (grouped, not merged)")
    args = ap.parse_args()

    client = httpx.Client(
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=30,
    )

    all_items = []
    backfill_pool = {}  # source -> [candidates]（互动门槛放宽的同主题候选）
    hot_pool = []
    errors = []
    boards = args.boards.split(",")
    include_hot = not args.no_hot

    for board in boards:
        board = board.strip()
        if not board:
            continue
        try:
            if board == "nga":
                # NGA 的挑战校验依赖 Referer（探测确认：无 Referer 一律 403）
                client.headers["Referer"] = "https://bbs.nga.cn/"
                items, backfill, hot = fetch_nga(client, args.max_per_board, include_hot, 8)
                all_items.extend(items)
                backfill_pool.setdefault(board, []).extend(backfill)
                hot_pool.extend(hot)
                del client.headers["Referer"]
            elif board == "jisilu":
                items, backfill, hot = fetch_jisilu(client, args.max_per_board, include_hot, 8)
                all_items.extend(items)
                backfill_pool.setdefault(board, []).extend(backfill)
                hot_pool.extend(hot)
            elif board == "hupu":
                client.headers["Referer"] = "https://bbs.hupu.com/stock"
                items, backfill, hot = fetch_hupu(client, args.max_per_board, include_hot, 8)
                all_items.extend(items)
                backfill_pool.setdefault(board, []).extend(backfill)
                hot_pool.extend(hot)
                del client.headers["Referer"]
            elif board == "xueqiu":
                # 雪球走真浏览器通道，不用共享 httpx client
                items, backfill, hot = fetch_xueqiu(args.max_per_board, include_hot, 8)
                all_items.extend(items)
                backfill_pool.setdefault(board, []).extend(backfill)
                hot_pool.extend(hot)
        except Exception as e:
            errors.append(f"{board}: {type(e).__name__}: {e}")
            log(f"  {board} ERROR: {e}")
        time.sleep(1.5)

    # ---- 轮换机制（2026-09-22）：已推送的跳过，名次顺延 ----
    # 抓取层把候选全量交上来；rotate_fresh 按 seen.json 剔除已推送项，
    # 板块新鲜量不足保底线时用递补池（同主题/时间窗、互动门槛放宽）补齐。
    # 目标：每个板块每天都给日报留足没见过的新内容，又不为凑数引入噪音。
    delivered_ids = load_seen_forum_ids()
    fresh_items, rot_stats = rotate_fresh(all_items, backfill_pool, delivered_ids, args.max_per_board)
    skipped = rot_stats["skipped_delivered"]
    topped_up_count = rot_stats["backfilled"]
    log(f"rotation: {rot_stats['seen_ids']} seen ids loaded; "
        f"skipped {skipped} delivered; fresh per board: {rot_stats['fresh_per_board']}; "
        f"backfilled {topped_up_count}")

    # engagement rank: communities' own voting
    fresh_items.sort(key=lambda x: x.get("replies", 0) + (x.get("views") or 0) / 100.0, reverse=True)
    all_items = fresh_items

    # 每站独立热榜 TOP-N：不设主题词门槛（用户要求全站视野），
    # 同 id 已在主题区出现时跳过（日报去重）。
    # 常驻巨型楼（回复 >3000）天天霸榜但标题永不变化，对日报无增量，排除。
    #
    # 轮换：热榜同样剔除已推送 id，名次顺延（每站候选扩到 8 条，取前 5）。
    #
    # 各站热度口径不同（NGA/集思录/虎扑是累计互动，雪球今日热门只有当日
    # 几小时积累），因此不做跨站合并打分——每站按自身榜序取 TOP hot_per_board，
    # 结构上保持 hot_topics = [{board, items}] 分组。
    seen_ids = {it["id"] for it in all_items}
    per_board = {}  # source -> [items in its own rank order]
    seen_hot = set()
    for it in hot_pool:
        if it["id"] in seen_ids or it["id"] in seen_hot:
            continue
        if it["id"] in delivered_ids:
            continue
        if (it.get("replies") or 0) > 3000:
            continue
        seen_hot.add(it["id"])
        per_board.setdefault(it["source"], []).append(it)

    board_order = ["nga", "jisilu", "hupu", "xueqiu"]
    hot_boards = []
    for src in board_order + [s for s in sorted(per_board) if s not in board_order]:
        if src not in per_board:
            continue
        items = per_board[src][: args.hot_per_board]
        for rank, it in enumerate(items, 1):
            it["hot_rank"] = rank
        hot_boards.append({
            "source": src,
            "source_name": items[0]["source_name"] if items else src,
            "board_label": items[0]["board"] if items else "",
            "items": items,
        })
    hot_total = sum(len(b["items"]) for b in hot_boards)

    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "topics": all_items,
        "hot_boards": hot_boards,
        "errors": errors or None,
    }
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    out = FEEDS_DIR / "feed-cn-forums.json"
    write_json(out, feed)
    log(f"wrote {out} with {len(all_items)} topics + {hot_total} hot across {len(hot_boards)} boards")

    manifest = {
        "output": str(out),
        "total": len(all_items),
        "hot_total": hot_total,
        "hot_boards": {b["source"]: len(b["items"]) for b in hot_boards},
        "by_board": {},
        "rotation": {
            "seen_ids": rot_stats["seen_ids"],
            "skipped_delivered": rot_stats["skipped_delivered"],
            "backfilled": rot_stats["backfilled"],
        },
        "errors": errors or None,
    }
    for it in all_items:
        manifest["by_board"][it["source"]] = manifest["by_board"].get(it["source"], 0) + 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
