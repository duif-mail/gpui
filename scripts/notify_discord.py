#!/usr/bin/env python3
"""Post a compact changelog or validation failure to a Discord webhook.

Uses the Python standard library only. No secret is ever printed in logs.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def shorten(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 20] + "\n… (see full diff)"


def render(report: dict, repository: str, run_url: str, commit: str, status: str) -> str:
    if status == "failed":
        old, new = report.get("previous", ""), report.get("current", "")
        upstream_link = (
            f"https://github.com/zed-industries/zed/compare/{old}...{new}"
            if old and new else "https://github.com/zed-industries/zed"
        )
        return (f"❌ **GPUI mirror sync failed — no update was published**\n"
                f"[Workflow logs]({run_url}) · [Upstream comparison]({upstream_link})\n"
                "Fix the failure, then rerun the workflow.")

    old, new = report.get("previous", ""), report["current"]
    short = lambda sha: sha[:10] if sha else "initial"
    upstream_link = (f"https://github.com/zed-industries/zed/compare/{old}...{new}"
                     if old else f"https://github.com/zed-industries/zed/commit/{new}")
    mirror_link = f"https://github.com/{repository}/commit/{commit}" if commit else f"https://github.com/{repository}/commits/main"
    lines = [
        ("⚠️ **GPUI mirror updated — platform checks FAILED**" if status == "warning"
         else "✅ **GPUI mirror automatically updated**"),
        f"Zed `{short(old)}` → `{short(new)}` · **{report['crate_count']} crates**",
        f"**Mirror diff:** {report['file_count']} files, +{report['added']:,} / -{report['removed']:,} lines",
        f"[Mirror commit (full diff)]({mirror_link}) · [Zed upstream diff]({upstream_link}) · [Run]({run_url})",
    ]
    if report.get("new_crates"):
        lines.append("**Added crates:** " + ", ".join("`" + item + "`" for item in report["new_crates"][:8]))
    if report.get("removed_crates"):
        lines.append("**Removed crates:** " + ", ".join("`" + item + "`" for item in report["removed_crates"][:8]))
    if report.get("commits"):
        lines.append("**Relevant upstream commits:**")
        for item in report["commits"][:10]:
            # Strip Discord pings/backticks from untrusted upstream commit titles.
            title = item["title"].replace("@", "＠").replace("`", "'").replace("\n", " ")
            lines.append(f"• [`{item['sha'][:8]}`](https://github.com/zed-industries/zed/commit/{item['sha']}) {shorten(title, 100)}")
        if len(report["commits"]) > 10 or report.get("commits_may_be_truncated"):
            lines.append(f"• … and additional changes in the linked diff")
    elif not report.get("history_complete") and old:
        lines.append("**Upstream history:** too old for local shallow checkout; see the full upstream diff link.")
    if report.get("largest_files"):
        lines.append("**Largest changed files:**")
        for item in report["largest_files"][:5]:
            lines.append(f"• `{item['path']}` (+{item['added']}/-{item['removed']})")
    if status == "warning":
        lines.append("**Warning:** GPUI was published despite failed platform checks. See workflow logs before depending on this revision.")
    # Discord's maximum message content is 2,000 characters. Keep useful links.
    return shorten("\n".join(lines), 1900)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--status", choices=["success", "warning", "failed"], required=True)
    parser.add_argument("--commit", default="")
    args = parser.parse_args()
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        raise SystemExit("Set repository Actions secret DISCORD_WEBHOOK_URL")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        raise SystemExit("GITHUB_REPOSITORY is not set; run this from GitHub Actions")
    report = json.loads(args.report.read_text()) if args.report.exists() else {}
    message = render(
        report,
        repository=repository,
        run_url=f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
        commit=args.commit,
        status=args.status,
    )
    payload = json.dumps({"content": message, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, payload, headers={"Content-Type": "application/json", "User-Agent": "gpui-mirror-sync/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            print(f"Discord notification sent (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        print(f"Discord notification failed (HTTP {e.code})")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
