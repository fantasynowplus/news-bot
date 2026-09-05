# FantasyPros News Bot

Pulls NFL news from the FantasyPros API every hour, filters it down to
players who are actually fantasy-relevant, and posts new items to
Bluesky, Facebook, and/or Discord (X optional — see below). Keeps track
of what it's already posted in `seen_ids.json` so nothing repeats.

---

## 1. Create the repo

1. Create a new GitHub repo and push all these files to it (`bot.py`,
   `requirements.txt`, `seen_ids.json`, `relevant_players.json`,
   `.github/workflows/post-news.yml`, this README).
2. Go to **Settings → Secrets and variables → Actions** in that repo —
   this is where every credential below gets pasted in as a secret.
   Never put credentials directly in `bot.py` or commit them.

---

## 2. FantasyPros API key

You already have this. Add it as a secret:

- `FANTASYPROS_API_KEY`

---

## 3. Bluesky setup

1. Log into Bluesky (the account you want the bot to post as).
2. Go to **Settings → Privacy and Security → App Passwords → Add App
   Password**. Name it something like `news-bot`. Copy the generated
   password (format `xxxx-xxxx-xxxx-xxxx`) — you won't see it again.
3. Add two secrets:
   - `BLUESKY_HANDLE` — e.g. `yourname.bsky.social`
   - `BLUESKY_APP_PASSWORD` — the app password from step 2 (**not**
     your real account password)

That's it — no app review, no cost.

---

## 4. Discord setup

1. In Discord, open the server and go to the channel you want news
   posted into.
2. Channel settings (gear icon) → **Integrations** → **Webhooks** →
   **New Webhook**. Give it a name/avatar if you want.
3. Click **Copy Webhook URL**.
4. Add one secret:
   - `DISCORD_WEBHOOK_URL`

That's the entire Discord setup — no bot, no token, no approval process.

---

## 5. Facebook Page setup

This one has more steps, but you will **not** need Facebook's App
Review process since you're posting to a Page you personally own/admin.

1. Go to **developers.facebook.com** → **My Apps** → **Create App** →
   choose type **Business**. Give it a name.
2. In the app dashboard, add the **Facebook Login for Business**
   product (this is what lets you generate Page tokens).
3. Open **Graph API Explorer**
   (developers.facebook.com/tools/explorer):
   - Select your app in the top-right dropdown.
   - Click **Generate Access Token**, log in, and grant the
     `pages_manage_posts` and `pages_read_engagement` permissions when
     prompted (you may need to add these under **Permissions** in the
     tool first).
   - This gives you a short-lived **User Access Token**.
4. Exchange it for a long-lived token (valid ~60 days) by hitting this
   URL in a browser, filling in your own values:
   ```
   https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=YOUR_APP_ID&client_secret=YOUR_APP_SECRET&fb_exchange_token=YOUR_SHORT_LIVED_USER_TOKEN
   ```
   (App ID and App Secret are in your app's **Settings → Basic**.)
   This returns a long-lived user access token.
5. Use that long-lived user token to get your Page's own token, which
   effectively never expires as long as you don't revoke it:
   ```
   https://graph.facebook.com/v21.0/me/accounts?access_token=YOUR_LONG_LIVED_USER_TOKEN
   ```
   This returns a list of Pages you manage, each with an `id` and an
   `access_token`. Grab the ones for the Page you want the bot posting
   to.
6. Add two secrets:
   - `FACEBOOK_PAGE_ID` — the Page's numeric `id` from step 5
   - `FACEBOOK_PAGE_ACCESS_TOKEN` — the Page's `access_token` from
     step 5

If posting ever starts failing with an auth error months from now, it
usually means the underlying token expired — regenerate from step 3.

---

## 6. X (Twitter) — optional, costs money

X moved to pay-per-use pricing in 2026 (no more free tier). Skip this
section unless you want to pay for it.

1. Create a developer app at developer.x.com/console. Set its
   permissions to **Read and Write**.
2. Load prepaid credits in the console (posting with a link costs
   about $0.20/post as of 2026 — check current rates there).
3. Add four secrets:
   - `X_API_KEY`
   - `X_API_SECRET`
   - `X_ACCESS_TOKEN`
   - `X_ACCESS_SECRET`

The bot checks for these automatically — if they're not set, it just
skips X and posts everywhere else.

---

## Configuration knobs

Set these as repo secrets/variables or edit `bot.py` directly:

- `FP_SPORT` — defaults to `nfl`.
- `FP_NEWS_LIMIT` — how many recent items to fetch per run (max 25).
- `FP_SCORING` — `PPR` (default), `STD`, or `HALF` — affects the
  consensus rankings used for relevance filtering.
- `POSITION_LIMITS` (in `bot.py`) — per-position rank cutoffs for what
  counts as "relevant." Defaults:
  ```python
  POSITION_LIMITS = {"QB": 32, "RB": 60, "WR": 80, "TE": 32, "K": 32, "DST": 32}
  ```

## How relevance filtering works

Before posting, the bot checks each news item's `player_id` against
FantasyPros' consensus rankings and only posts if that player falls
within `POSITION_LIMITS` for their position. Items with no `player_id`
(general league notes not tied to a player) are skipped. Rankings are
cached in `relevant_players.json` and refreshed once a day.

## How dedupe works

Each news item has a unique `id`. After a successful post to at least
one destination, that ID is added to `seen_ids.json`, which the
workflow commits back to the repo. The file is trimmed to the most
recent 1000 IDs.

## Testing locally

```bash
pip install -r requirements.txt
export FANTASYPROS_API_KEY=xxx
export BLUESKY_HANDLE=yourname.bsky.social
export BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
export FACEBOOK_PAGE_ID=xxx
export FACEBOOK_PAGE_ACCESS_TOKEN=xxx
python bot.py
```

Any destination whose environment variables aren't set is simply
skipped — you can test one platform at a time.
