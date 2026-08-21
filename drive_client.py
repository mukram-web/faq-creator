#!/usr/bin/env python3
"""
drive_client.py
===============
Read one Be10X session's Zoom chat from the "Weekly Sessions Files" Shared Drive,
using the `attendance-robot` Google service account.

The Shared Drive holds ~559 per-session folders named like:
    2026-06-28 - AI CAP B26 - AI Agents with n8n – Intro to Agents
    2026-06-27 - AI CAP B15 - AI in Python Part 1
Each folder contains attendee_*.csv + a poll CSV, and — for ~56% of sessions —
`meeting_saved_new_chat.txt` (older ones: `meeting_saved_chat.txt`).

Public surface:
    build_service(cred)                     -> Drive v3 service
    list_session_folders(svc)               -> [{id, name}, ...]
    rank_folders(folders, hint)             -> [(score, folder), ...]  best first
    fetch_session_chat(svc, hint, dest, slug) -> {matched_folder, chat_path, candidates, ...}

`cred` may be a service-account dict, a JSON string, or a path to the key file —
so the same code works from Streamlit secrets or a local file.
"""
import json
import re
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_ID = "0ADZkkxHLwZa9Uk9PVA"          # 'Weekly Sessions Files' Shared Drive
CHAT_NAMES = ("meeting_saved_new_chat.txt", "meeting_saved_chat.txt")

# tokens too generic to help match a session (mirrors be10x_faq_tool.find_session_folder)
_STOP = {"ai", "cap", "ecap", "b", "the", "of", "a", "for", "and", "with", "your",
         "using", "part", "e", "bsi", "at", "on", "to", "in"}


# ------------------------------------------------------------------ auth / service

def _load_credentials(cred):
    from google.oauth2 import service_account
    if isinstance(cred, dict):
        return service_account.Credentials.from_service_account_info(cred, scopes=SCOPES)
    s = str(cred).strip()
    if s.startswith("{"):                                  # raw JSON string (e.g. from secrets)
        return service_account.Credentials.from_service_account_info(json.loads(s), scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(s, scopes=SCOPES)  # path


def build_service(cred):
    """cred: service-account dict | JSON string | path to key file."""
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_load_credentials(cred), cache_discovery=False)


# ------------------------------------------------------------------ folder matching

def _norm(s: str) -> str:
    s = s.replace("#U2014", " ").replace("_", " ")
    return re.sub(r"[^0-9a-z]+", " ", s.lower()).strip()


def list_session_folders(svc, drive_id=DRIVE_ID):
    """All top-level session folders in the Shared Drive."""
    folders, token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{drive_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            corpora="drive", driveId=drive_id,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            fields="nextPageToken, files(id, name)", pageSize=1000, pageToken=token,
        ).execute()
        folders.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return folders


def rank_folders(folders, hint, top=8):
    """Rank session folders against a session-title hint. Returns [(score, folder), ...] best first.

    Titles repeat across dates/batches, so this returns several candidates for the
    caller (the app) to disambiguate. A distinctive batch token like 'b26' in the hint
    strongly boosts the right folder.
    """
    hint_norm = _norm(hint)
    key = set(hint_norm.split()) - _STOP
    scored = []
    for f in folders:
        toks = set(_norm(f["name"]).split())
        overlap = len(key & toks)
        score = overlap / max(1, len(key)) if key else 0.0
        if hint_norm and hint_norm in _norm(f["name"]):       # distinctive substring bonus
            score += 0.5
        if score > 0:
            scored.append((round(score, 3), f))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return scored[:top]


# ------------------------------------------------------------------ chat file

def folder_chat_file(svc, folder_id, drive_id=DRIVE_ID):
    """Return the chat file {id, name} inside a folder, or None."""
    kids = svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        corpora="drive", driveId=drive_id,
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        fields="files(id, name)", pageSize=1000,
    ).execute().get("files", [])
    by_name = {k["name"]: k for k in kids}
    for nm in CHAT_NAMES:
        if nm in by_name:
            return by_name[nm]
    return None


def download_file(svc, file_id, dest_path, drive_id=DRIVE_ID):
    from googleapiclient.http import MediaIoBaseDownload
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest_path


# ------------------------------------------------------------------ high-level

def fetch_session_chat(svc, hint, dest_dir, slug, drive_id=DRIVE_ID, folders=None, folder_id=None):
    """Match the session's folder and download its chat file if present.

    If `folder_id` is given (user picked an exact candidate), use it directly.
    Otherwise rank by `hint` and, among the top candidates, prefer one that has a chat file.

    Returns:
      {matched_folder, folder_id, chat_path (or None), has_chat, candidates}
    """
    if folder_id:
        chat = folder_chat_file(svc, folder_id, drive_id)
        name = _folder_name(svc, folder_id, drive_id)
        chat_path = None
        if chat:
            chat_path = download_file(svc, chat["id"], Path(dest_dir) / f"{slug}_chat.txt", drive_id)
        return {"matched_folder": name, "folder_id": folder_id,
                "chat_path": str(chat_path) if chat_path else None,
                "has_chat": chat_path is not None, "candidates": []}

    if folders is None:
        folders = list_session_folders(svc, drive_id)
    ranked = rank_folders(folders, hint)
    candidates = [{"score": sc, "name": f["name"], "id": f["id"]} for sc, f in ranked]
    if not ranked:
        return {"matched_folder": None, "folder_id": None, "chat_path": None,
                "has_chat": False, "candidates": []}

    # Only prefer-a-chat among folders TIED at the top score — these are duplicates of the
    # same session (e.g. the n8n folder that appears twice). Never drop to a lower-scored,
    # differently-named session just because it happens to have a chat file.
    best_score = ranked[0][0]
    top_tier = [f for sc, f in ranked if sc == best_score]
    chosen, chosen_chat = ranked[0][1], None
    for f in top_tier:
        chat = folder_chat_file(svc, f["id"], drive_id)
        if chat:
            chosen, chosen_chat = f, chat
            break

    chat_path = None
    if chosen_chat:
        chat_path = download_file(svc, chosen_chat["id"], Path(dest_dir) / f"{slug}_chat.txt", drive_id)
    return {"matched_folder": chosen["name"], "folder_id": chosen["id"],
            "chat_path": str(chat_path) if chat_path else None,
            "has_chat": chat_path is not None, "candidates": candidates}


def _folder_name(svc, folder_id, drive_id=DRIVE_ID):
    return svc.files().get(fileId=folder_id, supportsAllDrives=True, fields="name").execute().get("name")
