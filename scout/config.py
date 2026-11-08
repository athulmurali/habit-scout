"""Configuration: every tunable lives in DEFAULTS; ~/.habit-scout/config.json overrides any key.

Nested dicts (sources, install) are merged key by key, so an override only needs the keys it changes.
`scout config` prints the effective configuration.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("HABIT_SCOUT_HOME", HOME / ".habit-scout"))
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULTS = {
    # --- schedule ---
    "run_after_hour": 18,         # launchd checks hourly; the daily run happens at/after this hour
    "notify": True,               # macOS notification when new suggestions arrive

    # --- mining / wisdom thresholds ---
    "window_days": 14,            # look-back window for pattern mining
    "session_gap_min": 30,        # idle minutes that split two work sessions (for command chains)
    "min_days": 3,                # a pattern must show up on this many distinct days
    "min_count": 4,               # ...and at least this many times
    "min_minutes": 2.0,           # ...and plausibly cost this many minutes in the window
    "recent_days": 4,             # patterns not seen this recently are scored at half weight
    "template_similarity": 0.5,   # share of tokens two command steps must share to be one template
    "max_candidates": 8,          # patterns sent to the LLM per run
    "max_pending": 5,             # skip the LLM while this many suggestions await a decision

    # --- memory of past decisions ---
    "ignore_after_days": 14,      # unanswered suggestion -> 'ignored'
    "ignored_cooldown_days": 45,  # an ignored suggestion stays quiet this long
    "passed_cooldown_days": 21,   # a pattern the LLM declined is not re-sent for this long
    "retention_days": 60,         # raw events older than this are pruned

    # --- LLM ---
    "claude_bin": "claude",
    "model": "sonnet",
    "max_suggestions": 3,         # suggestions kept per run
    "min_confidence": 0.6,        # LLM confidence floor
    "digest_char_budget": 12000,  # ~3k tokens of input, hard cap
    "llm_timeout_sec": 600,
    "environment": "macOS, zsh, VS Code, Claude Code CLI",  # told to the LLM so it picks fitting tools

    # --- sources (set one to null to disable it) ---
    "sources": {
        "zsh_history": str(HOME / ".zsh_history"),
        "shell_log": str(DATA_DIR / "shell.log"),
        "vscode_user": str(HOME / "Library/Application Support/Code/User"),
        "claude_projects": str(HOME / ".claude/projects"),
    },
    # files scanned for existing aliases, so covered habits are not re-suggested
    "alias_files": [
        str(HOME / ".zshrc"),
        str(HOME / ".zsh_aliases"),
        str(HOME / ".aliases"),
        str(DATA_DIR / "aliases.zsh"),
    ],

    # --- where `scout accept` installs things (aliases/functions always go to ~/.habit-scout/aliases.zsh,
    #     which the zsh hook sources) ---
    "install": {
        "scripts_dir": str(HOME / ".local/bin"),             # must be on PATH (the zsh hook adds ~/.local/bin)
        "claude_commands_dir": str(HOME / ".claude/commands"),
    },

    # single commands never worth automating on their own
    "trivial_commands": [
        "ls", "ll", "la", "cd", "pwd", "clear", "exit", "history", "cat", "less",
        "man", "which", "echo", "code", "claude", "open", "z", "j", "fg", "bg",
        "top", "htop", "vim", "nvim", "nano", "source", "whoami", "date",
    ],
    "ignore_path_parts": ["node_modules", ".git", "dist", "build", ".next", "__pycache__"],
    # different spellings of the same action, rewritten to one form before templates are mined
    "command_synonyms": {
        "git switch -c": "git checkout -b",
        "git switch": "git checkout",
        "git stage": "git add",
        "yarn add": "npm install",
        "pnpm add": "npm install",
        "yarn run": "npm run",
        "pnpm run": "npm run",
    },
    # flags that never change what a command does, ignored when comparing commands
    "noise_flags": ["-q", "--quiet", "--silent", "--no-pager"],
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_FILE.exists():
        override = json.loads(CONFIG_FILE.read_text())
        for k, v in override.items():
            if k not in DEFAULTS:
                print(f"habit-scout: unknown config key {k!r} in {CONFIG_FILE} (ignored)", file=sys.stderr)
                continue
            if isinstance(v, dict) and isinstance(cfg[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    for k in ("sources", "install"):
        cfg[k] = {n: (os.path.expanduser(p) if isinstance(p, str) else p) for n, p in cfg[k].items()}
    cfg["alias_files"] = [os.path.expanduser(p) for p in cfg["alias_files"]]
    return cfg
