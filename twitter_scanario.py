import argparse
import json
import random
import time
from typing import List, Optional
from urllib.parse import urlparse

import bs4
import requests

from x_client_transaction import ClientTransaction
from x_client_transaction.utils import generate_headers, get_ondemand_file_url


PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

DEFAULT_QID_USER = "AWbeRIdkLtqTRN7yL_H8yw"   # UserByScreenName
DEFAULT_QID_TWEETS = "eApPT8jppbYXlweF_ByTyA"  # UserTweets


def sleep_jitter(a=0.7, b=1.7):
    time.sleep(random.uniform(a, b))


def retry_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    headers=None,
    params=None,
    data=None,
    json_body=None,
    max_retries=6,
    timeout=30,
):
    for i in range(max_retries):
        sleep_jitter()
        r = session.request(
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            json=json_body,
            timeout=timeout,
        )
        print(f"{method} {r.url.split('?')[0]} -> {r.status_code}")

        if r.status_code in (429, 500, 502, 503, 504):
            wait = min(40, 2 ** (i + 1))
            print(f"Retryable {r.status_code}. Sleep {wait}s")
            time.sleep(wait)
            continue

        r.raise_for_status()
        return r

    raise Exception("Max retries exceeded")


def activate_guest(session: requests.Session, bearer: str) -> str:
    r = retry_request(
        session,
        "POST",
        "https://api.x.com/1.1/guest/activate.json",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Origin": "https://x.com",
            "Referer": "https://x.com/",
            "Accept": "*/*",
        },
    )
    return r.json()["guest_token"]


_CT: Optional[ClientTransaction] = None


def get_client_transaction(session: requests.Session) -> ClientTransaction:
    global _CT
    if _CT is not None:
        return _CT

    session.headers.update(generate_headers())

    home = session.get("https://x.com", timeout=30)
    home.raise_for_status()
    home_soup = bs4.BeautifulSoup(home.content, "html.parser")

    ondemand_url = get_ondemand_file_url(response=home_soup)
    ond = session.get(ondemand_url, timeout=30)
    ond.raise_for_status()

    _CT = ClientTransaction(home_page_response=home_soup, ondemand_file_response=ond.text)
    return _CT


def gen_txn_id(session: requests.Session, method: str, full_url: str) -> str:
    ct = get_client_transaction(session)
    path = urlparse(full_url).path
    return ct.generate_transaction_id(method=method.upper(), path=path)


