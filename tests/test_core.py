import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ["HABIT_SCOUT_HOME"] = tempfile.mkdtemp()

from scout import collect, mine, store  # noqa: E402
from scout.redact import redact  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_generalizes_variable_parts(self):
        self.assertEqual(mine.norm_cmd('git commit -m "fix nav"'), "git commit -m <str>")
        self.assertEqual(mine.norm_cmd("cd ~/workspaces/foo && npm i"), "cd <path> && npm i")
        self.assertEqual(mine.norm_cmd("git show 3f2a9c1d"), "git show <hash>")
        self.assertEqual(mine.norm_cmd("node scripts/build.js 3"), "node <file> <n>")


class RedactTest(unittest.TestCase):
    def test_scrubs_secrets(self):
        out = redact("OPENAI_API_KEY=sk-abcdefghijklmnopqrstu curl https://u:p@host --token xyz")
        self.assertNotIn("sk-abc", out)
        self.assertNotIn("u:p@", out)
        self.assertNotIn("xyz", out)


class ZshHistoryTest(unittest.TestCase):
    def test_multiline_and_metafied(self):
        db = store.connect()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            # "é" = c3 a9 -> metafied as c3 83 89
            f.write(b": 2000000000:0;echo a \\\nb\n: 2000000001:0;echo caf\xc3\x83\x89\n")
        rows = list(collect.zsh_history(db, f.name, 0))
        self.assertEqual([r[4] for r in rows], ["echo a \nb", "echo café"])


class SequenceTest(unittest.TestCase):
    def test_subsequence_absorbed_by_longer_chain(self):
        rows, t = [], 1_000_000
        for day in range(4):
            for cmd in ("npm run lint", "npm test", "git push"):
                t += 10
                rows.append({"ts": t + day * 86400, "kind": "shell", "text": cmd, "session": None})
            t += 86400
        keys = {p["key"] for p in mine._sequences(rows, set())}
        self.assertIn("npm run lint ; npm test ; git push", keys)
        self.assertNotIn("npm run lint ; npm test", keys)


if __name__ == "__main__":
    unittest.main()
