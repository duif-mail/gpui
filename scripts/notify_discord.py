#!/usr/bin/env python3
"""Send one mirror-diff embed, or a plain-text sync failure, to Discord."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PREVIEW_LIMIT = 2400
TRUNCATION_NOTICE = "... preview truncated; open the mirror commit for the full diff"


def clean(value: object, limit: int = 140, preserve_lines: bool = False) -> str:
    text = str(value).replace("`", "'").replace("\r", "")
    if not preserve_lines:
        text = text.replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def preview_patch(path: Path) -> tuple[str, bool]:
    if not path.is_file():
        return "Diff preview unavailable. Open the mirror commit to inspect it.", False

    lines: list[str] = []
    length = 0
    binary_section = False
    truncated = False
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for original in source:
            line = original.rstrip("\r\n")
            if line.startswith("diff --git "):
                binary_section = False
            elif line.startswith("GIT binary patch"):
                binary_section = True
                line = "[binary patch omitted from preview]"
            elif binary_section:
                continue
            elif line.startswith("Binary files "):
                line = "[binary files differ; patch omitted]"

            # Keep upstream-controlled code from closing the Markdown code fence.
            line = line.replace("```", "''' ")
            width = len(line) + 1
            if length + width > PREVIEW_LIMIT:
                truncated = True
                break
            lines.append(line)
            length += width

    return "\n".join(lines) if lines else "No text diff available.", truncated


def build_payload(
    report: dict,
    repository: str,
    run_url: str,
    patch: Path,
    commit: str,
    status: str,
    pr_url: str = "",
    pr_failed: bool = False,
    test: bool = False,
) -> dict:
    if status == "failed":
        label = "TEST: " if test else ""
        return {
            "content": (
                f"❌ **{label}GPUI mirror sync failed — no update published**\n"
                f"[Workflow logs]({run_url})"
                + ("\nSimulated failure; no sync was attempted." if test else "")
            ),
            "allowed_mentions": {"parse": []},
        }

    old, new = report.get("previous", ""), report.get("current", "")
    mirror_url = (
        f"https://github.com/{repository}/commit/{commit}"
        if commit else f"https://github.com/{repository}/commits/main"
    )
    upstream_url = (
        f"https://github.com/zed-industries/zed/compare/{old}...{new}"
        if old and new else
        f"https://github.com/zed-industries/zed/commit/{new}" if new else
        "https://github.com/zed-industries/zed"
    )
    patch_text, truncated = preview_patch(patch)
    note = "**TEST ONLY — simulated changes; nothing published.**\n" if test else ""
    excerpt = f"```diff\n{patch_text}\n```"
    if truncated:
        excerpt += f"\n**{TRUNCATION_NOTICE}**"

    fields = [
        {
            "name": "Mirror changes",
            "value": (
                f"**{report.get('file_count', 0):,} files**  "
                f"+{report.get('added', 0):,} / -{report.get('removed', 0):,} lines"
            ),
            "inline": True,
        },
        {
            "name": "Upstream revision",
            "value": f"`{old[:10] if old else 'initial'}` -> `{new[:10] if new else 'unknown'}`",
            "inline": True,
        },
        {
            "name": "Crates",
            "value": str(report.get("crate_count", 0)),
            "inline": True,
        },
    ]

    added = report.get("new_crates", [])
    removed = report.get("removed_crates", [])
    if added or removed:
        pieces = []
        if added:
            pieces.append("**Added:** " + ", ".join(f"`{clean(x, 55)}`" for x in added[:6]))
            if len(added) > 6:
                pieces.append(f"... and {len(added) - 6} more added")
        if removed:
            pieces.append("**Removed:** " + ", ".join(f"`{clean(x, 55)}`" for x in removed[:6]))
            if len(removed) > 6:
                pieces.append(f"... and {len(removed) - 6} more removed")
        fields.append({"name": "Crate changes", "value": clean("\n".join(pieces), 450, preserve_lines=True), "inline": False})

    biggest = report.get("largest_files", [])
    if biggest:
        items = [
            f"`{clean(item.get('path', '?'), 70)}` "
            f"(+{item.get('added', 0)}/-{item.get('removed', 0)})"
            for item in biggest[:5]
        ]
        fields.append({"name": "Largest changed files", "value": clean("\n".join(items), 540, preserve_lines=True), "inline": False})

    links = [
        f"[Full mirror diff]({mirror_url})",
        f"[Zed comparison]({upstream_url})",
        f"[Workflow run]({run_url})",
    ]
    if pr_url:
        links.append(f"[Downstream PR]({pr_url})")
    fields.append({"name": "Links", "value": " · ".join(links), "inline": False})
    if pr_failed:
        fields.append({
            "name": "Downstream PR",
            "value": "⚠️ Failed to open or update the downstream PR; check the workflow run.",
            "inline": False,
        })

    embed = {
        "title": "TEST — GPUI mirror diff" if test else "GPUI mirror diff",
        "url": mirror_url,
        "color": 0x5865F2 if test else 0x57F287,
        "description": f"{note}{excerpt}",
        "fields": fields,
        "footer": {"text": "Duif · GPUI mirror automation"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # A single embed, no upstream commit list/embed and no binary payload.
    assert len(embed["description"]) <= 4096
    assert len(fields) <= 25
    assert all(len(f["name"]) <= 256 and len(f["value"]) <= 1024 for f in fields)
    total = (len(embed["title"]) + len(embed["description"]) +
             sum(len(f["name"]) + len(f["value"]) for f in fields) +
             len(embed["footer"]["text"]))
    assert total <= 6000, f"Discord embed too long: {total}"

    print(f"Mirror patch preview: {len(patch_text)} characters, truncated={truncated}", file=sys.stderr)
    print(f"Discord embed text: {total} characters; embeds=1", file=sys.stderr)
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def target_url(base_url: str, thread_id: str, create_thread: bool) -> str:
    parts = urllib.parse.urlsplit(base_url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    if thread_id:
        if not thread_id.isdecimal():
            raise SystemExit("DISCORD_THREAD_ID must be a numeric Discord thread ID")
        query["thread_id"] = thread_id
    if create_thread and "thread_id" in query:
        raise SystemExit("Specify a thread ID OR enable new forum threads, not both")
    query["wait"] = "true"
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--status", choices=["success", "failed"], required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--pr-url", default="")
    parser.add_argument("--pr-failed", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        raise SystemExit("GITHUB_REPOSITORY is missing")
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url and not args.dry_run:
        raise SystemExit("Set the DISCORD_WEBHOOK_URL Actions secret")

    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report.is_file() else {}
    if args.status == "success" and report.get("has_relevant_changes") is False:
        print("No GPUI code diff; skipping Discord notification.", file=sys.stderr)
        if args.dry_run:
            print(json.dumps({"skipped": True, "reason": "No relevant GPUI changes"}))
        return
    run_url = (
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    payload = build_payload(
        report, repository, run_url, args.patch, args.commit, args.status,
        pr_url=args.pr_url, pr_failed=args.pr_failed, test=args.test,
    )

    thread_id = os.environ.get("DISCORD_THREAD_ID", "").strip()
    create_thread = os.environ.get("DISCORD_CREATE_THREAD", "").strip().lower() == "true"
    if create_thread:
        revision = str(report.get("current", ""))[:12] or os.environ.get("GITHUB_RUN_ID", "test")
        payload["thread_name"] = (
            f"TEST GPUI diff {revision}" if args.test else
            f"GPUI sync {revision}" if args.status == "success" else
            f"GPUI sync FAILED {os.environ.get('GITHUB_RUN_ID', '')}"
        )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    request_url = target_url(url, thread_id, create_thread)
    request = urllib.request.Request(
        request_url,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/duif-mail/gpui, 1.0)",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            print(f"Discord notification delivered (HTTP {response.status})")
    except urllib.error.HTTPError as error:
        body = error.read(1200).decode("utf-8", errors="replace")
        # Do not print the webhook URL or token.
        body = body.replace(url, "[REDACTED]")
        token = parts_token(url)
        if token:
            body = body.replace(token, "[REDACTED]")
        raise SystemExit(f"Discord returned HTTP {error.code}: {body}") from None
    except urllib.error.URLError as error:
        raise SystemExit(f"Discord network error: {error.reason}") from None


def parts_token(url: str) -> str:
    return urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]


if __name__ == "__main__":
    main()
