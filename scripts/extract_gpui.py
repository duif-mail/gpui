#!/usr/bin/env python3
"""Build a standalone Cargo workspace containing GPUI and all in-tree dependencies.

Reads *all* dependency sections (including target-, dev- and build-dependencies),
so every selected platform is represented. Requires Python 3.11+ and tomlkit.
"""

from __future__ import annotations

import argparse
import copy
import shutil
import subprocess
import sys
import tomllib
from collections import deque
from collections.abc import Mapping
from pathlib import Path

import tomlkit

DEPS = ("dependencies", "dev-dependencies", "build-dependencies")
ROOTS = ("gpui", "gpui_platform")


def iter_deps(manifest: dict):
    for name in DEPS:
        yield from manifest.get(name, {}).items()
    for target in manifest.get("target", {}).values():
        for name in DEPS:
            yield from target.get(name, {}).items()


def inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("."))
    parser.add_argument("--root", action="append", dest="roots", default=[])
    args = parser.parse_args()
    source = args.upstream.resolve()
    output = args.output.resolve()
    if not (source / "Cargo.toml").is_file():
        parser.error("--upstream must point to the root of the Zed repository")
    if inside(output, source) or inside(source, output):
        parser.error("Source and destination must be separate directories")

    orig = tomlkit.parse((source / "Cargo.toml").read_text(encoding="utf-8"))
    ws_deps = orig["workspace"]["dependencies"]
    members: dict[str, Path] = {}
    indexed: dict[Path, tuple[str, dict]] = {}
    for member in orig["workspace"]["members"]:
        path = Path(str(member))
        manifest_path = source / path / "Cargo.toml"
        if not manifest_path.is_file():
            raise RuntimeError(f"Missing upstream workspace manifest: {manifest_path}")
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        name = data["package"]["name"]
        if name in members:
            raise RuntimeError(f"Duplicate crate name {name}")
        members[name] = path
        indexed[path] = (name, data)

    def resolve_path(path: Path) -> str:
        resolved = path.resolve()
        if not inside(resolved, source):
            raise RuntimeError(f"Dependency points outside upstream: {resolved}")
        rel = resolved.relative_to(source)
        if rel not in indexed:
            manifest = resolved / "Cargo.toml"
            if not manifest.is_file():
                raise RuntimeError(f"Path dependency has no Cargo.toml: {resolved}")
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            name = data["package"]["name"]
            indexed[rel] = (name, data)
            members[name] = rel
        return indexed[rel][0]

    roots = args.roots or ROOTS
    todo = deque(roots)
    selected: set[str] = set()
    used_ws_deps: set[str] = set()
    used_dep_names: set[str] = set()

    while todo:
        name = todo.popleft()
        if name in selected:
            continue
        if name not in members:
            raise RuntimeError(f"Required internal crate was not found: {name}")
        selected.add(name)
        rel = members[name]
        data = indexed[rel][1]
        for dep_name, spec in iter_deps(data):
            used_dep_names.add(dep_name)
            if not isinstance(spec, dict):
                continue
            if spec.get("workspace"):
                if dep_name not in ws_deps:
                    raise RuntimeError(f"{name} references unknown workspace dependency {dep_name}")
                used_ws_deps.add(dep_name)
                ws_spec = ws_deps[dep_name]
                if isinstance(ws_spec, Mapping) and "path" in ws_spec:
                    todo.append(resolve_path(source / str(ws_spec["path"])))
            elif "path" in spec:
                todo.append(resolve_path(source / rel / str(spec["path"])))

    # Cargo manifest generated with only selected members and workspace dependencies.
    generated = tomlkit.document()
    workspace = tomlkit.table()
    workspace.add("resolver", orig["workspace"].get("resolver", "2"))
    workspace.add("members", sorted(str(members[name]) for name in selected))
    workspace.add("default-members", [str(members[name]) for name in roots])
    if "package" in orig["workspace"]:
        workspace.add("package", copy.deepcopy(orig["workspace"]["package"]))
    dep_table = tomlkit.table()
    for name in sorted(used_ws_deps):
        dep_table.add(name, copy.deepcopy(ws_deps[name]))
    workspace.add("dependencies", dep_table)
    if "lints" in orig["workspace"]:
        workspace.add("lints", copy.deepcopy(orig["workspace"]["lints"]))
    generated.add("workspace", workspace)

    # Retain only patches with a direct name match. Transitive upstream patches
    # may need explicit allowlisting in extra-patches.toml; the CI build reveals this.
    if "patch" in orig:
        patches = tomlkit.table()
        for registry, table in orig["patch"].items():
            selected_patches = tomlkit.table()
            for name, spec in table.items():
                if name not in used_dep_names:
                    continue
                if isinstance(spec, Mapping) and "path" in spec:
                    # Local patches outside the closure are Zed-specific.
                    continue
                selected_patches.add(name, copy.deepcopy(spec))
            if selected_patches:
                patches.add(registry, selected_patches)
        if patches:
            generated.add("patch", patches)

    # Replace only generated content; never delete the workflow or extractor.
    (output / "crates").mkdir(parents=True, exist_ok=True)
    for crate in (output / "crates").iterdir():
        if crate.is_dir():
            shutil.rmtree(crate)
    # Account for selected directories outside crates/ (e.g. tooling/perf).
    stale = output / "generated-other-paths.txt"
    if stale.exists():
        for line in stale.read_text().splitlines():
            rel = Path(line)
            if rel.parts and rel.parts[0] not in {"crates", ".github", "scripts"}:
                dest = output / rel
                if dest.is_dir():
                    shutil.rmtree(dest)
        stale.unlink()
    extra_written = []
    for name in sorted(selected):
        rel = members[name]
        target = output / rel
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / rel, target, symlinks=False)
        if rel.parts[0] != "crates":
            extra_written.append(str(rel))
    if extra_written:
        (output / "generated-other-paths.txt").write_text("\n".join(extra_written) + "\n")
    (output / "SELECTED-CRATES").write_text("\n".join(
        sorted(str(members[name]) for name in selected)
    ) + "\n", encoding="utf-8")

    # Some build scripts use paths outside the crate itself. Additional shared
    # data paths can be listed one per line in scripts/extra-paths.txt.
    path_file = output / "scripts" / "extra-paths.txt"
    if path_file.is_file():
        for raw in path_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            rel = Path(line)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"Invalid extra path: {rel}")
            src, dst = source / rel, output / rel
            if not src.exists():
                raise RuntimeError(f"Missing extra path in upstream: {rel}")
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    (output / "Cargo.toml").write_text(tomlkit.dumps(generated), encoding="utf-8")
    for filename in ("LICENSE-APACHE", "LICENSE-GPL", "rust-toolchain.toml"):
        src = source / filename
        if src.exists():
            shutil.copy2(src, output / filename)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    (output / "UPSTREAM").write_text(commit + "\n", encoding="utf-8")
    bad_licenses = []
    for name in selected:
        license_str = indexed[members[name]][1]["package"].get("license", "")
        if any(x in license_str for x in ("GPL-", "AGPL-", "SSPL-")):
            bad_licenses.append((name, license_str))
    if bad_licenses:
        print(f"Warning: review copyleft in-tree crates: {bad_licenses}", file=sys.stderr)
    print(f"Extracted {len(selected)} crates from {commit[:12]}: {', '.join(sorted(selected))}")

if __name__ == "__main__":
    main()
