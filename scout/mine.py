"""Local pattern mining over the event window. Pure Python, zero tokens.

Pattern types:
  command   - the same (normalized) shell command typed again and again
  sequence  - the same 2-4 command chain repeated inside work sessions
  prompt    - near-duplicate prompts you keep giving Claude
  slash     - slash commands used often (signal of what already works)
  new_file  - new files of the same shape created repeatedly (template candidate)
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .redact import redact

SESSION_GAP = 30 * 60
_STOP = set("""a an the and or but to of in on for with this that it is are be can you i me my we our
please just now also then so do does make use using get want need like into from as at by not no yes
what how why when where which there here should would could will let lets okay ok hi hey thanks""".split())

_NORM = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'"), "<str>"),
    (re.compile(r"(?<![\w<])(?:~|/)[^\s'\"|;&<>]*"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hash>"),
    (re.compile(r"(?<![\w-])[\w./-]+\.(?:md|txt|json|js|jsx|ts|tsx|py|tex|pdf|csv|yml|yaml|sh|html|css)\b"), "<file>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def norm_cmd(cmd: str) -> str:
    s = cmd.strip()
    for rx, repl in _NORM:
        s = rx.sub(repl, s)
    return s.strip()


def _fp(kind, key):
    return hashlib.sha1((kind + "\0" + key).encode()).hexdigest()[:10]


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9_-]{2,}", text.lower()) if w not in _STOP}


def _is_trivial(ncmd, trivial):
    head = ncmd.split(" ", 1)[0]
    return head in trivial and not re.search(r"&&|\||;", ncmd)


def _pattern(kind, key, items, minutes, examples, **meta):
    tss = [t for t, _ in items]
    return {
        "fp": _fp(kind, key), "type": kind, "key": key, "count": len(items),
        "days": len({_day(t) for t in tss}), "first_seen": min(tss), "last_seen": max(tss),
        "minutes": round(minutes, 1), "examples": examples, **meta,
    }


def _top_examples(raws, n=3):
    return [f"{redact(r, 200)}  (x{c})" for r, c in Counter(raws).most_common(n)]


def _commands(rows, trivial):
    by_key = defaultdict(list)
    for r in rows:
        n = norm_cmd(r["text"])
        if n and not _is_trivial(n, trivial):
            by_key[(r["kind"], n)].append((r["ts"], r["text"]))
    out = []
    for (kind, key), items in by_key.items():
        raws = [t for _, t in items]
        avg_len = sum(len(x) for x in raws) / len(raws)
        sec = 2 + avg_len / 6  # recall + typing at ~6 chars/sec
        out.append(_pattern("command", key, items, len(items) * sec / 60, _top_examples(raws), via=kind))
    return out


def _sessions(rows):
    """Split a time-sorted stream into sessions (by explicit session id or idle gap)."""
    sess, cur, last, last_id = [], [], None, object()
    for r in rows:
        sid = r["session"]
        if cur and ((sid or None) != (last_id or None) or r["ts"] - last > SESSION_GAP):
            sess.append(cur)
            cur = []
        cur.append(r)
        last, last_id = r["ts"], sid
    if cur:
        sess.append(cur)
    return sess


def _sequences(rows, trivial):
    grams = defaultdict(list)
    for kind in ("shell", "agent_shell"):
        stream = [r for r in rows if r["kind"] == kind]
        for s in _sessions(stream):
            seq = []
            for r in s:
                n = norm_cmd(r["text"])
                head = n.split(" ", 1)[0]
                if not n or (head in trivial and head != "cd"):
                    continue
                if seq and seq[-1][0] == n:
                    continue  # collapse immediate repeats
                seq.append((n, r))
            for size in (2, 3, 4):
                for i in range(len(seq) - size + 1):
                    win = seq[i:i + size]
                    keys = tuple(k for k, _ in win)
                    if len(set(keys)) < size or all(_is_trivial(k, trivial) for k in keys):
                        continue
                    grams[(kind, keys)].append((win[0][1]["ts"], " ; ".join(r["text"] for _, r in win)))
    out = []
    for (kind, keys), items in grams.items():
        sec = sum(2 + len(k) / 6 for k in keys) + 3 * len(keys)  # typing + context switching
        p = _pattern("sequence", " ; ".join(keys), items, len(items) * sec / 60,
                     _top_examples([t for _, t in items], 2), via=kind, steps=len(keys))
        p["_keys"] = keys
        out.append(p)
    # drop sub-sequences that are explained by a longer sequence with similar frequency
    out.sort(key=lambda p: -p["steps"])
    kept = []
    for p in out:
        k = p["_keys"]
        if any(len(q["_keys"]) > len(k) and q["count"] >= 0.8 * p["count"] and
               any(q["_keys"][i:i + len(k)] == k for i in range(len(q["_keys"]) - len(k) + 1))
               for q in kept):
            continue
        kept.append(p)
    for p in kept:
        p.pop("_keys")
    return kept


def _prompts(rows):
    prompts = [(r["ts"], r["text"], _words(r["text"])) for r in rows if r["kind"] == "prompt"]
    clusters = []  # [vocab Counter, items]
    for ts, text, words in prompts:
        if len(words) < 3:
            continue
        best, best_j = None, 0.0
        for c in clusters:
            j = len(words & c["rep"]) / len(words | c["rep"])
            if j > best_j:
                best, best_j = c, j
        if best and best_j >= 0.45:
            best["items"].append((ts, text))
            best["vocab"].update(words)
        else:
            clusters.append({"rep": words, "vocab": Counter(words), "items": [(ts, text)]})
    out = []
    for c in clusters:
        if len(c["items"]) < 3:
            continue
        key = " ".join(sorted(w for w, _ in c["vocab"].most_common(5)))
        sec = sum(20 + len(t) / 5 for _, t in c["items"]) / len(c["items"])
        out.append(_pattern("prompt", key, c["items"], len(c["items"]) * sec / 60,
                            [redact(t, 220) for _, t in c["items"][-3:]]))
    return out


def _slash(rows):
    by = defaultdict(list)
    for r in rows:
        if r["kind"] == "slash":
            by[r["text"]].append((r["ts"], r["text"]))
    return [_pattern("slash", k, v, 0, [k]) for k, v in by.items()]


def _new_files(rows, ignore_parts):
    """Group first-ever edits of files by (workspace-relative dir shape, extension)."""
    first = {}
    for r in rows:
        if r["kind"] == "edit" and r["text"] not in first:
            first[r["text"]] = r
    groups = defaultdict(list)
    for path, r in first.items():
        p = Path(path)
        if any(part in ignore_parts for part in p.parts):
            continue
        ws = r["cwd"] or str(p.parent)
        try:
            rel = p.relative_to(ws)
        except ValueError:
            rel = Path(p.name)
        parent = rel.parent
        shapes = [str(parent / f"*{p.suffix}")]
        if len(parent.parts) >= 1:
            shapes.append(str(parent.parent / "*" / f"*{p.suffix}"))
        for shape in shapes:
            groups[(ws, shape)].append((r["ts"], str(rel)))
    out = []
    for (ws, shape), items in groups.items():
        if len(items) < 3:
            continue
        key = f"{Path(ws).name}/{shape}"
        out.append(_pattern("new_file", key, items, len(items) * 2.0,
                            [i[1] for i in sorted(items)[-4:]], workspace=ws))
    # keep the most specific shape when two shapes cover the same files
    out.sort(key=lambda p: (p["key"].count("*"), -p["count"]))
    kept, covered = [], []
    for p in out:
        files = set(p["examples"])
        if any(files <= c for c in covered):
            continue
        kept.append(p)
        covered.append(files)
    return kept


def mine(db, cfg) -> list:
    since = int(time.time()) - cfg["window_days"] * 86400
    rows = db.execute("SELECT * FROM events WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    trivial = set(cfg["trivial_commands"])
    shellish = [r for r in rows if r["kind"] in ("shell", "agent_shell")]
    patterns = (_commands(shellish, trivial) + _sequences(shellish, trivial) +
                _prompts(rows) + _slash(rows) + _new_files(rows, set(cfg["ignore_path_parts"])))
    now = int(time.time())
    db.execute("DELETE FROM patterns")
    db.executemany(
        "INSERT OR REPLACE INTO patterns VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(p["fp"], p["type"], p["key"], p["count"], p["days"], p["first_seen"], p["last_seen"],
          p["minutes"], json.dumps(p), now) for p in patterns],
    )
    db.commit()
    return patterns
