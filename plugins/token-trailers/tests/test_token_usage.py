"""Tests for scripts/token-usage.py against synthetic transcripts.

    python3 -B -m unittest discover -s plugins/token-trailers/tests
"""
import importlib.util
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
        ])
        since = tu.last_commit_ts(self.dir, SESSION)
        self.assertEqual(since, tu.parse_ts("2026-01-01T10:01:00Z"))
        self.assertEqual(self.trailers(since, (SESSION,))["AI-Requests"], "4")
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
                        'echo "fix" | git commit --file=-', 'echo "fix" | git commit --file -'):
            out = self.run_hook(command)
            self.assertEqual(out["permissionDecision"], "deny", command)
            self.assertIn("Tokens-Output: 42", out["permissionDecisionReason"])

    def test_hook_lets_everything_else_pass(self):
        for command in ('git commit -m "x\n\nTokens-Output: 5"', 'git commit -m "x $(token-usage.py)"',
                        "git commit --amend --no-edit", "git commit -F msg.txt", "git commit -F -msg.txt",
                        "git commit -F - <<'EOF'\nx\n\nTokens-Output: 5\nEOF",
                        "python3 token-usage.py | git commit -F -",
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


if __name__ == "__main__":
    unittest.main()
