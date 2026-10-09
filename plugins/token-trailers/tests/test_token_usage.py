"""Tests for scripts/token-usage.py against synthetic transcripts.

    python3 -B -m unittest discover -s plugins/token-trailers/tests
"""
import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "token-usage.py")
spec = importlib.util.spec_from_file_location("token_usage", SCRIPT)
tu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tu)

SESSION, OTHER = "aaaa1111-0000-0000-0000-000000000000", "bbbb2222-0000-0000-0000-000000000000"
FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)


def assistant(session, minute, msg_id, blocks, output=10):
    usage = {"input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3,
             "output_tokens": output}
    return {"type": "assistant", "sessionId": session, "timestamp": f"2026-01-01T10:{minute:02d}:00.000Z",
            "message": {"id": msg_id, "model": "claude-test", "usage": usage, "content": blocks}}


def bash(tool_id, command):
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}


def result(session, minute, tool_id, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "x"}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "sessionId": session, "timestamp": f"2026-01-01T10:{minute:02d}:30.000Z",
            "message": {"content": [block]}}


class TokenUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "projects", "-repo")
        os.makedirs(os.path.join(self.dir, SESSION, "subagents"))

    def write(self, name, entries):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(e) + "\n" for e in entries)
        return path

    def trailers(self, since=tu.EPOCH, sessions=()):
        return dict(line.split(": ", 1) for line in tu.trailers(*tu.collect(self.dir, since, FAR, sessions)))

    def test_request_spanning_several_lines_counts_once(self):
        self.write(f"{SESSION}.jsonl", [
            assistant(SESSION, 1, "m1", [{"type": "text", "text": "a"}]),
            assistant(SESSION, 1, "m1", [bash("t1", "ls")]),
            assistant(SESSION, 1, "m1", [bash("t2", "pwd")]),
        ])
        got = self.trailers()
        self.assertEqual((got["AI-Requests"], got["Tokens-Output"], got["AI-Tool-Calls"]), ("1", "10", "2"))
        self.assertEqual(got["AI-Tools"], "Bash=2")

    def test_token_classes_stay_separate(self):
        self.write(f"{SESSION}.jsonl", [assistant(SESSION, 1, "m1", []), assistant(SESSION, 2, "m2", [])])
        got = self.trailers()
        self.assertEqual([got[k] for _, k in tu.KEYS], ["2", "4", "6", "20"])

    def test_subagents_count_and_session_filter_separates_parallel_sessions(self):
        self.write(f"{SESSION}.jsonl", [assistant(SESSION, 1, "m1", [])])
        self.write(os.path.join(SESSION, "subagents", "agent-1.jsonl"),
                   [assistant(SESSION, 2, "s1", [{"type": "tool_use", "id": "t9", "name": "Read", "input": {}}])])
        self.write(f"{OTHER}.jsonl", [assistant(OTHER, 2, "o1", [bash("t3", "ls")])])
        self.assertEqual(self.trailers()["AI-Requests"], "3")
        mine = self.trailers(sessions=("aaaa1111",))
        self.assertEqual((mine["AI-Requests"], mine["AI-Tools"]), ("2", "Read=1"))

    def test_window_starts_at_last_successful_commit_with_trailers(self):
        commit = 'git add . && git commit -m "x\n\nTokens-Output: 1"'
        self.write(f"{SESSION}.jsonl", [
            assistant(SESSION, 1, "m1", [bash("t1", commit)]), result(SESSION, 1, "t1"),
            assistant(SESSION, 2, "m2", [bash("t2", commit)]), result(SESSION, 2, "t2", is_error=True),
            assistant(SESSION, 3, "m3", [bash("t3", 'git commit -m "no trailers"')]), result(SESSION, 3, "t3"),
            assistant(SESSION, 4, "m4", [bash("t4", commit)]),  # still running, no result yet
            # a commit that is only text in a heredoc body does not move the window
            assistant(SESSION, 5, "m5", [bash("t5", f"cat > test.py <<'EOF'\n{commit}\nEOF")]),
            result(SESSION, 5, "t5"),
        ])
        since = tu.last_commit_ts(self.dir, SESSION)
        self.assertEqual(since, tu.parse_ts("2026-01-01T10:01:00Z"))
        self.assertEqual(self.trailers(since, (SESSION,))["AI-Requests"], "5")
        self.assertEqual(tu.last_commit_ts(self.dir, OTHER), tu.EPOCH)

    def run_hook(self, command, tool="Bash", stdin=None):
        path = self.write(f"{SESSION}.jsonl", [assistant(SESSION, 1, "m1", [], output=42)])
        payload = {"session_id": SESSION, "transcript_path": path, "tool_name": tool,
                   "tool_input": {"command": command}}
        run = subprocess.run([sys.executable, "-B", SCRIPT, "--hook"], capture_output=True, text=True,
                             input=json.dumps(payload) if stdin is None else stdin)
        self.assertEqual(run.returncode, 0)
        return json.loads(run.stdout)["hookSpecificOutput"] if run.stdout.strip() else None

    def test_hook_denies_commit_without_trailers_and_names_values(self):
        for command in ('git commit -m "feat: x"', 'git add . && git commit -am "x"',
                        'git -C /repo commit -q -m "$(cat <<EOF\nfix\nEOF\n)"',
                        "git add . && git commit -q -F - <<'EOF'\nfix\nEOF", "git commit -qF- <<EOF\nfix\nEOF",
                        'echo "fix" | git commit --file=-', 'echo "fix" | git commit --file -',
                        "cat > f <<EOF && git commit -m x\ndata\nEOF",
                        "cat > f <<EOF\ndata\nEOF\ngit commit -m x"):
            out = self.run_hook(command)
            self.assertEqual(out["permissionDecision"], "deny", command)
            self.assertIn("Tokens-Output: 42", out["permissionDecisionReason"])

    def test_hook_lets_everything_else_pass(self):
        for command in ('git commit -m "x\n\nTokens-Output: 5"', 'git commit -m "x $(token-usage.py)"',
                        "git commit --amend --no-edit", "git commit -F msg.txt", "git commit -F -msg.txt",
                        "git commit -F - <<'EOF'\nx\n\nTokens-Output: 5\nEOF",
                        "python3 token-usage.py | git commit -F -",
                        "cat > test.py <<'EOF'\ngit add . && git commit -m \"x\"\nEOF",
                        'grep -r "git commit -m" docs/', "git log --oneline"):
            self.assertIsNone(self.run_hook(command), command)
        self.assertIsNone(self.run_hook('git commit -m "x"', tool="Edit"))

    def test_hook_never_blocks_on_its_own_errors(self):
        self.assertIsNone(self.run_hook("", stdin="not json"))
        self.assertIsNone(self.run_hook("", stdin=json.dumps({"tool_name": "Bash", "tool_input": {
            "command": 'git commit -m "x"'}})))

    def test_cli_finds_own_session_from_any_working_directory(self):
        self.write(f"{SESSION}.jsonl", [assistant(SESSION, 1, "m1", [], output=7)])
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.tmp.name, CLAUDE_CODE_SESSION_ID=SESSION)
        run = subprocess.run([sys.executable, "-B", SCRIPT], capture_output=True, text=True, env=env,
                             cwd=self.tmp.name)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Tokens-Output: 7", run.stdout)

    def test_session_start_hands_over_rule_with_script_path(self):
        run = subprocess.run([sys.executable, "-B", SCRIPT, "--session-start"], capture_output=True, text=True)
        out = json.loads(run.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn(os.path.abspath(SCRIPT), out["additionalContext"])


VALUES = "Tokens-Input: 1\nTokens-Cache-Write: 2\nTokens-Cache-Read: 3\nTokens-Output: {}\nAI-Requests: 4\nAI-Model: m"


class RepoTest(unittest.TestCase):
    """--stats and --backfill against a real repository with synthetic transcripts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=self.tmp.name, GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="A", GIT_AUTHOR_EMAIL="a@example.com",
                        GIT_COMMITTER_NAME="A", GIT_COMMITTER_EMAIL="a@example.com")
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.git("init", "-q", "-b", "main")
        self.dir = self.transcripts(self.git("rev-parse", "--show-toplevel"))

    def transcripts(self, root):
        path = os.path.join(self.tmp.name, "projects", "".join(c if c.isalnum() else "-" for c in root))
        os.makedirs(path)
        return path

    def git(self, *args, minute=None):
        env = self.env
        if minute is not None:
            when = f"2026-01-01T10:{minute:02d}:10Z"
            env = dict(env, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        run = subprocess.run(("git",) + args, cwd=self.repo, env=env, check=True, capture_output=True, text=True)
        return run.stdout.strip()

    def commit(self, minute, message):
        self.git("commit", "-q", "--allow-empty", "-m", message, minute=minute)
        return self.git("rev-parse", "HEAD")

    def write(self, entries, directory=None):
        with open(os.path.join(directory or self.dir, f"{SESSION}.jsonl"), "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(e) + "\n" for e in entries)

    def script(self, *args, cwd=None, ok=True):
        run = subprocess.run([sys.executable, "-B", SCRIPT, *args], cwd=cwd or self.repo, env=self.env,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode == 0, ok, run.stderr)
        return run.stdout if ok else run.stderr

    def test_stats_reads_trailers_and_notes_and_lists_commits_without_values(self):
        self.commit(1, "feat: a\n\nAI-Step: plan\n" + VALUES.format(10))
        noted = self.commit(2, "feat: b")
        self.git("notes", "add", "-m", "AI-Step: build\n" + VALUES.format(20), noted)
        bare = self.commit(3, "feat: c")
        self.write([assistant(SESSION, 1, "m1", [], output=50)])
        out = self.script("--stats")
        self.assertIn("with values 2 (trailers 1, notes 1), counted in another commit 0, without values 1", out)
        self.assertIn("| plan | 1 | 2 | 3 | 10 | 4 | 0 |", out)
        self.assertIn("| build | 1 | 2 | 3 | 20 | 4 | 0 |", out)
        self.assertIn("| Total | 2 | 4 | 6 | 30 | 8 | 0 |", out)
        self.assertIn(f"- {bare[:7]} 2026-01-01 feat: c", out)
        self.assertIn("| Transcripts | 1 | 2 | 3 | 50 | 1 | 0 |", out)
        self.assertIn("The transcripts are incomplete", out)
        rows = list(csv.DictReader(io.StringIO(self.script("--stats", "--csv"))))
        self.assertEqual([row["source"] for row in rows], ["none", "note", "trailer"])
        self.assertEqual([row["Tokens-Output"] for row in rows], ["0", "20", "10"])
        self.assertEqual(rows[1]["step"], "build")

    def test_stats_uses_transcripts_without_values_in_git_and_outside_a_repository(self):
        entries = [assistant(SESSION, 1, "m1", [bash("t1", "ls")], output=5), assistant(OTHER, 2, "m2", [], output=7)]
        self.commit(1, "feat: a")
        self.write(entries)
        out = self.script("--stats")
        self.assertIn("Source: transcripts on this machine", out)
        self.assertIn("| aaaa1111 | 1 | 2 | 3 | 5 | 1 | 1 |", out)
        self.assertIn("| Total | 2 | 4 | 6 | 12 | 2 | 1 |", out)
        plain = os.path.realpath(os.path.join(self.tmp.name, "plain"))
        os.makedirs(plain)
        self.write(entries, self.transcripts(plain))
        rows = list(csv.DictReader(io.StringIO(self.script("--stats", "--csv", cwd=plain))))
        self.assertEqual([(row["session"], row["Tokens-Output"]) for row in rows],
                         [("aaaa1111", "5"), ("bbbb2222", "7")])

    def history(self):
        """Three commits from two commit calls after one that Claude did not make."""
        base = self.commit(0, "chore: base")
        one = self.commit(5, "feat: one\n\nCo-Authored-By: C <c@example.com>")
        two, three = self.commit(8, "feat: two"), self.commit(8, "feat: three")
        call = 'git commit -m "x"'
        self.write([assistant(SESSION, 1, "m1", []), assistant(SESSION, 2, "m2", []), assistant(SESSION, 3, "m3", []),
                    assistant(SESSION, 5, "m5", [bash("t5", call)]), result(SESSION, 5, "t5"),
                    assistant(SESSION, 6, "m6", [bash("t6", call)]), result(SESSION, 6, "t6", is_error=True),
                    assistant(SESSION, 8, "m8", [bash("t8", call + " && " + call)]), result(SESSION, 8, "t8")])
        return base, one, two, three

    def test_backfill_proposes_values_from_the_commit_calls(self):
        base, one, two, three = self.history()
        out = self.script("--backfill")
        blocks = {block.split(" ", 1)[0]: block for block in out.strip().split("\n\n")}
        self.assertIn("no commit call found", blocks[base[:7]])
        self.assertIn("AI-Window: 2026-01-01T10:01:00Z/2026-01-01T10:05:00Z", blocks[one[:7]])
        self.assertIn("AI-Requests: 3", blocks[one[:7]])
        self.assertIn("AI-Requests: 2", blocks[two[:7]])  # the request of the first commit and the failed one
        self.assertIn(f"AI-Included-In: {two[:7]}", blocks[three[:7]])
        self.assertIn("needs a name", self.script("--backfill", "--apply", "notes", ok=False))
        self.assertEqual(self.git("rev-parse", "HEAD"), three)

    def test_backfill_rewrites_messages_and_keeps_trees_and_dates(self):
        base, one, two, three = self.history()
        self.git("notes", "add", "-m", "keep me", three)
        before = self.git("log", "--format=%T %aI %cI %s")
        self.script("--backfill", "--apply", "rewrite", "--step", f"{one[:7]}=first", "--step", f"{two[:7]}=second")
        self.assertEqual(self.git("log", "--format=%T %aI %cI %s"), before)
        self.assertEqual(self.git("rev-parse", "HEAD~3"), base)
        new_two = self.git("rev-parse", "--short=7", "HEAD~1")
        lines = self.git("log", "-1", "--format=%(trailers:only)", "HEAD~2").split("\n")
        self.assertEqual((lines[0], lines[-1]), ("AI-Step: first", "Co-Authored-By: C <c@example.com>"))
        self.assertIn("AI-Requests: 3", lines)
        self.assertIn("AI-Step: second\nAI-Window:", self.git("log", "-1", "--format=%B", "HEAD~1"))
        self.assertEqual(self.git("log", "-1", "--format=%(trailers:only)"),
                         f"AI-Step: second\nAI-Included-In: {new_two}")
        self.assertEqual(self.git("notes", "show", "HEAD"), "keep me")
        self.assertIn("with values 2 (trailers 2, notes 0), counted in another commit 1, without values 1",
                      self.script("--stats"))

    def test_backfill_attaches_notes_and_leaves_published_history_alone(self):
        base, one, two, three = self.history()
        self.git("update-ref", "refs/remotes/origin/main", three)
        steps = ("--range", "HEAD", "--step", f"{one[:7]}=first", "--step", f"{two[:7]}=second")
        self.assertIn("already on origin/main", self.script("--backfill", "--apply", "rewrite", *steps, ok=False))
        self.script("--backfill", "--apply", "notes", *steps)
        self.assertEqual(self.git("rev-parse", "HEAD"), three)
        self.assertIn("AI-Step: first\nAI-Window:", self.git("notes", "show", one))
        self.assertEqual(self.git("notes", "show", three), f"AI-Step: second\nAI-Included-In: {two[:7]}")
        self.assertIn("No commits without values", self.script("--backfill", "--range", "HEAD~3..HEAD"))

    def test_with_trailers_puts_lines_before_co_authored_by(self):
        self.assertEqual(tu.with_trailers("s\n\nbody\n\nRefs: 1\nCo-Authored-By: C\n", ["A: 1", "B: 2"]),
                         "s\n\nbody\n\nRefs: 1\nA: 1\nB: 2\nCo-Authored-By: C\n")
        self.assertEqual(tu.with_trailers("s\n\nbody: text\nmore\n", ["A: 1"]), "s\n\nbody: text\nmore\n\nA: 1\n")
        self.assertEqual(tu.with_trailers("subject: only\n", ["A: 1"]), "subject: only\n\nA: 1\n")


if __name__ == "__main__":
    unittest.main()
