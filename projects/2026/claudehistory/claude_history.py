#!/usr/bin/env python3
"""
claude_history.py — turn ~/.claude/projects transcripts into a browsable HTML site.

Reads every Claude Code session transcript (JSONL), groups them by the project
(working directory) they belong to, and writes a static site:

    out/
      index.html                  project list + conversation list + viewer
      data/index.json             lightweight metadata + search text
      data/<project>/<id>.json    one file per conversation, fetched on expand

Usage:
    python3 claude_history.py                 # build into ./out
    python3 claude_history.py --serve         # build, then serve on :8000
    python3 claude_history.py --out ~/chist --serve 9000

Stdlib only. No pip installs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SOURCE = Path.home() / ".claude" / "projects"


def default_archive() -> Path:
    """Persistent store for transcripts, outside anything Claude Code prunes."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "claude-history" / "archive"

# ---------------------------------------------------------------------------
# JSONL reading
# ---------------------------------------------------------------------------


def iter_records(path: Path):
    """Yield parsed JSON objects from a .jsonl file, skipping unparseable lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as exc:
        print(f"  ! could not read {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.S)
COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
COMMAND_MSG_RE = re.compile(r"<command-message>(.*?)</command-message>", re.S)
STDOUT_RE = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)
CAVEAT_RE = re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def truncate(text: str, limit: int):
    """Return (text, dropped_chars). dropped_chars is 0 when nothing was cut."""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


def stringify(value, limit: int) -> str:
    """Best-effort human-readable rendering of an arbitrary JSON value."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)[: max(limit, 200)]
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Subagent transcript index
# ---------------------------------------------------------------------------


class AgentIndex:
    """Locates subagent transcripts that belong to a session.

    Layout on disk:
        <session>/subagents/agent-<agentId>.jsonl
        <session>/subagents/workflows/wf_<runId>/agent-<id>.jsonl
    """

    def __init__(self, session_dir: Path):
        self.by_agent_id: dict[str, Path] = {}
        self.by_run_id: dict[str, list[Path]] = {}
        self.all: list[Path] = []
        sub = session_dir / "subagents"
        if not sub.is_dir():
            return
        for path in sorted(sub.rglob("agent-*.jsonl")):
            agent_id = path.stem[len("agent-") :]
            self.by_agent_id[agent_id] = path
            self.all.append(path)
            parent = path.parent
            if parent.name.startswith("wf_"):
                self.by_run_id.setdefault(parent.name[3:], []).append(path)

    def take(self, agent_id: str | None, run_id: str | None) -> list[Path]:
        picked: list[Path] = []
        if agent_id and agent_id in self.by_agent_id:
            picked.append(self.by_agent_id.pop(agent_id))
        if run_id:
            key = run_id[3:] if run_id.startswith("wf_") else run_id
            for path in self.by_run_id.pop(key, []):
                self.by_agent_id.pop(path.stem[len("agent-") :], None)
                picked.append(path)
        for path in picked:
            if path in self.all:
                self.all.remove(path)
        return picked

    def leftovers(self) -> list[Path]:
        remaining = list(self.all)
        self.all = []
        return remaining


# ---------------------------------------------------------------------------
# Conversation parsing
# ---------------------------------------------------------------------------


class Options:
    def __init__(self, args):
        self.result_chars = args.max_result_chars
        self.input_chars = args.max_input_chars
        self.text_chars = args.max_text_chars
        self.images = not args.no_images
        self.image_bytes = args.max_image_kb * 1024
        self.thinking = not args.no_thinking
        self.tools = not args.no_tools
        self.subagents = not args.no_subagents
        self.search_chars = args.search_chars


def collect_tool_results(records) -> dict:
    """Map tool_use_id -> {"blocks": [...], "meta": toolUseResult, "isError": bool}."""
    results = {}
    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid:
                continue
            results[tid] = {
                "content": block.get("content"),
                "isError": bool(block.get("is_error")),
                "meta": rec.get("toolUseResult"),
            }
    return results


def render_tool_result(entry, opts: Options) -> dict:
    """Turn a raw tool_result payload into {text, dropped, images, isError}."""
    out = {"text": "", "dropped": 0, "images": [], "isError": entry["isError"]}
    content = entry["content"]
    parts: list[str] = []

    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "image":
                src = block.get("source") or {}
                data = src.get("data") or ""
                media = src.get("media_type") or "image/png"
                if opts.images and data:
                    # base64 length ~= 4/3 of decoded size
                    if len(data) * 3 // 4 <= opts.image_bytes:
                        out["images"].append(f"data:{media};base64,{data}")
                    else:
                        parts.append(f"[image omitted: {len(data) * 3 // 4 // 1024} KB]")
                elif data:
                    parts.append("[image]")
            elif btype == "tool_reference":
                parts.append(f"[tool reference: {block.get('name', '?')}]")
            else:
                parts.append(stringify(block, opts.result_chars))
    elif content is not None:
        parts.append(stringify(content, opts.result_chars))

    text = strip_ansi("\n".join(p for p in parts if p))
    if not text and not out["images"]:
        meta = entry.get("meta")
        if meta is not None:
            text = stringify(meta, opts.result_chars)
    out["text"], out["dropped"] = truncate(text, opts.result_chars)
    return out


def summarise_tool_input(name: str, tool_input) -> str:
    """One-line preview shown on the collapsed tool row."""
    if not isinstance(tool_input, dict):
        return stringify(tool_input, 160)[:160]
    for key in (
        "command",
        "file_path",
        "path",
        "pattern",
        "query",
        "url",
        "prompt",
        "description",
        "skill",
        "notebook_path",
    ):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            one_line = " ".join(value.split())
            return one_line[:160]
    return " ".join(json.dumps(tool_input, ensure_ascii=False).split())[:160]


def shrink_tool_input(tool_input, opts: Options):
    """Truncate long string values inside a tool input so the JSON stays small."""
    if isinstance(tool_input, dict):
        out = {}
        for key, value in tool_input.items():
            out[key] = shrink_tool_input(value, opts)
        return out
    if isinstance(tool_input, list):
        return [shrink_tool_input(v, opts) for v in tool_input]
    if isinstance(tool_input, str):
        text, dropped = truncate(tool_input, opts.input_chars)
        return text + (f"\n… [{dropped} more characters]" if dropped else "")
    return tool_input


def split_reminders(text: str):
    """Pull <system-reminder> blocks out of user text."""
    reminders = [m.strip() for m in SYSTEM_REMINDER_RE.findall(text)]
    cleaned = SYSTEM_REMINDER_RE.sub("", text)
    return cleaned.strip(), reminders


