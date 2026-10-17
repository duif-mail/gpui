#!/usr/bin/env python3
"""Render the title and body of a pull request that moves a downstream pin of this mirror.

Compares the mirror commit the downstream repository pins today (--old) with the new
one (--new). The comparison uses the mirror's own history, so it covers every sync
since the pin and not only the latest one. A partial Zed clone (--upstream) is
optional and only adds the list of upstream commits. Standard library only.
"""
from __future__ import annotations

import argparse
import subprocess
import tomllib
from pathlib import Path

from report_sync import git

ZED = "https://github.com/zed-industries/zed"
MAX_BODY = 60_000  # GitHub rejects bodies over 65,536 characters
MAX_UPSTREAM_COMMITS = 40
MAX_DEPENDENCY_LINES = 25
MAX_SYNC_COMMITS = 100
SYNC_SUBJECT = "Sync GPUI to Zed"

# Files the mirror itself owns. They are not GPUI, so they don't count towards the size of an update.
MIRROR_DIRS = {".github", "scripts"}
MIRROR_FILES = {"README.md", ".gitignore", "UPSTREAM", "SELECTED-CRATES", "generated-other-paths.txt"}


def is_mirror_file(path: str) -> bool:
    return path in MIRROR_FILES or path.split("/", 1)[0] in MIRROR_DIRS


def is_commit(repo: Path, sha: str) -> bool:
    if not sha:
        return False
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True)
    return proc.returncode == 0


def show(repo: Path, ref: str, path: str) -> str:
    return git(repo, "show", f"{ref}:{path}", check=False)


def code(text: str, limit: int = 100) -> str:
    """Untrusted text as an inline code span: no mentions, no issue links, no markup."""
    text = text.replace("`", "'").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"`{text}`"


def short(sha: str) -> str:
    return sha[:10]


def describe(spec: object) -> str:
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        if "version" in spec:
            return str(spec["version"])
        if "git" in spec:
            ref = spec.get("rev") or spec.get("tag") or spec.get("branch") or "HEAD"
            return f"git {str(ref)[:12]}"
        if "path" in spec:
            return "path"
    return "?"


def workspace_dependencies(text: str) -> dict:
    return tomllib.loads(text).get("workspace", {}).get("dependencies", {}) if text else {}


def toolchain_channel(text: str) -> str:
    return str(tomllib.loads(text).get("toolchain", {}).get("channel", "")) if text else ""


