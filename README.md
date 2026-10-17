# GPUI (standalone mirror)

An automatically maintained, slimmed-down mirror of [GPUI](https://github.com/zed-industries/zed/tree/main/crates/gpui), the GPU-accelerated UI framework that powers the [Zed](https://zed.dev) editor.

GPUI lives inside the large [`zed-industries/zed`](https://github.com/zed-industries/zed) monorepo. This repository extracts only what GPUI needs to build, so you can depend on it, browse it, or vendor it without cloning the whole editor.

> **Unofficial.** This project is not affiliated with or endorsed by Zed Industries. All code is copied unmodified from upstream. For GPUI itself, issues and contributions belong in the [Zed repository](https://github.com/zed-industries/zed).

## What's in here

The mirror contains `gpui`, `gpui_platform`, and every in-tree crate they depend on (directly or transitively), laid out as a standalone Cargo workspace:

| Path | Description |
| --- | --- |
| `crates/` | The extracted GPUI crates and their in-tree dependencies |
| `Cargo.toml` | Generated workspace manifest, containing only the workspace dependencies and patches the selected crates use |
| `rust-toolchain.toml` | Rust toolchain pinned by upstream |
| `UPSTREAM` | Full SHA of the Zed commit this mirror was generated from |
| `SELECTED-CRATES` | The list of crates currently included |
| `LICENSE-APACHE`, `LICENSE-GPL` | License files copied from upstream |

Everything else in Zed (the editor, language servers, collaboration, extensions, assets, and so on) is left out.

## Using it

Add GPUI as a git dependency, and pin it to a specific revision so updates are opt-in:

```toml
[dependencies]
gpui = { git = "https://github.com/duif-mail/gpui", rev = "<mirror-commit-sha>" }
gpui_platform = { git = "https://github.com/duif-mail/gpui", rev = "<mirror-commit-sha>" }
```

Each mirror commit is titled `Sync GPUI to Zed <short-sha>`, and the `UPSTREAM` file at that commit records exactly which Zed revision it corresponds to.

Because GPUI is under active development upstream and has no stability guarantees, expect breaking changes between syncs.

### Building from source

```sh
cargo check -p gpui -p gpui_platform
```

Use the Rust toolchain specified in `rust-toolchain.toml` (rustup picks it up automatically).

On Linux you'll need the usual windowing, font and graphics development packages:

```sh
sudo apt-get install -y pkg-config libasound2-dev libfontconfig1-dev \
  libwayland-dev libxkbcommon-dev libx11-dev libxcursor-dev \
  libxi-dev libxrandr-dev libxinerama-dev libgl1-mesa-dev libegl1-mesa-dev
```

macOS and Windows need no extra system packages beyond the standard platform toolchains.

## How the sync works

A GitHub Actions workflow ([`sync-gpui.yml`](.github/workflows/sync-gpui.yml)) runs every six hours, and can also be started manually.

1. **Prepare.** Shallow-clones the latest Zed `main` into a temporary directory.
2. **Extract.** [`scripts/extract_gpui.py`](scripts/extract_gpui.py) starts from the root crates and walks the dependency graph, including target-specific, dev and build dependencies, so every platform backend is represented. It copies the crates it finds and writes a new workspace `Cargo.toml` from the workspace dependencies they use. Upstream `[patch]` entries are kept only when a selected crate needs them.
3. **Diff.** If the generated tree is identical to what's already on `main`, the run stops here.
4. **Validate.** The proposed mirror is built on Linux, Windows and macOS (`cargo metadata` and `cargo check -p gpui -p gpui_platform`).
5. **Publish.** The change is committed to `main` as `Sync GPUI to Zed <sha>`.
6. **Notify.** A summary is posted to a Discord webhook: crates added or removed, diff size, and the upstream commits that touched the mirrored crates. This step requires the `DISCORD_WEBHOOK_URL` Actions secret.

If the platform checks fail, the update is still published, but the notification is flagged as a warning. Check the workflow logs before updating.

A separate workflow, [`validate-gpui.yml`](.github/workflows/validate-gpui.yml), can be run manually to re-run the cross-platform build checks against the current `main`.

### Running the extractor locally

Requires Python 3.11+ and `tomlkit`.

```sh
pip install tomlkit==0.13.3
git clone --depth 1 https://github.com/zed-industries/zed.git ../zed
python scripts/extract_gpui.py --upstream ../zed --output .
```

Useful options:

- `--root <crate>` (repeatable) overrides the default root crates (`gpui`, `gpui_platform`).
- `scripts/extra-paths.txt` lists additional upstream paths to copy (one per line, relative to the Zed root) for build scripts or `include_*!` macros that reach outside their crate. Only add entries after a build reports a missing resource.

The extractor replaces generated content (`crates/`, `Cargo.toml`, `UPSTREAM`, `SELECTED-CRATES`) but never touches `.github/` or `scripts/`.

## Licensing

The mirrored code keeps its upstream licenses. GPUI is licensed under the Apache License 2.0 (see `LICENSE-APACHE`). Each crate declares its own license in its `Cargo.toml`, so check the crates you depend on. The extractor prints a warning whenever a selected in-tree crate carries a GPL, AGPL or SSPL license.