def parse_conversation(path: Path, opts: Options, depth: int = 0) -> dict:
    """Parse one transcript file into a renderable message list plus metadata."""
    records = list(iter_records(path))
    session_dir = path.parent / path.stem
    agents = AgentIndex(session_dir) if (opts.subagents and depth == 0) else AgentIndex(Path("/nonexistent"))
    results = collect_tool_results(records)

    messages: list[dict] = []
    meta = {
        "id": path.stem,
        "title": None,
        "cwd": None,
        "gitBranch": None,
        "version": None,
        "models": [],
        "slug": None,
        "agentName": None,
        "start": None,
        "end": None,
        "userTurns": 0,
        "assistantTurns": 0,
        "toolCalls": 0,
    }
    search_parts: list[str] = []
    pending_command: dict | None = None
    current_assistant: dict | None = None

    def push(message: dict):
        nonlocal current_assistant
        messages.append(message)
        if message.get("role") != "assistant":
            current_assistant = None

    for rec in records:
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if ts:
            if meta["start"] is None or ts < meta["start"]:
                meta["start"] = ts
            if meta["end"] is None or ts > meta["end"]:
                meta["end"] = ts
        if rec.get("cwd") and not meta["cwd"]:
            meta["cwd"] = rec["cwd"]
        if rec.get("gitBranch") and not meta["gitBranch"]:
            meta["gitBranch"] = rec["gitBranch"]
        if rec.get("version"):
            meta["version"] = rec["version"]
        if rec.get("slug") and not meta["slug"]:
            meta["slug"] = rec["slug"]

        if rtype == "ai-title" and rec.get("aiTitle"):
            meta["title"] = rec["aiTitle"]
            continue
        if rtype == "agent-name" and rec.get("agentName"):
            meta["agentName"] = rec["agentName"]
            continue
        if rtype not in ("user", "assistant"):
            continue

        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")

        # ---------------- user ----------------
        if rtype == "user":
            if isinstance(content, str):
                raw = content
                if CAVEAT_RE.search(raw):
                    raw = CAVEAT_RE.sub("", raw).strip()
                    if not raw:
                        continue

                stdout_match = STDOUT_RE.search(raw)
                if stdout_match:
                    output = strip_ansi(stdout_match.group(1).strip())
                    output, _ = truncate(output, opts.result_chars)
                    if pending_command is not None:
                        pending_command["output"] = output
                        pending_command = None
                    elif output:
                        push({"role": "output", "ts": ts, "text": output})
                    continue

                cmd_match = COMMAND_NAME_RE.search(raw)
                if cmd_match:
                    args_match = COMMAND_ARGS_RE.search(raw)
                    msg_match = COMMAND_MSG_RE.search(raw)
                    pending_command = {
                        "role": "command",
                        "ts": ts,
                        "name": cmd_match.group(1).strip(),
                        "args": (args_match.group(1).strip() if args_match else ""),
                        "label": (msg_match.group(1).strip() if msg_match else ""),
                        "output": "",
                    }
                    push(pending_command)
                    continue

                if rec.get("isMeta"):
                    continue

                text, reminders = split_reminders(raw)
                if not text and not reminders:
                    continue
                text, dropped = truncate(text, opts.text_chars)
                meta["userTurns"] += 1
                search_parts.append(text)
                push(
                    {
                        "role": "user",
                        "ts": ts,
                        "blocks": [{"t": "text", "text": text, "dropped": dropped}],
                        "reminders": reminders if reminders else None,
                    }
                )
                continue

            if isinstance(content, list):
                blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text, reminders = split_reminders(block.get("text", ""))
                        if not text:
                            continue
                        text, dropped = truncate(text, opts.text_chars)
                        search_parts.append(text)
                        blocks.append({"t": "text", "text": text, "dropped": dropped})
                    elif block.get("type") == "image":
                        src = block.get("source") or {}
                        data = src.get("data") or ""
                        if opts.images and data and len(data) * 3 // 4 <= opts.image_bytes:
                            media = src.get("media_type") or "image/png"
                            blocks.append({"t": "image", "src": f"data:{media};base64,{data}"})
                        else:
                            blocks.append({"t": "text", "text": "[image]", "dropped": 0})
                if blocks:
                    meta["userTurns"] += 1
                    push({"role": "user", "ts": ts, "blocks": blocks, "reminders": None})
            continue

        # ---------------- assistant ----------------
        if not isinstance(content, list):
            continue
        model = msg.get("model")
        if model and model not in meta["models"]:
            meta["models"].append(model)

        request_id = rec.get("requestId")
        if current_assistant is not None and request_id and current_assistant.get("_req") == request_id:
            target = current_assistant
        else:
            target = {
                "role": "assistant",
                "ts": ts,
                "model": model,
                "effort": rec.get("effort"),
                "blocks": [],
                "_req": request_id,
            }
            meta["assistantTurns"] += 1
            push(target)
            current_assistant = target

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if not text.strip():
                    continue
                search_parts.append(text)
                text, dropped = truncate(text, opts.text_chars)
                target["blocks"].append({"t": "text", "text": text, "dropped": dropped})
            elif btype == "thinking":
                if not opts.thinking:
                    continue
                text = block.get("thinking", "")
                if not text.strip():
                    continue
                text, dropped = truncate(text, opts.text_chars)
                target["blocks"].append({"t": "thinking", "text": text, "dropped": dropped})
            elif btype == "tool_use":
                if not opts.tools:
                    continue
                meta["toolCalls"] += 1
                name = block.get("name", "tool")
                tool_input = block.get("input")
                entry = results.get(block.get("id"))
                tool_block = {
                    "t": "tool",
                    "name": name,
                    "summary": summarise_tool_input(name, tool_input),
                    "input": shrink_tool_input(tool_input, opts),
                    "result": render_tool_result(entry, opts) if entry else None,
                }
                tur = (entry or {}).get("meta")
                if isinstance(tur, dict):
                    nested = agents.take(tur.get("agentId"), tur.get("runId"))
                    if nested:
                        tool_block["agents"] = [
                            parse_conversation(p, opts, depth + 1) for p in nested
                        ]
                target["blocks"].append(tool_block)

    for message in messages:
        message.pop("_req", None)

    orphans = agents.leftovers()
    orphan_convs = [parse_conversation(p, opts, depth + 1) for p in orphans]

    if not meta["title"]:
        for message in messages:
            if message.get("role") == "user":
                for block in message.get("blocks", []):
                    if block.get("t") == "text" and block.get("text"):
                        first = " ".join(block["text"].split())
                        meta["title"] = first[:90] + ("…" if len(first) > 90 else "")
                        break
            if meta["title"]:
                break
    if not meta["title"]:
        meta["title"] = meta["agentName"] or f"Session {path.stem[:8]}"

    search_text = " ".join(search_parts)
    if len(search_text) > opts.search_chars:
        search_text = search_text[: opts.search_chars]

    return {
        "meta": meta,
        "messages": messages,
        "orphanAgents": orphan_convs,
        "search": search_text,
    }


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

MANIFEST_VERSION = 1


def load_manifest(archive: Path) -> dict:
    path = archive / "manifest.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! manifest unreadable, starting a new one: {exc}", file=sys.stderr)
    return {"version": MANIFEST_VERSION, "created": None, "files": {}}


