"""Collectors. Each reads one local source incrementally and yields event tuples:
(ts, source, kind, cwd, text, session, extra). No network, no LLM.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import store

_ZSH_RE = re.compile(r"^: (\d+):\d+;(.*)$")


def _unmetafy(b: bytes) -> bytes:
    # zsh stores bytes >= 0x83 as 0x83 followed by (byte ^ 0x20)
    if b"\x83" not in b:
        return b
    out, it = bytearray(), iter(b)
    for c in it:
        if c == 0x83:
            n = next(it, None)
            if n is not None:
                out.append(n ^ 0x20)
        else:
            out.append(c)
    return bytes(out)


def zsh_history(db, path, since):
    p = Path(path)
    if not p.exists():
        return
    last = store.get_cursor(db, "zsh_ts", since)
    entries, cur = [], None
    for line in _unmetafy(p.read_bytes()).decode("utf-8", "replace").split("\n"):
        m = _ZSH_RE.match(line)
        if m and not (cur and cur[1].endswith("\\")):
            if cur:
                entries.append(cur)
            cur = [int(m.group(1)), m.group(2)]
        elif cur is not None:
            cur[1] = (cur[1][:-1] if cur[1].endswith("\\") else cur[1]) + "\n" + line
    if cur:
        entries.append(cur)
    newest = last
    for ts, cmd in entries:
        cmd = cmd.strip()
        if ts > last and cmd:
            newest = max(newest, ts)
            yield (ts, "zsh_history", "shell", None, cmd, None, None)
    store.set_cursor(db, "zsh_ts", newest)


def shell_log(db, path, since):
    """Lines written by shell/habit-scout.zsh: ts<TAB>exit<TAB>cwd<TAB>term<TAB>cmd."""
    p = Path(path)
    if not p.exists():
        return
    offset = store.get_cursor(db, "shell_log_offset", 0)
    if offset > p.stat().st_size:  # rotated / truncated
        offset = 0
    with p.open("rb") as f:
        f.seek(offset)
        data = f.read()
        store.set_cursor(db, "shell_log_offset", offset + len(data))
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5 or not parts[0].isdigit():
            continue
        ts, code, cwd, term, cmd = parts
        cmd = cmd.replace("\\n", "\n").strip()
        if int(ts) >= since and cmd:
            extra = json.dumps({"exit": int(code) if code.lstrip("-").isdigit() else None, "term": term})
            yield (int(ts), "shell_hook", "shell", cwd, cmd, None, extra)


def _iso_ts(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def claude_sessions(db, root, since):
    """Human prompts and the shell commands Claude ran on your behalf."""
    rootp = Path(root)
    if not rootp.exists():
        return
    seen = store.get_cursor(db, "claude_mtimes", {})
    for f in rootp.glob("*/*.jsonl"):
        mt = f.stat().st_mtime
        if mt < since or seen.get(str(f), 0) >= mt:
            continue
        seen[str(f)] = mt
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type":"user"' not in line and '"tool_use"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                ts = _iso_ts(o.get("timestamp"))
                if not ts or ts < since:
                    continue
                cwd, sess = o.get("cwd"), o.get("sessionId")
                msg = o.get("message") or {}
                content = msg.get("content")
                if o.get("type") == "user" and not o.get("isMeta") and not o.get("toolUseResult"):
                    text = content if isinstance(content, str) else None
                    if isinstance(content, list) and not any(c.get("type") == "tool_result" for c in content if isinstance(c, dict)):
                        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                    if not text or not text.strip():
                        continue
                    text = text.strip()
                    m = re.match(r"<command-name>/?([^<]+)</command-name>", text)
                    if m:
                        yield (ts, "claude", "slash", cwd, "/" + m.group(1).strip(), sess, None)
                    elif not text.startswith(("<", "[Request interrupted")):
                        yield (ts, "claude", "prompt", cwd, text[:2000], sess, None)
                elif o.get("type") == "assistant" and isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                            cmd = (c.get("input") or {}).get("command", "").strip()
                            if cmd:
                                yield (ts, "claude", "agent_shell", cwd, cmd, sess, None)
    store.set_cursor(db, "claude_mtimes", seen)


def _uri_path(uri):
    u = urlparse(uri)
    return unquote(u.path) if u.scheme == "file" else None


def vscode(db, user_dir, since):
    """File saves from VS Code's local History, plus workspace opens."""
    user = Path(user_dir)
    folders = []
    for wj in (user / "workspaceStorage").glob("*/workspace.json"):
        try:
            folder = _uri_path(json.loads(wj.read_text()).get("folder", ""))
        except ValueError:
            continue
        if not folder:
            continue
        folders.append(folder)
        state = wj.parent / "state.vscdb"
        mt = int((state if state.exists() else wj).stat().st_mtime)
        if mt >= since:
            # one event per day per workspace is enough
            day_ts = mt - mt % 86400
            yield (day_ts, "vscode", "workspace", folder, folder, None, None)
    folders.sort(key=len, reverse=True)

    last_ms = store.get_cursor(db, "vscode_hist_ms", since * 1000)
    newest = last_ms
    for ej in (user / "History").glob("*/entries.json"):
        if ej.stat().st_mtime * 1000 <= last_ms:
            continue
        try:
            data = json.loads(ej.read_text())
        except ValueError:
            continue
        path = _uri_path(data.get("resource", ""))
        if not path:
            continue
        ws = next((w for w in folders if path.startswith(w + "/")), None)
        for e in data.get("entries", []):
            ms = e.get("timestamp", 0)
            if ms > last_ms:
                newest = max(newest, ms)
                extra = json.dumps({"src": e["source"]}) if e.get("source") else None
                yield (ms // 1000, "vscode", "edit", ws or str(Path(path).parent), path, None, extra)
    store.set_cursor(db, "vscode_hist_ms", newest)


def collect_all(db, cfg) -> int:
    since = int(time.time()) - cfg["retention_days"] * 86400
    src = cfg["sources"]
    added = 0
    # hook log first so its richer rows (cwd, exit code) win the UNIQUE(kind, ts, text) dedupe
    for gen in (
        shell_log(db, src["shell_log"], since),
        zsh_history(db, src["zsh_history"], since),
        claude_sessions(db, src["claude_projects"], since),
        vscode(db, src["vscode_user"], since),
    ):
        added += store.add_events(db, gen)
    store.prune(db, cfg["retention_days"])
    db.commit()
    return added
