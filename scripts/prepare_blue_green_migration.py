#!/usr/bin/env python3
"""Build and verify a Green migration generation without modifying Blue files.

Uses Google Drive files.copy for full-fidelity backups/staging. All writes go
to copied spreadsheets. The emitted manifest is an artifact, NOT activation.
"""
import argparse
import json
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from migrate_book0_book2 import (
    BOOK0_HEADERS, TARGET_HEADERS, VIDEO_HEADERS, MEMBERSHIP_HEADERS,
    CHANNEL_ID, PLAYLIST_ID, checked_rows, pad,
)

LEGACY_TARGET_HEADERS = [
    "active", "channel_url", "channel_id", "display_name", "playlist_id",
    "playlist_label", "sync_frequency", "max_items", "default_tags", "note",
]
LEGACY_VIDEO_HEADERS = [
    "sync_run_id", "sync_datetime", "channel_id", "channel_title",
    "playlist_id", "playlist_title", "video_id", "video_title",
    "video_description", "video_url", "thumbnail_url", "published_at",
    "duration_seconds", "position_in_playlist", "video_tags",
    "privacy_status", "origin_type",
]
TABS = {
    "book0": [("collection_requests", BOOK0_HEADERS, (0, 1))],
    "book1": [("collection_targets_v2", TARGET_HEADERS, (0,))],
    "book2": [
        ("videos_v2", VIDEO_HEADERS, (0,)),
        ("video_playlist_memberships", MEMBERSHIP_HEADERS, (0, 1)),
    ],
}
RANGES = {
    "book0": "collection_requests!A1:B1000",
    "book1": "collection_targets_v2!A1:J1000",
    "book2": "videos_v2!A1:N60000",
}
def call(request, label, attempts=9):
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            wait = min(90, 2 ** attempt + random.uniform(0, 1))
            print(f"{label}: HTTP {e.resp.status}, waiting {wait:.1f}s", flush=True)
            time.sleep(wait)

def index_rows(values, headers, keys, tab):
    if not values or values[0] != headers:
        raise ValueError(f"{tab}: unexpected headers")
    indexed = {}
    for raw in values[1:]:
        row = pad(raw, len(headers))
        key = tuple(row[i] for i in keys)
        if key in indexed:
            raise ValueError(f"{tab}: duplicate key {key}")
        indexed[key] = row
    return indexed

def read_index(sheets, spreadsheet, tab, headers, keys):
    values = call(sheets.values().get(
        spreadsheetId=spreadsheet, range=f"{tab}!A1:Z60000"
    ), f"read {tab}").get("values", [])
    return index_rows(values, headers, keys, tab)

def build_data(sheets, ids):
    legacy_targets = checked_rows(sheets, ids["book1"], "youtube_targets",
                                  LEGACY_TARGET_HEADERS, "J1000")
    legacy_videos = checked_rows(sheets, ids["book2"], "youtube_videos_raw",
                                 LEGACY_VIDEO_HEADERS, "Q60000")
    historic_ids = defaultdict(set)
    for raw in legacy_videos:
        row = pad(raw, 17)
        if row[2] and row[3]:
            historic_ids[row[3]].add(row[2])
    # Resolve using exact, historically observed titles.
    aliases = {"ゆる書道学ラジオ（書く書くしかじか）": "書く書くしかじか（ゆる書道学ラジオ）"}
    requests, targets = [], []
    for raw in legacy_targets:
        row = pad(raw, 10)
        if row[0].lower() not in ("true", "1", "yes"):
            continue
        name = row[3]
        matches = historic_ids.get(aliases.get(name, name), set())
        channel = row[2] or (next(iter(matches)) if len(matches) == 1 else "")
        if not CHANNEL_ID.fullmatch(channel) or (matches and channel not in matches):
            raise ValueError(f"Unresolved channel: {name}: {sorted(matches)}")
        playlist = row[4].strip()
        if playlist and not PLAYLIST_ID.fullmatch(playlist):
            raise ValueError(f"Invalid playlist ID: {name}")
        kind, source = ("PLAYLIST", playlist) if playlist else ("CHANNEL", channel)
        requests.append([kind, source])
        targets.append([f"{kind}:{source}", kind, source, channel, playlist,
                        name, row[5], "NEEDS_VERIFICATION", "", ""])
    videos, memberships = {}, {}
    for raw in legacy_videos:
        row = pad(raw, 17)
        video_id = row[6]
        if not video_id:
            continue
        if video_id not in videos:
            videos[video_id] = [
                video_id, row[7], row[8], row[9], row[10], row[11],
                row[12], row[2], row[1], row[1], "", "NOT_CHECKED", "", ""
            ]
        elif row[1] > videos[video_id][9]:
            videos[video_id][9] = row[1]
        for playlist in (v.strip() for v in row[4].split(",")):
            if not playlist:
                continue
            key = (video_id, playlist)
            if key not in memberships:
                memberships[key] = [video_id, playlist, "", row[1], row[1], "OBSERVED"]
            elif row[1] > memberships[key][4]:
                memberships[key][4] = row[1]
    if len(requests) != len({tuple(row) for row in requests}):
        raise ValueError("Duplicate collection targets")
    return {
        "book0": {"collection_requests": requests},
        "book1": {"collection_targets_v2": targets},
        "book2": {
            "videos_v2": list(videos.values()),
            "video_playlist_memberships": list(memberships.values()),
        },
    }

