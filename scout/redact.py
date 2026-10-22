"""Scrub secrets before anything leaves the machine."""
from __future__ import annotations

import re

_RULES = [
    # KEY=value / --token value / -p value style
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASS(?:WORD)?|PWD|AUTH|CRED)[A-Z0-9_]*)=\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)(--?(?:token|password|passwd|secret|api-?key|auth)[= ])\S+"), r"\1<redacted>"),
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+"), r"\1<redacted>"),
    # creds embedded in URLs
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
    # well-known token shapes
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"), "<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "<redacted-jwt>"),
    # long opaque blobs
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "<redacted-blob>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
]


def redact(text: str, limit: int = 300) -> str:
    for rx, repl in _RULES:
        text = rx.sub(repl, text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
