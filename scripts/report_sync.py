#!/usr/bin/env python3
"""Create a Discord-friendly, machine-readable GPUI mirror change summary.

Run after extraction and `git add -A`, but before committing. The diff is the
actual generated mirror diff, not the much larger upstream Zed diff.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def read_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mirror, upstream = args.mirror.resolve(), args.upstream.resolve()
    previous = args.previous.read_text().strip() if args.previous.is_file() else ""
    current = (mirror / "UPSTREAM").read_text().strip()

    original = git(mirror, "show", "HEAD:SELECTED-CRATES", check=False)
    old_crates = set(original.splitlines()) if original else set()
    new_crates = read_lines(mirror / "SELECTED-CRATES")
    paths = sorted(old_crates | new_crates)
    # Include global manifest/toolchain commits only when the generated copy changed.
    changed_root = git(mirror, "diff", "--cached", "--name-only", "--", "Cargo.toml", "rust-toolchain.toml")
    paths.extend(changed_root.splitlines())

    numstat = git(mirror, "diff", "--cached", "--numstat", "--no-renames")
    files = []
    total_added = total_deleted = 0
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, name = parts
        added, deleted = (int(a) if a.isdigit() else 0), (int(d) if d.isdigit() else 0)
        total_added += added
        total_deleted += deleted
        files.append({"path": name, "added": added, "removed": deleted})
    files.sort(key=lambda item: item["added"] + item["removed"], reverse=True)

    # Shallow git history may not include the previous revision. The full
    # comparison URL is always provided even when the local commit list is incomplete.
    old_available = bool(previous) and bool(git(upstream, "cat-file", "-t", previous, check=False))
    is_ancestor = old_available and subprocess.run(
        ["git", "-C", str(upstream), "merge-base", "--is-ancestor", previous, current],
        capture_output=True,
    ).returncode == 0
    commits: list[dict[str, str]] = []
    if is_ancestor and previous != current and paths:
        log = git(upstream, "log", "--no-merges", "--max-count=60", "--format=%H%x09%s", f"{previous}..{current}", "--", *paths)
        for line in log.splitlines():
            sha, _, title = line.partition("\t")
            commits.append({"sha": sha, "title": title.strip()})

    report = {
        "previous": previous,
        "current": current,
        "history_complete": bool(is_ancestor),
        "new_crates": sorted(new_crates - old_crates),
        "removed_crates": sorted(old_crates - new_crates),
        "crate_count": len(new_crates),
        "file_count": len(files),
        "added": total_added,
        "removed": total_deleted,
        "largest_files": files[:10],
        "commits": commits,
        "commits_may_be_truncated": len(commits) == 60,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Generated report: {len(files)} files, +{total_added}/-{total_deleted}, {len(commits)} relevant commits")


if __name__ == "__main__":
    main()
