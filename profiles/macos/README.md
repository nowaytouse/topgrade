# This Mac's maintained Topgrade profile

This optional Apple Silicon macOS profile versions the existing journal-protected updater scripts and their declared configuration. It does not replace Homebrew's Rust with PATH tricks, remove historical kegs, disable errors, or claim that a fork can make unavailable vendor services succeed.

The 25 scripts, five TOML files and two npm security patch packages are covered by `manifest.json`. The original transaction/recovery implementations remain intact. The fork updater tracks this fork's main branch, and the built-in Helm step uses the typed `[helm].repository_updater` path to avoid an extra shell owner. Homebrew Topgrade remains pinned as a fallback. LLVM 23.1.4 remains pinned; historical LLVM HEAD is retained.

## Review and install

Use the existing Homebrew Python 3.15 (the installer requires Python 3.11 or later):

```sh
python3 profiles/macos/install.py
python3 -m unittest discover -s profiles/macos/tests -v
# After the reviewed fork main has been pushed, build/verify/switch its binary first:
bash profiles/macos/bin/topgrade-rustup-headsync --dry-run
bash profiles/macos/bin/topgrade-rustup-headsync update
python3 profiles/macos/install.py --apply
python3 profiles/macos/install.py --verify
```

The default command is a **read-only plan**, including on an empty test home. `--apply` is the only installation action. It accepts only fixed destination names and verified source bytes; initial adoption of an existing file requires its recorded preimage hash. Subsequent upgrades require the previously installed bytes. A local edit, unknown existing file, symlink, special file, corrupt source or pending transaction stops deployment.

All changed files are backed up before the first replacement. Data assets install before executable helpers, and configuration installs last. Each individual replacement is atomic. A fsynced journal and lock protect deployment, and caught errors attempt rollback. The installation is recoverable across interruption; it is not one atomic rename across all files.

The installer prints a transaction ID. To restore its previous files:

```sh
python3 profiles/macos/install.py --rollback TRANSACTION
```

Rollback checks all destinations and backup hashes before changing anything. It refuses to overwrite later user edits or a newer profile. Newly created files are removed only when their bytes still match that transaction; unrelated files and directories remain. Original file modes and timestamps are restored. Private journals and backups live under `~/.local/state/topgrade-macos-profile/transactions/`; never commit them. `--home /absolute/test/home` is for isolated validation.

Companion profile upgrades require reviewing the fork checkout and explicitly applying this installer; a binary self-update does not silently deploy new scripts or configuration. Existing target file permissions are retained in the manifest rather than made more permissive.

Installing this profile does not build or switch the Topgrade binary itself. The root workflow verifies a fork binary before switching the existing managed launcher, with its separate rollback mechanism. Do not run a second full updater while deployment or a previous update is active.

## Existing local prerequisites

This is a profile for the already maintained Mac, not a bootstrap of every tool from an empty computer. Keep these machine-owned settings and assets outside this public repository:

- Homebrew, its taps, exact pins, installed formula options, Rustup Nightly, Xcode and macOS permissions.
- `~/.config/project-deps.json` (local repository roots and policy), `~/.agents/.skill-lock.json`, Cursor's bundled skills, Codex/Claude settings, Git and npm configuration, tokens and Keychain credentials.
- Conda environments, MAS account authorization, cloud components and vendor application preferences.
- The SQLite source checkout at `~/Developer/sqlite-git`, generated SQLite runtime/shims, Gradle runtime and caches, and the existing Sparkle CLI at `~/.local/opt/sparkle-cli/sparkle.app/Contents/MacOS/sparkle`.
- Prior journals, backups, package receipts, application state, downloaded archives and logs.

Generated runtime files stay managed by their existing updaters; they are not copied into Git. The SQLite Python updater still verifies/maintains its existing default Topgrade command entry, so this profile intentionally installs the standard `~/.config/topgrade.toml` instead of a parallel unrelated configuration. TOML command paths use HOME; tool paths target Apple Silicon Homebrew's `/opt/homebrew`.

Skills remain updated by their existing owners and independent coverage checks. A literal empty disable list would duplicate owners for HEAD-sync, Skills, MAS, gcloud and Conda. There is no `ignore_failures` setting.

## Upstream maintenance and Helm

Keep `origin` pointing to this fork and `upstream` to `topgrade-rs/topgrade`. Start from a reviewed clean tree, fetch upstream and merge `upstream/main` into the fork; stop and resolve conflicts without reset or force-push. Run Rust fmt/Clippy/tests and the profile tests, then push the reviewed fork branch. Build the new binary with the fork self-updater and explicitly apply/verify the matching companion profile. There is no unattended overwrite of locally edited configuration.

Helm remains an enabled built-in step. The optional updater downloads the entire public Bitnami index through official HTTPS with compression and compares its complete bytes to the regular local index. Only an identical body is reported current, without writing Helm's cache. Changed/absent indexes and other repositories use native Helm, and native failure remains nonzero. TLS verification, official redirect-host checks, bounded downloads, local configuration drift checks and rejected cache symlinks remain active. This addresses the unchanged large-index transfer case; it does not promise that future changed-index transfers will succeed.

## npm reference data

The guard now reads versioned reference data from `~/.local/share/topgrade-macos-profile/npm-security/`. It retains full before/after npm fingerprints and the original verified patch metadata. Only complete official `undici 6.29.1` and `ip-address 10.7.5` package trees are shipped, including their MIT licenses; the complete npm installation is not vendored. Exact per-package tree verification, full live-tree post-verification, original-directory rollback, declared ranges and the bounded official advisory query remain.

The validation report is a historical October 2026 probe. Its local workspace paths are replaced by neutral placeholders; its commands are evidence, not commands to execute verbatim. It does not assert that future npm releases or all installed applications are vulnerability-free. Unknown npm trees are checked without being overwritten. New guard receipts/backups live in the private profile state directory.

## Acceptance

Synthetic HOME tests cover read-only planning, installation, idempotence, exact rollback, mode-only rollback, drift, symlink/path rejection, interrupted deployment recovery, disk failure and modified patch trees. These tests never run real updates or use the user's live profile.

The real device acceptance result must be recorded separately from source checks and isolated tests. Helm transfers, plugin marketplaces, MAS authorization and CI billing can still fail independently. Preserve their errors and the original environment rather than removing update owners to manufacture a green summary.
