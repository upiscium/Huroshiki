# Changelog

## Unreleased

### Migration

- Added Pack Copy migration for changing a Pack's Minecraft/loader target without
  mutating the source. The complete source is snapshotted, source and destination
  locks are acquired in canonical order, and a resolved target with canonical root
  provenance is exchanged into staging only after its full dependency closures and
  collision checks succeed. Existing targets are never clobbered, operational
  deployment/server settings are not copied, and unresolved roots remain
  `resolution-required` until explicit canonical Remove or Replace choices are made.
- Added Template Copy migration as a preview-first, one-shot target creation flow,
  including explicit root selection, complete closure resolution, warning
  acknowledgements, and atomic publication. Template and Pack origins are resolved
  through an explicit session Authority: the fixed source snapshot, plan digest,
  resolution attempt, and selected conflict options remain authoritative from CLI
  planning through execution; no persistent Template association or content overlay
  is created.
- Both Copy flows are available as dry-run-by-default `packctl` commands and as
  cancellable TUI sessions with deterministic previews, explicit conflict choices,
  warning acknowledgements, atomic no-clobber publication, and retained cleanup
  ownership. Template migration preserves its original precommit diagnostic until
  bounded cleanup completes.

### Publication

- Added side-aware publication manifests with reserved namespace enforcement and
  descriptor-bound source snapshots, then stages their exact files into immutable
  remote generations with bounded SSH transfer and full byte, hash, mode, and path
  verification.
- Added Packwiz semantic verification, atomic `current` activation, and a bounded
  restart phase. Stale targets fail before remote work, activation and restart
  outcomes are reported separately, and an uncertain remote outcome is never
  retried or presented as success.
- Unified CLI and TUI Publish on one previewed, digest-bound Core plan with a shared
  cancellation event and deadline. The TUI retains cleanup ownership and supports
  cancellation and retry, while the former standalone build, deploy, and restart
  commands have been retired in favor of `packctl publish`.

### CLI and TUI

- Added exact MOD artifact selection by Modrinth version ID or CurseForge file ID,
  with dry-run CLI controls and an Installed MOD version browser. Users can keep an
  exact choice automatic, pin or unpin it, return to automatic selection, browse
  compatible Modrinth releases, and see current/new file IDs in Update previews.
- Persisted exact version intent transactionally and enforce it across staged Add,
  Update, Profiles, Template Import, and Copy migration. Locked choices remain hard
  constraints, unlocked choices may advance where the operation permits, and stale,
  drifted, incompatible, orphaned, or contradictory intent fails closed before
  publication.
- Normalized safe multiline Modrinth search descriptions, fixed selected checkbox
  rendering, and made `q` the consistent idle/back or active-cancel key across TUI
  screens while preserving literal input and cleanup-aware navigation.

### Reliability and Transaction Safety

- Added explicit cleanup-pending and publication-uncertain lifecycle handling.
  Failed cleanup, incomplete process termination, stale source or staging identity,
  and uncertain publication retain the transaction diagnostics and canonical lock
  ownership for bounded retry instead of reporting success or deleting recovery
  evidence. Cancellation and deadlines remain shared across snapshot, resolution,
  refresh, publication, and cleanup, with navigation and shutdown waiting for
  named worker completion.
- Bounded rsync/SSH output, deadlines, cancellation, process-group termination, and
  parent/descendant cleanup throughout publication. Packwiz and Packwiz Installer
  failures now retain redacted bounded logs with lifecycle metadata for CLI and TUI
  diagnostics instead of losing the resolver's actionable output.

## 0.2.0-rc.5 - 2026-08-03

### Provider Artifacts

- Fixed valid Packwiz-native `mode = "metadata:curseforge"` artifacts being rejected
  for lacking `download.url`. Huroshiki now delegates this mode to the pinned
  Packwiz Installer, verifies the produced artifact against the original declared
  hash, normalizes the isolated verification metadata side, and continues to
  fail closed for manual-download-only or unverifiable files.
