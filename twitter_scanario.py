import argparse
import json
import logging
import random
import re
import sys
import time
from html import unescape
from typing import Any, Dict, List, Optional

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

def setup_logging(log_path: Optional[str]):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_path:
        handlers.insert(0, logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

def log(msg: str):
    logging.info(msg)

def sleep_jitter(a: float = 0.7, b: float = 1.9):
    time.sleep(random.uniform(a, b))

def new_session(referer: str) -> requests.Session:
    s = requests.Session()
    # Make headers more browser-like (without auth)
    s.headers.update({
        "user-agent": UA,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9,uk;q=0.8,ru;q=0.7",
        "connection": "keep-alive",
        "referer": referer,
        "upgrade-insecure-requests": "1",
        "dnt": "1",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-site",
        "sec-fetch-user": "?1",
    })
    return s

def fetch_profile_timeline_html(session: requests.Session, username: str, max_retries: int = 7) -> str:
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
    log(f"Fetching public profile timeline HTML: {url}")

    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        sleep_jitter()
        r = session.get(url, timeout=30)
        log(f"HTTP {r.status_code}")

        if r.status_code == 200:
            if not r.text or len(r.text) < 200:
                raise RuntimeError("Empty/too short response body")
            return r.text

        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            if ra and ra.isdigit():
                wait_s = int(ra) + random.uniform(0.5, 2.0)
                log(f"429 with Retry-After={ra}s")
            else:
                wait_s = min(60 * 5, backoff) + random.uniform(0.5, 2.5)
                log("429 without Retry-After, using backoff + jitter")
                backoff = min(backoff * 1.8, 120)

            log(f"Rate limited (429). Waiting {wait_s:.1f}s, retry {attempt}/{max_retries}...")
            time.sleep(wait_s)
            continue

        # Some transient errors you may want to retry
        if r.status_code in (502, 503, 520, 521, 522):
            wait_s = min(30, backoff) + random.uniform(0.5, 2.0)
            log(f"Transient HTTP {r.status_code}. Waiting {wait_s:.1f}s, retry {attempt}/{max_retries}...")
            time.sleep(wait_s)
            backoff = min(backoff * 1.6, 60)
            continue

        raise RuntimeError(f"Profile timeline HTTP {r.status_code}")

    raise RuntimeError("Too many retries. Try later, reduce frequency, or use a proxy.")

def extract_next_data_json(html: str) -> Optional[Dict[str, Any]]:
    """
    Try to extract <script id="__NEXT_DATA__" type="application/json">...</script>.
    Return dict if found/parsed, else None.
    """
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None

    raw = m.group(1).strip()
    raw = unescape(raw)
    try:
        return json.loads(raw)
    except Exception:
        return None

def iter_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from iter_dicts(it)

def is_retweet(d: Dict[str, Any]) -> bool:
    if "retweeted_status" in d or "retweeted_status_result" in d:
        return True
    if d.get("retweeted") is True and any(k in d for k in ("source_status_id", "retweeted_status_id")):
        return True
    return False

def is_reply(d: Dict[str, Any]) -> bool:
    if d.get("in_reply_to_status_id_str") or d.get("in_reply_to_status_id"):
        return True
    if d.get("inReplyToStatusId") or d.get("inReplyToStatusIdStr"):
        return True
    return False

def normalize_text(t: str) -> str:
    t = unescape(t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\u200b", "").replace("\u2060", "").replace("\ufeff", "")
    # Keep line breaks internally; we will write as one line per tweet later
    t = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in t.split("\n"))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

def pick_text(d: Dict[str, Any]) -> Optional[str]:
    t = d.get("full_text") or d.get("text")
    if isinstance(t, str) and t.strip():
        return normalize_text(t)
    return None

def looks_like_ui_junk(text: str) -> bool:
    low = text.strip().lower()
    bad = {
        "log in", "sign up", "show this thread", "view", "join x",
        "something went wrong", "try again",
    }
    return low in bad

def extract_tweets_from_next_data(
    data: Dict[str, Any],
    want: int,
    drop_retweets: bool,
    drop_replies: bool,
) -> List[str]:
    collected: List[str] = []
    seen = set()

    for d in iter_dicts(data):
        txt = pick_text(d)
        if not txt:
            continue

        if looks_like_ui_junk(txt):
            continue

        if drop_retweets and is_retweet(d):
            continue
        if drop_replies and is_reply(d):
            continue

        if txt not in seen:
            seen.add(txt)
            collected.append(txt)
            if len(collected) >= want:
                break

    return collected

def strip_tags(html: str) -> str:
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", "", html)
    return html

def extract_tweets_from_html_fallback(html: str, want: int) -> List[str]:
    """
    Best-effort: extract tweet text blocks from syndication HTML when __NEXT_DATA__ is absent.
    """
    patterns = [
        r'class="tweet-text"[^>]*>(.*?)</p>',
        r'class="timeline-Tweet-text"[^>]*>(.*?)</p>',
        r'data-testid="tweetText"[^>]*>(.*?)</div>',
        r'data-tweet-text="([^"]+)"',
    ]

    out: List[str] = []
    seen: set = set()

    for pat in patterns:
        for m in re.finditer(pat, html, flags=re.IGNORECASE | re.DOTALL):
            chunk = m.group(1)
            chunk = strip_tags(chunk)
            chunk = normalize_text(chunk)
            if not chunk or looks_like_ui_junk(chunk):
                continue

            if chunk not in seen:
                seen.add(chunk)
                out.append(chunk)
                if len(out) >= want:
                    return out

    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="screen_name without @, e.g. elonmusk")
    ap.add_argument("--out", default="tweets.txt")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--log", default="run.log")

    # Filters are OPTIONAL now (default: include everything to match 'latest 10 tweets')
    ap.add_argument("--no-retweets", action="store_true", help="Exclude retweets")
    ap.add_argument("--no-replies", action="store_true", help="Exclude replies")

    args = ap.parse_args()

    log_path = None if args.log == "-" else args.log
    setup_logging(log_path)

    log("Starting twitter_noauth_profile (public profile timeline, no-auth)")

    target = max(1, args.count)
    session = new_session(referer=f"https://x.com/{args.user}")

    html = fetch_profile_timeline_html(session, args.user)

    # 1) Try __NEXT_DATA__
    data = extract_next_data_json(html)
    tweets: List[str] = []
    if data:
        log("Found __NEXT_DATA__. Extracting tweets from JSON...")
        tweets = extract_tweets_from_next_data(
            data,
            want=target,
            drop_retweets=args.no_retweets,
            drop_replies=args.no_replies,
        )
        log(f"Extracted {len(tweets)} tweets from __NEXT_DATA__")
    else:
        log("No __NEXT_DATA__ found in HTML. Will use HTML fallback parsing.")

    # 2) Fallback to HTML parsing if needed
    if len(tweets) < target:
        log("Trying HTML fallback parsing...")
        fb = extract_tweets_from_html_fallback(html, want=target)
        log(f"Extracted {len(fb)} tweets from HTML fallback")
        # Merge, dedupe
        seen = set(tweets)
        for t in fb:
            if t not in seen:
                tweets.append(t)
                seen.add(t)
        tweets = tweets[:target]

    if not tweets:
        raise RuntimeError(
            "No tweets extracted. Possible reasons: endpoint blocked, layout changed, "
            "account protected, or response contains no tweet data."
        )

    with open(args.out, "w", encoding="utf-8") as f:
        for t in tweets[:target]:
            f.write(t.replace("\n", " ").strip() + "\n")

    log(f"Saved {min(len(tweets), target)} tweets to {args.out}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Error: {e}")
        raise
