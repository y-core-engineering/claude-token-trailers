#!/usr/bin/env python3
"""Token usage and tool calls from Claude Code transcripts, printed as git trailers.

    token-usage.py                    # own session, since its last commit with trailers
    token-usage.py --since 2026-10-09T09:56:54Z --until 2026-10-09T10:57:41Z
    token-usage.py --since 2026-10-09T11:19:43Z --session 51e570ae
    token-usage.py --hook             # PreToolUse hook, reads the hook input from stdin
    token-usage.py --session-start    # SessionStart hook, hands Claude the rule
    token-usage.py --stats            # statistics of the project, --csv for a spreadsheet
    token-usage.py --backfill         # proposes values for commits without them

Without arguments the session from CLAUDE_CODE_SESSION_ID is used. With --since
all sessions of the project are counted unless --session narrows them down
(ID or its prefix, repeatable). Subagents count towards their session.
Timestamps without a time zone are read as UTC.

As a hook the script stops a `git commit -m` that lacks token trailers and
names the values to repeat the commit with. On any error it lets the commit
through, so the measurement never blocks the work.

--stats reads the trailers and git notes of the commits (--range, default
HEAD). Without such values, outside a git repository or with --source
transcripts it evaluates the transcripts of the project instead.

--backfill looks up the commit call of every commit without values in the
transcripts (--range, default: the commits ahead of the remote default
branch) and prints the lines it would add. --apply notes attaches them as git
notes, --apply rewrite writes them into the commit messages of the current
branch. Each commit that gets values needs a name: --step HASH=NAME.
"""
import argparse
import csv
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

KEYS = (
    ("input_tokens", "Tokens-Input"),
    ("cache_creation_input_tokens", "Tokens-Cache-Write"),
    ("cache_read_input_tokens", "Tokens-Cache-Read"),
    ("output_tokens", "Tokens-Output"),
)
# Cache writes have two prices. The share with the 1-hour lifetime is a detail
# of Tokens-Cache-Write, not a fifth class; the rest was written for 5 minutes.
WRITE_1H = "Tokens-Cache-Write-1h"
# `git commit` as a command of its own, also after &&, ; or | and with -C/-c before it.
COMMIT_RE = re.compile(r"(?:^|[;&|(\n])\s*git(?:\s+-[cC]\s+\S+)*\s+commit\b")
# A heredoc body is data for another command; a commit named in it is only text.
HEREDOC_RE = re.compile(r"(<<-?\s*(['\"]?)(\w+)\2[^\n]*)\n.*?^[ \t]*\3[ \t]*$", re.S | re.M)
# Only commits whose message is part of the command can be checked: -m, or
# -F - with the message on stdin (heredoc or pipe).
MESSAGE_RE = re.compile(
    r"\s(?:-[a-zA-Z]*m|--message)\b"
    r"|\s(?:-[a-zA-Z]*F\s*|--file[=\s]\s*)-(?=[\s<;&|)]|$)"
)
# Trailers are written out in the command or inserted by calling this script.
MARKERS = ("Tokens-Output:", "token-usage.py")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
COLUMNS = tuple(name for _, name in KEYS) + ("AI-Requests", "AI-Tool-Calls")
LOG_FORMAT = "%H%x1f%aI%x1f%cI%x1f%s%x1f%(trailers:only,unfold)%x1f%N%x1e"
FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]*(.*)$", re.M)
# Git stores commit times in whole seconds, the transcripts in milliseconds.
SLACK = timedelta(seconds=2)


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


def is_commit(command):
    return bool(COMMIT_RE.search(HEREDOC_RE.sub(r"\1", command)))


def is_trailered_commit(command):
    return is_commit(command) and any(m in command for m in MARKERS)


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


def write_1h(usages):
    """Cache-write tokens with the 1-hour lifetime; None if a request does not say how its writes split."""
    total = 0
    for usage in usages:
        detail = usage.get("cache_creation")
        if isinstance(detail, dict):
            total += detail.get("ephemeral_1h_input_tokens") or 0
        elif usage.get("cache_creation_input_tokens"):
            return None
    return total


def trailers(seen, tools):
    lines = [f"{name}: {sum(usage.get(key) or 0 for usage, _ in seen.values())}" for key, name in KEYS]
    hour = write_1h(usage for usage, _ in seen.values())
    if hour is not None:
        lines.insert(2, f"{WRITE_1H}: {hour}")
    lines.append(f"AI-Requests: {len(seen)}")
    lines.append(f"AI-Model: {', '.join(sorted({model for _, model in seen.values() if model}))}")
    lines.append(f"AI-Tool-Calls: {len(tools)}")
    if tools:
        lines.append(f"AI-Tools: {', '.join(f'{name}={n}' for name, n in Counter(tools.values()).most_common())}")
    return lines