- Fixed Packwiz artifact materialization command construction to invoke the pinned
  Packwiz Installer JAR via explicit class entrypoint:

  ```text
  java -cp <packwiz-installer.jar> link.infra.packwiz.installer.Main
  ```

  This avoids `java -jar` and the upstream `RequiresBootstrap` manifest entrypoint,
  so runtime behavior no longer depends on a bundled bootstrapper and never patches,
  rewrites, or replaces the pinned installer JAR. Manual-download-only failures
  remain scoped to failed
  metadata-mode runs, and installer diagnostics are now surfaced with bounded tails
  plus the failed artifact identity.
- Added a regression in metadata/cross-provider artifact workflows that
  preserves explicit command-line identity under `metadata:curseforge` even when the
  installer JAR manifest reports `RequiresBootstrap`.
- Issue #122 live investigation verified this path against CurseForge project
  `309927` and file `6529130` in a manual live investigation.

- Release metadata verification for this PR is deterministic-only and does not
  rerun that live call.

## 0.2.0-rc.4 - 2026-08-02

### Dependency Equivalence

- Fixed the `v0.2.0-rc.3` regression that rejected verified cross-provider transitive dependency
  collisions in legacy Packs without `.huroshiki-roots.json`. Provenance is now separated into
  `explicit`, `dependency`, and `unknown`; `existing unknown + incoming dependency` preserves the
  existing metadata, unions sides, omits the duplicate incoming metadata/JAR, and does not infer or
  create a root manifest. Ambiguous collisions with an incoming explicit root still fail closed
  before materialization.
- Kept artifact/JAR equivalence verification mandatory through strict declared SHA-256, verified
  materialized SHA-256, or an exact target-loader MOD ID/version set. Canonical candidate provenance
  and existing-state now participate in the evidence binding with metadata, target context, and
  artifact evidence. The shared policy applies to Add, Profiles, Template Import, and Update.

## 0.2.0-rc.3 - 2026-08-02

### Provider Search

- Changed TUI CurseForge Install to Packwiz-native interactive search: labels are display-only, the
  selected root's positive numeric project ID is verified with an isolated root-only probe, and the
  canonical complete dependency closure is then re-resolved by numeric ID and merged.
  `provider_lookup.py` is Modrinth-only, Huroshiki no longer searches the CurseForge API directly,
  and no CurseForge API key is required. Noninteractive CLI, Profile, Template, and migration paths
  require positive numeric CurseForge project IDs and reject names, slugs, and URLs.

### Dependency Equivalence

- Added verified cross-provider dependency equivalence. Modrinth and CurseForge dependencies that
  collide by metadata path or JAR filename collapse only when declared SHA-256 values match, when
  isolated materialization produces the same artifact SHA-256, or when the complete target-loader
  MOD ID/version set matches exactly. The deterministic winner keeps one metadata/JAR record, sides
  are unioned, and root provenance is preserved. Explicit-root pairs, URL-provider candidates,
  version or loader mismatches, differing MOD ID sets, and unverifiable artifacts fail closed; names,
  slugs, display labels, and filenames alone are never equivalence evidence.

### Reliability and Transaction Safety

- Pinned Packwiz Installer v0.5.14 for bounded provider-artifact materialization. Cancellation,
  monotonic deadlines, process-group termination, server-thread cleanup, retained workspace
  ownership, transaction locks, and bounded cleanup retry remain coordinated across Add, Profiles,
  Template creation/import, Update, and migration resolution.
- Moved TUI Update selection, dependency verification, and apply work off the Textual event loop.
  Navigation and shutdown cancel and wait for the named non-daemon worker, retaining transaction and
  lock ownership whenever cleanup integrity cannot be proven.

## 0.2.0-rc.2 - 2026-08-01

### Content Management

- Added the snapshot-based Content overlay core used to safely plan and atomically publish
  multi-file changes across common, client, and server overlays. Plans detect stale content,
  unsafe entries, portable collisions, and effective cross-side conflicts before publication.
  Listing and snapshots stream file digests and full-file UTF-8/NUL classification with bounded
  text probes, while staged writes remain cancellable and post-publication rollback receives a
  separate bounded cleanup budget.
