#!/usr/bin/env python3
"""CLI for the shared discussion board between concurrent planning agents.

Messages are stored in a JSONL file. Concurrency safety is handled via
fcntl.flock (exclusive lock on every append, shared lock on reads).

The board path is resolved from (in order):
  1. --board CLI flag
  2. DISCUSSION_BOARD environment variable
  3. .artifacts/discussion/chat.jsonl (relative to cwd)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time

CATEGORIES = [
    "requirement",
    "design_decision",
    "concern",
    "question",
    "proposal",
    "agreement",
    "update",
    "system",
]

DEFAULT_BOARD = os.path.join(".artifacts", "discussion", "chat.jsonl")


def _board_path(args: argparse.Namespace) -> str:
    return (
        getattr(args, "board", None)
        or os.environ.get("DISCUSSION_BOARD")
        or DEFAULT_BOARD
    )


def _read_messages(path: str) -> list[dict]:
    """Read all messages from the board, tolerating partial trailing lines."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            lines = f.readlines()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    messages = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip partial / corrupt lines
    return messages


def _append_message(path: str, msg: dict) -> dict:
    """Atomically append a message, assigning its index."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            # Count existing lines to determine idx
            f.seek(0)
            idx = sum(1 for l in f if l.strip())
            msg["idx"] = idx
            msg["ts"] = time.time()
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return msg


def _format_message(msg: dict, highlight_for: str | None = None) -> str:
    """Format a single message for display."""
    sender = msg.get("from", "?")
    target = msg.get("to")
    cat = msg.get("category", "")
    content = msg.get("content", "")
    idx = msg.get("idx", "?")
    reply_to = msg.get("reply_to")

    # Build header
    if target:
        header = f"[{sender} → {target}]"
    else:
        header = f"[{sender}]"

    cat_str = f" ({cat})" if cat else ""
    reply_str = f" (reply to #{reply_to})" if reply_to is not None else ""

    # Highlight if directed to the reader
    directed = ""
    if highlight_for and target and target.lower() == highlight_for.lower():
        directed = " ⚠️ DIRECTED TO YOU:"

    return f"[msg #{idx}] {header}{cat_str}{reply_str}{directed} {content}"


# ── Commands ────────────────────────────────────────────────────────────


def cmd_post(args: argparse.Namespace) -> None:
    path = _board_path(args)
    msg = {
        "from": args.sender,
        "to": args.to or None,
        "content": args.message,
        "category": args.category or None,
        "reply_to": None,
        "awaiting_reply": False,
    }
    result = _append_message(path, msg)
    print(json.dumps({"ok": True, "idx": result["idx"]}))


def cmd_read(args: argparse.Namespace) -> None:
    path = _board_path(args)
    messages = _read_messages(path)

    since = getattr(args, "since", None)
    if since is not None:
        messages = [m for m in messages if m.get("idx", 0) >= since]

    highlight = getattr(args, "for_agent", None)
    if not messages:
        print("(no messages)")
        return

    lines = [_format_message(m, highlight_for=highlight) for m in messages]
    print("\n\n".join(lines))


def cmd_reply(args: argparse.Namespace) -> None:
    path = _board_path(args)
    msg = {
        "from": args.sender,
        "to": None,
        "content": args.message,
        "category": args.category or None,
        "reply_to": args.to_msg,
        "awaiting_reply": False,
    }
    # Infer the recipient from the original message
    messages = _read_messages(path)
    for m in messages:
        if m.get("idx") == args.to_msg:
            msg["to"] = m.get("from")
            break

    result = _append_message(path, msg)
    print(json.dumps({"ok": True, "idx": result["idx"], "reply_to": args.to_msg}))


def cmd_post_and_wait(args: argparse.Namespace) -> None:
    path = _board_path(args)
    msg = {
        "from": args.sender,
        "to": args.to,
        "content": args.message,
        "category": args.category or "question",
        "reply_to": None,
        "awaiting_reply": True,
    }
    result = _append_message(path, msg)
    my_idx = result["idx"]
    target = args.to.lower()
    timeout = args.timeout
    poll_interval = 3.0
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        messages = _read_messages(path)
        for m in messages:
            if (
                m.get("reply_to") == my_idx
                and m.get("from", "").lower() == target
            ):
                print(json.dumps({
                    "replied": True,
                    "reply_idx": m["idx"],
                    "from": m["from"],
                    "content": m["content"],
                }))
                return

    print(json.dumps({
        "replied": False,
        "error": f"No response from {args.to} within {timeout}s",
        "original_idx": my_idx,
    }))


def cmd_status(args: argparse.Namespace) -> None:
    path = _board_path(args)
    messages = _read_messages(path)
    if not messages:
        print("No messages yet.")
        return

    now = time.time()
    # Per-agent stats
    agents: dict[str, dict] = {}
    for m in messages:
        sender = m.get("from", "?")
        if sender == "System":
            continue
        if sender not in agents:
            agents[sender] = {"count": 0, "last_ts": 0}
        agents[sender]["count"] += 1
        agents[sender]["last_ts"] = max(agents[sender]["last_ts"], m.get("ts", 0))

    for agent, stats in sorted(agents.items()):
        ago = int(now - stats["last_ts"])
        print(f"{agent}: {stats['count']} messages (last: {ago}s ago)")

    # Unanswered directed messages
    reply_targets = {m.get("reply_to") for m in messages if m.get("reply_to") is not None}
    unanswered = [
        m for m in messages
        if m.get("to") and m.get("idx") not in reply_targets
    ]
    if unanswered:
        print(f"\n⚠️ {len(unanswered)} unanswered directed message(s):")
        for m in unanswered:
            print(f'  [#{m["idx"]}] {m["from"]} → {m["to"]}: "{m["content"][:80]}"')
    else:
        print("\nNo unanswered directed messages.")


# ── CLI ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discussion board CLI for concurrent planning agents",
    )
    parser.add_argument("--board", help="Path to chat.jsonl (default: $DISCUSSION_BOARD or .artifacts/discussion/chat.jsonl)")
    sub = parser.add_subparsers(dest="command", required=True)

    # post
    p_post = sub.add_parser("post", help="Post a message")
    p_post.add_argument("--from", dest="sender", required=True, help="Your role name")
    p_post.add_argument("--to", default=None, help="Direct message to a specific agent")
    p_post.add_argument("--message", required=True, help="Message content")
    p_post.add_argument("--category", default=None, choices=CATEGORIES, help="Message category")

    # read
    p_read = sub.add_parser("read", help="Read the discussion")
    p_read.add_argument("--for", dest="for_agent", default=None, help="Highlight messages directed to you")
    p_read.add_argument("--since", type=int, default=None, help="Only show messages from this index onwards")

    # reply
    p_reply = sub.add_parser("reply", help="Reply to a specific message")
    p_reply.add_argument("--from", dest="sender", required=True, help="Your role name")
    p_reply.add_argument("--to-msg", type=int, required=True, help="Index of the message to reply to")
    p_reply.add_argument("--message", required=True, help="Reply content")
    p_reply.add_argument("--category", default=None, choices=CATEGORIES, help="Message category")

    # post_and_wait
    p_wait = sub.add_parser("post_and_wait", help="Post and wait for a reply")
    p_wait.add_argument("--from", dest="sender", required=True, help="Your role name")
    p_wait.add_argument("--to", required=True, help="Agent to wait for a reply from")
    p_wait.add_argument("--message", required=True, help="Message content")
    p_wait.add_argument("--category", default=None, choices=CATEGORIES, help="Message category")
    p_wait.add_argument("--timeout", type=int, default=180, help="Timeout in seconds (default: 180)")

    # status
    sub.add_parser("status", help="Show discussion summary")

    args = parser.parse_args()

    commands = {
        "post": cmd_post,
        "read": cmd_read,
        "reply": cmd_reply,
        "post_and_wait": cmd_post_and_wait,
        "status": cmd_status,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
