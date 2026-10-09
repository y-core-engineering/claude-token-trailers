---
name: stats
description: Token and tool-call statistics for a Claude Code project, per work step, day and model. Reads the token trailers and git notes of the commits, or the transcripts when git carries no values or the directory is no repository. Use when the user asks for token usage, AI usage, a usage report, or a CSV of it for a spreadsheet.
---

# Token statistics

The script is `${CLAUDE_PLUGIN_ROOT}/scripts/token-usage.py`. Seen from this skill's directory that is `../../scripts/token-usage.py`. Run it in the project the user asks about.

```
python3 <script> --stats                              # Markdown report
python3 <script> --stats --csv > token-usage.csv      # one row per commit
python3 <script> --stats --source transcripts --csv   # one row per day, session and model
python3 <script> --stats --range main..HEAD           # only these commits
```

## Where the values come from

| Source | Used when | Holds |
|---|---|---|
| Trailers in the commit messages | the commits carry them | permanent, the same for everyone with the repository |
| Git notes | a commit has no trailers but a note | permanent, but only after `git fetch origin 'refs/notes/*:refs/notes/*'` |
| Transcripts under `~/.claude/projects/` | git carries no values, or there is no repository | only this machine, only until Claude Code cleans them up |

`--source auto` is the default: git if at least one commit carries values, transcripts otherwise. Fetch the notes first if the remote has some, otherwise those commits count as without values.

## Reporting

1. Run the report and show the tables unchanged. Do not add the four token classes up into one number; they cost differently.
2. Name the coverage line: how many commits carry values, how many are counted in another commit, how many have none.
3. If commits have no values, say so and offer the `backfill` skill.
4. Explain the last table. "Not attached" is work in the transcripts that no commit carries: orientation, abandoned attempts, sessions without a commit. If the script says the transcripts are incomplete, that row is missing and the difference cannot be stated.

For costs, write the CSV and leave the prices to the user. The CSV keeps the token classes in separate columns so each can be multiplied with its own price. Do not state prices from memory.

## Limits

- Step names are grouped as written. `spec` and `spec, open questions` are two rows.
- Merge commits are left out; they carry no work of their own.
- A day is the author date of the commit, in transcript mode the local date of the request.
- Opening the CSV in a spreadsheet with a decimal-comma locale needs the import dialog with comma as separator.
