# This Mac's maintained Topgrade profile

This optional Apple Silicon macOS profile versions the existing journal-protected updater scripts and their declared configuration. It does not replace Homebrew's Rust with PATH tricks, remove historical kegs, disable errors, or claim that a fork can make unavailable vendor services succeed.

The 26 scripts, five TOML files and two npm security patch packages are covered by `manifest.json`. The original transaction/recovery implementations remain intact. The fork updater tracks this fork's main branch, and the built-in Helm step uses the typed `[helm].repository_updater` path to avoid an extra shell owner. The fork is the sole active Topgrade installation; the former Homebrew Topgrade was backed up and uninstalled on this Mac. Binary rollback uses retained managed fork releases, independently of Homebrew. LLVM 23.1.4 remains pinned; historical LLVM HEAD is retained.

## Review and install

Use the existing Homebrew Python 3.15 (the installer requires Python 3.15 or later):

```sh
python3.15 profiles/macos/install.py
python3.15 -m unittest discover -s profiles/macos/tests -v
# After the reviewed fork main has been pushed, build/verify/switch its binary first:
bash profiles/macos/bin/topgrade-rustup-headsync --dry-run
bash profiles/macos/bin/topgrade-rustup-headsync update
python3.15 profiles/macos/install.py --apply
python3.15 profiles/macos/install.py --verify
```

The default command is a **read-only plan**, including on an empty test home. `--apply` is the only installation action. It accepts only fixed destination names and verified source bytes; initial adoption of an existing file requires its recorded preimage hash. Subsequent upgrades require the previously installed bytes. A local edit, unknown existing file, symlink, special file, corrupt source or pending transaction stops deployment.

All changed files are backed up before the first replacement. Data assets install before executable helpers, and configuration installs last. Each individual replacement is atomic. A fsynced journal and lock protect deployment, and caught errors attempt rollback. The installation is recoverable across interruption; it is not one atomic rename across all files.

The installer prints a transaction ID. To restore its previous files:

```sh
python3.15 profiles/macos/install.py --rollback TRANSACTION
```

Rollback checks all destinations and backup hashes before changing anything. It refuses to overwrite later user edits or a newer profile. Newly created files are removed only when their bytes still match that transaction; unrelated files and directories remain. Original file modes and timestamps are restored. Private journals and backups live under `~/.local/state/topgrade-macos-profile/transactions/`; never commit them. `--home /absolute/test/home` is for isolated validation.

This fork owns companion script source, installation and configuration through this profile. The installed `~/.local/bin` entry points are its managed runtime copies, not a second independently maintained script set. Keep these entry points because the declared configuration and transaction scripts call them. Companion profile upgrades require reviewing the fork checkout and explicitly applying this installer; a binary self-update does not silently deploy new scripts or configuration. Existing target file permissions are retained in the manifest rather than made more permissive.

Installing this profile does not build or switch the Topgrade binary itself. The root workflow verifies a fork binary before switching the existing managed launcher, with its separate rollback mechanism. Do not run a second full updater while deployment or a previous update is active.

## Python 3.15 runtime

The owned profile contains 18 operational Python entrypoints, the installer and eight test files. They use only the Python standard library, so the profile has no pip runtime dependencies or additional dependency lockfile. The separate root Python package uses maturin only to build the Rust binary.

Versioned `python3.15` launchers select the runtime explicitly; the SQLite integration retains its explicit Homebrew Python 3.15 ABI target. The four embedded HEAD-sync Python blocks and the Gradle/Preview shell forwarders also select `python3.15`. Owned Zig, Gradle and Docker helper children inherit `sys.executable`. The installer rejects Python versions below 3.15 before accessing deployment targets or creating state. This changes the fork's profile requirements, not the user's global PATH or unrelated tool environments.

The CLI version report continues to report the user's actual default `python3`; it does not hide an unexpected PATH choice. Synthetic HEAD-sync fixtures provide both Python command names using the test interpreter, preserving all 30 recovery and guard cases without invoking production Homebrew.

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