- Added a Pack Content browser and bounded UTF-8 editor for common, client, and server
  overlays, including filtered metadata views, KubeJS script presets, safe create/delete/move
  operations, conflict previews, stale-edit protection, and atomic application through the
  Content transaction core. Save previews preserve the active editor draft when cancelled, and
  recursive browser scans remain cancellable. Binary editing remains unsupported.
- Added bounded local file and directory import for Pack Content, including immutable source
  inspection, overwrite policies, conflict summaries, and transactional preview/apply.
- Added read-only Pack Content path details and clipboard actions with snapshot-based stale-entry
  protection, plus TypeScript and assets/data support for KubeJS creation presets.

### Provider Search

- Added CurseForge project search and strict numeric/project-URL resolution through the isolated
  provider helper. Install results carry canonical numeric project IDs with title, author, and
  description; labels and slugs are never reused as identity. Requests require
  `HUROSHIKI_CURSEFORGE_API_KEY`, keep it in the API header, enforce Minecraft/loader filters,
  bounded responses and redirects, and retain the shared cancellation/deadline process lifecycle.

### Reliability and Transaction Safety

- Added the core snapshot and transaction foundation for copy-based Pack migration. It safely scans
  and streams a detached source copy, holds source and target locks in canonical order, records a
  staged migration plan without rewriting target versions, and provides the internally gated
  publication foundation used by target resolution.
- Added target resolution for staged Pack migrations. Explicit root provenance is committed with the
  Packwiz source, target projects are initialized from scratch, provider roots and complete dependency
  closures are rebuilt for the target Minecraft/loader tuple, and URL roots fail closed when loader or
  Minecraft compatibility is unknown. Resolution reports canonical dependency deltas and collisions,
  then stops at `resolved` or `resolution-required` without authorizing publication. Successful target
  source handoff uses a verified descriptor-relative atomic exchange; unresolved, cancelled, stale, or
  cleanup-uncertain operations retain diagnostic transaction and lock ownership.
- Existing Packs without committed root provenance now enter `resolution-required` with explicit
  metadata candidates instead of failing before migration planning. Selected roots are checked against
  the fixed detached source and committed atomically with the Packwiz ignore rule; no dependency is
  implicitly promoted to a root. Legacy URL metadata requires an explicit canonical ID and is refreshed
  before the provenance source exchange.
- Added explicit Remove and Replace resolution for unresolved Pack migration roots. Requests cover the
  complete unresolved snapshot, reject stale or non-canonical choices before resolver work, and rebuild
  every effective root closure in a fresh attempt workspace. Incomplete attempts preserve formal staging;
  complete attempts stop at `resolved` after verified source exchange and do not independently authorize
  publication.
- Completed the copy-migration publication path from a resolved resolution plan. It issues an opaque,
  digest-bound handoff tied to the plan identity, resolution attempt, fixed source snapshot, resolved
  source, whole target staging tree, and exact warning acknowledgements. Planning safely derives target
  `pack.yaml` from the detached source snapshot, replaces the Pack ID and display name, and does not
  inherit `distribution` or `minecraft_server` operational settings. Publication uses
  `renameat2(RENAME_NOREPLACE)` for atomic no-clobber installation, then revalidates the published
  semantic snapshot. Cleanup failure retains the published target and lock ownership for bounded retry;
  publication does not modify the source Pack.
- Install checkpoint preparation now runs inside the add-operation worker and shares cancellation
  plus one operation-wide absolute deadline across copy, resolution, URL download, merge, rollback,
  interactive PTY polling, cleanup, and navigation. Checkpoints, resolver trees, and failed staged
  sources are atomically handed to retained transaction state instead of recursively deleted by the
  operation; worker-start failure releases only operation ownership while preserving the transaction
  and project lock. Incomplete PTY termination retains cleanup ownership and the project lock until
  bounded navigation or discard cleanup proves group drain and parent reap.

