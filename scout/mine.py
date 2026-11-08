"""Local pattern mining over the event window. Pure Python, zero tokens.

Pattern types:
  command   - the same (normalized) shell command typed again and again
  sequence  - the same 2-4 command chain repeated inside work sessions
  template  - the same command *shape* with different arguments each time, e.g.
              `git checkout -b <*> refs/tags/<*>`; found per step inside chains, after expanding
              your aliases and mapping equivalent commands (config: command_synonyms)
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


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b", re.S)
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||(?<!\$)\{|\})\s*")
_REDIRECT = re.compile(r"^\d*(?:>>?|<)&?\S*$")
_PLACEHOLDER = re.compile(r"^<[a-z*]+>$")
WILD = "<*>"


def read_aliases(cfg):
    """name -> normalized expansion, from the user's alias files."""
    out = {}
    rx = re.compile(r"^\s*alias\s+([\w.-]+)=(['\"]?)(.*)\2\s*$")
    for f in cfg["alias_files"]:
        p = Path(f)
        if not p.exists():
            continue
        for line in p.read_text(errors="replace").splitlines():
            m = rx.match(line)
            if m:
                out[m.group(1)] = norm_cmd(m.group(3))
    return out


def segments(text, aliases=None, synonyms=None, noise=()):
    """Split a (possibly chained, multi-line) command into normalized token lists, one per step.

    Heredoc bodies and redirections are dropped, a leading alias is expanded, and equivalent
    commands are rewritten to one canonical spelling so different ways of doing the same thing align.
    """
    aliases, synonyms = aliases or {}, sorted((synonyms or {}).items(), key=lambda kv: -len(kv[0]))
    out = []
    for line in _HEREDOC.sub(" <heredoc>", text).splitlines():
        for seg in _SPLIT.split(norm_cmd(line)):
            toks = [t for t in seg.split()
                    if t not in noise and not (_REDIRECT.match(t) and not _PLACEHOLDER.match(t))]
            if not toks:
                continue
            if toks[0] in aliases and aliases[toks[0]]:
                toks = aliases[toks[0]].split() + toks[1:]
            joined = " ".join(toks)
            for src, dst in synonyms:
                if joined == src or joined.startswith(src + " "):
                    toks = (dst + joined[len(src):]).split()
                    break
            out.append(toks)
    return out


def _is_flag(t):
    return t.startswith("-") and t != "-"


def _fit(tpl, toks):
    """Share of positions that agree; None if a flag differs (flags are structure, not arguments)."""
    same = 0
    for a, b in zip(tpl, toks):
        if a == b:
            same += 1
        elif _is_flag(b) or (a != WILD and _is_flag(a)):
            return None
    return same / len(toks)


def _templates(rows, trivial, cfg, aliases):
    """Drain-style clustering of command steps: same tool, same flags, varying arguments."""
    sim = cfg["template_similarity"]
    synonyms, noise = cfg["command_synonyms"], set(cfg["noise_flags"])
    groups, clusters, per_event = defaultdict(list), [], []
    for r in rows:
        hit = []
        for toks in segments(r["text"], aliases, synonyms, noise):
            if len(toks) < 3 or toks[0] in trivial or "<heredoc>" in toks:
                continue  # inline scripts have no stable shape
            best, score = None, 0.0
            for c in groups[(toks[0], toks[1], len(toks))]:
                f = _fit(c["tpl"], toks)
                if f is not None and f > score:
                    best, score = c, f
            if best is None or score < sim:
                best = {"tpl": list(toks), "items": [], "variants": Counter(), "via": Counter()}
                groups[(toks[0], toks[1], len(toks))].append(best)
                clusters.append(best)
            else:
                best["tpl"] = [a if a == b else WILD for a, b in zip(best["tpl"], toks)]
            best["variants"][" ".join(toks)] += 1
            if not any(h is best for h in hit):  # count each template once per command
                hit.append(best)
                best["items"].append((r["ts"], r["text"]))
                best["via"][r["kind"]] += 1
        per_event.append(hit)

    def usable(c):
        # `grep -i <*>` or `git show <*>` is just a tool taking an argument; a habit has more shape:
        # at least 4 tokens, half of them fixed, and 2+ fixed words that are not flags
        tpl = c["tpl"]
        const = [t for t in tpl if t != WILD]
        words = [t for t in const if not _is_flag(t)]
        return (len(c["variants"]) >= 2 and WILD in tpl and len(tpl) >= 4
                and len(const) >= len(tpl) / 2 and len(words) >= 2)

    keep = [c for c in clusters if usable(c)]
    together = defaultdict(Counter)  # template -> templates seen in the same command
    for hit in per_event:
        live = [c for c in hit if usable(c)]
        for c in live:
            for o in live:
                if o is not c:
                    together[id(c)][" ".join(o["tpl"])] += 1
    out = []
    for c in keep:
        tpl = c["tpl"]
        key = " ".join(tpl)
        slots = []
        for i, t in enumerate(tpl):
            if t == WILD:
                vals = Counter(v.split()[i] for v in c["variants"].elements())
                slots.append([redact(v, 60) for v, _ in vals.most_common(4)])
        sec = 3 + len(key) / 6 + 4 * len(slots)  # typing + working out the varying values
        out.append(_pattern(
            "template", key, c["items"], len(c["items"]) * sec / 60,
            [f"{redact(v, 160)}  (x{n})" for v, n in c["variants"].most_common(3)],
            via=c["via"].most_common(1)[0][0], variants=len(c["variants"]), slots=slots,
            often_with=[k for k, n in together[id(c)].most_common(3) if n >= 2]))
    return out


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


def _sessions(rows, gap=SESSION_GAP):
    """Split a time-sorted stream into sessions (by explicit session id or idle gap)."""
    sess, cur, last, last_id = [], [], None, object()
    for r in rows:
        sid = r["session"]
        if cur and ((sid or None) != (last_id or None) or r["ts"] - last > gap):
            sess.append(cur)
            cur = []
        cur.append(r)
        last, last_id = r["ts"], sid
    if cur:
        sess.append(cur)
    return sess


def _sequences(rows, trivial, gap=SESSION_GAP):
    grams = defaultdict(list)
    for kind in ("shell", "agent_shell"):
        stream = [r for r in rows if r["kind"] == kind]
        for s in _sessions(stream, gap):
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
    gap = cfg["session_gap_min"] * 60
    patterns = (_commands(shellish, trivial) + _sequences(shellish, trivial, gap) +
                _templates(shellish, trivial, cfg, read_aliases(cfg)) +
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
