"""habit-scout CLI.

  scout run [--dry-run] [--no-llm]             run now (manual runs don't use up the scheduled daily run)
  scout candidates                              show what would be sent to the LLM (free)
  scout list [--all]                            suggestions awaiting your decision
  scout show ID                                 print a suggestion and its file
  scout accept ID [-y]                          install it
  scout reject ID [-r REASON]                   never suggest this again (reason teaches the agent)
  scout status                                  runs, token spend, adoption of accepted items
  scout config                                  print the effective configuration
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import advise, collect, config, mine, store
from .config import CONFIG_FILE, DATA_DIR


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _notify(title, msg):
    script = f'display notification {json.dumps(msg)} with title {json.dumps(title)}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def cmd_run(args, cfg, db):
    today = date.today().isoformat()
    if args.scheduled and datetime.now().hour < cfg["run_after_hour"]:
        return  # launchd wakes us hourly; only do the day's run once the workday is mostly over
    if args.scheduled and not args.force and db.execute(
            "SELECT 1 FROM runs WHERE day=?", (today,)).fetchone():
        _log("scheduled wake: today's run already done, skipping")
        return
    # only the scheduled run claims the day's slot; manual runs are keyed by time so they don't block it
    run_key = today if args.scheduled else f"{today}@{datetime.now():%H:%M}"
    added = collect.collect_all(db, cfg)
    _log(f"collected {added} new events")
    patterns = mine.mine(db, cfg)
    advise.expire_stale(db, cfg)
    cands, notes = advise.select_candidates(db, cfg, patterns)
    _log(f"mined {len(patterns)} patterns -> {len(cands)} candidates; filtered: {notes}")

    pending = db.execute("SELECT COUNT(*) FROM suggestions WHERE status='proposed'").fetchone()[0]
    run = {"day": run_key, "started": int(time.time()), "events_added": added, "candidates": len(cands),
           "llm_called": 0, "suggestions": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "note": ""}
    new_ids = []
    if not cands:
        run["note"] = "nothing new worth automating"
    elif pending >= cfg["max_pending"]:
        run["note"] = f"{pending} suggestions still awaiting your decision; not adding more"
    elif args.no_llm or args.dry_run:
        digest, text = advise.build_digest(db, cfg, cands, patterns)
        run["note"] = f"llm skipped by flag (digest {len(text)} chars)"
        if args.dry_run:
            print(json.dumps(digest, indent=2, ensure_ascii=False))
    else:
        digest, text = advise.build_digest(db, cfg, cands, patterns)
        _log(f"asking {cfg['model']} about {len(digest['candidates'])} candidates ({len(text)} chars)")
        try:
            data, meta = advise.call_llm(cfg, text)
            run.update(meta, llm_called=1)
            new_ids = advise.save_suggestions(db, cfg, data, cands[: len(digest["candidates"])])
            run["suggestions"] = len(new_ids)
        except Exception as e:  # keep the run record; try again tomorrow
            run["note"] = f"llm error: {e}"[:500]
            _log(run["note"])
            if not args.force:
                db.commit()
                return _write_report(db, cfg, run, cands, new_ids)
    if not args.dry_run:
        db.execute("INSERT OR REPLACE INTO runs VALUES(:day,:started,:events_added,:candidates,:llm_called,"
                   ":suggestions,:cost_usd,:input_tokens,:output_tokens,:note)", run)
        db.commit()
    path = _write_report(db, cfg, run, cands, new_ids)
    _log(f"report: {path}")
    if new_ids and cfg["notify"] and not args.dry_run:
        _notify("habit-scout", f"{len(new_ids)} new automation idea(s). Run: scout list")


def _write_report(db, cfg, run, cands, new_ids):
    out = DATA_DIR / "reports"
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# habit-scout — {run['day']}", "",
             f"events added: {run['events_added']} · candidates: {run['candidates']} · "
             f"LLM: {'yes' if run['llm_called'] else 'no'}"
             + (f" (${run['cost_usd'] or 0:.4f}, {run['input_tokens']} in / {run['output_tokens']} out tokens)"
                if run["llm_called"] else ""), ""]
    if run["note"]:
        lines += [f"> {run['note']}", ""]
    if new_ids:
        lines += ["## New suggestions", ""]
        for r in db.execute(f"SELECT * FROM suggestions WHERE id IN ({','.join('?' * len(new_ids))})", new_ids):
            lines += [f"### #{r['id']} {r['title']}  ({r['kind']}, conf {r['confidence']:.2f}, "
                      f"~{r['minutes_per_week']:.0f} min/wk)", r["why"], f"`{r['path']}`", ""]
    passed = [c for c in cands if c.get("llm_skip_reason")]
    if passed:
        lines += ["## Considered but not recommended", ""]
        lines += [f"- `{c['key'][:90]}` ({c['count']}x/{c['days']}d): {c['llm_skip_reason']}" for c in passed]
        lines.append("")
    lines += [f"Respond with `scout accept ID`, `scout reject ID -r \"why\"`, or ignore (expires in {cfg['ignore_after_days']} days)."]
    path = out / f"{run['day'].replace(':', '')}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def cmd_candidates(args, cfg, db):
    collect.collect_all(db, cfg)
    patterns = mine.mine(db, cfg)
    cands, notes = advise.select_candidates(db, cfg, patterns)
    print(f"{len(patterns)} patterns, filtered: {notes}\n")
    for p in cands:
        print(f"[{p['type']:8}] {p['fp']}  {p['count']}x/{p['days']}d  ~{p['minutes']}min  {p['key'][:100]}")
        for e in p["examples"][:2]:
            print(f"           e.g. {e[:120]}")


def _get(db, sid):
    r = db.execute("SELECT * FROM suggestions WHERE id=? AND status != 'passed'", (sid,)).fetchone()
    if not r:
        sys.exit(f"no suggestion #{sid}")
    return r


def cmd_list(args, cfg, db):
    q = "SELECT * FROM suggestions WHERE status != 'passed'" + ("" if args.all else " AND status='proposed'")
    rows = db.execute(q + " ORDER BY id").fetchall()
    if not rows:
        print("nothing pending" if not args.all else "no suggestions yet")
    for r in rows:
        print(f"#{r['id']:<3} [{r['status']:8}] {r['title']}  ({r['kind']}, ~{r['minutes_per_week'] or 0:.0f} min/wk)")


def cmd_show(args, cfg, db):
    r = _get(db, args.id)
    print(f"#{r['id']} {r['title']}\n{r['why']}\nkind={r['kind']} confidence={r['confidence']} status={r['status']}")
    if r["install_hint"]:
        print(f"install: {r['install_hint']}")
    print(f"\n--- {r['path']} ---\n{Path(r['path']).read_text()}")


def _install(r, cfg):
    src = Path(r["path"])
    kind = r["kind"]
    if kind in ("alias", "function"):
        dest = DATA_DIR / "aliases.zsh"
        with dest.open("a") as f:
            f.write(f"\n# habit-scout #{r['id']}: {r['title']}\n{src.read_text()}")
        return f"appended to {dest} (open a new terminal or `source {dest}`)"
    if kind == "script":
        dest = Path(cfg["install"]["scripts_dir"]) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            sys.exit(f"{dest} already exists; not overwriting")
        shutil.copy2(src, dest)
        dest.chmod(0o755)
        return f"installed {dest}"
    if kind == "claude_command":
        dest = Path(cfg["install"]["claude_commands_dir"]) / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            sys.exit(f"{dest} already exists; not overwriting")
        shutil.copy2(src, dest)
        return f"installed {dest} (use it as /{dest.stem} in Claude Code)"
    return f"manual install needed: {r['install_hint'] or src}"


def cmd_accept(args, cfg, db):
    r = _get(db, args.id)
    if not args.yes:
        cmd_show(args, cfg, db)
        if input("\ninstall this? [y/N] ").strip().lower() != "y":
            return print("cancelled")
    print(_install(r, cfg))
    db.execute("UPDATE suggestions SET status='accepted', status_ts=? WHERE id=?", (int(time.time()), r["id"]))
    db.commit()


def cmd_reject(args, cfg, db):
    r = _get(db, args.id)
    db.execute("UPDATE suggestions SET status='rejected', status_ts=?, reason=? WHERE id=?",
               (int(time.time()), args.reason, r["id"]))
    db.commit()
    print(f"rejected #{r['id']}; it won't come back" + (" and the reason will inform future picks" if args.reason else ""))


def cmd_status(args, cfg, db):
    print("recent runs:")
    for r in db.execute("SELECT * FROM runs ORDER BY day DESC LIMIT 10"):
        llm = f"LLM ${r['cost_usd'] or 0:.4f} {r['input_tokens']}/{r['output_tokens']} tok" if r["llm_called"] else "no LLM"
        print(f"  {r['day']}  +{r['events_added']} ev  {r['candidates']} cand  {r['suggestions']} sugg  {llm}  {r['note'] or ''}")
    tot = db.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*), SUM(llm_called) FROM runs").fetchone()
    print(f"total: {tot[1]} runs, {tot[2] or 0} LLM calls, ${tot[0]:.4f}")
    counts = dict(db.execute("SELECT status, COUNT(*) FROM suggestions GROUP BY status").fetchall())
    print(f"suggestions: {counts}")
    # adoption: did the habit shrink after accepting the automation?
    pats = {r["fp"]: r for r in db.execute("SELECT fp, count, days FROM patterns")}
    for r in db.execute("SELECT * FROM suggestions WHERE status='accepted'"):
        p = pats.get(r["fp"])
        now_rate = (p["count"] / max(1, p["days"])) if p else 0
        print(f"  #{r['id']} {r['title']}: manual rate {r['baseline_per_day']:.1f}/day -> {now_rate:.1f}/day")


def cmd_config(args, cfg, db):
    src = CONFIG_FILE if CONFIG_FILE.exists() else f"defaults only (create {CONFIG_FILE} to override)"
    print(f"# {src}")
    print(json.dumps(cfg, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scout", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print the LLM digest, call nothing, save nothing")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("candidates")
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    for name in ("show", "accept", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        if name == "accept":
            p.add_argument("-y", "--yes", action="store_true")
        if name == "reject":
            p.add_argument("-r", "--reason", default="")
    sub.add_parser("status")
    sub.add_parser("config")
    args = ap.parse_args(argv)
    cfg = config.load()
    db = store.connect()
    globals()[f"cmd_{args.cmd}"](args, cfg, db)


if __name__ == "__main__":
    main()