## 0.2.0-rc.1 - 2026-07-30

### Highlights

- Import one or more ordered Templates into an existing Pack as a reviewed, one-shot transaction.
- Preview and apply loader-version-only migrations from either `packctl` or the TUI.
- Search Modrinth canonically from Install and carry verified project IDs into Packwiz resolution.
- Bound Packwiz cancellation, process-group termination, descendant cleanup, and parent reaping.
- Track transaction discard through an explicit, observable, retryable cleanup lifecycle.

### New Features

- Added the Template Import planner, versioned conflict-resolution format, noninteractive CLI, and
  ordered TUI flow. Imports merge complete dependency closures without copying Template content or
  creating a persistent association.
- Added loader version migration under `Settings` -> `Versions` and
  `packctl loader-version <pack> <version|latest|recommended>`. Minecraft version and loader type
  remain fixed, and CLI migration is a dry run unless `--apply` is supplied.
- Added `Settings` -> `Deployment` and the corresponding deployment configuration commands.
- Added `Settings` -> `Client Distribution` for Public Pack URLs and Packwiz Installer commands.
- Added canonical Modrinth search and identity normalization. Modrinth IDs, slugs, and project URLs
  resolve through the provider API; CurseForge automation requires a numeric project ID.
- Added isolated provider lookup for Modrinth identity and search requests.
- Added effective URL policy inspection and local overrides. Public Pack URLs require HTTPS and
  URL-provided JARs use DNS-pinned public-network validation, bounded downloads, and collision-safe
  metadata filenames.
- Propagated requested sides to every member of each Packwiz dependency closure, including unchanged
  installed dependencies shared by multiple roots.

### Reliability and Transaction Safety

- Provider lookup and noninteractive Packwiz commands run in isolated process groups. Cancellation
  escalates through SIGTERM and SIGKILL with bounded waits, verifies group drain and parent reap, and
  rejects orphaned descendants even when the original parent exited successfully.
- Single Packwiz commands have a 120-second cap. Multi-command remove, build, and Template creation
  operations share one 600-second absolute deadline instead of renewing a timeout per command.
- Update preparation uses one cancellation event and operation-wide absolute deadline. Baseline
  refresh, resolver work, source copies, normalization copies, and content snapshots run outside the
  Textual event loop and clean up before navigation.
- Transaction creation snapshots and revalidates Pack source plus committed/local configuration.
  Source publication is atomic and verifies the exact live source before replacement; external
  changes fail closed and retained state preserves recovery evidence.
- Pack discard has explicit `active`, `discarding`, `discarded`, and `failed` states. Cleanup workers
  are non-daemon and bounded; timeout or incomplete cleanup retains the root and project lock for an
  observable retry. Completed trees are handed to normal retained-state cleanup.
- Settings writes pin project directories, validate prospective merged configuration, and publish
  through Linux `renameat2` compare-and-swap without moving an external replacement away.
- Multiple profiles apply in declared order under one project lock and one staged transaction. A
  resolver, refresh, interruption, or concurrent-source failure leaves the real Pack source intact.
- Builds stage both distributions and publish them with an all-or-nothing swap. Validation, builds,
  and basic TUI content editing share a no-follow overlay policy that rejects symlinks, special
  entries, and Packwiz-owned names.
- Resolver closure merges require compatible metadata except for `side`; portable Unicode and
  case-insensitive path/JAR collisions fail closed.
- URL downloads reject non-public literal/resolved addresses and redirects by default, pin each
  connection to an approved DNS result, and stop live sockets on cancellation or deadline. The
  machine-local `url_allow_private_networks` opt-in does not permit other special-use ranges.
- `nix flake check` now includes the complete Python unit suite as a sandboxed release gate.

### CLI and TUI Changes

- `huroshiki --version` and `packctl --version` report the packaged release version.
- Project-specific `packctl` commands use explicit Pack or Template IDs; there is no selected-project
  shell context.
