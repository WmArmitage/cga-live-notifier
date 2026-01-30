import os
import time
import sqlite3
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

import feedparser
import requests
import yaml

DB_PATH = "/app/data/state.db"
CFG_PATH = "/app/config.yml"

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"


def load_config():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_videos (
            video_id TEXT PRIMARY KEY,
            committee TEXT,
            first_seen_ts INTEGER
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS notified_live (
            video_id TEXT PRIMARY KEY,
            committee TEXT,
            notified_ts INTEGER
        )
    """)

    # next_check_ts lets us postpone checking far-future upcoming streams
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watch_queue (
            video_id TEXT PRIMARY KEY,
            committee TEXT,
            next_check_ts INTEGER
        )
    """)

    conn.commit()
    return conn


def now_ts() -> int:
    return int(time.time())


def parse_rfc3339(ts: str) -> int | None:
    if not ts:
        return None
    try:
        # Example: "2025-01-15T14:00:00Z"
        if ts.endswith("Z"):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(ts)
        return int(dt.timestamp())
    except Exception:
        return None


def extract_video_id(entry) -> str | None:
    link = entry.get("link", "")
    if not link:
        return None
    qs = parse_qs(urlparse(link).query)
    return qs.get("v", [None])[0]


def post_discord(webhook_url: str, content: str):
    resp = requests.post(webhook_url, json={"content": content}, timeout=15)
    resp.raise_for_status()


def yt_videos_list(video_ids: list[str]) -> dict:
    params = {
        "key": YOUTUBE_API_KEY,
        "part": "snippet,liveStreamingDetails",
        "id": ",".join(video_ids),
    }
    r = requests.get(YOUTUBE_VIDEOS_ENDPOINT, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def is_live(item: dict) -> bool:
    snippet = item.get("snippet") or {}
    return snippet.get("liveBroadcastContent") == "live"


def is_upcoming(item: dict) -> bool:
    snippet = item.get("snippet") or {}
    return snippet.get("liveBroadcastContent") == "upcoming"


def scheduled_start_ts(item: dict) -> int | None:
    lsd = item.get("liveStreamingDetails") or {}
    return parse_rfc3339(lsd.get("scheduledStartTime"))


def main():
    if not YOUTUBE_API_KEY:
        raise SystemExit("Missing YOUTUBE_API_KEY in environment (.env)")

    cfg = load_config()
    poll_cfg = cfg.get("poll", {}) or {}

    rss_minutes = int(poll_cfg.get("rss_minutes", 10))
    watch_seconds = int(poll_cfg.get("watch_seconds", 60))
    watch_window_hours = int(poll_cfg.get("watch_window_hours", 4))

    webhook = (cfg.get("global_webhook_url") or "").strip()
    if not webhook or "PASTE_" in webhook:
        raise SystemExit("Set global_webhook_url in config.yml to your Discord webhook URL.")

    committees = cfg.get("committees", []) or []
    if not committees:
        raise SystemExit("No committees found in config.yml")

    conn = init_db()
    cur = conn.cursor()

    last_rss_check = 0

    while True:
        now = now_ts()

        # 1) RSS sweep (free)
        if now - last_rss_check >= rss_minutes * 60:
            last_rss_check = now

            for c in committees:
                committee = c["name"]
                rss = c["rss"]

                feed = feedparser.parse(rss)
                for entry in feed.entries[:12]:
                    vid = extract_video_id(entry)
                    if not vid:
                        continue

                    # record first seen
                    cur.execute("SELECT 1 FROM seen_videos WHERE video_id=?", (vid,))
                    if cur.fetchone() is None:
                        cur.execute(
                            "INSERT INTO seen_videos(video_id, committee, first_seen_ts) VALUES(?,?,?)",
                            (vid, committee, now),
                        )
                        conn.commit()

                    # add/update watch queue: check soon (unless we already postponed)
                    cur.execute("SELECT next_check_ts FROM watch_queue WHERE video_id=?", (vid,))
                    row = cur.fetchone()
                    if row is None:
                        cur.execute(
                            "INSERT INTO watch_queue(video_id, committee, next_check_ts) VALUES(?,?,?)",
                            (vid, committee, now),  # check ASAP
                        )
                        conn.commit()

        # 2) Watch loop (API) - only check items whose next_check_ts <= now
        cur.execute("SELECT video_id, committee, next_check_ts FROM watch_queue WHERE next_check_ts <= ?", (now,))
        due = cur.fetchall()

        if due:
            # batch IDs (max 50 per API call)
            ids = [x[0] for x in due]
            id_to_committee = {x[0]: x[1] for x in due}

            for i in range(0, len(ids), 50):
                batch = ids[i:i+50]
                data = yt_videos_list(batch)
                items = {it["id"]: it for it in data.get("items", [])}

                for vid in batch:
                    committee = id_to_committee.get(vid, "Committee")
                    item = items.get(vid)

                    if not item:
                        # video disappeared or private; stop tracking
                        cur.execute("DELETE FROM watch_queue WHERE video_id=?", (vid,))
                        conn.commit()
                        continue

                    if is_live(item):
                        # notify once
                        cur.execute("SELECT 1 FROM notified_live WHERE video_id=?", (vid,))
                        if cur.fetchone() is None:
                            msg = f"**{committee} Committee is Live!**\nhttps://www.youtube.com/watch?v={vid}"
                            post_discord(webhook, msg)
                            cur.execute(
                                "INSERT INTO notified_live(video_id, committee, notified_ts) VALUES(?,?,?)",
                                (vid, committee, now),
                            )
                            conn.commit()

                        # stop tracking after notify
                        cur.execute("DELETE FROM watch_queue WHERE video_id=?", (vid,))
                        conn.commit()
                        continue

                    # If upcoming, postpone checks until within watch_window_hours of scheduled start
                    if is_upcoming(item):
                        start = scheduled_start_ts(item)
                        if start:
                            window = watch_window_hours * 3600
                            if start - now > window:
                                next_check = start - window
                                cur.execute(
                                    "UPDATE watch_queue SET next_check_ts=? WHERE video_id=?",
                                    (next_check, vid),
                                )
                                conn.commit()
                                continue

                    # Default: check again soon
                    cur.execute(
                        "UPDATE watch_queue SET next_check_ts=? WHERE video_id=?",
                        (now + watch_seconds, vid),
                    )
                    conn.commit()

        time.sleep(watch_seconds)


if __name__ == "__main__":
    main()