def hook():
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command", "")
    if data.get("tool_name") != "Bash" or not is_commit(command):
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


def git(*args, stdin=None, env=None):
    return subprocess.run(("git",) + args, check=True, capture_output=True, text=True, input=stdin, env=env).stdout


def default_branch():
    """Default branch on the remote, None without one."""
    for args in (("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"),
                 ("rev-parse", "-q", "--verify", "--abbrev-ref", "origin/main"),
                 ("rev-parse", "-q", "--verify", "--abbrev-ref", "origin/master")):
        try:
            return git(*args).strip()
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def commits(rev):
    """Commits of the range without merges, newest first, with the values from trailers or notes."""
    try:
        log = git("log", "--no-merges", f"--format={LOG_FORMAT}", rev)
    except (OSError, subprocess.CalledProcessError):
        return []
    rows = []
    for record in filter(str.strip, log.split("\x1e")):
        sha, author, committer, subject, trailer, note = record.strip("\n").split("\x1f")
        for source, text in (("trailer", trailer), ("note", note)):
            fields = dict(FIELD_RE.findall(text))
            if "Tokens-Output" in fields or "AI-Included-In" in fields:
                break
        else:
            source, fields = "none", {}
        rows.append({"commit": sha, "date": author[:10], "times": (parse_ts(author), parse_ts(committer)),
                     "subject": subject, "source": source, "noted": bool(note.strip()), "fields": fields})
    return rows


def number(fields, name):
    try:
        return int(fields.get(name) or 0)
    except ValueError:
        return 0


def transcript_records(directory):
    """Usage of the transcripts, one record per day, session and model."""
    seen, tools, records, usages = {}, {}, {}, {}
    for entry in entries(directory):
        msg = entry["message"]
        if entry.get("type") != "assistant" or not msg.get("usage"):
            continue
        key = (parse_ts(entry["timestamp"]).astimezone().date().isoformat(),
               str(entry.get("sessionId", ""))[:8], msg.get("model") or "")
        for block in blocks(entry, "tool_use"):
            tools[block["id"]] = key
        seen.setdefault(msg["id"], (key, msg["usage"]))

    def record(key):
        return records.setdefault(key, {**dict(zip(("date", "session", "model"), key)), **dict.fromkeys(COLUMNS, 0)})

    for key, usage in seen.values():
        for field, name in KEYS:
            record(key)[name] += usage.get(field) or 0
        record(key)["AI-Requests"] += 1
        usages.setdefault(key, []).append(usage)
    for key, group in usages.items():
        records[key][WRITE_1H] = write_1h(group)
    for key in tools.values():
        record(key)["AI-Tool-Calls"] += 1
    return [records[key] for key in sorted(records)]


def total(records):
    return {name: sum(record[name] for record in records) for name in COLUMNS}


def table(label, groups):
    lines = [f"| {label} | " + " | ".join(COLUMNS) + " |", "|---|" + "---:|" * len(COLUMNS)]
    lines += [f"| {key} | " + " | ".join(str(sums[name]) for name in COLUMNS) + " |" for key, sums in groups]
    return lines + [""]


def by(label, records, key):
    groups = {}
    for record in records:
        groups.setdefault(record[key] or "-", []).append(record)
    rows = [(name, total(group)) for name, group in sorted(groups.items())]
    return [f"### By {label.lower()}", ""] + table(label, rows + [("Total", total(records))])


