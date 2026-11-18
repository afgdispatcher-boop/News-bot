#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
News-bot: نړیوال خبرونه له معتبرو رسنیو راټولوي او په فېسبوک پاڼه یې نشر کوي.
"""

import calendar
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


MAX_AGE_MINUTES = env_int("MAX_AGE_MINUTES", 10)
MAX_POSTS_PER_RUN = env_int("MAX_POSTS_PER_RUN", 3)
SLEEP_BETWEEN_POSTS = env_int("SLEEP_BETWEEN_POSTS", 20)
KEEP_HISTORY = 1500
SIMILARITY = 0.5
FB_API_VERSION = os.getenv("FB_API_VERSION", "v23.0")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
OUTPUT_LANGUAGE = os.getenv("OUTPUT_LANGUAGE", "Pashto")

FB_PAGE_ID = os.getenv("FB_PAGE_ID", "").strip()
FB_PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

FEEDS = [
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("DW", "https://rss.dw.com/rdf/rss-en-world"),
    ("France 24", "https://www.france24.com/en/rss"),
    ("NPR", "https://feeds.npr.org/1004/rss.xml"),
    ("CNN", "http://rss.cnn.com/rss/edition_world.rss"),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/world.xml"),
    ("BBC Pashto", "https://feeds.bbci.co.uk/pashto/rss.xml"),
]

SKIP_PATTERNS = [
    r"^live\b", r"\blive blog\b", r"\blive updates\b", r"^watch\b",
    r"^podcast", r"^opinion", r"\bquiz\b", r"\bnewsletter\b",
    r"\bhoroscope\b", r"\bsponsored\b",
]

STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "after", "over",
    "into", "about", "says", "say", "has", "have", "are", "was", "were",
    "will", "its", "his", "her", "their", "new", "amid", "than", "you",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0; +https://github.com)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("news-bot")


class FatalError(Exception):
    pass


class RateLimited(Exception):
    pass


def load_state():
    default = {"posted": [], "titles": [], "heartbeat": ""}
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bad state")
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception as exc:
        log.error("state.json خراب دی، نوی جوړېږي: %s", exc)
        try:
            STATE_FILE.rename(STATE_FILE.with_suffix(".broken"))
        except Exception:
            pass
        return default


def save_state(state):
    state["posted"] = state["posted"][-KEEP_HISTORY:]
    state["titles"] = state["titles"][-400:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url):
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def url_key(url):
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:20]


def title_tokens(title):
    words = re.findall(r"[a-z0-9\u0600-\u06FF]+", title.lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def is_similar(tokens_a, tokens_b):
    if not tokens_a or not tokens_b:
        return False
    inter = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return union > 0 and inter / union >= SIMILARITY


def should_skip_title(title):
    low = title.lower()
    return any(re.search(p, low) for p in SKIP_PATTERNS)


def fetch_feed(name, url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        return parsed.entries or []
    except Exception as exc:
        log.warning("%s: فیډ ونه لوستل شو (%s)", name, exc)
        return []


def collect_candidates(now_ts):
    items = []
    for name, url in FEEDS:
        for entry in fetch_feed(name, url):
            try:
                link = (entry.get("link") or "").strip()
                title = clean_text(entry.get("title"))
                if not link.startswith("http") or len(title) < 15:
                    continue
                if should_skip_title(title):
                    continue
                t = entry.get("published_parsed") or entry.get("updated_parsed")
                if not t:
                    continue
                ts = calendar.timegm(t)
                age_min = (now_ts - ts) / 60.0
                if age_min < -3 or age_min > MAX_AGE_MINUTES:
                    continue
                summary = clean_text(entry.get("summary") or entry.get("description"))
                items.append({
                    "source": name,
                    "title": title,
                    "summary": summary[:600],
                    "link": link,
                    "key": url_key(link),
                    "ts": ts,
                    "tokens": title_tokens(title),
                })
            except Exception as exc:
                log.warning("%s: یو خبر پرېښودل شو (%s)", name, exc)
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


def rewrite_with_ai(item):
    if not GEMINI_API_KEY:
        return None
    prompt = (
        f"Rewrite this news item in {OUTPUT_LANGUAGE} in your own words, "
        "2 to 3 short sentences. Use ONLY the facts given below. "
        "Do not add anything, do not guess, no opinions, no emojis, no hashtags. "
        "Return only the rewritten text.\n\n"
        f"Headline: {item['title']}\n"
        f"Summary: {item['summary']}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        r = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 400},
            },
            timeout=40,
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if 30 <= len(text) <= 1200:
            return text
    except Exception as exc:
        log.warning("AI بیاليکنه ناکامه شوه، اصلي سرليک کارول کېږي: %s", exc)
    return None


def build_message(item):
    body = rewrite_with_ai(item) or item["title"]
    return f"{body}\n\n📰 سرچینه: {item['source']}"


def post_to_facebook(message, link):
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PAGE_ID}/feed"
    payload = {"message": message, "link": link, "access_token": FB_PAGE_TOKEN}
    last_err = ""
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, data=payload, timeout=30)
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.ok and data.get("id"):
                return data["id"]

            err = data.get("error", {}) if isinstance(data, dict) else {}
            code = err.get("code")
            last_err = f"HTTP {resp.status_code} code={code} msg={err.get('message')}"

            if code == 190:
                raise FatalError(f"د فېسبوک ټوکن ناسم دی: {err.get('message')}")
            if code in (4, 17, 32, 613):
                raise RateLimited(last_err)
            if code in (100, 200, 10, 368):
                break
        except (FatalError, RateLimited):
            raise
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(5 * attempt)
    log.error("پوسټ ناکام شو: %s", last_err)
    return None


def main():
    if not DRY_RUN and (not FB_PAGE_ID or not FB_PAGE_TOKEN):
        log.error("FB_PAGE_ID او FB_PAGE_TOKEN باید په Secrets کې وي.")
        return 1

    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("heartbeat") != today:
        state["heartbeat"] = today
        save_state(state)

    posted_keys = set(state["posted"])
    recent_tokens = [title_tokens(t) for t in state["titles"]]

    candidates = collect_candidates(time.time())
    log.info("نوي کاندیدان: %d", len(candidates))

    selected = []
    for item in candidates:
        if item["key"] in posted_keys:
            continue
        if any(is_similar(item["tokens"], tk) for tk in recent_tokens):
            posted_keys.add(item["key"])
            state["posted"].append(item["key"])
            continue
        selected.append(item)
        recent_tokens.append(item["tokens"])
        if len(selected) >= MAX_POSTS_PER_RUN:
            break

    if not selected:
        log.info("نوی خبر نشته.")
        save_state(state)
        return 0

    for idx, item in enumerate(reversed(selected)):
        message = build_message(item)
        if DRY_RUN:
            log.info("[DRY RUN]\n%s\n%s\n", message, item["link"])
            continue
        try:
            post_id = post_to_facebook(message, item["link"])
        except FatalError as exc:
            log.error(str(exc))
            save_state(state)
            return 1
        except RateLimited as exc:
            log.warning("محدودیت: %s — په راتلونکي ځل کې دوام", exc)
            break

        if post_id:
            log.info("نشر شو [%s]: %s", item["source"], item["title"])
            state["posted"].append(item["key"])
            state["titles"].append(item["title"])
            save_state(state)
        if idx < len(selected) - 1:
            time.sleep(SLEEP_BETWEEN_POSTS)

    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("ناڅاپي غلطي")
        sys.exit(1)
