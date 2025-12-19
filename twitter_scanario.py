
import argparse
import logging
import random
import re
import sys
import time
import json
from typing import Optional, List, Tuple

import requests



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


def sleep_jitter():
    time.sleep(random.uniform(0.8, 1.6))



UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def new_session(username: str, auth_token: str, ct0: str):
    s = requests.Session()
    s.headers.update({
        "user-agent": UA,
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": f"https://x.com/{username}",
        "origin": "https://x.com",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    })
    # cookies
    s.cookies.set("auth_token", auth_token, domain=".x.com", path="/")
    s.cookies.set("ct0", ct0, domain=".x.com", path="/")
    return s



def safe_json_or_log(r: requests.Response, label: str) -> dict:
    text = r.text
    try:
        js = r.json()
    except Exception:
        log(f"{label}: non-JSON response (HTTP {r.status_code}), first 200 chars:")
        log(text[:200])
        raise RuntimeError(f"{label}: expected JSON, got non-JSON (see log)")
    return js



def discover_bearer_and_queryids(session: requests.Session, username: str) -> Tuple[str, str, str]:

    url = f"https://x.com/{username}"
    log(f"Fetching profile HTML for JS discovery: {url}")
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Profile HTML status: {r.status_code}")
    log(f"Profile HTML status: {r.status_code}")
    html = r.text

    js_urls = re.findall(
        r'https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js',
        html
    )
    if not js_urls:
        raise RuntimeError("Could not find main.*.js in profile HTML")

    js_url = js_urls[0]
    log(f"Fetching JS bundle: {js_url}")
    jr = session.get(js_url, timeout=30)
    if jr.status_code != 200:
        raise RuntimeError(f"JS bundle fetch failed: HTTP {jr.status_code}")
    text = jr.text

    # bearer
    bm = re.search(r'AAAAAAAAA[^"\\]{20,200}', text)
    if not bm:
        raise RuntimeError("Bearer token not found in JS bundle")
    bearer = bm.group(0)
    log("Bearer + queryIds discovered")

    # queryId для UserByScreenName
    qid_user_by_name = None
    qid_user_tweets = None

    for m in re.finditer(r'{queryId:"([^"]+)",operationName:"([^"]+)"', text):
        qid, op = m.groups()
        if op == "UserByScreenName":
            qid_user_by_name = qid
        elif op == "UserTweets":
            qid_user_tweets = qid

    if not qid_user_by_name:
        raise RuntimeError("queryId for UserByScreenName not found in JS bundle")
    if not qid_user_tweets:
        raise RuntimeError("queryId for UserTweets not found in JS bundle")

    return bearer, qid_user_by_name, qid_user_tweets

def user_id_via_graphql(session, bearer, qid_user_by_name: str, username: str) -> str:

    url = f"https://x.com/i/api/graphql/{qid_user_by_name}/UserByScreenName"

    variables = {
        "screen_name": username,
        "withSafetyModeUserFields": True,
    }

    features = {
        "hidden_profile_likes_enabled": True,
        "hidden_profile_subscriptions_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "subscriptions_verification_info_verified_since_enabled": True,
        "highlights_tweets_tab_ui_enabled": True,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "unified_cards_ad_metadata_container_dynamic_card_content_query_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_refetch_user_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": False,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "subscriptions_verification_info_is_identity_verified_enabled": True,
        "subscriptions_verification_info_verified_since_legacy_enabled": True,
        "highlights_tweets_tab_ui_with_revenue_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
        "responsive_web_profile_redirect_enabled": True,
        "responsive_web_twitter_article_notes_tab_enabled": False,
        "subscriptions_feature_can_gift_premium": True,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
    }

    r = session.get(
        url,
        headers={"authorization": f"Bearer {bearer}"},
        params={
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        },
        timeout=30,
    )

    js = safe_json_or_log(r, "UserByScreenName")

    if "data" not in js:
        log("GraphQL response has NO 'data'. Full response:")
        log(json.dumps(js, ensure_ascii=False)[:800])
        raise RuntimeError("GraphQL returned no data (see log above)")

    try:
        rest_id = js["data"]["user"]["result"]["rest_id"]
        if not rest_id:
            raise KeyError("empty rest_id")
        return rest_id
    except Exception as e:
        log("GraphQL data present but unexpected structure:")
        log(json.dumps(js, ensure_ascii=False)[:800])
        raise RuntimeError(f"Could not extract rest_id: {e}")



