#!/usr/bin/env python3
"""Token usage and tool calls from Claude Code transcripts, printed as git trailers.

    token-usage.py                    # own session, since its last commit with trailers
    token-usage.py --since 2026-10-09T09:56:54Z --until 2026-10-09T10:57:41Z
    token-usage.py --since 2026-10-09T11:19:43Z --session 51e570ae
    token-usage.py --hook             # PreToolUse hook, reads the hook input from stdin
    token-usage.py --session-start    # SessionStart hook, hands Claude the rule

Without arguments the session from CLAUDE_CODE_SESSION_ID is used. With --since
all sessions of the project are counted unless --session narrows them down
(ID or its prefix, repeatable). Subagents count towards their session.
Timestamps without a time zone are read as UTC.

As a hook the script stops a `git commit -m` that lacks token trailers and
names the values to repeat the commit with. On any error it lets the commit
through, so the measurement never blocks the work.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

KEYS = (
    ("input_tokens", "Tokens-Input"),
    ("cache_creation_input_tokens", "Tokens-Cache-Write"),
    ("cache_read_input_tokens", "Tokens-Cache-Read"),
    ("output_tokens", "Tokens-Output"),
)
# `git commit` as a command of its own, also after &&, ; or | and with -C/-c before it.
COMMIT_RE = re.compile(r"(?:^|[;&|(\n])\s*git(?:\s+-[cC]\s+\S+)*\s+commit\b")
# Only commits whose message is part of the command can be checked: -m, or
# -F - with the message on stdin (heredoc or pipe).
MESSAGE_RE = re.compile(
    r"\s(?:-[a-zA-Z]*m|--message)\b"
    r"|\s(?:-[a-zA-Z]*F\s*|--file[=\s]\s*)-(?=[\s<;&|)]|$)"
)
# Trailers are written out in the command or inserted by calling this script.
MARKERS = ("Tokens-Output:", "token-usage.py")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_ts(value):
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def project_dir(session=None):
    """Transcript directory of the project; a known session wins over the cwd."""
    projects = os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")), "projects")
    if session:
        hits = glob.glob(os.path.join(projects, "*", f"{glob.escape(session)}*.jsonl"))
        if hits:
            return os.path.dirname(hits[0])
    try:
        root = subprocess.check_output(
            ("git", "rev-parse", "--show-toplevel"), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        root = os.getcwd()
    slug = "".join(c if c.isalnum() else "-" for c in root)
    return os.path.join(projects, slug)


def entries(directory):
    for path in glob.glob(os.path.join(directory, "**", "*.jsonl"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry.get("message"), dict) and entry.get("timestamp"):
                    yield entry


def blocks(entry, kind):
    content = entry["message"].get("content")
    return [b for b in content if b.get("type") == kind] if isinstance(content, list) else []


def is_trailered_commit(command):
    return bool(COMMIT_RE.search(command)) and any(m in command for m in MARKERS)


def last_commit_ts(directory, session):
    """Time of the last successful commit with trailers in this session."""
    # Only commits with an error-free result count: denied, failed and
    # still running calls do not move the window.
    succeeded, commits = set(), []
    for entry in entries(directory):
        if not str(entry.get("sessionId", "")).startswith(session):
            continue
        for block in blocks(entry, "tool_result"):
            if not block.get("is_error"):
                succeeded.add(block.get("tool_use_id"))
        for block in blocks(entry, "tool_use"):
            command = (block.get("input") or {}).get("command", "")
            if block.get("name") == "Bash" and is_trailered_commit(command):
                commits.append((parse_ts(entry["timestamp"]), block["id"]))
    done = [ts for ts, tool_id in commits if tool_id in succeeded]
    return max(done) if done else EPOCH


def collect(directory, since, until, sessions):
    # A request is written as one line per content block, each carrying the
    # same usage, so requests are deduplicated by message ID. Tool calls are
    # single content blocks and are counted by their own ID.
    seen, tools = {}, {}
    for entry in entries(directory):
        msg = entry["message"]
        if entry.get("type") != "assistant" or not msg.get("usage"):
            continue
        if not since <= parse_ts(entry["timestamp"]) < until:
            continue
        if sessions and not str(entry.get("sessionId", "")).startswith(sessions):
            continue
        for block in blocks(entry, "tool_use"):
            tools[block["id"]] = block["name"]
        seen.setdefault(msg["id"], (msg["usage"], msg.get("model")))
    return seen, tools


def trailers(seen, tools):
    lines = [f"{name}: {sum(usage.get(key) or 0 for usage, _ in seen.values())}" for key, name in KEYS]
    lines.append(f"AI-Requests: {len(seen)}")
    lines.append(f"AI-Model: {', '.join(sorted({model for _, model in seen.values() if model}))}")
    lines.append(f"AI-Tool-Calls: {len(tools)}")
    if tools:
        lines.append(f"AI-Tools: {', '.join(f'{name}={n}' for name, n in Counter(tools.values()).most_common())}")
    return lines


def hook():
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command", "")
    if data.get("tool_name") != "Bash" or not COMMIT_RE.search(command):
        return
    if not MESSAGE_RE.search(command) or any(m in command for m in MARKERS):
        return
    directory, session = os.path.dirname(data["transcript_path"]), data["session_id"]
    now = datetime.now(timezone.utc)
    seen, tools = collect(directory, last_commit_ts(directory, session), now, (session,))
    if not seen:
        return
    reason = (
        "This commit lacks the token trailers. Repeat it unchanged and append these lines "
        "as trailers at the end of the commit message, before Co-Authored-By. Add a line "
        "`AI-Step: <short name of the work step>` in front of them.\n\n"
        + "\n".join(trailers(seen, tools))
    )
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout, ensure_ascii=False)


def session_start():
    script = os.path.abspath(__file__)
    rule = (
        "Token trailers per commit: every commit carries its AI usage as git trailers. "
        f"Before committing, get the values with `python3 \"{script}\"` (no arguments: own "
        "session since its last commit) and append the output unchanged at the end of the "
        "commit message, before Co-Authored-By. Add `AI-Step: <short name of the work step>` "
        "in front of it. Never add the four token classes up into one number. A hook stops "
        "`git commit -m` and `git commit -F -` without these trailers and names the values; "
        "repeat the commit with the lines it gives you."
    )
    json.dump({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": rule}},
              sys.stdout, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="start (ISO 8601); default: last commit of the own session")
    parser.add_argument("--until", help="end (ISO 8601), default: now")
    parser.add_argument("--session", action="append", default=[],
                        help="only this session (ID or prefix), repeatable")
    parser.add_argument("--hook", action="store_true", help="run as PreToolUse hook")
    parser.add_argument("--session-start", action="store_true", help="run as SessionStart hook")
    args = parser.parse_args()

    if args.hook or args.session_start:
        try:
            hook() if args.hook else session_start()
        except Exception as exc:  # the measurement must never prevent a commit
            print(f"token-usage hook: {exc}", file=sys.stderr)
        return

    sessions = tuple(args.session)
    until = parse_ts(args.until) if args.until else datetime.now(timezone.utc)
    if args.since:
        directory, since = project_dir(), parse_ts(args.since)
    else:
        sessions = sessions or tuple(filter(None, [os.environ.get("CLAUDE_CODE_SESSION_ID")]))
        if len(sessions) != 1:
            sys.exit("Without --since exactly one session is needed (CLAUDE_CODE_SESSION_ID or --session).")
        directory = project_dir(sessions[0])
        since = last_commit_ts(directory, sessions[0])
    seen, tools = collect(directory, since, until, sessions)
    if not seen:
        sys.exit(f"No requests found between {since.isoformat()} and {until.isoformat()}.")
    print("\n".join(trailers(seen, tools)))


if __name__ == "__main__":
    main()
