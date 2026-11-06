"""Git co-change (FR-07): which files change together, with support and confidence."""
from __future__ import annotations

import subprocess
from collections import Counter
from itertools import permutations

MAX_FILES_PER_COMMIT = 30  # bulk imports and mass renames say nothing about coupling
MIN_SUPPORT = 3
MAX_COMMITS = 2000


def commits(root: str) -> list[list[str]]:
    res = subprocess.run(["git", "-C", root, "log", f"-n{MAX_COMMITS}", "--no-merges", "--format=%x01", "--name-only"],
                         capture_output=True, text=True, check=True)
    groups = []
    for chunk in res.stdout.split("\x01"):
        files = sorted({l.strip() for l in chunk.splitlines() if l.strip()})
        if files:
            groups.append(files)
    return groups


def compute(groups: list[list[str]]) -> list[tuple]:
    touched: Counter = Counter()
    pairs: Counter = Counter()
    for files in groups:
        if len(files) > MAX_FILES_PER_COMMIT:
            continue
        touched.update(files)
        pairs.update(permutations(files, 2))
    return sorted((a, b, n, round(n / touched[a], 4)) for (a, b), n in pairs.items() if n >= MIN_SUPPORT)


def rebuild(conn, root: str) -> dict:
    rows = compute(commits(root))
    conn.execute("DELETE FROM cochange")
    conn.executemany("INSERT INTO cochange(file_a, file_b, support, confidence) VALUES (?,?,?,?)", rows)
    return {"pairs": len(rows)}


def head(root: str) -> str:
    res = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else ""