def build_graphql_headers(
    *,
    session: requests.Session,
    bearer: str,
    guest: str,
    referer: str,
    full_url: str,
    method: str = "GET",
    client_uuid: Optional[str] = None,
) -> dict:
    txn_id = gen_txn_id(session, method, full_url)

    h = {
        "Authorization": f"Bearer {bearer}",
        "x-guest-token": guest,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "x-client-transaction-id": txn_id,
        "x-twitter-auth-type": "OAuth2Client",
        "Accept": "*/*",
        "Origin": "https://x.com",
        "Referer": referer,
        "User-Agent": session.headers["User-Agent"],
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if client_uuid:
        h["x-client-uuid"] = client_uuid
    return h


def graphql_user_by_screen_name(
    session: requests.Session,
    bearer: str,
    guest: str,
    qid_user: str,
    screen_name: str,
    client_uuid: Optional[str] = None,
) -> dict:
    url = f"https://api.x.com/graphql/{qid_user}/UserByScreenName"

    headers = build_graphql_headers(
        session=session,
        bearer=bearer,
        guest=guest,
        referer=f"https://x.com/{screen_name}",
        full_url=url,
        method="GET",
        client_uuid=client_uuid,
    )

    params = {
        "variables": json.dumps(
            {"screen_name": screen_name, "withGrokTranslatedBio": False},
            separators=(",", ":"),
        ),
        "features": json.dumps(
            {
                "hidden_profile_subscriptions_enabled": True,
                "profile_label_improvements_pcf_label_in_post_enabled": True,
                "responsive_web_profile_redirect_enabled": False,
                "rweb_tipjar_consumption_enabled": False,
                "verified_phone_label_enabled": False,
                "subscriptions_verification_info_is_identity_verified_enabled": True,
                "subscriptions_verification_info_verified_since_enabled": True,
                "highlights_tweets_tab_ui_enabled": True,
                "responsive_web_twitter_article_notes_tab_enabled": True,
                "subscriptions_feature_can_gift_premium": True,
                "creator_subscriptions_tweet_preview_api_enabled": True,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "responsive_web_graphql_timeline_navigation_enabled": True,
            },
            separators=(",", ":"),
        ),
        "fieldToggles": json.dumps(
            {"withPayments": False, "withAuxiliaryUserLabels": True},
            separators=(",", ":"),
        ),
    }

    r = retry_request(session, "GET", url, headers=headers, params=params)
    return r.json()


def graphql_user_tweets(
    session: requests.Session,
    bearer: str,
    guest: str,
    qid_tweets: str,
    user_id: str,
    count: int,
    client_uuid: Optional[str] = None,
) -> dict:
    url = f"https://api.x.com/graphql/{qid_tweets}/UserTweets"

    headers = build_graphql_headers(
        session=session,
        bearer=bearer,
        guest=guest,
        referer=f"https://x.com/i/user/{user_id}",
        full_url=url,
        method="GET",
        client_uuid=client_uuid,
    )

    params = {
        "variables": json.dumps(
            {
                "userId": str(user_id),
                "count": int(max(count, 20)),
                "includePromotedContent": True,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            },
            separators=(",", ":"),
        ),
        "features": json.dumps(
            {
                "responsive_web_graphql_timeline_navigation_enabled": True,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "view_counts_everywhere_api_enabled": True,
                "longform_notetweets_consumption_enabled": True,
            },
            separators=(",", ":"),
        ),
    }

    r = retry_request(session, "GET", url, headers=headers, params=params)
    return r.json()


def extract_tweets_text_only(data: dict) -> List[str]:
    def find_instructions(root: dict):
        candidates = [
            ("data", "user", "result", "timeline_v2", "timeline", "instructions"),
            ("data", "user", "result", "timeline", "timeline", "instructions"),
            ("data", "user", "result", "timeline", "instructions"),
            ("data", "user", "result", "timeline_response", "timeline", "instructions"),
            ("data", "user", "result", "timelineResponse", "timeline", "instructions"),
        ]
        for path in candidates:
            cur = root
            ok = True
            for key in path:
                if isinstance(cur, dict) and key in cur:
                    cur = cur[key]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list):
                return cur
        return None

    instructions = find_instructions(data)
    if not instructions:
        top = list(data.keys())
        raise KeyError(f"Cannot find instructions in response. Top keys: {top}")

    tweets: List[str] = []

    for inst in instructions:
        if inst.get("type") not in ("TimelineAddEntries", "TimelineReplaceEntry", "TimelinePinEntry"):
            continue

        entries = inst.get("entries") or []
        for entry in entries:
            content = entry.get("content") or {}

            item = content.get("itemContent") or {}
            tweet = (item.get("tweet_results") or {}).get("result") or {}
            legacy = tweet.get("legacy") or {}
            full_text = legacy.get("full_text")

            if not full_text:
                items = content.get("items") or []
                for it in items:
                    ic = (it.get("item") or {}).get("itemContent") or {}
                    tw = (ic.get("tweet_results") or {}).get("result") or {}
                    lg = tw.get("legacy") or {}
                    if lg.get("full_text"):
                        full_text = lg["full_text"]
                        break

            if full_text:
                text = full_text.replace("\n", " ").strip()
                if text and text not in tweets:
                    tweets.append(text)

    return tweets


def scrape(
    username: str,
    count: int,
    qid_user: str,
    qid_tweets: str,
    proxy: Optional[str],
    client_uuid: Optional[str],
) -> List[str]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    bearer = PUBLIC_BEARER
    guest = activate_guest(session, bearer)

    user_data = graphql_user_by_screen_name(
        session, bearer, guest, qid_user, username, client_uuid
    )
    rest_id = user_data["data"]["user"]["result"]["rest_id"]

    timeline = graphql_user_tweets(
        session, bearer, guest, qid_tweets, rest_id, count, client_uuid
    )

    tweets = extract_tweets_text_only(timeline)
    return tweets[:count]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--out", default="tweets.txt")

    parser.add_argument("--qid-user", default=DEFAULT_QID_USER)
    parser.add_argument("--qid-tweets", default=DEFAULT_QID_TWEETS)

    parser.add_argument("--client-uuid", default=None, help="optional x-client-uuid from DevTools")
    parser.add_argument("--proxy", default=None, help="optional proxy, e.g. http://user:pass@host:port")

    args = parser.parse_args()

    tweets = scrape(
        username=args.user,
        count=args.count,
        qid_user=args.qid_user,
        qid_tweets=args.qid_tweets,
        proxy=args.proxy,
        client_uuid=args.client_uuid,
    )

    with open(args.out, "w", encoding="utf-8") as f:
        for t in tweets:
            f.write(t + "\n")

    print(f"Saved {len(tweets)} tweets to {args.out}")