def dependency_changes(old: dict, new: dict) -> list[str]:
    lines = []
    for name in sorted(old.keys() | new.keys()):
        if name not in old:
            lines.append(f"- `{name}` added ({code(describe(new[name]))})")
        elif name not in new:
            lines.append(f"- `{name}` removed")
        elif old[name] != new[name]:
            before, after = describe(old[name]), describe(new[name])
            detail = f"{code(before)} → {code(after)}" if before != after else "features or options changed"
            lines.append(f"- `{name}`: {detail}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror", type=Path, required=True, help="full-history checkout of this repository")
    parser.add_argument("--upstream", type=Path, help="optional (partial) clone of Zed")
    parser.add_argument("--old", default="", help="mirror commit pinned downstream today")
    parser.add_argument("--new", required=True, help="mirror commit to pin")
    parser.add_argument("--repository", required=True, help="owner/name of this repository")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--unvalidated", action="store_true",
                        help="the cross-platform checks were not run for this PR (manual, forced run)")
    parser.add_argument("--body-out", type=Path, required=True)
    parser.add_argument("--title-out", type=Path, required=True)
    args = parser.parse_args()
    mirror, old, new, repo = args.mirror.resolve(), args.old, args.new, args.repository

    new_up = show(mirror, new, "UPSTREAM")
    if not new_up:
        raise SystemExit(f"{new} has no UPSTREAM file")
    old_known = is_commit(mirror, old) and bool(show(mirror, old, "UPSTREAM"))
    old_up = show(mirror, old, "UPSTREAM") if old_known else ""

    def crates(ref: str) -> set[str]:
        return {line.strip() for line in show(mirror, ref, "SELECTED-CRATES").splitlines() if line.strip()}

    new_crates = crates(new)
    old_crates = crates(old) if old_known else set()

    # Commits between the two pins. The bot's sync commits hold only the extracted GPUI
    # files; any other commit is a change to the mirror's own tooling.
    range_commits: list[tuple[str, str]] = []
    if old_known:
        log = git(mirror, "log", "--format=%H%x09%s", f"--max-count={MAX_SYNC_COMMITS}", f"{old}..{new}", check=False)
        for line in log.splitlines():
            sha, _, subject = line.partition("\t")
            range_commits.append((sha, subject))
    syncs = [(sha, subject) for sha, subject in range_commits if subject.startswith(SYNC_SUBJECT)]
    tooling_commits = len(range_commits) - len(syncs)

    base = f"https://github.com/{repo}"
    zed_link = f"{ZED}/compare/{old_up}...{new_up}" if old_up else f"{ZED}/commit/{new_up}"
    title = f"Bump GPUI mirror to Zed {short(new_up)}"

    out: list[str] = ["## What and why", ""]
    if old_known:
        if len(syncs) == 1:
            mirror_line = f"- [Mirror commit]({base}/commit/{syncs[0][0]}): the GPUI changes in this update"
        else:
            mirror_line = (f"- [Mirror diff]({base}/compare/{old}...{new}): the combined diff of {len(syncs)} syncs, "
                           "each linked below"
                           + (", including changes to the mirror's own tooling" if tooling_commits else ""))
        out += [
            f"Moves the GPUI pin from [`{short(old)}`]({base}/commit/{old}) (Zed `{short(old_up)}`) "
            f"to [`{short(new)}`]({base}/commit/{new}) (Zed `{short(new_up)}`).",
            "",
            mirror_line,
            f"- [Zed diff]({zed_link}): the same range in upstream, including everything the mirror leaves out",
        ]
    else:
        out += [
            f"Pins GPUI at [`{short(new)}`]({base}/commit/{new}) (Zed `{short(new_up)}`).",
            "",
            (f"The previous pin `{short(old)}` is not part of this mirror's history, so no diff is shown."
             if old else "There is no previous pin to compare with."),
            f"- [Zed commit]({zed_link})",
        ]

    # Summary table
    old_channel = toolchain_channel(show(mirror, old, "rust-toolchain.toml")) if old_known else ""
    new_channel = toolchain_channel(show(mirror, new, "rust-toolchain.toml"))
    rows = [
        ("Mirror commit", f"`{short(old)}`" if old_known else "—", f"`{short(new)}`"),
        ("Zed commit", f"`{short(old_up)}`" if old_known else "—", f"`{short(new_up)}`"),
        ("Crates", str(len(old_crates)) if old_known else "—", str(len(new_crates))),
    ]
    if new_channel:
        rows.append(("Rust toolchain", f"`{old_channel}`" if old_channel else "—", f"`{new_channel}`"))
    out += ["", "| | Before | After |", "| --- | --- | --- |"]
    out += [f"| {name} | {before} | {after} |" for name, before, after in rows]

    out += ["", "## How it was checked", ""]
    run = f" ([run]({args.run_url}))" if args.run_url else ""
    if args.unvalidated:
        out.append(
            "- The mirror's cross-platform checks were **not re-run** for this PR, which was started manually. "
            f"Check this commit's sync run in the [mirror's Actions]({base}/actions) before merging."
        )
    else:
        out.append(f"- Mirror CI ran `cargo check -p gpui -p gpui_platform` on Linux, Windows and macOS against this commit{run}.")
    out.append("- Not checked there: this repository's own build and tests with the new pin. The checks on this PR cover that.")
    if old_known and new_channel != old_channel:
        out.append(f"- **The Rust toolchain changed** (`{old_channel or 'unset'}` → `{new_channel or 'unset'}`). Check local setups and CI images.")

    changes: list[str] = []
    if old_known:
        added, removed = sorted(new_crates - old_crates), sorted(old_crates - new_crates)
        if added:
            changes += ["### Crates added", ""] + [f"- `{item}`" for item in added] + [""]
        if removed:
            changes += ["### Crates removed", ""] + [f"- `{item}`" for item in removed] + [""]

        deps = dependency_changes(
            workspace_dependencies(show(mirror, old, "Cargo.toml")),
            workspace_dependencies(show(mirror, new, "Cargo.toml")),
        )
        if deps:
            changes += [f"### Dependency changes ({len(deps)})", "", "Workspace dependencies GPUI pulls in. Version bumps here are the usual cause of build failures.", ""]
            changes += deps[:MAX_DEPENDENCY_LINES]
            if len(deps) > MAX_DEPENDENCY_LINES:
                changes.append(f"- … and {len(deps) - MAX_DEPENDENCY_LINES} more in the mirror diff")
            changes.append("")

        numstat = git(mirror, "diff", "--numstat", "--no-renames", old, new, check=False)
        files, total_added, total_removed = [], 0, 0
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            a, d, name = parts
            if is_mirror_file(name):
                continue
            a_n, d_n = (int(a) if a.isdigit() else 0), (int(d) if d.isdigit() else 0)
            total_added += a_n
            total_removed += d_n
            files.append((a_n + d_n, a_n, d_n, name))
        files.sort(reverse=True)
        changes += [f"### Size of the change", "", f"{len(files)} files changed, +{total_added:,} / -{total_removed:,} lines. Only GPUI's own files are counted, not the mirror's tooling.", ""]
        if files:
            changes += ["Largest files:", ""]
            changes += [f"- `{name}` (+{a_n}/-{d_n})" for _, a_n, d_n, name in files[:10]]
            changes.append("")

        if len(syncs) > 1:
            truncated = len(range_commits) == MAX_SYNC_COMMITS
            changes += [f"### Mirror syncs included ({len(syncs)}{'+' if truncated else ''})", "",
                        "Syncs that landed since the current pin; they are bundled into this one PR. Each commit holds only the GPUI changes of that sync.", ""]
            for sha, subject in syncs[:10]:
                changes.append(f"- [`{short(sha)}`]({base}/commit/{sha}) {code(subject)}")
            if len(syncs) > 10:
                changes.append(f"- … and {len(syncs) - 10} more")
            changes.append("")

        upstream = args.upstream.resolve() if args.upstream else None
        paths = sorted(old_crates | new_crates)
        if upstream and old_up and paths and is_commit(upstream, old_up) and is_commit(upstream, new_up):
            ancestor = subprocess.run(
                ["git", "-C", str(upstream), "merge-base", "--is-ancestor", old_up, new_up], capture_output=True
            ).returncode == 0
            if ancestor and old_up != new_up:
                log = git(upstream, "log", "--no-merges", f"--max-count={MAX_UPSTREAM_COMMITS + 1}",
                          "--format=%H%x09%s", f"{old_up}..{new_up}", "--", *paths, check=False)
                commits = log.splitlines()
                if commits:
                    changes += ["### Upstream commits that touched these crates", ""]
                    for line in commits[:MAX_UPSTREAM_COMMITS]:
                        sha, _, subject = line.partition("\t")
                        changes.append(f"- [`{short(sha)}`]({ZED}/commit/{sha}) {code(subject)}")
                    if len(commits) > MAX_UPSTREAM_COMMITS:
                        changes.append(f"- … and more; see the [Zed diff]({zed_link})")
                    changes.append("")
        elif old_up and old_up != new_up:
            changes += ["### Upstream commits", "", f"The Zed history for this range was not available to the workflow. See the [Zed diff]({zed_link}).", ""]
    if changes:
        out += ["", "## What changed", ""] + changes

    out += [
        "",
        "---",
        "",
        "This PR is opened and kept up to date by the mirror's sync workflow. Its branch is force-updated on every sync until the PR is merged or closed, so don't push commits to it."
        + (f" [Workflow run]({args.run_url})." if args.run_url else ""),
    ]

    body = "\n".join(out) + "\n"
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY].rsplit("\n", 1)[0] + "\n\n… (truncated; see the linked diffs)\n"
    args.body_out.write_text(body, encoding="utf-8")
    args.title_out.write_text(title + "\n", encoding="utf-8")
    print(f"Rendered '{title}' ({len(body):,} characters)")


if __name__ == "__main__":
    main()