def prepare_plan(sheets, ids, desired):
    plan = {}
    for book, tabs in TABS.items():
        plan[book] = {}
        for tab, headers, keys in tabs:
            existing = read_index(sheets, ids[book], tab, headers, keys)
            additions = []
            seen = set(existing)
            for row in desired[book][tab]:
                key = tuple(row[i] for i in keys)
                if key not in seen:
                    additions.append(row)
                    seen.add(key)
            plan[book][tab] = {"existing": len(existing), "add": additions,
                               "expected": len(seen)}
    return plan

def copy_book(drive, source, label, generation):
    result = call(drive.files().copy(
        fileId=source,
        body={"name": f"YuruLink GREEN {generation} {label}"},
        fields="id,name,webViewLink",
    ), f"copy {label}")
    return result["id"]

def append_chunks(sheets, book_id, tab, rows, width, max_bytes=1200000):
    if not rows:
        return
    chunks, chunk, size = [], [], 0
    for row in rows:
        encoded_size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 4
        if encoded_size > max_bytes:
            raise ValueError(f"One row exceeds payload budget: {tab}")
        if chunk and size + encoded_size > max_bytes:
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(row)
        size += encoded_size
    if chunk:
        chunks.append(chunk)
    end_col = chr(64 + width)
    for number, values in enumerate(chunks, 1):
        # append is non-idempotent on ambiguous network failures; do not blindly
        # retry an uncertain append. Re-read the staging tab and resume instead.
        call(sheets.values().append(
            spreadsheetId=book_id, range=f"{tab}!A:{end_col}",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": values},
        ), f"{tab} chunk {number}/{len(chunks)}")
        print(f"{tab}: wrote chunk {number}/{len(chunks)} ({len(values)} rows)", flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Create and fill Green copies")
    parser.add_argument("--manifest", default="green-manifest.json")
    args = parser.parse_args()
    credentials, _ = google.auth.default(scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    ids = {key: os.environ[f"{key.upper()}_SPREADSHEET_ID"]
           for key in ("book0", "book1", "book2")}
    desired = build_data(sheets, ids)
    plan = prepare_plan(sheets, ids, desired)
    summary = {book: {tab: {"existing": p["existing"], "missing": len(p["add"]),
                            "expected": p["expected"]}
                      for tab, p in tabs.items()} for book, tabs in plan.items()}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not args.apply:
        print("DRY RUN: no copies or writes made", flush=True)
        return
    generation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = {"generation_id": generation, "status": "PREPARING",
                "source": ids, "green": {}, "expected": summary,
                "activated": False}
    def save():
        with open(args.manifest, "w", encoding="utf-8") as out:
            json.dump(manifest, out, ensure_ascii=False, indent=2)
    save()
    try:
        for book in ids:
            manifest["green"][book] = copy_book(drive, ids[book], book, generation)
            save()
        for book, tabs in TABS.items():
            for tab, headers, keys in tabs:
                append_chunks(sheets, manifest["green"][book], tab,
                              plan[book][tab]["add"], len(headers))
                actual = read_index(sheets, manifest["green"][book], tab, headers, keys)
                if len(actual) != plan[book][tab]["expected"]:
                    raise ValueError(f"{book}/{tab}: expected {plan[book][tab]['expected']} rows, got {len(actual)}")
                print(f"VERIFIED {book}/{tab}: {len(actual)} rows", flush=True)
        manifest["status"] = "READY_FOR_ACTIVATION"
        save()
        print("GREEN READY; BLUE UNCHANGED; NOT ACTIVATED", flush=True)
    except BaseException:
        manifest["status"] = "FAILED_RETAIN_GREEN_FOR_INSPECTION"
        save()
        raise

if __name__ == "__main__":
    main()
