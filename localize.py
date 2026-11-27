#!/usr/bin/env python3
"""Add Portuguese (and English) subtitles plus a pt-BR title/description to published Wild Rank videos.

Runs on a schedule in GitHub Actions. For every job in jobs/*.json whose title matches a video on the
channel, it uploads <slug>_pt.srt / <slug>_en.srt from the job's release (if those tracks are missing)
and sets the pt-BR localization from the job's title_pt / description_pt.

Env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN (OAuth, scope youtube.force-ssl).
"""
import json
import os
import sys
from pathlib import Path

import requests

API = "https://www.googleapis.com/youtube/v3"
RELEASES = "https://github.com/wildranktv-dev/wildrank-pipeline/releases/download"


def token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}, timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


def main():
    if not all(os.environ.get(k) for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")):
        print("YouTube API secrets not configured; skipping.")
        return
    H = {"Authorization": f"Bearer {token()}"}

    def get(path, **params):
        r = requests.get(f"{API}/{path}", headers=H, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    ch = get("channels", part="contentDetails", mine="true")["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    videos = {}
    page = None
    while True:
        params = dict(part="snippet", playlistId=uploads, maxResults=50)
        if page:
            params["pageToken"] = page
        data = get("playlistItems", **params)
        for it in data.get("items", []):
            videos[it["snippet"]["title"].strip()] = it["snippet"]["resourceId"]["videoId"]
        page = data.get("nextPageToken")
        if not page:
            break

    for jp in sorted(Path("jobs").glob("*.json")):
        job = json.loads(jp.read_text())
        vid = videos.get(job["title"].strip())
        if not vid:
            continue
        slug = job["slug"]
        print(f"== {slug} -> {vid}")

        tracks = get("captions", part="snippet", videoId=vid).get("items", [])
        have = {(t["snippet"]["language"].lower(), t["snippet"].get("trackKind")) for t in tracks}
        for lang, code, name in (("pt", "pt-BR", "Português"), ("en", "en", "English")):
            if any(l.startswith(lang) and k != "asr" for l, k in have):
                print(f"   {code} subtitles already present")
                continue
            srt = requests.get(f"{RELEASES}/{slug}/{slug}_{lang}.srt", timeout=60)
            if not srt.ok:
                print(f"   no {lang} srt in release")
                continue
            meta = json.dumps({"snippet": {"videoId": vid, "language": code, "name": name}})
            r = requests.post(
                "https://www.googleapis.com/upload/youtube/v3/captions",
                params={"part": "snippet", "uploadType": "multipart"}, headers=H, timeout=120,
                files={"metadata": ("metadata", meta, "application/json; charset=UTF-8"),
                       "file": (f"{slug}_{lang}.srt", srt.content, "application/octet-stream")})
            print(f"   upload {code} subtitles: {r.status_code} {r.text[:200] if not r.ok else ''}")

        if job.get("title_pt"):
            v = get("videos", part="snippet,localizations", id=vid)["items"][0]
            loc = v.get("localizations", {})
            if "pt-BR" in loc:
                print("   pt-BR title already set")
                continue
            sn = v["snippet"]
            body = {
                "id": vid,
                "snippet": {"title": sn["title"], "description": sn.get("description", ""),
                            "categoryId": sn["categoryId"], "tags": sn.get("tags", []),
                            "defaultLanguage": sn.get("defaultLanguage") or "en"},
                "localizations": {**loc, "pt-BR": {"title": job["title_pt"][:100],
                                                   "description": job.get("description_pt", "")[:5000]}},
            }
            r = requests.put(f"{API}/videos", params={"part": "snippet,localizations"}, headers=H,
                             json=body, timeout=60)
            print(f"   pt-BR title/description: {r.status_code} {r.text[:200] if not r.ok else ''}")


if __name__ == "__main__":
    sys.exit(main())
