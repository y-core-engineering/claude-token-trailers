---
name: backfill
description: Adds token trailers after the fact to commits that were made without them, for example before the plugin was installed. Computes the values from the transcripts and writes them into the commit messages of an unmerged branch or attaches them as git notes. Use when the user asks why commits have no token numbers, or wants missing values added.
---

# Backfill token values

The script is `${CLAUDE_PLUGIN_ROOT}/scripts/token-usage.py`. Seen from this skill's directory that is `../../scripts/token-usage.py`. Run it in the repository whose commits lack values.

Nothing is written before the user has seen the proposal and agreed to it.

## 1. Propose

```
python3 <script> --backfill                        # commits ahead of the remote default branch
python3 <script> --backfill --range HEAD~20..HEAD  # any other range
```

For every commit without values the script looks for the Bash call that created it and counts the session from its previous commit call up to this one. Each commit gets one of three results:

- **values** with an `AI-Window` line that names the counted time span
- **`AI-Included-In: <commit>`** when several commits came from one call; the first carries the values
- **no commit call found**: the commit was made outside Claude Code, on another machine, or the transcripts are gone. Leave these commits alone and tell the user.

Show the proposal as a table: commit, subject, requests, output tokens, result.

## 2. Name the work steps

Every commit that gets values needs an `AI-Step`. Propose a name per commit from its subject and the names already used in the repository (`git log --format='%(trailers:key=AI-Step,valueonly)' | sort -u`), and let the user confirm or change them. Commits with `AI-Included-In` take the name of the commit they point to.

## 3. Choose where the values go

| Situation | Mode | Effect |
|---|---|---|
| Commits are only on a branch that is not merged | `--apply rewrite` | Messages are rewritten. Content, authors and dates stay, the hashes of these commits and all later ones change. |
| Commits are on the default branch, or others build on them | `--apply notes` | Git notes are attached. No hash changes. |

The script refuses to rewrite commits that are on the remote default branch and ranges that contain merge commits. Do not work around that; use notes.

Ask the user before applying, and say plainly when a rewrite will need a force push.

```
python3 <script> --backfill --apply rewrite --step abc1234=architecture --step def5678=spec
python3 <script> --backfill --apply notes --range HEAD~20..HEAD --step abc1234=architecture
```

## 4. Publish

The script changes the local repository only. Name the commands and run them only if the user says so:

- after a rewrite of a pushed branch: `git push --force-with-lease`
- after notes: `git push origin refs/notes/commits`

Finish with `python3 <script> --stats` and report the coverage line.

## Limits

- A rewrite drops commit signatures. Notes of rewritten commits are copied to the new ones.
- The window is found through the transcripts of this machine. If Claude Code has cleaned them up, there is nothing to count.
- The values are as good as the match between commit time and commit call. A commit that was amended or rebased later is matched by its author date.
