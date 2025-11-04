#!/usr/bin/env python3
"""
KUBookRecs: Auto-Reply Bot (GO-LIVE V3)
---------------------------------------
- Ready for production posting on public subs.
- Reads ALL secrets from env for safety:
    REDDIT_CLIENT_ID
    REDDIT_CLIENT_SECRET
    REDDIT_USERNAME
    REDDIT_PASSWORD           (use App Password or "password:123456" if 2FA)
- Defaults include your current app id/secret, but env overrides are supported.
- Guardrails enabled (be nice to mods!).

Usage (example):
    export REDDIT_USERNAME="Arijenn2891"
    export REDDIT_PASSWORD="your-app-password-or-pass:code"
    python kubookrecs_bot_live_log.py
"""

import os, re, time, traceback
from datetime import datetime, timezone
import pandas as pd
import praw
from praw.exceptions import APIException, RedditAPIException

# -------------------- CONFIG --------------------
ALLOWED_SUBS = ["kubookrecs", "books"]
LIMIT_PER_SUB = 25
MAX_REPLIES_PER_RUN = 3
COOLDOWN_SECONDS = 120
MAX_POST_AGE_HOURS = 24

# App credentials (env overrides supported)
CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "y0jPsjIR2NsSKS32H1-xOQ")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "lbO6vsdC1XFvoz6IA-BZezCPjLyeuA")
USER_AGENT = os.getenv("REDDIT_USER_AGENT", "KUBookRecsBot:v1.0 (by u/Arijenn2891)")

# Account credentials (required)
USERNAME = os.getenv("REDDIT_USERNAME", "")
PASSWORD = os.getenv("REDDIT_PASSWORD", "")

CSV_PATH = "book_recommendations.csv"

# -------------------- CONNECT --------------------
def connect():
    if not USERNAME or not PASSWORD:
        raise SystemExit("Missing REDDIT_USERNAME or REDDIT_PASSWORD environment variables.")
    reddit = praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT,
        username=USERNAME,
        password=PASSWORD,
        check_for_async=False
    )
    me = str(reddit.user.me())
    print(f"Authenticated as: {me}")
    return reddit, me

# -------------------- DATA --------------------
REQUIRED_COLS = ["title","author","year","genres","tropes","vibes","heat","pacing",
                 "format_availability","ku","audio","pages","content_notes","comps",
                 "hook_line","why_readers_might_like"]

def load_books(path):
    df = pd.read_csv(path, dtype=str).fillna("")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    df["ku"] = df["ku"].str.lower().isin(["true","1","yes","y"])
    df["audio"] = df["audio"].str.lower().isin(["true","1","yes","y"])
    df["pages"] = pd.to_numeric(df["pages"], errors="coerce").fillna(0).astype(int)
    return df

# -------------------- PREFS EXTRACTION --------------------
GENRES = ["romance","romantic suspense","cozy mystery","mystery","thriller",
          "fantasy","science fiction","sci-fi","space opera",
          "historical fiction","wwii","women's fiction"]
TROPES = ["found family","enemies to lovers","slow burn","grumpy sunshine",
          "heist","fake dating","amateur sleuth","secret identity","second chance",
          "dual timeline","forbidden love","forced proximity"]
VIBES  = ["hopeful","dark","witty","cozy","bittersweet","gritty","tender","atmospheric","high-octane","epic","steady","fast"]
NOPE_KEYWORDS = ["animal death","on-page sa","sexual assault","cheating","graphic violence","gore"]

def extract_prefs(text: str):
    low = text.lower()
    prefs = {
        "genres_like": [], "tropes": [], "vibes": [], "heat": None,
        "ku": None, "audio": None, "hard_nopes": [], "max_pages": None, "comps_like": []
    }
    for g in GENRES:
        if g in low:
            prefs["genres_like"].append("WWII historical fiction" if g=="wwii" else g)
    for t in TROPES:
        if t in low: prefs["tropes"].append(t)
    for v in VIBES:
        if v in low: prefs["vibes"].append(v)
    if re.search(r"\bclosed(-|\s)?door\b|clean\b", low): prefs["heat"] = "closed"
    elif "fade to black" in low: prefs["heat"] = "fade"
    elif re.search(r"\bopen(-|\s)?door\b|spice|steamy|spicy", low): prefs["heat"] = "open"
    if "kindle unlimited" in low or re.search(r"\bku\b", low): prefs["ku"] = True
    if "audiobook" in low or "audio" in low: prefs["audio"] = True
    m = re.search(r"(?:under|<)\s*(\d{2,4})\s*pages", low)
    if m: prefs["max_pages"] = int(m.group(1))
    for nope in NOPE_KEYWORDS:
        if nope in low or f"no {nope.split()[0]}" in low:
            prefs["hard_nopes"].append(nope)
    m2 = re.search(r"like\s+([A-Za-z0-9'\":\- ]{3,60})", text, flags=re.I)
    if m2: prefs["comps_like"].append(m2.group(1).strip())
    return prefs

