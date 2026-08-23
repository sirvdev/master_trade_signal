#!/usr/bin/env python3
"""
tools/check_channel_types.py
============================
Answers one question per channel: can anyone other than an admin post into the
chat this listener subscribes to?

Why it matters
--------------
`events.NewMessage(chats=<id>)` behaves differently depending on the entity:

  broadcast channel  only admins can post. Member comments live in a SEPARATE
                     linked discussion group with a different chat id, which
                     this listener never subscribes to. Sender filtering is
                     redundant here.

  megagroup          every member posts into the same chat id. A member typing
                     "close all" arrives at the listener looking exactly like an
                     operator instruction. Sender filtering is the only reliable
                     defence; phrasing guards are a heuristic that will
                     eventually be wrong.

The exported corpus contains member messages ("How much profits you closeddd?",
"Made $1164 off that one"). That is evidence for megagroup OR for the export
tool having pulled the linked discussion group as well. Those two have opposite
consequences, and this script distinguishes them instead of guessing.

For any megagroup it also prints the admin IDs, which is exactly what goes in
`operator_sender_ids`.

Usage
-----
    python tools/check_channel_types.py            # enabled channels
    python tools/check_channel_types.py --all      # every channel in the file

Read-only: it opens the existing session, reads entity metadata, and exits.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from telethon import TelegramClient                          # noqa: E402
from telethon.tl.types import Channel, Chat                  # noqa: E402

from config import load_config                               # noqa: E402


async def main(show_all: bool) -> int:
    cfg = load_config()
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH missing from .env")
        return 2

    chans = cfg.channels if show_all else [c for c in cfg.channels if c.enabled]
    if not chans:
        print("No channels to check (use --all to include disabled ones).")
        return 0

    suggested: dict[str, list[int]] = {}
    async with TelegramClient(cfg.tg_session_name,
                              int(cfg.tg_api_id), cfg.tg_api_hash) as client:
        for ch in chans:
            print(f"\n{ch.name}  ({ch.id})")
            try:
                ent = await client.get_entity(int(ch.id))
            except Exception as e:
                print(f"   UNREACHABLE: {type(e).__name__}: {e}")
                print("   The session is not joined to this chat, or the id is "
                      "wrong. It will never fire.")
                continue

            if isinstance(ent, Chat):
                kind, open_posting = "legacy group", True
            elif isinstance(ent, Channel):
                if getattr(ent, "megagroup", False):
                    kind, open_posting = "megagroup", True
                elif getattr(ent, "gigagroup", False):
                    kind, open_posting = "broadcast group", True
                else:
                    kind, open_posting = "broadcast channel", False
            else:
                kind, open_posting = type(ent).__name__, True

            print(f"   type            : {kind}")
            print(f"   members can post: {'YES' if open_posting else 'no'}")
            linked = getattr(ent, "linked_chat_id", None)
            if linked:
                print(f"   linked discussion group: {linked}  "
                      f"(this listener does NOT subscribe to it)")

            if not open_posting:
                print("   -> sender filtering is REDUNDANT for this channel.")
                print("      Leave operator_sender_ids empty.")
                continue

            print("   -> sender filtering MATTERS: a member can post an "
                  "instruction-shaped message into this exact chat.")
            try:
                admins = [u.id async for u in client.iter_participants(
                    ent, filter=__import__(
                        "telethon.tl.types", fromlist=["ChannelParticipantsAdmins"]
                    ).ChannelParticipantsAdmins)]
                print(f"      admins: {admins}")
                suggested[str(ch.id)] = admins
            except Exception as e:
                print(f"      could not list admins: {type(e).__name__}: {e}")
                print("      Post one message yourself and read sender_id from "
                      "the listener log instead.")

    if suggested:
        print("\n" + "=" * 68)
        print("Paste into the matching channel's `parser` block in channels.json:")
        print("=" * 68)
        for cid, ids in suggested.items():
            print(f'  // channel {cid}')
            print(f'  "operator_sender_ids": {json.dumps(ids)},')
            print(f'  "require_sender_verification": true')
    else:
        print("\nNo channel needs sender filtering. Leave "
              "operator_sender_ids empty everywhere.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="include disabled channels")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.all)))
