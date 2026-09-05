#!/usr/bin/env python3
"""
FantasyPros News -> Bluesky / Facebook / Discord / X bot.

Pulls recent NFL news from the FantasyPros API, filters it down to
players who are actually fantasy-relevant (based on FantasyPros
consensus rankings), and posts any new items to whichever
destinations have credentials configured. Tracks posted item IDs in
seen_ids.json so the same story never goes out twice.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------- Config ----------

SPORT = os.environ.get("FP_SPORT", "nfl")
NEWS_LIMIT = int(os.environ.get("FP_NEWS_LIMIT", "25"))
FANTASYPROS_API_KEY = os.environ["FANTASYPROS_API_KEY"]

# Only post news about players ranked within these per-position cutoffs
# (based on FantasyPros consensus ECR). Adjust to taste — wider leagues
# or superflex formats may want higher numbers, especially for QB.
POSITION_LIMITS = {
    "QB": 18,
    "RB": 50,
    "WR": 60,
    "TE": 18,
    "K": 4,
    "DST": 0,
}
SCORING = os.environ.get("FP_SCORING", "PPR")  # STD, PPR, or HALF
RANKINGS_MAX_AGE_HOURS = int(os.environ.get("FP_RANKINGS_MAX_AGE_HOURS", "20"))

# Facebook Page
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")

# Bluesky
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE")
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD")

# Discord
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

SEEN_IDS_PATH = Path(__file__).parent / "seen_ids.json"
MAX_SEEN_IDS_KEPT = 1000  # trim the file so it doesn't grow forever

RELEVANT_PLAYERS_PATH = Path(__file__).parent / "relevant_players.json"

FANTASYPROS_URL = f"https://api.fantasypros.com/public/v2/json/{SPORT}/news"
RANKINGS_URL_TMPL = (
    "https://api.fantasypros.com/public/v2/json/{sport}/{season}/consensus-rankings"
)
FACEBOOK_GRAPH_VERSION = "v21.0"


# ---------- Relevant player filtering ----------

def fetch_relevant_player_ids() -> set:
    """Pulls consensus rankings and returns the set of player_ids that
    fall within POSITION_LIMITS for their position."""
    season = datetime.utcnow().year
    resp = requests.get(
        RANKINGS_URL_TMPL.format(sport=SPORT, season=season),
        headers={"x-api-key": FANTASYPROS_API_KEY},
        params={"position": "ALL", "scoring": SCORING},
        timeout=30,
    )
    resp.raise_for_status()
    players = resp.json().get("players", [])

    relevant_ids = set()
    for p in players:
        pos_rank = p.get("pos_rank", "")  # e.g. "RB12"
        match = re.match(r"([A-Z]+)(\d+)", pos_rank or "")
        if not match:
            continue
        position, rank = match.group(1), int(match.group(2))
        limit = POSITION_LIMITS.get(position)
        if limit and rank <= limit:
            relevant_ids.add(str(p.get("player_id")))
    return relevant_ids


def load_relevant_player_ids() -> set:
    """Returns cached relevant player IDs, refreshing from the API if the
    cache is missing or older than RANKINGS_MAX_AGE_HOURS."""
    if RELEVANT_PLAYERS_PATH.exists():
        with open(RELEVANT_PLAYERS_PATH) as f:
            cache = json.load(f)
        age_hours = (time.time() - cache.get("fetched_at", 0)) / 3600
        if age_hours < RANKINGS_MAX_AGE_HOURS:
            return set(cache.get("player_ids", []))

    try:
        ids = fetch_relevant_player_ids()
    except Exception as e:
        print(f"Failed to refresh rankings, falling back to stale/no cache: {e}", file=sys.stderr)
        if RELEVANT_PLAYERS_PATH.exists():
            with open(RELEVANT_PLAYERS_PATH) as f:
                return set(json.load(f).get("player_ids", []))
        return set()

    with open(RELEVANT_PLAYERS_PATH, "w") as f:
        json.dump({"fetched_at": time.time(), "player_ids": sorted(ids)}, f)
    return ids


# ---------- FantasyPros news ----------

def fetch_news():
    resp = requests.get(
        FANTASYPROS_URL,
        headers={"x-api-key": FANTASYPROS_API_KEY},
        params={"limit": NEWS_LIMIT},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    # FantasyPros returns newest-first; we want to post oldest-first
    # so the timeline reads chronologically.
    return list(reversed(data.get("items", [])))


def load_seen_ids() -> set:
    if not SEEN_IDS_PATH.exists():
        return set()
    with open(SEEN_IDS_PATH) as f:
        return set(json.load(f))


def save_seen_ids(seen_ids: set):
    trimmed = list(seen_ids)[-MAX_SEEN_IDS_KEPT:]
    with open(SEEN_IDS_PATH, "w") as f:
        json.dump(trimmed, f)


def format_post_text(item: dict, max_len: int) -> str:
    """Plain 'title + link' text, truncating the title if needed so the
    whole thing fits under max_len characters."""
    title = item.get("title", "").strip()
    link = item.get("link", "").strip()

    reserved = len(link) + 1 if link else 0
    room_for_title = max_len - reserved

    if len(title) > room_for_title:
        title = title[: max(room_for_title - 1, 0)].rstrip() + "…"

    return f"{title} {link}".strip() if link else title


# ---------- Bluesky ----------

def bluesky_configured() -> bool:
    return bool(BLUESKY_HANDLE and BLUESKY_APP_PASSWORD)


def post_to_bluesky(item: dict):
    from atproto import Client, client_utils

    client = Client()
    client.login(BLUESKY_HANDLE, BLUESKY_APP_PASSWORD)

    title = item.get("title", "").strip()
    link = item.get("link", "").strip()

    # 300 grapheme limit on Bluesky; leave a little headroom.
    max_len = 295

    if link:
        room_for_title = max_len - len(link) - 1
        display_title = title
        if len(display_title) > room_for_title:
            display_title = display_title[: max(room_for_title - 1, 0)].rstrip() + "…"
        builder = client_utils.TextBuilder().text(f"{display_title} ").link(link, link)
        client.send_post(builder)
    else:
        client.send_post(text=format_post_text(item, max_len=max_len))


# ---------- Facebook Page ----------

def facebook_configured() -> bool:
    return bool(FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN)


def post_to_facebook(item: dict):
    title = item.get("title", "").strip()
    link = item.get("link", "").strip()

    url = f"https://graph.facebook.com/{FACEBOOK_GRAPH_VERSION}/{FACEBOOK_PAGE_ID}/feed"
    payload = {
        "message": title,
        "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
    }
    if link:
        payload["link"] = link  # lets Facebook render a link preview card

    resp = requests.post(url, data=payload, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Facebook API error {resp.status_code}: {resp.text}")


# ---------- Discord ----------

def discord_configured() -> bool:
    return bool(DISCORD_WEBHOOK_URL)


def post_to_discord(item: dict):
    text = format_post_text(item, max_len=1900)  # Discord's limit is 2000
    resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Discord webhook error {resp.status_code}: {resp.text}")


# ---------- X ----------

def x_configured() -> bool:
    return all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET])


def post_to_x(item: dict):
    import tweepy

    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET,
    )
    text = format_post_text(item, max_len=280)
    client.create_tweet(text=text)


# ---------- Main ----------

DESTINATIONS = [
    ("bluesky", bluesky_configured, post_to_bluesky),
    ("facebook", facebook_configured, post_to_facebook),
    ("discord", discord_configured, post_to_discord),
    ("x", x_configured, post_to_x),
]


def main():
    active = [name for name, configured, _ in DESTINATIONS if configured()]
    if not active:
        print("No destination credentials configured — nothing to do.", file=sys.stderr)
    else:
        print(f"Active destinations: {', '.join(active)}")

    seen_ids = load_seen_ids()
    relevant_player_ids = load_relevant_player_ids()
    items = fetch_news()

    relevant_items = [
        i for i in items if str(i.get("player_id")) in relevant_player_ids
    ]
    new_items = [i for i in relevant_items if str(i.get("id")) not in seen_ids]
    print(
        f"Fetched {len(items)} items, {len(relevant_items)} about relevant "
        f"players, {len(new_items)} are new."
    )

    for item in new_items:
        item_id = str(item.get("id"))
        title = item.get("title", "(no title)")

        posted_anywhere = False

        for name, configured, post_fn in DESTINATIONS:
            if not configured():
                continue
            try:
                post_fn(item)
                print(f"[{name}] posted: {title}")
                posted_anywhere = True
            except Exception as e:
                print(f"[{name}] FAILED on '{title}': {e}", file=sys.stderr)

        if posted_anywhere:
            seen_ids.add(item_id)

    save_seen_ids(seen_ids)


if __name__ == "__main__":
    main()