def sync_archive(source: Path, archive: Path) -> tuple[set[str], dict, dict]:
    """Mirror every transcript from `source` into `archive`, never deleting.

    Claude Code prunes ~/.claude/projects (30 days by default), so the archive is
    append-only: files that disappear upstream stay here forever. Returns
    (paths still live upstream, manifest, per-run counters).
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    archive.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(archive)
    if not manifest.get("created"):          # setdefault would keep an existing null
        manifest["created"] = now
    files = manifest["files"]

    stats = {"added": 0, "updated": 0, "unchanged": 0, "shrunk": 0, "bytes": 0}
    live: set[str] = set()

    for src in sorted(source.rglob("*.jsonl")):
        if not src.is_file():
            continue
        rel = src.relative_to(source).as_posix()
        live.add(rel)
        dst = archive / rel
        try:
            src_stat = src.stat()
        except OSError as exc:
            print(f"  ! skipping {rel}: {exc}", file=sys.stderr)
            continue

        if not dst.exists():
            action = "added"
        else:
            dst_stat = dst.stat()
            if src_stat.st_size > dst_stat.st_size:
                action = "updated"          # session grew — resumed and appended to
            elif src_stat.st_size < dst_stat.st_size:
                action = "shrunk"           # upstream lost content; keep the archive
            elif src_stat.st_mtime > dst_stat.st_mtime + 1:
                action = "updated"          # same size, rewritten in place
            else:
                action = "unchanged"

        if action in ("added", "updated"):
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                print(f"  ! could not archive {rel}: {exc}", file=sys.stderr)
                continue
            stats["bytes"] += src_stat.st_size
        elif action == "shrunk":
            print(f"  ! {rel} is smaller upstream than in the archive — keeping the archived copy")

        stats[action] += 1
        entry = files.get(rel) or {"firstSeen": now}
        entry.update({"size": dst.stat().st_size, "lastSeen": now})
        files[rel] = entry

    manifest["version"] = MANIFEST_VERSION
    manifest["updated"] = now
    manifest["source"] = str(source)
    stats["archivedOnly"] = sum(1 for rel, e in files.items() if e.get("lastSeen") != now)

    tmp = archive / "manifest.json.tmp"
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    tmp.replace(archive / "manifest.json")

    return live, manifest, stats


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------


def decode_project_dirname(name: str) -> str:
    """Fallback path reconstruction when no record carries a cwd."""
    return "/" + name.lstrip("-").replace("-", "/")


def display_name(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else path)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")
    return slug or "project"


def build(source: Path, out: Path, opts: Options, live: set[str] | None = None) -> dict:
    """Render the site from `source`. `live` is the set of transcript paths still
    present upstream; anything else is marked as surviving only in the archive."""
    data_dir = out / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    projects = []
    total_conversations = 0
    total_messages = 0
    total_archived = 0

    project_dirs = sorted(d for d in source.iterdir() if d.is_dir())
    for pdir in project_dirs:
        session_files = sorted(pdir.glob("*.jsonl"))
        if not session_files:
            continue

        conversations = []
        project_path = None
        pslug = slugify(pdir.name)
        pdata = data_dir / pslug
        pdata.mkdir(parents=True, exist_ok=True)

        for sfile in session_files:
            conv = parse_conversation(sfile, opts)
            if not conv["messages"] and not conv["orphanAgents"]:
                continue
            cmeta = conv["meta"]
            if cmeta["cwd"] and not project_path:
                project_path = cmeta["cwd"]

            rel = f"data/{pslug}/{cmeta['id']}.json"
            payload = {
                "meta": cmeta,
                "messages": conv["messages"],
                "orphanAgents": conv["orphanAgents"],
            }
            archived_only = live is not None and sfile.relative_to(source).as_posix() not in live
            cmeta["archivedOnly"] = archived_only
            total_archived += 1 if archived_only else 0

            target = pdata / f"{cmeta['id']}.json"
            with target.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

            conversations.append(
                {
                    "id": cmeta["id"],
                    "title": cmeta["title"],
                    "start": cmeta["start"],
                    "end": cmeta["end"],
                    "gitBranch": cmeta["gitBranch"],
                    "models": cmeta["models"],
                    "version": cmeta["version"],
                    "slug": cmeta["slug"],
                    "userTurns": cmeta["userTurns"],
                    "assistantTurns": cmeta["assistantTurns"],
                    "toolCalls": cmeta["toolCalls"],
                    "archivedOnly": archived_only,
                    "bytes": target.stat().st_size,
                    "file": rel,
                    "q": conv["search"].lower(),
                }
            )
            total_messages += len(conv["messages"])

        if not conversations:
            shutil.rmtree(pdata, ignore_errors=True)
            continue

        conversations.sort(key=lambda c: c["start"] or "", reverse=True)
        total_conversations += len(conversations)
        path = project_path or decode_project_dirname(pdir.name)
        projects.append(
            {
                "slug": pslug,
                "path": path,
                "name": display_name(path),
                "dir": pdir.name,
                "conversations": conversations,
                "archivedOnly": sum(1 for c in conversations if c["archivedOnly"]),
                "start": min((c["start"] or "" for c in conversations), default=""),
                "end": max((c["end"] or "" for c in conversations), default=""),
            }
        )
        archived_here = sum(1 for c in conversations if c["archivedOnly"])
        note = f"  ({archived_here} archive-only)" if archived_here else ""
        print(f"  {display_name(path):<44} {len(conversations):>3} conversation(s){note}")

    projects.sort(key=lambda p: p["end"] or "", reverse=True)

    index = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "stats": {
            "projects": len(projects),
            "conversations": total_conversations,
            "messages": total_messages,
            "archivedOnly": total_archived,
        },
        "projects": projects,
    }
    with (data_dir / "index.json").open("w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))

    (out / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    return index


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude History</title>
<style>
:root {
  --bg: #f6f7f9;
  --panel: #ffffff;
  --panel-2: #fafbfc;
  --border: #e2e5ea;
  --border-soft: #edeff3;
  --text: #16181d;
  --muted: #6b7280;
  --faint: #9aa1ac;
  --accent: #c2703f;
  --accent-soft: #f6ede6;
  --user: #2f6f4f;
  --user-soft: #eaf4ee;
  --archive: #4a6fa5;
  --archive-soft: #e9eef7;
  --code-bg: #f2f3f6;
  --tool-bg: #f7f8fa;
  --shadow: 0 1px 2px rgba(16,18,24,.05), 0 6px 20px rgba(16,18,24,.05);
  --radius: 10px;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
:root[data-theme="dark"] {
  --bg: #14161a;
  --panel: #1c1f25;
  --panel-2: #21252c;
  --border: #2e333c;
  --border-soft: #262b33;
  --text: #e6e8ec;
  --muted: #9aa2ae;
  --faint: #6d7681;
  --accent: #e08c58;
  --accent-soft: #2b231c;
  --user: #6bbd8f;
  --user-soft: #1a2620;
  --archive: #7ea6de;
  --archive-soft: #1b2230;
  --code-bg: #171a1f;
  --tool-bg: #191d23;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.25);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14161a; --panel: #1c1f25; --panel-2: #21252c; --border: #2e333c;
    --border-soft: #262b33; --text: #e6e8ec; --muted: #9aa2ae; --faint: #6d7681;
    --accent: #e08c58; --accent-soft: #2b231c; --user: #6bbd8f; --user-soft: #1a2620;
    --archive: #7ea6de; --archive-soft: #1b2230;
    --code-bg: #171a1f; --tool-bg: #191d23;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.25);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); }

/* ---------- header ---------- */
header {
  position: sticky; top: 0; z-index: 50;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
}
.hwrap { max-width: 1120px; margin: 0 auto; padding: 14px 20px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.brand { display: flex; align-items: baseline; gap: 10px; margin-right: auto; }
.brand h1 { font-size: 17px; margin: 0; font-weight: 650; letter-spacing: -.01em; }
.brand .sub { color: var(--faint); font-size: 12.5px; }
.search {
  flex: 1 1 280px; max-width: 460px; position: relative;
}
.search input {
  width: 100%; padding: 8px 32px 8px 32px; font: inherit; font-size: 14px;
  color: var(--text); background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; outline: none;
}
.search input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.search .icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--faint); font-size: 13px; pointer-events: none; }
.search kbd {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  font: 11px var(--mono); color: var(--faint); border: 1px solid var(--border);
  border-radius: 4px; padding: 1px 5px; background: var(--panel-2);
}
.btn {
  font: inherit; font-size: 13px; color: var(--muted); cursor: pointer;
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 11px; line-height: 1;
}
.btn:hover { color: var(--text); border-color: var(--faint); }

/* ---------- layout ---------- */
main { max-width: 1120px; margin: 0 auto; padding: 22px 20px 80px; }
.notice {
  background: var(--accent-soft); border: 1px solid var(--accent);
  border-radius: var(--radius); padding: 14px 16px; margin-bottom: 20px; font-size: 14px;
}
.notice code { font: 12.5px var(--mono); background: var(--code-bg); padding: 2px 6px; border-radius: 4px; }
.summary { color: var(--muted); font-size: 13px; margin: 0 0 16px; }

/* ---------- project ---------- */
.project {
  background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
  margin-bottom: 10px; box-shadow: var(--shadow); overflow: hidden;
}
.project > .head {
  display: flex; align-items: center; gap: 12px; padding: 13px 16px; cursor: pointer;
  user-select: none;
}
.project > .head:hover { background: var(--panel-2); }
.chev { color: var(--faint); font-size: 11px; transition: transform .16s ease; flex: none; width: 10px; }
/* direct-child selectors only: an open project must not rotate its rows' chevrons */
.project.open > .head .chev, .conv.open > .head .chev { transform: rotate(90deg); }
.pname { font-weight: 600; font-size: 14.5px; letter-spacing: -.005em; }
.ppath { color: var(--faint); font: 12px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pmeta { margin-left: auto; display: flex; gap: 8px; align-items: center; flex: none; }
.pill {
  font-size: 11.5px; color: var(--muted); background: var(--panel-2);
  border: 1px solid var(--border-soft); border-radius: 20px; padding: 2px 9px; white-space: nowrap;
}
.pill.accent { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.pill.arch { color: var(--archive); background: var(--archive-soft); border-color: transparent; }

/* ---------- conversation row ---------- */
.convlist { border-top: 1px solid var(--border-soft); display: none; }
.project.open .convlist { display: block; }
.conv { border-bottom: 1px solid var(--border-soft); }
.conv:last-child { border-bottom: none; }
.conv > .head {
  display: flex; gap: 12px; align-items: center; padding: 11px 16px 11px 30px;
  cursor: pointer; user-select: none;
}
.conv > .head:hover { background: var(--panel-2); }
.ctitle { font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cmeta { margin-left: auto; display: flex; gap: 7px; align-items: center; flex: none; color: var(--faint); font-size: 11.5px; }
.body { display: none; padding: 6px 16px 22px 30px; }
.conv.open .body { display: block; }
.loading { color: var(--faint); font-size: 13px; padding: 14px 0; }

.detailbar {
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 10px 0 14px;
  border-bottom: 1px dashed var(--border); margin-bottom: 16px;
}

/* ---------- reading / full transcript toggle ---------- */
.viewtoggle {
  display: inline-flex; border: 1px solid var(--border); border-radius: 7px;
  overflow: hidden; margin-right: 3px; flex: none;
}
.vbtn {
  font: inherit; font-size: 11.5px; line-height: 1.6; padding: 2px 10px;
  background: var(--panel); color: var(--muted); border: 0; cursor: pointer;
}
.vbtn + .vbtn { border-left: 1px solid var(--border); }
.vbtn:hover:not(.on) { color: var(--text); background: var(--panel-2); }
.vbtn.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.hidnote { display: none; }
/* reading view: keep only what a person actually said or wrote */
.body.reading .hidnote { display: inline-block; }
.body.reading details.box,
.body.reading .msg.aux,
.body.reading .msg.no-prose,
.body.reading .label.aux { display: none; }
.body.reading .msg.cont > .role { display: none; }
.body.reading .msg.cont { margin-top: -10px; }

/* ---------- messages ---------- */
.msg { margin: 0 0 18px; }
.role {
  font-size: 11px; letter-spacing: .07em; text-transform: uppercase; font-weight: 700;
  color: var(--faint); margin-bottom: 6px; display: flex; gap: 8px; align-items: center;
}
.msg.user .role { color: var(--user); }
.msg.assistant .role { color: var(--accent); }
.role .ts { font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--faint); font-size: 11.5px; }
.bubble {
  border-left: 2px solid var(--border); padding-left: 14px;
}
.msg.user .bubble { border-left-color: var(--user); background: var(--user-soft); padding: 10px 14px; border-radius: 0 8px 8px 0; }
.msg.assistant .bubble { border-left-color: var(--accent); }

/* ---------- markdown ---------- */
.md > *:first-child { margin-top: 0; }
.md > *:last-child { margin-bottom: 0; }
.md h1, .md h2, .md h3, .md h4 { margin: 1.1em 0 .4em; line-height: 1.3; font-weight: 650; }
.md h1 { font-size: 1.32em; } .md h2 { font-size: 1.18em; } .md h3 { font-size: 1.06em; } .md h4 { font-size: 1em; }
.md p { margin: .55em 0; }
.md ul, .md ol { margin: .5em 0; padding-left: 1.5em; }
.md li { margin: .2em 0; }
.md blockquote { margin: .6em 0; padding: .1em 0 .1em 1em; border-left: 3px solid var(--border); color: var(--muted); }
.md hr { border: none; border-top: 1px solid var(--border); margin: 1.2em 0; }
.md table { border-collapse: collapse; margin: .7em 0; font-size: .93em; }
.md th, .md td { border: 1px solid var(--border); padding: 5px 10px; text-align: left; }
.md th { background: var(--panel-2); }
code, .mono { font-family: var(--mono); font-size: 12.8px; }
.md :not(pre) > code { background: var(--code-bg); padding: 1.5px 5px; border-radius: 4px; }
pre {
  background: var(--code-bg); border: 1px solid var(--border-soft); border-radius: 8px;
  padding: 11px 13px; overflow-x: auto; margin: .7em 0; line-height: 1.5;
}
pre code { background: none; padding: 0; }
.tok-str { color: #98643a; } .tok-com { color: var(--faint); font-style: italic; }
.tok-kw { color: #8250a8; } .tok-num { color: #2f6f4f; }
:root[data-theme="dark"] .tok-str { color: #d9a06a; }
:root[data-theme="dark"] .tok-kw { color: #c39ae6; }
:root[data-theme="dark"] .tok-num { color: #8fd6a9; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .tok-str { color: #d9a06a; }
  :root:not([data-theme="light"]) .tok-kw { color: #c39ae6; }
  :root:not([data-theme="light"]) .tok-num { color: #8fd6a9; }
}
.md img, .msg img { max-width: 100%; border-radius: 6px; border: 1px solid var(--border); }

/* ---------- collapsibles ---------- */
details.box {
  border: 1px solid var(--border-soft); border-radius: 8px; background: var(--tool-bg);
  margin: 8px 0; overflow: hidden;
}
details.box > summary {
  cursor: pointer; padding: 7px 11px; font-size: 12.5px; list-style: none;
  display: flex; gap: 9px; align-items: center; color: var(--muted);
}
details.box > summary::-webkit-details-marker { display: none; }
details.box > summary:hover { background: var(--panel-2); }
details.box > summary .tname { font: 600 12.5px var(--mono); color: var(--text); flex: none; }
details.box > summary .targ {
  font: 12px var(--mono); color: var(--faint); overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
details.box > summary .marker {
  color: var(--faint); font-size: 10px; flex: none;
  display: inline-block; transition: transform .16s ease;
}
details.box[open] > summary .marker { transform: rotate(90deg); }
details.box[open] > summary { border-bottom: 1px solid var(--border-soft); }
.boxin { padding: 10px 12px; }
.boxin > *:first-child { margin-top: 0; }
.boxin > *:last-child { margin-bottom: 0; }
.label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--faint); margin: 10px 0 4px; font-weight: 600; }
.label:first-child { margin-top: 0; }
details.thinking { background: transparent; border-style: dashed; }
details.thinking .boxin { color: var(--muted); font-size: 14px; }
details.err > summary .tname { color: #c0392b; }
:root[data-theme="dark"] details.err > summary .tname { color: #e88b7d; }
.trunc { color: var(--faint); font-size: 11.5px; font-style: italic; margin-top: 6px; }
.agentbox { border-left: 2px solid var(--accent); margin: 10px 0 4px; padding-left: 12px; }
.cmd {
  font: 12.5px var(--mono); color: var(--muted); background: var(--panel-2);
  border: 1px solid var(--border-soft); border-radius: 6px; padding: 5px 10px;
  display: inline-block; margin-bottom: 6px;
}
.cmd b { color: var(--accent); font-weight: 600; }
.hit { background: rgba(226,160,80,.35); border-radius: 2px; }
.empty { color: var(--faint); padding: 40px 0; text-align: center; font-size: 14px; }
</style>
</head>
<body>
<header>
  <div class="hwrap">
    <div class="brand">
      <h1>Claude History</h1>
      <span class="sub" id="brandsub"></span>
    </div>
    <div class="search">
      <span class="icon">⌕</span>
      <input id="q" type="search" placeholder="Search titles and conversation text…" autocomplete="off" spellcheck="false">
      <kbd>/</kbd>
    </div>
    <button class="btn" id="expandall">Expand all</button>
    <button class="btn" id="theme">◐</button>
  </div>
</header>
<main>
  <div id="notice"></div>
  <p class="summary" id="summary"></p>
  <div id="list"></div>
</main>

<script>
"use strict";

/* ===================== theme ===================== */
const root = document.documentElement;
const savedTheme = localStorage.getItem("ch-theme");
if (savedTheme) root.setAttribute("data-theme", savedTheme);
document.getElementById("theme").addEventListener("click", () => {
  const cur = root.getAttribute("data-theme");
  const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const next = cur ? (cur === "dark" ? "light" : "dark") : (sysDark ? "light" : "dark");
  root.setAttribute("data-theme", next);
  localStorage.setItem("ch-theme", next);
});

/* ===================== helpers ===================== */
const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
function fmtRange(a, b) {
  const x = fmtDate(a), y = fmtDate(b);
  if (!x) return "";
  return x === y ? x : `${x} – ${y}`;
}
function fmtDur(a, b) {
  if (!a || !b) return "";
  const ms = new Date(b) - new Date(a);
  if (!(ms > 0)) return "";
  const m = Math.round(ms / 60000);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  return h + "h" + (m % 60 ? " " + (m % 60) + "m" : "");
}
function fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
function shortModel(m) {
  if (!m) return "";
  return String(m).replace(/^claude-/, "").replace(/-\d{8}$/, "");
}

/* ===================== markdown ===================== */
const KEYWORDS = new RegExp("\\b(?:function|const|let|var|return|if|else|for|while|class|import|from|export|def|async|await|new|try|catch|finally|throw|this|self|null|None|True|False|true|false|elif|in|not|and|or|with|as|lambda|yield|switch|case|break|continue|struct|interface|type|public|private|static|void|int|str|bool|float|end|then|do|fi|esac|echo|sudo|cd|ls|git|npm|python3?)\\b", "g");

/* Placeholders shield an already-processed span from every later pass. The index
   is encoded with letters so the number and keyword regexes can never match one,
   and the sentinel is a control char that cannot appear in HTML-escaped text. */
const SENT = "\u0001";
const DIGITS = "ABCDEFGHIJ";
const PLACEHOLDER_RE = /\u0001([A-J]+)\u0001/g;

function makeStash() {
  const items = [];
  return {
    keep(htmlText) {
      const idx = items.push(htmlText) - 1;
      return SENT + String(idx).replace(/\d/g, d => DIGITS[+d]) + SENT;
    },
    restore(text) {
      // a stashed value can itself contain placeholders, so expand until stable
      for (let pass = 0; pass < 6 && text.indexOf(SENT) !== -1; pass++) {
        text = text.replace(PLACEHOLDER_RE, (m, key) =>
          items[+key.replace(/[A-J]/g, c => String(DIGITS.indexOf(c)))]);
      }
      return text;
    },
  };
}

function highlight(code) {
  let out = esc(code);
  const s = makeStash();
  const wrap = (cls, txt) => s.keep(`<span class="${cls}">${txt}</span>`);
  // strings first, so their contents are shielded from every later pass
  out = out.replace(/(&quot;(?:[^&\\\n]|\\.|&(?!quot;))*&quot;|&#39;(?:[^&\\\n]|\\.|&(?!#39;))*&#39;|`[^`\n]*`)/g,
    m => wrap("tok-str", m));
  out = out.replace(/(^|\s)(#[^\n]*|\/\/[^\n]*)/g, (m, pre, c) => pre + wrap("tok-com", c));
  out = out.replace(/\/\*[\s\S]*?\*\//g, m => wrap("tok-com", m));
  out = out.replace(/\b\d+(?:\.\d+)?\b/g, m => wrap("tok-num", m));
  out = out.replace(KEYWORDS, m => wrap("tok-kw", m));
  return s.restore(out);
}

function inlineMd(src) {
  let out = esc(src);
  const s = makeStash();
  out = out.replace(/`([^`]+)`/g, (m, c) => s.keep(`<code>${c}</code>`));
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g,
    (m, alt, url) => s.keep(`<img src="${encodeURI(url)}" alt="${alt}">`));
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)[^)]*\)/g,
    (m, text, url) => s.keep(`<a href="${encodeURI(url)}" target="_blank" rel="noopener">${text}</a>`));
  out = out.replace(/(^|[\s(])(https?:\/\/[^\s<>()]+)/g,
    (m, pre, url) => pre + s.keep(`<a href="${encodeURI(url)}" target="_blank" rel="noopener">${url}</a>`));
  out = out.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  return s.restore(out);
}

function md(src) {
  if (!src) return "";
  const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const listStack = [];

  const closeLists = (toDepth = 0) => {
    while (listStack.length > toDepth) out.push(listStack.pop() === "ul" ? "</ul>" : "</ol>");
  };

  while (i < lines.length) {
    const line = lines[i];

    const fence = line.match(/^\s*```+\s*([\w+.#-]*)/);
    if (fence) {
      closeLists();
      const lang = fence[1] || "";
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code class="lang-${esc(lang)}">${highlight(buf.join("\n"), lang)}</code></pre>`);
      continue;
    }

    if (/^\s*$/.test(line)) { closeLists(); i++; continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeLists();
      const lvl = Math.min(h[1].length, 4);
      out.push(`<h${lvl}>${inlineMd(h[2])}</h${lvl}>`);
      i++; continue;
    }

    if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) { closeLists(); out.push("<hr>"); i++; continue; }

    if (/^\s*>/.test(line)) {
      closeLists();
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${md(buf.join("\n"))}</blockquote>`);
      continue;
    }

    // table
    if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      closeLists();
      const cells = r => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(c => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push("<table><thead><tr>" + head.map(c => `<th>${inlineMd(c)}</th>`).join("") + "</tr></thead><tbody>"
        + rows.map(r => "<tr>" + r.map(c => `<td>${inlineMd(c)}</td>`).join("") + "</tr>").join("")
        + "</tbody></table>");
      continue;
    }

    const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      const depth = Math.floor(li[1].replace(/\t/g, "    ").length / 2) + 1;
      const kind = /^\d/.test(li[2]) ? "ol" : "ul";
      while (listStack.length > depth) out.push(listStack.pop() === "ul" ? "</ul>" : "</ol>");
      while (listStack.length < depth) { listStack.push(kind); out.push(kind === "ul" ? "<ul>" : "<ol>"); }
      if (listStack[listStack.length - 1] !== kind) {
        out.push(listStack.pop() === "ul" ? "</ul>" : "</ol>");
        listStack.push(kind); out.push(kind === "ul" ? "<ul>" : "<ol>");
      }
      out.push(`<li>${inlineMd(li[3])}</li>`);
      i++; continue;
    }

    closeLists();
    const buf = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*(?:```|#{1,6}\s|>|[-*+]\s|\d+[.)]\s)/.test(lines[i])) {
      buf.push(lines[i++]);
    }
    if (buf.length) out.push(`<p>${inlineMd(buf.join("\n")).replace(/\n/g, "<br>")}</p>`);
    else i++;
  }
  closeLists();
  return out.join("\n");
}

/* ===================== conversation rendering ===================== */
function truncNote(n) {
  return n ? `<div class="trunc">… ${n.toLocaleString()} more characters truncated</div>` : "";
}

function renderToolInput(input) {
  if (input == null) return "";
  if (typeof input === "string") return `<pre><code>${esc(input)}</code></pre>`;
  const keys = Object.keys(input);
  if (!keys.length) return "";
  let h = "";
  for (const k of keys) {
    const v = input[k];
    const text = typeof v === "string" ? v : JSON.stringify(v, null, 2);
    h += `<div class="label">${esc(k)}</div><pre><code>${highlight(text || "", "")}</code></pre>`;
  }
  return h;
}

function renderResult(r) {
  if (!r) return `<div class="label">Result</div><div class="trunc">no result recorded</div>`;
  let h = `<div class="label">${r.isError ? "Error" : "Result"}</div>`;
  if (r.text) h += `<pre><code>${esc(r.text)}</code></pre>` + truncNote(r.dropped);
  for (const src of (r.images || [])) h += `<img src="${src}" alt="tool output image">`;
  if (!r.text && !(r.images || []).length) h += `<div class="trunc">empty</div>`;
  return h;
}

/* Collapsed content (tool output, thinking, subagents) is by far the bulk of a
   transcript — often tens of MB. `store` holds a thunk per collapsible; the body
   is built the first time the user opens it, so the initial DOM stays small. */
function lazyBox(store, cls, name, arg, thunk) {
  const idx = store.push(thunk) - 1;
  return `<details class="box ${cls}" data-lazy="${idx}"><summary><span class="marker">▸</span>`
    + `<span class="tname">${esc(name)}</span><span class="targ">${esc(arg || "")}</span></summary>`
    + `<div class="boxin"></div></details>`;
}

function renderBlock(b, store) {
  if (b.t === "text") return `<div class="md">${md(b.text)}</div>` + truncNote(b.dropped);
  if (b.t === "image") return `<img src="${b.src}" alt="attached image">`;
  if (b.t === "thinking") {
    const peek = (b.text || "").slice(0, 110).replace(/\s+/g, " ");
    return lazyBox(store, "thinking", "Thinking", peek,
      () => `<div class="md">${md(b.text)}</div>${truncNote(b.dropped)}`);
  }
  if (b.t === "tool") {
    const cls = b.result && b.result.isError ? "err" : "";
    return lazyBox(store, cls, b.name, b.summary, () => {
      let inner = renderToolInput(b.input) + renderResult(b.result);
      for (const a of (b.agents || [])) {
        inner += `<div class="label">Subagent transcript — ${esc(a.meta.title || a.meta.id)}</div>`
          + `<div class="agentbox">${renderMessages(a.messages, store)}</div>`;
      }
      return inner;
    });
  }
  return "";
}

/* Anything that is not something a person said or wrote. Reading view hides these,
   including turns that were nothing but tool calls (their header would be left empty). */
const isProse = b => b.t === "text" || b.t === "image";

function countAux(messages) {
  let n = 0;
  for (const m of messages || []) {
    if (m.role === "command" || m.role === "output") { n++; continue; }
    n += (m.blocks || []).filter(b => !isProse(b)).length;
    if (m.reminders && m.reminders.length) n++;
  }
  return n;
}

function renderMessages(messages, store) {
  let h = "";
  let lastProseRole = null;   // for suppressing repeated speaker labels in reading view
  for (const m of messages || []) {
    if (m.role === "command") {
      h += `<div class="msg aux"><div class="cmd"><b>${esc(m.name)}</b>${m.args ? " " + esc(m.args) : ""}</div>`;
      if (m.output) h += `<pre><code>${esc(m.output)}</code></pre>`;
      h += `</div>`;
      continue;
    }
    if (m.role === "output") {
      h += `<div class="msg aux"><pre><code>${esc(m.text)}</code></pre></div>`;
      continue;
    }
    const label = m.role === "user" ? "You" : "Claude";
    const extra = m.role === "assistant" && m.model ? ` · ${esc(shortModel(m.model))}` : "";
    const prose = (m.blocks || []).some(isProse);
    // hiding tool-only turns can leave a run of identical speaker labels; mark the
    // repeats so reading view shows one label per actual change of speaker
    const cont = prose && lastProseRole === m.role ? " cont" : "";
    if (prose) lastProseRole = m.role;
    h += `<div class="msg ${m.role}${prose ? "" : " no-prose"}${cont}">`
      + `<div class="role">${label}<span class="ts">${esc(fmtTime(m.ts))}${extra}</span></div>`
      + `<div class="bubble">`;
    for (const b of m.blocks || []) h += renderBlock(b, store);
    if (m.reminders && m.reminders.length) {
      h += lazyBox(store, "", "System reminders", String(m.reminders.length),
        () => m.reminders.map(r => `<pre><code>${esc(r)}</code></pre>`).join(""));
    }
    h += `</div></div>`;
  }
  return h || `<div class="trunc">No messages recorded.</div>`;
}

function renderConversation(data, store) {
  const m = data.meta;
  const bits = [];
  if (m.start) bits.push(fmtDateTime(m.start));
  const dur = fmtDur(m.start, m.end);
  if (dur) bits.push(dur);
  bits.push(`${m.userTurns} user · ${m.assistantTurns} assistant`);
  if (m.toolCalls) bits.push(`${m.toolCalls} tool calls`);
  if (m.gitBranch) bits.push(`⎇ ${m.gitBranch}`);
  for (const mo of (m.models || [])) bits.push(shortModel(mo));
  if (m.version) bits.push(`cc ${m.version}`);
  if (m.slug) bits.push(m.slug);
  bits.push(m.id);

  const arch = m.archivedOnly
    ? `<span class="pill arch" title="Deleted from ~/.claude/projects — kept by the archive">archived</span>`
    : "";
  const aux = countAux(data.messages) + (data.orphanAgents || []).length;
  const view = currentView();
  const toggle = `<span class="viewtoggle" role="group" aria-label="Transcript detail">`
    + `<button class="vbtn${view === "reading" ? " on" : ""}" data-view="reading">Reading</button>`
    + `<button class="vbtn${view === "full" ? " on" : ""}" data-view="full">Full</button></span>`
    + (aux ? `<span class="pill hidnote">${aux} tool call${aux === 1 ? "" : "s"} &amp; sidechains hidden</span>` : "");

  let h = `<div class="detailbar">${toggle}${arch}${bits.map(b => `<span class="pill">${esc(b)}</span>`).join("")}</div>`;
  h += renderMessages(data.messages, store);
  if ((data.orphanAgents || []).length) {
    h += `<div class="label aux">Subagent runs not linked to a specific tool call</div>`;
    for (const a of data.orphanAgents) {
      h += lazyBox(store, "", a.meta.title || a.meta.id, a.meta.id,
        () => `<div class="agentbox">${renderMessages(a.messages, store)}</div>`);
    }
  }
  return h;
}

/* ===================== index rendering ===================== */
let INDEX = null;
const cache = new Map();

/* The toggle is per conversation, but the last choice seeds newly opened ones —
   otherwise you would flip every conversation by hand. */
function currentView() {
  return localStorage.getItem("ch-view") === "full" ? "full" : "reading";
}
function applyView(body, view) {
  body.classList.toggle("reading", view === "reading");
  for (const b of body.querySelectorAll(":scope > .detailbar .vbtn")) {
    b.classList.toggle("on", b.dataset.view === view);
  }
}

function convRow(p, c) {
  const bits = [];
  if (c.start) bits.push(fmtDate(c.start));
  bits.push(`${c.userTurns + c.assistantTurns} msgs`);
  if (c.gitBranch) bits.push(c.gitBranch);
  const arch = c.archivedOnly
    ? `<span class="pill arch" title="Deleted from ~/.claude/projects — kept by the archive">archived</span>`
    : "";
  return `<div class="conv" data-p="${esc(p.slug)}" data-c="${esc(c.id)}" data-file="${esc(c.file)}">
    <div class="head"><span class="chev">▶</span>
      <span class="ctitle">${esc(c.title)}</span>
      <span class="cmeta">${arch}${bits.map(b => `<span class="pill">${esc(b)}</span>`).join("")}</span>
    </div>
    <div class="body"><div class="loading">Loading…</div></div>
  </div>`;
}

function projectCard(p, convs) {
  return `<div class="project" data-p="${esc(p.slug)}">
    <div class="head"><span class="chev">▶</span>
      <span class="pname">${esc(p.name)}</span>
      <span class="ppath">${esc(p.path)}</span>
      <span class="pmeta">
        ${p.archivedOnly ? `<span class="pill arch">${p.archivedOnly} archived</span>` : ""}
        <span class="pill accent">${convs.length}${convs.length !== p.conversations.length ? "/" + p.conversations.length : ""} conv</span>
        <span class="pill">${esc(fmtRange(p.start, p.end))}</span>
      </span>
    </div>
    <div class="convlist">${convs.map(c => convRow(p, c)).join("")}</div>
  </div>`;
}

function render(query) {
  const q = (query || "").trim().toLowerCase();
  const list = document.getElementById("list");
  const openProjects = new Set([...document.querySelectorAll(".project.open")].map(e => e.dataset.p));
  let shown = 0, cards = "";

  for (const p of INDEX.projects) {
    let convs = p.conversations;
    if (q) {
      convs = convs.filter(c =>
        (c.title || "").toLowerCase().includes(q) ||
        (c.q || "").includes(q) ||
        (c.gitBranch || "").toLowerCase().includes(q) ||
        (p.path || "").toLowerCase().includes(q));
    }
    if (!convs.length) continue;
    shown += convs.length;
    cards += projectCard(p, convs);
  }

  list.innerHTML = cards || `<div class="empty">No conversations match “${esc(q)}”.</div>`;

  for (const el of list.querySelectorAll(".project")) {
    if (q || openProjects.has(el.dataset.p)) el.classList.add("open");
  }

  const s = INDEX.stats;
  const archNote = s.archivedOnly ? ` · ${s.archivedOnly} kept only by the archive` : "";
  document.getElementById("summary").textContent = q
    ? `${shown} matching conversation${shown === 1 ? "" : "s"}`
    : `${s.conversations} conversations across ${s.projects} projects${archNote}`
      + ` · generated ${fmtDateTime(INDEX.generated)}`;
}

async function openConversation(el) {
  const body = el.querySelector(".body");
  const file = el.dataset.file;
  if (el.dataset.loaded === "1") return;
  el.dataset.loaded = "1";
  try {
    let data = cache.get(file);
    if (!data) {
      const res = await fetch(file);
      if (!res.ok) throw new Error(res.status + " " + res.statusText);
      data = await res.json();
      cache.set(file, data);
    }
    const store = [];
    body.innerHTML = renderConversation(data, store);
    applyView(body, currentView());
    // `toggle` does not bubble, so listen in the capture phase
    body.addEventListener("toggle", ev => {
      const box = ev.target;
      if (!box.open || box.dataset.lazy === undefined) return;
      const thunk = store[+box.dataset.lazy];
      delete box.dataset.lazy;
      const slot = box.querySelector(":scope > .boxin");
      if (slot && thunk) slot.innerHTML = thunk();
    }, true);
  } catch (err) {
    el.dataset.loaded = "0";
    body.innerHTML = `<div class="loading">Could not load this conversation: ${esc(err.message)}</div>`;
  }
}

/* ===================== events ===================== */
document.getElementById("list").addEventListener("click", ev => {
  const vbtn = ev.target.closest(".vbtn");
  if (vbtn) {
    const view = vbtn.dataset.view;
    localStorage.setItem("ch-view", view);
    applyView(vbtn.closest(".body"), view);
    return;
  }
  const convHead = ev.target.closest(".conv > .head");
  if (convHead) {
    const conv = convHead.parentElement;
    conv.classList.toggle("open");
    if (conv.classList.contains("open")) {
      openConversation(conv);
      history.replaceState(null, "", "#" + conv.dataset.p + "/" + conv.dataset.c);
    }
    return;
  }
  const projHead = ev.target.closest(".project > .head");
  if (projHead) projHead.parentElement.classList.toggle("open");
});

const qInput = document.getElementById("q");
let debounce;
qInput.addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => render(qInput.value), 140);
});
document.addEventListener("keydown", ev => {
  if (ev.key === "/" && document.activeElement !== qInput) { ev.preventDefault(); qInput.focus(); qInput.select(); }
  if (ev.key === "Escape" && document.activeElement === qInput) { qInput.value = ""; render(""); qInput.blur(); }
});
document.getElementById("expandall").addEventListener("click", ev => {
  const projects = [...document.querySelectorAll(".project")];
  const anyClosed = projects.some(p => !p.classList.contains("open"));
  projects.forEach(p => p.classList.toggle("open", anyClosed));
  ev.target.textContent = anyClosed ? "Collapse all" : "Expand all";
});

/* ===================== boot ===================== */
(async function () {
  const notice = document.getElementById("notice");
  try {
    const res = await fetch("data/index.json");
    if (!res.ok) throw new Error(res.status + " " + res.statusText);
    INDEX = await res.json();
  } catch (err) {
    notice.innerHTML = location.protocol === "file:"
      ? `<div class="notice"><b>This page needs a web server.</b> Browsers block <code>fetch()</code> over
         <code>file://</code>, so the conversation data can’t be loaded. Run
         <code>python3 claude_history.py --serve</code> and open the printed URL.</div>`
      : `<div class="notice"><b>Could not load data/index.json.</b> ${esc(err.message)}</div>`;
    document.getElementById("summary").textContent = "";
    return;
  }

  document.getElementById("brandsub").textContent = INDEX.source;
  render("");

  const hash = decodeURIComponent(location.hash.slice(1));
  if (hash.includes("/")) {
    const [pslug, cid] = hash.split("/");
    const conv = document.querySelector(`.conv[data-p="${CSS.escape(pslug)}"][data-c="${CSS.escape(cid)}"]`);
    if (conv) {
      conv.closest(".project").classList.add("open");
      conv.classList.add("open");
      openConversation(conv);
      conv.scrollIntoView({ block: "start" });
    }
  }
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def serve(out: Path, port: int):
    import http.server
    import socketserver

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(out), **kw)

        def log_message(self, fmt, *args):  # quieter output
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"\nServing {out} at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Render Claude Code conversation history as a browsable HTML site.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Claude projects directory")
    ap.add_argument("--out", type=Path, default=Path("out"), help="output directory")
    ap.add_argument("--archive", type=Path, default=default_archive(),
                    help="permanent transcript archive (append-only)")
    ap.add_argument("--no-archive", action="store_true",
                    help="skip the archive and build straight from --source")
    ap.add_argument("--sync-only", action="store_true",
                    help="update the archive without building the site")
    ap.add_argument("--serve", nargs="?", const=8000, type=int, metavar="PORT",
                    help="serve the site after building")
    ap.add_argument("--max-result-chars", type=int, default=6000, help="truncate tool results")
    ap.add_argument("--max-input-chars", type=int, default=4000, help="truncate tool input strings")
    ap.add_argument("--max-text-chars", type=int, default=200000, help="truncate message text")
    ap.add_argument("--max-image-kb", type=int, default=1500, help="skip images larger than this")
    ap.add_argument("--search-chars", type=int, default=12000, help="searchable text kept per conversation")
    ap.add_argument("--no-images", action="store_true", help="drop embedded images")
    ap.add_argument("--no-thinking", action="store_true", help="drop assistant thinking blocks")
    ap.add_argument("--no-tools", action="store_true", help="drop tool calls and results")
    ap.add_argument("--no-subagents", action="store_true", help="drop nested subagent transcripts")
    args = ap.parse_args(argv)

    source = args.source.expanduser()
    out = args.out.expanduser()
    archive = args.archive.expanduser()

    if not source.is_dir():
        print(f"error: no such directory: {source}", file=sys.stderr)
        return 1

    live: set[str] | None = None
    build_root = source

    if args.no_archive:
        if args.sync_only:
            print("error: --sync-only and --no-archive are contradictory", file=sys.stderr)
            return 1
        print(f"Reading {source} (archive disabled)")
    else:
        print(f"Archiving {source} -> {archive}")
        live, _manifest, sync = sync_archive(source, archive)
        parts = [f"{sync['added']} new", f"{sync['updated']} updated", f"{sync['unchanged']} unchanged"]
        if sync["shrunk"]:
            parts.append(f"{sync['shrunk']} kept from archive")
        print(f"  {', '.join(parts)}  ({sync['bytes'] / 1048576:.1f} MB copied)")
        if sync["archivedOnly"]:
            print(f"  {sync['archivedOnly']} transcript(s) no longer upstream — preserved here")
        build_root = archive
        if args.sync_only:
            total = sum(f.stat().st_size for f in archive.rglob("*.jsonl"))
            print(f"\nArchive holds {len(list(archive.rglob('*.jsonl')))} transcripts, {total / 1048576:.1f} MB")
            return 0

    out.mkdir(parents=True, exist_ok=True)
    index = build(build_root, out, Options(args), live)

    stats = index["stats"]
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    archived = stats.get("archivedOnly") or 0
    suffix = f", {archived} of them archive-only" if archived else ""
    print(
        f"\nWrote {stats['conversations']} conversations from {stats['projects']} projects{suffix} "
        f"({stats['messages']} messages, {size / 1048576:.1f} MB) to {out}"
    )
    print(f"Open {out / 'index.html'} — needs a web server, e.g. --serve")

    if args.serve is not None:
        serve(out, args.serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
