"""Configuration: defaults overridable by ~/.habit-scout/config.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("HABIT_SCOUT_HOME", HOME / ".habit-scout"))

DEFAULTS = {
    # --- mining / wisdom thresholds ---
    "window_days": 14,        # look-back window for pattern mining
    "min_days": 3,            # a pattern must show up on this many distinct days
    "min_count": 4,           # ...and at least this many times
    "min_minutes": 2.0,       # ...and plausibly cost this many minutes in the window
    "max_candidates": 8,      # patterns sent to the LLM per run
    "max_suggestions": 3,     # suggestions kept per run
    "min_confidence": 0.6,    # LLM confidence floor
    "max_pending": 5,         # skip the LLM while this many suggestions await a decision
    "ignore_after_days": 14,  # unanswered suggestion -> 'ignored'
    "retention_days": 60,     # raw events older than this are pruned
    # --- LLM ---
    "claude_bin": "claude",
    "model": "sonnet",
    "digest_char_budget": 12000,  # ~3k tokens of input, hard cap
    "llm_timeout_sec": 600,
    "notify": True,
    "run_after_hour": 18,     # launchd checks hourly; the daily run happens at/after this hour
    # --- sources ---
    "sources": {
        "zsh_history": str(HOME / ".zsh_history"),
        "shell_log": str(DATA_DIR / "shell.log"),
        "vscode_user": str(HOME / "Library/Application Support/Code/User"),
        "claude_projects": str(HOME / ".claude/projects"),
    },
    "alias_files": [
        str(HOME / ".zshrc"),
        str(HOME / ".zsh_aliases"),
        str(HOME / ".aliases"),
        str(DATA_DIR / "aliases.zsh"),
    ],
    # single commands never worth automating on their own
    "trivial_commands": [
        "ls", "ll", "la", "cd", "pwd", "clear", "exit", "history", "cat", "less",
        "man", "which", "echo", "code", "claude", "open", "z", "j", "fg", "bg",
        "top", "htop", "vim", "nvim", "nano", "source", "whoami", "date",
    ],
    "ignore_path_parts": ["node_modules", ".git", "dist", "build", ".next", "__pycache__"],
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    user = DATA_DIR / "config.json"
    if user.exists():
        override = json.loads(user.read_text())
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg
