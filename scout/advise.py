"""The judgment layer: decide what is worth asking the LLM about, ask once, keep only good answers."""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from .config import DATA_DIR
from .mine import norm_cmd

SYSTEM = """You are habit-scout, a frugal, skeptical automation advisor for one developer.
You receive locally-mined usage patterns (shell commands, command sequences, repeated prompts
to an AI coding agent, repeatedly created files) plus a history of what the developer accepted,
rejected or ignored before. Recommend automations ONLY when they clearly pay off.

Principles:
- Silence is a valid answer. Returning zero recommendations is better than a weak one.
- Value = time saved x frequency, minus the cost of remembering a new command. Tiny savings on
  rare commands are not worth it. Prefer one high-value automation over three marginal ones.
- Prefer the lightest mechanism that works: shell alias/function < small script < VS Code task or
  snippet < Claude slash command (for repeated prompts) < anything bigger.
- Generalize: parameterize the varying parts you can see across the examples.
- Learn from history: never re-propose something resembling a rejected item; if the developer
  ignores or rejects a kind of suggestion, raise your bar for that kind.
- Scripts must be safe and idempotent: `set -euo pipefail` for bash, no destructive actions
  (rm -rf, force push, reset --hard, deleting data) without an explicit confirmation prompt,
  no secrets, no network calls that were not in the original pattern.
- Keep content short. Aliases/functions: just the definition lines. Claude commands: a markdown
  prompt file using $ARGUMENTS for the varying part.
- confidence reflects how sure you are the developer will actually USE it, not that it runs."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fp": {"type": "string", "description": "fingerprint of the pattern addressed"},
                    "title": {"type": "string"},
                    "why": {"type": "string", "description": "1-2 sentences citing the evidence"},
                    "kind": {"type": "string", "enum": ["alias", "function", "script", "vscode_task",
                                                        "vscode_snippet", "claude_command", "git_alias", "other"]},
                    "confidence": {"type": "number"},
                    "minutes_per_week": {"type": "number"},
                    "filename": {"type": "string", "description": "e.g. gpush.sh, aliases.zsh, tailor-resume.md"},
                    "content": {"type": "string"},
                    "install_hint": {"type": "string"},
                },
                "required": ["fp", "title", "why", "kind", "confidence", "minutes_per_week", "filename", "content"],
            },
        },
        "skipped": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"fp": {"type": "string"}, "reason": {"type": "string"}},
                      "required": ["fp", "reason"]},
        },
    },
    "required": ["recommendations"],
}


# ---------------------------------------------------------------- memory of past suggestions

def expire_stale(db, cfg):
    cutoff = int(time.time()) - cfg["ignore_after_days"] * 86400
    db.execute("UPDATE suggestions SET status='ignored', status_ts=? WHERE status='proposed' AND created < ?",
               (int(time.time()), cutoff))


def _words(s):
    return set(re.findall(r"[a-z0-9]{3,}", s.lower()))


def _suppressed(db, cfg, p):
    """True if this pattern (or one very like it) was already handled."""
    rows = db.execute("SELECT fp, pattern_key, status, status_ts FROM suggestions").fetchall()
    now = int(time.time())
    for r in rows:
        same = r["fp"] == p["fp"]
        if not same and p["type"] in ("prompt", "sequence") and r["pattern_key"]:
            a, b = _words(p["key"]), _words(r["pattern_key"])
            same = bool(a | b) and len(a & b) / len(a | b) >= 0.6
        if not same:
            continue
        if r["status"] in ("proposed", "accepted", "rejected"):
            return True
        if r["status"] == "ignored" and now - (r["status_ts"] or 0) < cfg["ignored_cooldown_days"] * 86400:
            return True
        if r["status"] == "passed" and now - (r["status_ts"] or 0) < cfg["passed_cooldown_days"] * 86400:
            return True  # the LLM already judged it not worth it recently
    return False


def existing_aliases(cfg):
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


def _covered_by_alias(p, aliases):
    if p["type"] not in ("command", "sequence"):
        return None
    for name, exp in aliases.items():
        if exp and (p["key"] == exp or p["key"].startswith(exp + " ")):
            return name
    return None


# ---------------------------------------------------------------- the gate

def select_candidates(db, cfg, patterns):
    aliases = existing_aliases(cfg)
    recent = int(time.time()) - cfg["recent_days"] * 86400
    cands, notes = [], Counter()
    for p in patterns:
        if p["type"] == "slash":
            continue
        if p["days"] < cfg["min_days"] or p["count"] < cfg["min_count"]:
            notes["below frequency bar"] += 1
            continue
        if p["minutes"] < cfg["min_minutes"]:
            notes["too little time at stake"] += 1
            continue
        if _covered_by_alias(p, aliases):
            notes["already aliased"] += 1
            continue
        if _suppressed(db, cfg, p):
            notes["already suggested/decided"] += 1
            continue
        # still a live habit? favour patterns seen in the last few days
        p["score"] = p["minutes"] * (1.0 if p["last_seen"] >= recent else 0.5) * min(p["days"], 7) / 7
        cands.append(p)
    cands.sort(key=lambda p: -p["score"])
    # one representative per near-identical key (a command and the sequence it starts often overlap)
    picked = []
    for p in cands:
        steps = frozenset(p["key"].split(" ; "))
        if any(p["key"] in q["key"] or q["key"] in p["key"] or steps == frozenset(q["key"].split(" ; "))
               for q in picked):
            continue
        picked.append(p)
        if len(picked) >= cfg["max_candidates"]:
            break
    return picked, dict(notes)


# ---------------------------------------------------------------- digest + LLM call

def _history_summary(db):
    rows = db.execute("SELECT title, kind, status, reason, minutes_per_week FROM suggestions "
                      "WHERE status NOT IN ('proposed', 'passed') ORDER BY status_ts DESC LIMIT 15").fetchall()
    return [{k: r[k] for k in ("title", "kind", "status", "reason") if r[k]} for r in rows]


def _profile(db, cfg, patterns):
    slash = sorted((p for p in patterns if p["type"] == "slash"), key=lambda p: -p["count"])[:5]
    ws = db.execute("SELECT cwd, COUNT(*) c FROM events WHERE kind IN ('workspace','edit') AND ts >= ? "
                    "GROUP BY cwd ORDER BY c DESC LIMIT 5", (int(time.time()) - cfg["window_days"] * 86400,)).fetchall()
    return {
        "platform": cfg["environment"],
        "active_workspaces": [Path(r["cwd"]).name for r in ws if r["cwd"]],
        "slash_commands_already_used": [f'{p["key"]} (x{p["count"]})' for p in slash],
    }


def build_digest(db, cfg, cands, patterns):
    slim = [{k: p[k] for k in ("fp", "type", "key", "count", "days", "minutes", "examples", "via", "steps", "workspace")
             if k in p} for p in cands]
    digest = {"profile": _profile(db, cfg, patterns), "past_decisions": _history_summary(db), "candidates": slim}
    text = json.dumps(digest, separators=(",", ":"), ensure_ascii=False)
    while len(text) > cfg["digest_char_budget"] and digest["candidates"]:
        digest["candidates"].pop()
        text = json.dumps(digest, separators=(",", ":"), ensure_ascii=False)
    return digest, text


def call_llm(cfg, digest_text):
    prompt = ("Here are today's candidate patterns as JSON. Recommend at most "
              f"{cfg['max_suggestions']} automations (zero is fine).\n\n{digest_text}")
    cmd = [cfg["claude_bin"], "-p", "--model", cfg["model"], "--output-format", "json",
           "--tools", "", "--no-session-persistence", "--strict-mcp-config",
           "--system-prompt", SYSTEM, "--json-schema", json.dumps(OUTPUT_SCHEMA)]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=cfg["llm_timeout_sec"])
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}")
    out = json.loads(proc.stdout)
    data = out.get("structured_output")
    if data is None:
        m = re.search(r"\{.*\}", out.get("result", ""), re.S)
        data = json.loads(m.group(0)) if m else {"recommendations": []}
    usage = out.get("usage") or {}
    meta = {
        "cost_usd": out.get("total_cost_usd"),
        "input_tokens": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": usage.get("output_tokens"),
    }
    return data, meta


# ---------------------------------------------------------------- materialize suggestions

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "suggestion"


def save_suggestions(db, cfg, data, cands):
    by_fp = {p["fp"]: p for p in cands}
    recs = [r for r in data.get("recommendations", [])
            if r.get("fp") in by_fp and r.get("confidence", 0) >= cfg["min_confidence"] and r.get("content")]
    recs.sort(key=lambda r: -(r.get("minutes_per_week", 0) * r.get("confidence", 0)))
    saved = []
    for r in recs[: cfg["max_suggestions"]]:
        p = by_fp[r["fp"]]
        cur = db.execute(
            "INSERT INTO suggestions(fp, pattern_key, created, title, kind, why, confidence, minutes_per_week, "
            "filename, install_hint, baseline_per_day) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (r["fp"], p["key"], int(time.time()), r["title"], r["kind"], r["why"], r["confidence"],
             r.get("minutes_per_week", 0), Path(r["filename"]).name, r.get("install_hint", ""),
             p["count"] / max(1, p["days"])))
        sid = cur.lastrowid
        d = DATA_DIR / "suggestions" / f"{sid:03d}-{_slug(r['title'])}"
        d.mkdir(parents=True, exist_ok=True)
        f = d / Path(r["filename"]).name
        f.write_text(r["content"].rstrip() + "\n")
        if f.suffix in (".sh", ".py", "") and r["kind"] == "script":
            f.chmod(0o755)
        (d / "README.md").write_text(
            f"# {r['title']}\n\n{r['why']}\n\n- kind: {r['kind']}\n- confidence: {r['confidence']}\n"
            f"- est. minutes saved/week: {r.get('minutes_per_week')}\n- pattern: `{p['key']}` "
            f"({p['count']}x over {p['days']} days)\n\n## Install\n{r.get('install_hint') or 'scout accept ' + str(sid)}\n")
        db.execute("UPDATE suggestions SET path=? WHERE id=?", (str(f), sid))
        saved.append(sid)
    db.commit()
    reasons = {s.get("fp"): s.get("reason") for s in data.get("skipped", []) or []}
    addressed = {r["fp"] for r in recs[: cfg["max_suggestions"]]}
    now = int(time.time())
    for fp, p in by_fp.items():
        if fp in addressed:
            continue
        p["llm_skip_reason"] = reasons.get(fp, "not recommended")
        # remember the verdict so the same pattern is not re-sent (and re-billed) tomorrow
        db.execute("INSERT INTO suggestions(fp, pattern_key, created, title, status, status_ts, reason) "
                   "VALUES(?,?,?,?, 'passed', ?, ?)", (fp, p["key"], now, p["key"][:80], now, p["llm_skip_reason"]))
    db.commit()
    return saved
