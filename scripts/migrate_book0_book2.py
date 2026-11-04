#!/usr/bin/env python3
"""Idempotent, opt-in migration of YuruLink Book0/1/2.

Run with --dry-run first. Uses ADC/WIF. Does not change legacy tabs or workflows.
"""
import argparse
import os
import re
from collections import defaultdict

import google.auth
from googleapiclient.discovery import build

BOOK0_HEADERS = ["target_type", "target_id"]
TARGET_HEADERS = ["target_key", "source_type", "source_id", "resolved_channel_id",
                  "resolved_playlist_id", "channel_title", "playlist_title",
                  "collection_status", "last_success_at", "last_error"]
VIDEO_HEADERS = ["video_id", "video_title", "video_description", "video_url",
                 "thumbnail_url", "published_at", "duration_iso8601", "channel_id",
                 "first_seen_at", "last_seen_at", "last_checked_at",
                 "availability_status", "unavailable_since", "availability_reason"]
MEMBERSHIP_HEADERS = ["video_id", "playlist_id", "target_key", "first_seen_at",
                      "last_seen_at", "membership_status"]
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def rows(api, book, tab, end):
    return api.values().get(spreadsheetId=book, range=f"{tab}!A1:{end}").execute().get("values", [])


def checked_rows(api, book, tab, headers, end):
    result = rows(api, book, tab, end)
    if not result or result[0] != headers:
        raise ValueError(f"{tab}: unexpected headers; migration aborted")
    return result[1:]


def pad(row, n):
    return (row + [""] * n)[:n]


def append_missing(api, book, tab, headers, desired, key_columns, dry_run):
    existing = checked_rows(api, book, tab, headers, "Z60000")
    keys = {tuple(pad(r, len(headers))[i] for i in key_columns) for r in existing}
    new = []
    for row in desired:
        key = tuple(row[i] for i in key_columns)
        if key not in keys:
            new.append(row)
            keys.add(key)
    print(f"{tab}: existing={len(existing)}, missing={len(new)}")
    if dry_run or not new:
        return
    # Sheet capacity is enlarged before appending; append API may insert rows itself,
    # but large imports should not depend on the default 1000-row grid.
    for offset in range(0, len(new), 100):
        api.values().append(spreadsheetId=book, range=f"{tab}!A:{chr(64+len(headers))}",
                            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                            body={"values": new[offset:offset+100]}).execute()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Validate and report only")
    args = p.parse_args()
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    api = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
    b0, b1, b2 = (os.environ[x] for x in
                  ("BOOK0_SPREADSHEET_ID", "BOOK1_SPREADSHEET_ID", "BOOK2_SPREADSHEET_ID"))

    legacy_targets = checked_rows(api, b1, "youtube_targets",
                                  ["active", "channel_url", "channel_id", "display_name",
                                   "playlist_id", "playlist_label", "sync_frequency",
                                   "max_items", "default_tags", "note"], "J1000")
    legacy_videos = checked_rows(api, b2, "youtube_videos_raw",
                                 ["sync_run_id", "sync_datetime", "channel_id",
                                  "channel_title", "playlist_id", "playlist_title",
                                  "video_id", "video_title", "video_description",
                                  "video_url", "thumbnail_url", "published_at",
                                  "duration_seconds", "position_in_playlist",
                                  "video_tags", "privacy_status", "origin_type"], "Q60000")

    channel_ids = defaultdict(set)
    for raw in legacy_videos:
        r = pad(raw, 17)
        if r[2] and r[3]:
            channel_ids[r[3]].add(r[2])
    # Resolve from historic metadata, never guess from a similar channel name.
    # One known legacy display-name difference is verified by the old URL mapping.
    aliases = {"ゆる書道学ラジオ（書く書くしかじか）": "書く書くしかじか"}
    requests = []
    targets = []
    unresolved = []
    for raw in legacy_targets:
        r = pad(raw, 10)
        if r[0].lower() not in ("true", "1", "yes"):
            continue
        name = r[3]
        ids = channel_ids.get(aliases.get(name, name), set())
        cid = r[2] or (next(iter(ids)) if len(ids) == 1 else "")
        if not CHANNEL_ID.fullmatch(cid) or (ids and cid not in ids):
            unresolved.append((name, r[1], sorted(ids)))
            continue
        playlist = r[4].strip()
        if playlist and not PLAYLIST_ID.fullmatch(playlist):
            raise ValueError(f"Invalid playlist ID for {name}")
        typ, source_id = ("PLAYLIST", playlist) if playlist else ("CHANNEL", cid)
        requests.append([typ, source_id])
        targets.append([f"{typ}:{source_id}", typ, source_id, cid, playlist,
                        name, r[5], "NEEDS_VERIFICATION", "", ""])
    if unresolved:
        raise ValueError(f"Unresolved/ambiguous legacy targets: {unresolved}")
    if len({tuple(r) for r in requests}) != len(requests):
        raise ValueError("Duplicate collection requests")
    print(f"Legacy active targets: {len(requests)}; videos: {len(legacy_videos)}")

    existing_requests = checked_rows(api, b0, "collection_requests", BOOK0_HEADERS, "B1000")
    existing_set = {tuple(pad(r, 2)) for r in existing_requests}
    new_requests = [r for r in requests if tuple(r) not in existing_set]
    print(f"Book0: existing={len(existing_requests)}, missing={len(new_requests)}")
    # Never remove user-entered Book0 targets or overwrite human changes.
    if not args.dry_run and new_requests:
        api.values().append(spreadsheetId=b0, range="collection_requests!A:B",
                            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                            body={"values": new_requests}).execute()

    append_missing(api, b1, "collection_targets_v2", TARGET_HEADERS, targets, (0,), args.dry_run)

    videos = {}
    memberships = {}
    for raw in legacy_videos:
        r = pad(raw, 17)
        vid = r[6]
        if not vid:
            continue
        if vid not in videos:
            videos[vid] = [vid, r[7], r[8], r[9], r[10], r[11], r[12],
                           r[2], r[1], r[1], "", "NOT_CHECKED", "", ""]
        else:
            # Preserve first observation; update last observation only in-memory.
            if r[1] > videos[vid][9]:
                videos[vid][9] = r[1]
        playlist_ids = [x.strip() for x in r[4].split(",") if x.strip()]
        for playlist_id in playlist_ids:
            key = (vid, playlist_id)
            if key not in memberships:
                memberships[key] = [vid, playlist_id, "", r[1], r[1], "OBSERVED"]
            elif r[1] > memberships[key][4]:
                memberships[key][4] = r[1]
    print(f"Normalized: {len(videos)} videos, {len(memberships)} memberships")
    append_missing(api, b2, "videos_v2", VIDEO_HEADERS, list(videos.values()), (0,), args.dry_run)
    append_missing(api, b2, "video_playlist_memberships", MEMBERSHIP_HEADERS,
                   list(memberships.values()), (0, 1), args.dry_run)
    print("Migration complete" if not args.dry_run else "Dry run complete; no changes")


if __name__ == "__main__":
    main()