# -------------------- SCORING --------------------
def contains_any(cell, terms):
    c = str(cell).lower()
    return any(t.lower() in c for t in terms)

def score_row(row, prefs):
    s = 0
    if prefs["genres_like"]: s += 3 * contains_any(row["genres"], prefs["genres_like"])
    if prefs["tropes"]:      s += 2 * contains_any(row["tropes"], prefs["tropes"])
    if prefs["vibes"]:       s += 1 * contains_any(row["vibes"], prefs["vibes"])
    if prefs["heat"] and prefs["heat"] in str(row["heat"]).lower(): s += 1
    if prefs["ku"] and bool(row["ku"]): s += 1
    if prefs["audio"] and bool(row["audio"]): s += 1
    try:
        if prefs["max_pages"] and int(row["pages"]) > prefs["max_pages"]:
            s -= 2
    except Exception:
        pass
    if prefs["hard_nopes"] and contains_any(row["content_notes"], prefs["hard_nopes"]):
        s -= 4
    if prefs["comps_like"] and contains_any(row["comps"], prefs["comps_like"]):
        s += 2
    return s

def pick_books(df, prefs, k=4):
    scored = df.copy()
    scored["score"] = scored.apply(lambda r: score_row(r, prefs), axis=1)
    best = scored.sort_values("score", ascending=False).head(k)
    return best[best["score"] > 0]

# -------------------- RENDER --------------------
def summarize_prefs(prefs):
    bits = []
    if prefs["genres_like"]: bits.append(", ".join(prefs["genres_like"]))
    if prefs["tropes"]: bits.append(prefs["tropes"][0] + " trope")
    if prefs["vibes"]: bits.append(prefs["vibes"][0] + " vibe")
    if prefs["heat"]: bits.append(prefs["heat"] + "-door")
    if prefs["ku"]: bits.append("KU ok")
    if prefs["audio"]: bits.append("audio ok")
    if prefs["hard_nopes"]: bits.append("avoid: " + ", ".join(prefs["hard_nopes"]))
    return ", ".join(bits) if bits else "your preferences"

def render_reply(prefs, picks):
    lines = [f"Based on {summarize_prefs(prefs)}, here are some picks:"]
    for _, r in picks.iterrows():
        why = r["why_readers_might_like"] or r["hook_line"]
        cw = f"  **CW:** {r['content_notes']}" if r["content_notes"] else ""
        lines.append(f"• **{r['title']}** by {r['author']} — {why}{cw}")
    lines.append("\nWant KU-only or audio-first options? I can swap.")
    return "\n".join(lines)

# -------------------- GUARDRAILS --------------------
def is_recent(post):
    age_hours = (datetime.now(timezone.utc) - datetime.fromtimestamp(post.created_utc, tz=timezone.utc)).total_seconds()/3600
    return age_hours <= MAX_POST_AGE_HOURS

def already_replied(post, my_username):
    try:
        post.comments.replace_more(limit=0)
        for c in post.comments.list():
            if str(c.author).lower() == my_username.lower():
                return True
    except Exception:
        pass
    return False

def looks_like_request(text):
    t = text.lower()
    return bool(re.search(r"\b(rec|recommend|suggest|looking for|what should i read)\b", t))

# -------------------- MAIN --------------------
def main():
    books = load_books(CSV_PATH)
    reddit, me = connect()
    replies = 0

    for sub in ALLOWED_SUBS:
        print(f"\n=== r/{sub} ===")
        for post in reddit.subreddit(sub).new(limit=LIMIT_PER_SUB):
            if replies >= MAX_REPLIES_PER_RUN:
                print(f"Reached MAX_REPLIES_PER_RUN ({MAX_REPLIES_PER_RUN}). Stopping.")
                return

            text = f"{post.title}\n\n{post.selftext or ''}"
            if not looks_like_request(text): 
                continue
            if not is_recent(post): 
                continue
            if str(post.author).lower() == me.lower(): 
                continue
            if already_replied(post, me): 
                continue

            prefs = extract_prefs(text)
            picks = pick_books(books, prefs, k=4)
            if picks.empty: 
                continue

            reply = render_reply(prefs, picks)
            print("-"*80)
            print(post.title, "|", post.permalink)
            print(reply)

            try:
                post.reply(reply)
                replies += 1
                print(f"✅ Replied. Cooldown {COOLDOWN_SECONDS}s…")
                time.sleep(COOLDOWN_SECONDS)
            except (APIException, RedditAPIException) as e:
                print(f"⚠️ API error: {e}. Skipping this post.")
                time.sleep(10)
            except Exception:
                print("⚠️ Unexpected error:\n", traceback.format_exc())
                time.sleep(10)

    print(f"Done. Total replies this run: {replies}")

if __name__ == "__main__":
    main()
