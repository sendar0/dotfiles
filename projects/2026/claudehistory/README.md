# claude_history.py

Archives your Claude Code transcripts permanently, and turns them into a browsable
HTML site organised by project (working directory).

Claude Code deletes transcripts in `~/.claude/projects` after **30 days** by default
(`cleanupPeriodDays` in `~/.claude/settings.json`). Every run of this script copies them
into an append-only archive first, so conversations survive the cleanup.

Python 3 stdlib only — no pip installs.

## Usage

```sh
python3 claude_history.py              # sync the archive, then build into ./out
python3 claude_history.py --serve      # ...and serve on http://127.0.0.1:8000
python3 claude_history.py --sync-only  # just back up, no site (good for cron)
```

Run it again whenever you like: the archive is updated in place and the site rebuilt
from it. Only changed transcripts are copied, so repeat runs are cheap — the first run
here copied 300 MB, the second copied 4.6 MB.

The page needs a web server: browsers block `fetch()` over `file://`, and conversation
bodies are fetched on demand. `--serve` starts one; any static server over `out/` works.

## The archive

Default location: `$XDG_DATA_HOME/claude-history/archive`, otherwise
`~/.local/share/claude-history/archive`. Override with `--archive DIR`.

It stores the **raw JSONL**, not the rendered output, so improvements to the renderer
apply retroactively to conversations Claude has already deleted. Rules:

- **Append-only.** Nothing is ever deleted from the archive, whatever happens upstream.
- **Grown files are updated.** Resuming a session appends to its transcript; the longer
  version wins.
- **Shrunk files are refused.** If a transcript is *smaller* upstream than in the archive,
  the archived copy is kept and a warning is printed — a backup should never lose data to
  a truncated source.
- `manifest.json` records each file's size plus when it was first and last seen upstream.
  That is how conversations missing from `~/.claude/projects` get flagged **archived** in
  the UI (a blue pill on the project, the row, and the conversation itself).

To keep more than 30 days live as well, set `"cleanupPeriodDays": 365` in
`~/.claude/settings.json`. Note that neither that setting nor this archive can recover
transcripts already deleted — both only protect from now on.

## Output layout

```
out/
  index.html                    project list, conversation list, viewer (all CSS/JS inline)
  data/index.json               metadata + search text for every conversation (~400 KB)
  data/<project>/<session>.json one file per conversation, fetched when you expand it
```

`out/` is disposable and fully rebuilt each run; the archive is the durable copy.

## The page

- Projects are collapsible cards, sorted by most recent activity; each shows its full path,
  conversation count and date range.
- Expanding a project lists its conversations (Claude's own auto-generated title, date,
  message count, git branch). Expanding a conversation fetches and renders it inline.
- **Reading / Full toggle** on every conversation. *Reading* (the default) strips everything
  that isn't someone talking — tool calls, thinking, subagent and workflow transcripts,
  system reminders, slash commands — leaving just the prose. A pill reports how much is
  hidden so nothing looks lost. *Full* restores the complete transcript. The last choice is
  remembered and seeds newly opened conversations.
- Full-text search over titles, message text, branches and paths. Press `/` to focus, `Esc` to clear.
- Dark/light mode follows the system theme; the `◐` button overrides it and the choice sticks.
- Deep links: the URL hash tracks the open conversation, so you can bookmark or share one.

Each conversation renders user and assistant messages as markdown (code blocks get light
syntax highlighting). Thinking blocks, tool calls with their inputs and results, embedded
screenshots, system reminders and nested subagent/workflow transcripts are all included,
collapsed behind toggles.

Reading view also hides turns that consisted only of tool calls, and suppresses the repeated
speaker label across a run of Claude turns — otherwise removing the tool calls between them
leaves a column of empty `CLAUDE` headers instead of continuous prose.

Collapsed content is built only when you open it. That matters: the largest transcript here
is 19 MB, but its initial DOM is ~660 KB and it opens in about a second.

## Options

| Flag | Default | Effect |
|---|---|---|
| `--source DIR` | `~/.claude/projects` | Where to read transcripts from |
| `--archive DIR` | `~/.local/share/claude-history/archive` | Permanent append-only transcript store |
| `--no-archive` | | Skip the archive, build straight from `--source` |
| `--sync-only` | | Update the archive without building the site |
| `--out DIR` | `out` | Output directory (`data/` is rebuilt from scratch each run) |
| `--serve [PORT]` | `8000` | Serve `out/` after building |
| `--max-result-chars N` | `6000` | Truncate each tool result |
| `--max-input-chars N` | `4000` | Truncate long strings in tool inputs |
| `--max-text-chars N` | `200000` | Truncate message text |
| `--max-image-kb N` | `1500` | Skip embedded images larger than this |
| `--search-chars N` | `12000` | Searchable text kept per conversation in the index |
| `--no-images` | | Drop embedded screenshots (they are over half the output size) |
| `--no-thinking` | | Drop assistant thinking blocks |
| `--no-tools` | | Drop tool calls and results |
| `--no-subagents` | | Drop nested subagent transcripts |

Truncated content is marked in place with the number of characters dropped.

## Keeping it current

The archive only captures what is on disk when it runs, so it needs to run at least once
every `cleanupPeriodDays`. A daily cron entry is plenty:

```cron
0 9 * * * cd /home/sander/projects/2026/claudehistory && python3 claude_history.py --sync-only
```

## How transcripts are read

- `<project>/<sessionId>.jsonl` is one conversation. Project directory names are ambiguous
  (`/` and `-` both become `-`), so the real path comes from the `cwd` field in the records.
- Tool results live in later records; they are paired back to their `tool_use` by id and
  rendered inside the tool call rather than as separate messages.
- Subagents write their own transcripts under `<sessionId>/subagents/`. They are linked to
  the spawning tool call via `toolUseResult.agentId`, and workflow agents via
  `toolUseResult.runId` → `workflows/wf_<runId>/`. Anything that can't be linked is listed
  at the end of the conversation rather than dropped.
- Project directories with no session transcripts (only a `memory/` directory, say) are skipped.
