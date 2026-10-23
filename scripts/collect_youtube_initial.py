#!/usr/bin/env python3
"""Initial YouTube inventory into Book2 (append-only raw snapshot).

Credentials:
  YOUTUBE_API_KEY: YouTube Data API v3 key
  GOOGLE_SERVICE_ACCOUNT_JSON: full JSON service account credential
  BOOK1_SPREADSHEET_ID / BOOK2_SPREADSHEET_ID
Share both Sheets with the service account email (Editor).
"""
import json
import os
import time
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HEADERS = [
    "sync_run_id", "sync_datetime", "channel_id", "channel_title",
    "playlist_id", "playlist_title", "video_id", "video_title",
    "video_description", "video_url", "thumbnail_url", "published_at",
    "duration_seconds", "position_in_playlist", "video_tags",
    "privacy_status", "origin_type",
]
CONFIG_HEADERS = [
    "active", "channel_url", "channel_id", "display_name", "playlist_id",
    "playlist_label", "sync_frequency", "max_items", "default_tags", "note",
]

def pages(resource, **params):
    token = None
    while True:
        request = dict(params)
        if token:
            request["pageToken"] = token
        for attempt in range(5):
            try:
                response = resource.list(**request).execute()
                break
            except HttpError as error:
                if error.resp.status not in (429, 500, 502, 503, 504) or attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        yield from response.get("items", [])
        token = response.get("nextPageToken")
        if not token:
            break

def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]

def main():
    youtube = build("youtube", "v3", developerKey=os.environ["YOUTUBE_API_KEY"], cache_discovery=False)
    credentials = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
    book1 = os.environ["BOOK1_SPREADSHEET_ID"]
    book2 = os.environ["BOOK2_SPREADSHEET_ID"]
    config = sheets.values().get(spreadsheetId=book1, range="youtube_targets!A1:J1000").execute().get("values", [])
    if not config or config[0] != CONFIG_HEADERS:
        raise ValueError("Book1 youtube_targets headers do not match the expected schema")
    targets = [dict(zip(CONFIG_HEADERS, row + [""] * (len(CONFIG_HEADERS) - len(row))))
               for row in config[1:] if row and str(row[0]).lower() in ("true", "1", "yes")]
    if not targets:
        raise ValueError("No active channels in Book1; refusing to run")
    existing = sheets.values().get(spreadsheetId=book2, range="youtube_videos_raw!A1:Q1").execute().get("values", [])
    if not existing:
        sheets.values().update(spreadsheetId=book2, range="youtube_videos_raw!A1",
                               valueInputOption="RAW", body={"values": [HEADERS]}).execute()
    elif existing[0] != HEADERS:
        raise ValueError("Book2 headers differ; refusing to overwrite")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    seen = set()
    output = []
    for target in targets:
        channel_id = target["channel_id"].strip()
        url = target["channel_url"].rstrip("/")
        if not channel_id:
            handle = url.rsplit("/", 1)[-1]
            if not handle.startswith("@"):
                raise ValueError(f"Cannot resolve channel URL: {url}")
            found = youtube.channels().list(part="snippet,contentDetails", forHandle=handle).execute().get("items", [])
        else:
            found = youtube.channels().list(part="snippet,contentDetails", id=channel_id).execute().get("items", [])
        if len(found) != 1:
            raise ValueError(f"Channel resolution failed or ambiguous: {url}")
        channel = found[0]
        channel_id = channel["id"]
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
        specified = target["playlist_id"].strip()
        playlist_info = {}
        if specified:
            result = youtube.playlists().list(part="snippet", id=specified).execute().get("items", [])
            if not result:
                raise ValueError(f"Playlist not found: {specified}")
            playlist_info[specified] = result[0]["snippet"]["title"]
        else:
            for item in pages(youtube.playlists(), part="snippet", channelId=channel_id, maxResults=50):
                playlist_info[item["id"]] = item["snippet"]["title"]
            # Always include uploads, even if the channel also has playlists.
            playlist_info.setdefault(uploads, "Uploads")
        videos = {}
        for playlist_id, playlist_title in playlist_info.items():
            for position, item in enumerate(pages(youtube.playlistItems(), part="snippet,contentDetails",
                                                   playlistId=playlist_id, maxResults=50)):
                video_id = item.get("contentDetails", {}).get("videoId")
                if not video_id or video_id in seen or video_id in videos:
                    continue
                videos[video_id] = (playlist_id, playlist_title, position)
        for batch in chunks(list(videos), 50):
            details = youtube.videos().list(part="snippet,contentDetails,status", id=",".join(batch),
                                           maxResults=50).execute().get("items", [])
            for video in details:
                video_id = video["id"]
                if video_id in seen:
                    continue
                seen.add(video_id)
                snippet = video["snippet"]
                playlist_id, playlist_title, position = videos[video_id]
                output.append([
                    run_id, run_id, channel_id, channel["snippet"]["title"],
                    playlist_id, playlist_title, video_id, snippet.get("title", ""),
                    snippet.get("description", ""), f"https://www.youtube.com/watch?v={video_id}",
                    snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                    snippet.get("publishedAt", ""), video["contentDetails"].get("duration", ""),
                    position, ",".join(snippet.get("tags", [])),
                    video.get("status", {}).get("privacyStatus", ""),
                    "uploads" if playlist_id == uploads else "playlist",
                ])
        print(f"{target['display_name']}: {len(videos)} playlist entries")
    # Batch writes limit request count and make initial runs restart-friendly by checking IDs.
    previous = sheets.values().get(spreadsheetId=book2, range="youtube_videos_raw!G2:G").execute().get("values", [])
    stored = {row[0] for row in previous if row}
    new_rows = [row for row in output if row[6] not in stored]
    for batch in chunks(new_rows, 300):
        sheets.values().append(spreadsheetId=book2, range="youtube_videos_raw!A:Q",
                               valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                               body={"values": batch}).execute()
    print(f"Collected {len(output)} unique videos; appended {len(new_rows)} new rows")

if __name__ == "__main__":
    main()
