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
    "QB": 32,
    "RB": 60,
    "WR": 80,
    "TE": 32,
}
SCORING = os.environ.get("FP_SCORING", "PPR")  # STD, PPR, or HALF
RANKINGS_MAX_AGE_HOURS = int(os.environ.get("FP_RANKINGS_MAX_AGE_HOURS", "20"))

# Bluesky
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE")
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD")

# Facebook Page
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN")

# Discord
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# X (optional — add these secrets later if/when you get X API credits)
X_API_KEY = os.environ.get("X_API_KEY")
X_API_SECRET = os.environ.get("X_API_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET")

SEEN_IDS_PATH = Path(__file__).parent / "seen_ids.json"
MAX_SEEN_IDS_KEPT = 1000  # trim the file so it doesn't grow forever

RELEVANT_PLAYERS_PATH = Path(__file__).parent / "relevant_players.json"

FANTASYPROS_URL = f"https://api.fantasypros.com/public/v2/json/{SPORT}/news"
RANKINGS_URL_TMPL = (
    "https://api.fantasypros.com/public/v2/json/{sport}/{season}/consensus-rankings"
)
FACEBOOK_GRAPH_VERSION = "v21.0"


# ---------- Relevant player filtering ----------

def fetch_relevant_players() -> dict:
    """Pulls consensus rankings, one call per position (the API doesn't
    accept position=ALL), and returns {player_id: player_name} for
    players ranked within POSITION_LIMITS for their position."""
    season = datetime.utcnow().year
    relevant = {}

    for position, limit in POSITION_LIMITS.items():
        resp = requests.get(
            RANKINGS_URL_TMPL.format(sport=SPORT, season=season),
            headers={"x-api-key": FANTASYPROS_API_KEY},
            params={"position": position, "scoring": SCORING},
            timeout=30,
        )
        if not resp.ok:
            print(f"Rankings request failed for {position} ({resp.status_code}): {resp.text}", file=sys.stderr)
        resp.raise_for_status()
        players = resp.json().get("players", [])

        # Sort by ECR ascending (best players first) and take the top N
        # for this position, regardless of the order the API returns.
        players.sort(key=lambda p: float(p.get("rank_ecr", 9999)))
        for p in players[:limit]:
            relevant[str(p.get("player_id"))] = p.get("player_name", "")

    return relevant


def load_relevant_players() -> dict:
    """Returns cached {player_id: player_name}, refreshing from the API
    if the cache is missing or older than RANKINGS_MAX_AGE_HOURS."""
    if RELEVANT_PLAYERS_PATH.exists():
        with open(RELEVANT_PLAYERS_PATH) as f:
            cache = json.load(f)
        age_hours = (time.time() - cache.get("fetched_at", 0)) / 3600
        if age_hours < RANKINGS_MAX_AGE_HOURS:
            return cache.get("players", {})

    try:
        players = fetch_relevant_players()
    except Exception as e:
        print(f"Failed to refresh rankings, falling back to stale/no cache: {e}", file=sys.stderr)
        if RELEVANT_PLAYERS_PATH.exists():
            with open(RELEVANT_PLAYERS_PATH) as f:
                return json.load(f).get("players", {})
        return {}

    with open(RELEVANT_PLAYERS_PATH, "w") as f:
        json.dump({"fetched_at": time.time(), "players": players}, f)
    return players


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
    impact = (item.get("impact") or "").strip()
    category = (item.get("category") or "").strip().lower()
    emoji = CATEGORY_EMOJI.get(category, "📰")

    # 300 grapheme limit on Bluesky; leave a little headroom.
    max_len = 295
    reserved_link = len(link) + 1 if link else 0

    header = f"{emoji} {title}".strip()
    if len(header) > max_len - reserved_link:
        header = header[: max(max_len - reserved_link - 1, 0)].rstrip() + "…"

    body_parts = [header]
    used = len(header)
    if impact:
        remaining = max_len - used - reserved_link - 2  # 2 chars for the blank line
        if remaining > 20:  # not worth including a sliver of a sentence
            snippet = impact
            if len(snippet) > remaining:
                snippet = snippet[: remaining - 1].rstrip() + "…"
            body_parts.append(snippet)

    text_body = "\n\n".join(body_parts)

    if link:
        builder = client_utils.TextBuilder().text(f"{text_body} ").link(link, link)
        client.send_post(builder)
    else:
        client.send_post(text=text_body)


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

CATEGORY_COLORS = {
    "injury": 0xE74C3C,      # red
    "breaking": 0xF1C40F,    # gold
    "rumor": 0x9B59B6,       # purple
    "transaction": 0x2ECC71, # green
    "recap": 0x3498DB,       # blue
}
CATEGORY_EMOJI = {
    "injury": "🚨",
    "breaking": "⚡",
    "rumor": "🔍",
    "transaction": "🔄",
    "recap": "📋",
}
DEFAULT_EMBED_COLOR = 0x95A5A6  # gray fallback for unknown categories


def discord_configured() -> bool:
    return bool(DISCORD_WEBHOOK_URL)


def post_to_discord(item: dict):
    title = item.get("title", "(no title)").strip()
    link = item.get("link", "").strip()
    impact = (item.get("impact") or "").strip()
    category = (item.get("category") or "").strip().lower()
    team = (item.get("team_id") or "").strip()

    emoji = CATEGORY_EMOJI.get(category, "📰")
    color = CATEGORY_COLORS.get(category, DEFAULT_EMBED_COLOR)

    embed = {
        "title": f"{emoji} {title}"[:256],
        "color": color,
    }
    if link:
        embed["url"] = link
    if impact:
        embed["description"] = impact[:4000]

    footer_bits = [b for b in [category.capitalize(), team] if b]
    if footer_bits:
        embed["footer"] = {"text": " • ".join(footer_bits)}

    created = item.get("created")
    if created:
        try:
            dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            embed["timestamp"] = dt.isoformat() + "Z"
        except ValueError:
            pass  # timestamp is optional; skip if the format doesn't parse

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=30)
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
    relevant_player_ids = set(load_relevant_players().keys())
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