def fetch_user_tweets_page(
    session: requests.Session,
    bearer: str,
    qid_user_tweets: str,
    user_id: str,
    cursor: Optional[str],
) -> dict:

    url = f"https://x.com/i/api/graphql/{qid_user_tweets}/UserTweets"

    variables = {
        "userId": user_id,
        "count": 40,
        "includePromotedContent": True,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
        "withV2Timeline": True,
    }
    if cursor:
        variables["cursor"] = cursor

    features = {
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "premium_content_api_read_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "responsive_web_grok_show_grok_translated_post": False,
        "responsive_web_grok_analysis_button_from_backend": True,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_grok_image_annotation_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
        "rweb_video_screen_enabled": False,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
        "responsive_web_profile_redirect_enabled": True,
        "responsive_web_grok_analyze_post_followups_enabled": False,
        "responsive_web_grok_community_note_auto_translation_is_enabled": False,
        "responsive_web_jetfuel_frame": False,
        "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
        "responsive_web_grok_share_attachment_enabled": False,
        "responsive_web_grok_imagine_annotation_enabled": False,
    }

    field_toggles = {
        "withArticlePlainText": False,
    }

    r = session.get(
        url,
        headers={"authorization": f"Bearer {bearer}"},
        params={
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
            "fieldToggles": json.dumps(field_toggles, separators=(",", ":")),
        },
        timeout=30,
    )

    return safe_json_or_log(r, "UserTweets")


def extract_texts_from_usertweets(js: dict, expected_user_id: str) -> Tuple[List[str], Optional[str]]:

    tweets: List[str] = []
    next_cursor: Optional[str] = None

    timeline = None
    if "data" in js and "user" in js["data"]:
        user = js["data"]["user"]["result"]
        timeline = user.get("timeline_v2") or user.get("timeline")
    if not timeline:
        log("UserTweets: no timeline in data")
        return tweets, None

    instrs = timeline.get("timeline", {}).get("instructions", [])
    for instr in instrs:
        if instr.get("type") == "TimelineAddEntries":
            entries = instr.get("entries", [])
            for e in entries:
                entry_id = e.get("entryId", "")
                content = e.get("content", {})

                if entry_id.startswith("tweet-"):
                    item = content.get("itemContent", {})
                    tweet_results = item.get("tweet_results", {})
                    result = tweet_results.get("result")
                    if not isinstance(result, dict):
                        continue

                    legacy = result.get("legacy", {})
                    core = result.get("core", {})
                    user_result = core.get("user_results", {}).get("result", {})
                    author_rest_id = user_result.get("rest_id")
                    if author_rest_id != expected_user_id:
                        continue

                    if legacy.get("retweeted_status_result"):
                        continue
                    if legacy.get("in_reply_to_status_id_str"):
                        continue

                    full_text = legacy.get("full_text") or legacy.get("text") or ""
                    full_text = full_text.replace("\r\n", "\n").replace("\r", "\n")
                    full_text = re.sub(r"\s+", " ", full_text).strip()
                    if full_text:
                        tweets.append(full_text)

                if entry_id.startswith("cursor-bottom-"):
                    cursor_content = content.get("value") or content.get("itemContent", {}).get("value")
                    if cursor_content:
                        next_cursor = cursor_content

    return tweets, next_cursor



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--auth-token", required=True)
    ap.add_argument("--ct0", required=True)
    ap.add_argument("--out", default="tweets.txt")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--log")
    args = ap.parse_args()

    setup_logging(args.log)
    log("Starting twitter_scanario (GraphQL UserTweets)")

    session = new_session(args.user, args.auth_token, args.ct0)

    bearer, qid_user_by_name, qid_user_tweets = discover_bearer_and_queryids(session, args.user)
    log("Bearer discovery OK")

    user_id = user_id_via_graphql(session, bearer, qid_user_by_name, args.user)
    log(f"user_id resolved: {user_id}")

    target = max(1, args.count)
    collected: List[str] = []
    cursor: Optional[str] = None

    for page in range(10):
        sleep_jitter()
        js = fetch_user_tweets_page(session, bearer, qid_user_tweets, user_id, cursor)
        batch, cursor = extract_texts_from_usertweets(js, user_id)

        if not batch:
            log(f"Page {page+1}: no tweets in page, stopping")
            break

        for t in batch:
            t1 = t.strip()
            if not t1:
                continue
            if t1 and t1 not in collected:
                collected.append(t1)
                if len(collected) >= target:
                    break

        log(f"Page {page+1}: got {len(batch)} tweets, total={len(collected)}, cursor={'yes' if cursor else 'no'}")

        if len(collected) >= target or not cursor:
            break

    if not collected:
        raise RuntimeError("No tweets collected (UserTweets blocked or empty, see log)")

    with open(args.out, "w", encoding="utf-8") as f:
        for t in collected[:target]:
            f.write(t + "\n")

    log(f"Saved {min(len(collected), target)} tweets to {args.out}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Error: {e}")
        raise