def stats(rev, source, as_csv):
    directory = project_dir()
    rows = commits(rev) if source != "transcripts" else []
    valued = [row for row in rows if "Tokens-Output" in row["fields"]]
    if source == "auto":
        source = "git" if valued else "transcripts"
    logged = transcript_records(directory)
    if source == "git":
        head = ("commit", "date", "subject", "source", "step", "included_in", "model")
        records = [{"commit": row["commit"][:7], "date": row["date"], "subject": row["subject"],
                    "source": row["source"], "step": row["fields"].get("AI-Step", ""),
                    "included_in": row["fields"].get("AI-Included-In", ""),
                    "model": row["fields"].get("AI-Model", ""),
                    **{name: number(row["fields"], name) for name in COLUMNS},
                    WRITE_1H: number(row["fields"], WRITE_1H) if WRITE_1H in row["fields"] else None}
                   for row in rows]
    else:
        head, records = ("date", "session", "model"), logged
    if as_csv:
        out = csv.writer(sys.stdout, lineterminator="\n")
        # The 1-hour share comes last and stays empty where it is unknown.
        names = head + COLUMNS + (WRITE_1H,)
        out.writerow(names)
        out.writerows(["" if record[name] is None else record[name] for name in names] for record in records)
        return

    print(f"# Token usage\n\nProject: {os.getcwd()}")
    if source == "transcripts":
        print("Source: transcripts on this machine\n")
        if not records:
            print("No transcripts found for this project.")
            return
        sections = (("Day", "date"), ("Session", "session"), ("Model", "model"))
    else:
        counted = [record for record in records if record["source"] != "none"]
        included = sum(1 for record in counted if record["included_in"])
        print("Source: commits (trailers and git notes)")
        print(f"Commits: {len(records)}, with values {len(counted) - included} "
              f"(trailers {sum(1 for row in valued if row['source'] == 'trailer')}, "
              f"notes {sum(1 for row in valued if row['source'] == 'note')}), "
              f"counted in another commit {included}, without values {len(records) - len(counted)}\n")
        records = [record for record in counted if not record["included_in"]]
        sections = (("Step", "step"), ("Day", "date"), ("Model", "model"))
    for label, key in sections:
        print("\n".join(by(label, records, key)))
    if source == "git":
        missing = [row for row in rows if row["source"] == "none"]
        if missing:
            print("### Commits without values\n")
            print("\n".join(f"- {row['commit'][:7]} {row['date']} {row['subject']}" for row in missing) + "\n")
        print("### Transcripts on this machine\n")
        if not logged:
            print("No transcripts found for this project.")
            return
        held, attached = total(logged), total(records)
        rest = {name: held[name] - attached[name] for name in COLUMNS}
        lines = [("Transcripts", held), ("Attached to commits", attached)]
        if min(rest.values()) >= 0:
            lines.append(("Not attached", rest))
        print("\n".join(table("", lines)))
        if min(rest.values()) < 0:
            print("The commits carry more than the transcripts hold. The transcripts are incomplete: "
                  "cleaned up, or the work ran on another machine or in another project directory.")


def commit_calls(directory):
    """Successful commit calls of all sessions as (session, start, end)."""
    ended, calls = {}, []
    for entry in entries(directory):
        ts = parse_ts(entry["timestamp"])
        for block in blocks(entry, "tool_result"):
            if not block.get("is_error"):
                ended[block.get("tool_use_id")] = ts
        for block in blocks(entry, "tool_use"):
            if block.get("name") == "Bash" and is_commit((block.get("input") or {}).get("command", "")):
                calls.append((str(entry.get("sessionId", "")), ts, block["id"]))
    return sorted((session, start, ended[tool_id]) for session, start, tool_id in calls if tool_id in ended)