Helm remains an enabled built-in step. The optional updater downloads the entire public Bitnami index through official HTTPS with compression and compares its complete bytes to the regular local index. Only an identical body that also passes native Helm index parsing is reported current, without writing Helm's cache. Special files are opened without blocking and rejected. Changed/absent indexes and other repositories use native Helm, and native failure remains nonzero. TLS verification, official redirect-host checks, bounded downloads, local configuration drift checks and rejected cache symlinks remain active. This addresses the unchanged large-index transfer case; it does not promise that future changed-index transfers will succeed.

## GitHub CLI Skills ownership

The enabled GitHub CLI Skills step uses `[github_cli].skills_updater` to call the versioned `topgrade-gh-skills-verified` helper. It validates installed metadata and registered ownership before updating anything. GitHub-managed real directories are grouped by their parent and selected by directory basename with native `gh skill update --dir ... --all ...`; JSON display aliases are not update arguments.

Skills without GitHub metadata stay with their verified existing owners. NPX records must match the canonical shared installation; additional host copies must match its complete file tree. Cursor mirrors must match the existing fixed native-skill allowlist and vendor contents. Unknown owners, invalid metadata, changed copies or pinned entries stop this step instead of being reported current. This does not claim that NPX or Cursor updated anything: their existing independent updater and coverage checks still run.

Before updates, the helper follows native GH's unpinned reference policy: the latest published release, or the repository default branch when no release exists. Other release lookup failures remain errors. The complete tree for that reference must contain each declared skill directory and SKILL.md; truncated or invalid responses fail closed. After native updates, metadata must match the verified directory tree SHA and original source/path. A nonzero exit, native warning or unknown completion output remains a failure, including warnings followed by “All skills are up to date.”. Native GH writes its own installation records to the shared lock. The helper validates the lock schema before updates, rejects publisher/path/pin collisions and checks that native writes preserve unrelated entries and dismissed choices. Dry runs require the complete lock bytes to stay unchanged. The helper never injects metadata into foreign skills, independently rewrites the shared lock, unpins skills or uses force. Other installations keep the upstream native behavior unless this optional hook is configured.

## Per-repository dependency compatibility

The fork opts into its existing compatible dependency-update path with the root file `.topgrade-dependencies.json` containing exactly `{"allow_incompatible": false}`. The repository still participates in Rolling updates; Cargo uses `cargo update` within its declared requirements instead of `cargo upgrade --incompatible allow --pinned allow`. Other surfaces retain their existing compatible behavior, including action updates.

Repositories without this marker keep their previous configured edge behavior unchanged. The marker can only narrow that behavior; it cannot override CLI `--compatible` to enable incompatible changes. A malformed, duplicate-key, oversized, symlinked or nonregular policy file fails before that repository's update commands execute. The maximum size is 1024 bytes, and the policy is included in dependency backups. Only this fork adds the marker; global project configuration and other repositories are untouched.

## npm reference data

The guard now reads versioned reference data from `~/.local/share/topgrade-macos-profile/npm-security/`. Its two vendor trees live inside `node_modules/` so the existing Rolling dependency discovery prunes them. They are verified guard-owned package data, not development projects for generic npm dependency updates; narrow Git ignore exceptions version only these two trees. Historical `patches/` destinations remain accepted solely so prior deployment journals can still roll back. It retains full before/after npm fingerprints and the original verified patch metadata. Only complete official `undici 6.29.1` and `ip-address 10.7.5` package trees are shipped, including their MIT licenses; the complete npm installation is not vendored. Exact per-package tree verification, full live-tree post-verification, original-directory rollback, declared ranges and the bounded official advisory query remain.

The validation report is a historical October 2026 probe. Its local workspace paths are replaced by neutral placeholders; its commands are evidence, not commands to execute verbatim. It does not assert that future npm releases or all installed applications are vulnerability-free. Unknown npm trees are checked without being overwritten. New guard receipts/backups live in the private profile state directory.

## Acceptance

Synthetic HOME tests cover read-only planning, installation, idempotence, exact rollback, mode-only rollback, drift, symlink/path rejection, interrupted deployment recovery, disk failure and modified patch trees. These tests never run real updates or use the user's live profile.

The real device acceptance result must be recorded separately from source checks and isolated tests. Helm transfers, plugin marketplaces, MAS authorization and CI billing can still fail independently. Preserve their errors and the original environment rather than removing update owners to manufacture a green summary.