- Pack settings now expose `Deployment`, `Client Distribution`, and `Versions` screens.
- Existing Packs expose `Pack` -> `Apply Template` with ordered selection, conflict resolution,
  staged preview, and atomic apply/discard.
- Install searches Modrinth using canonical IDs. Bare Modrinth input is a search query; use
  `mr:<id-or-slug>` or a Modrinth project URL for exact lookup. CurseForge accepts numeric IDs only.
- Update preparation runs in a background worker with progress, cancellation, and bounded cleanup.
  CLI update is fail-closed unless `--allow-partial` is explicitly supplied; partial success returns
  status 2 and skips `--build`.
- Successful transactions retain replaced sources for normal state cleanup. Use
  `packctl clean-huroshiki-state` to preview retention cleanup and add `--apply` to remove selections.

### Breaking Changes

#### Public CLI boundary

- `huroshiki` is the interactive TUI, `packctl` is the noninteractive CLI, and Just is available only
  for repository development.
- `packctl use`, `packctl current`, the public `MODPACK` environment context, public Just management
  recipes, and packaged global `_just` completion are removed. The package installs `_packctl` and
  `_huroshiki` instead.
- Replace `just build-for demo` with `packctl build demo`, and replace context-based `just update`
  with `packctl update demo --build`. The README contains the full command migration table.

#### Legacy Template format

- `packctl migrate-template`, `templates/<id>/source`, legacy `source/pack.toml` fallback, and legacy
  `source/*.pw.toml` fallback are removed. Any `templates/<id>/source` entry, including a symlink, is
  a validation error.
- Committed `template.yaml` requires `id`, `display_name`, `enabled`, `minecraft`, `loader`,
  `reference_loader_version`, and `mods`. Local configuration cannot supply missing semantic data.

#### Configuration schema

- `pack.local.yaml` permits only `distribution.rsync_target`, `distribution.public_pack_url`,
  `minecraft_server.ssh_host`, `minecraft_server.stack_dir`, `minecraft_server.service`,
  `url_max_jar_size_bytes`, and `url_allow_private_networks`.
- `template.local.yaml` permits only `url_max_jar_size_bytes` and
  `url_allow_private_networks`. The private-network opt-in is prohibited in committed manifests.
- Unknown keys and identity or Packwiz/Template semantic settings in local files fail validation.

#### Explicit project identity

- Every noninteractive project-specific operation requires an explicit Pack or Template ID. Closure
  roots are selected only by canonical provider/project ID; CurseForge operations require a numeric
  project ID and never fall back to display labels or metadata names.

### Migration from v0.1.0

1. Back up the complete managed repository, including ignored local YAML and `.huroshiki` recovery
   state.
2. Check each `templates/<id>/` for a legacy `source` entry before running the new version.
3. Convert each legacy Template to committed manifest data with all required keys: `id`,
   `display_name`, `enabled`, `minecraft`, `loader`, `reference_loader_version`, and `mods`. Extract
   provider IDs and sides from the old source, then remove the legacy `source` entry.
4. Move or remove prohibited keys from `pack.local.yaml` and `template.local.yaml`; keep identity and
   semantic settings in committed manifests.
5. Replace old Just management commands and selected-project context with explicit `packctl` commands.
6. Run `packctl validate` from the managed repository root.
7. Run `packctl validate-for <id>` for every Pack reported by `packctl list`.
8. Run `packctl validate-template <id>` for every Template reported by `packctl list-templates`.
9. Build each enabled Pack with `packctl build <id>` and review the generated client/server output.
10. Open `huroshiki` and verify the Main Menu, each Pack, each Template, Install, Update, Settings,
    basic content-file editing, and retained-state views used by your workflow.

```bash
packctl validate
packctl list
packctl list-templates
packctl validate-for demo
packctl validate-template base
packctl build demo
```

If migration fails, correct the committed manifest or allowed local configuration and rerun
validation. Do not repair a migration by editing the real Packwiz `packs/<id>/source` during an
active operation.
