# token-trailers

A Claude Code plugin that records AI usage per commit. Every commit Claude creates carries its token usage and tool calls as git trailers, so you can work out what a merged pull request cost in tokens.

## Installation

```
/plugin marketplace add y-core-engineering/claude-token-trailers
/plugin install token-trailers@token-trailers
```

Requirements: `python3` 3.9 or newer and `git` on the path. The plugin has no other dependencies.

## What a commit looks like afterwards

```
feat(board): move cards with drag and drop

AI-Step: story-2-3
Tokens-Input: 286
Tokens-Cache-Write: 1190990
Tokens-Cache-Read: 25914175
Tokens-Output: 66732
AI-Requests: 124
AI-Model: claude-opus-5-5
AI-Tool-Calls: 186
AI-Tools: Bash=62, Read=57, AskUserQuestion=25, Write=10
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

The four token classes are kept apart because they are billed very differently. Cache reads usually make up most of the volume and the smallest share of the cost, so a single total would mislead.

For tool calls only the name and count are recorded, never the input.

## How it works

The plugin installs two hooks:

| Hook | Effect |
|---|---|
| `SessionStart` | Hands Claude the rule and the path of the script that computes the values before a commit. |
| `PreToolUse` on `Bash` | Checks every `git commit -m` and `git commit -F -`. If the trailers are missing, the hook stops the commit and names the values. Claude repeats the commit with them. |

The numbers come from the transcripts Claude Code stores under `~/.claude/projects/`. Counted is the own session including its subagents, from its last commit with trailers up to the current one. Parallel sessions in the same project do not mix.

## Evaluating

Single values per commit:

```
git log --format='%h | %(trailers:key=Tokens-Output,valueonly,separator=) | %(trailers:key=AI-Requests,valueonly,separator=)'
```

The script can also be called directly, for example to evaluate a time window after the fact:

```
python3 plugins/token-trailers/scripts/token-usage.py --since 2026-10-09T09:56:54Z --until 2026-10-09T10:57:41Z
python3 plugins/token-trailers/scripts/token-usage.py --since 2026-10-09T11:19:43Z --session 51e570ae
```

## Limits

- Only commits Claude creates through the Bash tool are covered. Your own commits in a terminal do not pass the hook.
- Only `git commit -m` and `git commit -F -` (message on stdin, heredoc or pipe) are checked. With `--amend --no-edit` or `-F file` the message is not part of the command, so the hook lets these commits through.
- The request that triggers a commit counts towards the following commit.
- Work in a session that ends without a commit is attached to no commit. For an honest metric it belongs into the evaluation as overhead.
- A squash merge drops the trailers of the individual commits.
- Claude Code deletes transcripts after 30 days by default (`cleanupPeriodDays`). Whatever is not in a commit by then cannot be reconstructed.
- The script reads the transcript format and the `CLAUDE_CODE_SESSION_ID` variable. Neither is a documented interface, and both may change with a new Claude Code version.
- The hook never blocks because of its own errors. If it finds no data or crashes, the commit goes through without trailers.

## Development

```
python3 -B -m unittest discover -s plugins/token-trailers/tests
claude plugin validate .
claude plugin validate plugins/token-trailers
```

To try it without installing: `claude --plugin-dir plugins/token-trailers`.

## License

MIT, see [LICENSE](LICENSE).