def stamp(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan(directory, rows):
    """Lines for every commit without values, oldest first; None where no commit call is found."""
    calls, owner, plans = commit_calls(directory), {}, []
    for row in reversed(rows):
        if row["source"] != "none":
            continue
        call = next((c for c in calls if any(c[1] - SLACK <= t <= c[2] + SLACK for t in row["times"])), None)
        if call is None:
            plans.append((row, None))
        elif call in owner:
            # Several commits from one call: the first carries the values.
            plans.append((row, [f"AI-Included-In: {owner[call][:7]}"]))
        else:
            session, start, _ = call
            since = max((c[1] for c in calls if c[0] == session and c[1] < start), default=None)
            if since is None:
                since = min(parse_ts(e["timestamp"]) for e in entries(directory) if e.get("sessionId") == session)
            values = trailers(*collect(directory, since, start, (session,)))
            values = [line for line in values if not line.endswith(": ")]  # no model without requests
            owner[call] = row["commit"]
            plans.append((row, [f"AI-Window: {stamp(since)}/{stamp(start)}"] + values))
    return plans


def with_trailers(message, lines):
    """The message with the lines in its trailer block, before Co-Authored-By."""
    body = message.rstrip("\n").split("\n")
    start = max((i + 1 for i, line in enumerate(body) if not line.strip()), default=0)
    block = body[start:]
    if start and block and all(FIELD_RE.fullmatch(line) for line in block):
        at = next((i for i, line in enumerate(block) if line.lower().startswith("co-authored-by:")), len(block))
        block[at:at] = lines
        return "\n".join(body[:start] + block) + "\n"
    return "\n".join(body + [""] + lines) + "\n"


def rewrite(todo, noted):
    """Writes the lines into the messages on the current branch; trees, authors and dates stay."""
    first = next(iter(todo))
    branch, old_tip = git("symbolic-ref", "-q", "HEAD").strip(), git("rev-parse", "HEAD").strip()
    remote = default_branch()
    if remote and subprocess.run(("git", "merge-base", "--is-ancestor", first, remote)).returncode == 0:
        sys.exit(f"{first[:7]} is already on {remote}. Rewriting would change published history; use --apply notes.")
    if git("rev-list", "--merges", f"{first}^..HEAD").strip():
        sys.exit("There are merge commits after the first commit to rewrite; use --apply notes.")
    chain = git("rev-list", "--reverse", f"{first}^..HEAD").split()
    if not set(todo) <= set(chain):
        sys.exit("Not all commits are on the current branch; check it out or use --apply notes.")
    parent, new = git("rev-parse", f"{first}^").strip(), {}
    for sha in chain:
        fields = git("show", "-s", "--format=%T%n%an%n%ae%n%aI%n%cn%n%ce%n%cI", sha).split("\n")
        tree, an, ae, ad, cn, ce, cd = fields[:7]
        message = git("cat-file", "commit", sha).split("\n\n", 1)[1]
        if sha in todo:
            lines = [f"AI-Included-In: {new[todo[sha][1]][:7]}" if line.startswith("AI-Included-In: ") else line
                     for line in todo[sha][0]]
            message = with_trailers(message, lines)
        env = dict(os.environ, GIT_AUTHOR_NAME=an, GIT_AUTHOR_EMAIL=ae, GIT_AUTHOR_DATE=ad,
                   GIT_COMMITTER_NAME=cn, GIT_COMMITTER_EMAIL=ce, GIT_COMMITTER_DATE=cd)
        parent = new[sha] = git("commit-tree", tree, "-p", parent, "-F", "-", stdin=message, env=env).strip()
        if sha in noted:
            git("notes", "copy", sha, parent)
        print(f"{sha[:7]} -> {parent[:7]}")
    git("update-ref", "-m", "token-trailers: backfill", branch, parent, old_tip)
    print(f"\n{branch} moved from {old_tip[:7]} to {parent[:7]}. If the branch is pushed, it needs "
          "`git push --force-with-lease`.")


def backfill(rev, mode, steps):
    rows = commits(rev)
    plans, names, todo = plan(project_dir(), rows), {}, {}
    if not plans:
        print("No commits without values in this range.")
        return
    for row, lines in plans:
        sha = row["commit"]
        print(f"{sha[:7]} {row['date']} {row['subject']}")
        if lines is None:
            print("  no commit call found in the transcripts on this machine\n")
            continue
        owner = next((o for o in names if lines[0] == f"AI-Included-In: {o[:7]}"), sha)
        names[sha] = next((name for prefix, name in steps if sha.startswith(prefix)), names.get(owner))
        if names[sha]:
            lines = [f"AI-Step: {names[sha]}"] + lines
        elif mode:
            sys.exit(f"{sha[:7]} needs a name for its work step: --step {sha[:7]}=NAME")
        todo[sha] = (lines, owner)
        print("\n".join(f"  {line}" for line in lines) + "\n")
    if not mode or not todo:
        return
    try:
        if mode == "notes":
            for sha, (lines, _) in todo.items():
                git("notes", "add", "-m", "\n".join(lines), sha)
            print(f"Notes added to {len(todo)} commits. Publish them with `git push origin refs/notes/commits`.")
        else:
            rewrite(todo, {row["commit"] for row in rows if row["noted"]})
    except subprocess.CalledProcessError as exc:
        sys.exit(f"git {' '.join(exc.cmd[1:3])} failed: {(exc.stderr or '').strip()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", help="start (ISO 8601); default: last commit of the own session")
    parser.add_argument("--until", help="end (ISO 8601), default: now")
    parser.add_argument("--session", action="append", default=[],
                        help="only this session (ID or prefix), repeatable")
    parser.add_argument("--hook", action="store_true", help="run as PreToolUse hook")
    parser.add_argument("--session-start", action="store_true", help="run as SessionStart hook")
    parser.add_argument("--stats", action="store_true", help="statistics from trailers, git notes or transcripts")
    parser.add_argument("--source", choices=("auto", "git", "transcripts"), default="auto",
                        help="--stats: where the values come from; default: git if commits carry values")
    parser.add_argument("--csv", action="store_true",
                        help="--stats: one CSV row per commit or per day, session and model")
    parser.add_argument("--backfill", action="store_true", help="propose values for commits without them")
    parser.add_argument("--apply", choices=("notes", "rewrite"),
                        help="--backfill: attach git notes or rewrite the messages")
    parser.add_argument("--step", action="append", default=[], metavar="HASH=NAME",
                        help="--backfill: name of the work step of a commit, repeatable")
    parser.add_argument("--range", help="--stats, --backfill: revision range of the commits")
    args = parser.parse_args()

    if args.hook or args.session_start:
        try:
            hook() if args.hook else session_start()
        except Exception as exc:  # the measurement must never prevent a commit
            print(f"token-usage hook: {exc}", file=sys.stderr)
        return
    if args.stats:
        return stats(args.range or "HEAD", args.source, args.csv)
    if args.backfill:
        remote = default_branch()
        steps = [tuple(step.split("=", 1)) for step in args.step if "=" in step]
        return backfill(args.range or (f"{remote}..HEAD" if remote else "HEAD"), args.apply, steps)

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
