# Changelog

## Unreleased

### Review Safety Follow-ups (#32-#37)

- Transaction creation now verifies pack source and machine-local/committed configuration before
  and after staging, so copy/setup races abort without overwriting external changes. Profiles reject
  template keys before opening a transaction.
- Resolver closure merges now require semantically identical metadata except for `side`, and all
  generated metadata paths and filenames use portable Unicode/case-insensitive collision rules.
- URL downloads reject non-public literal/resolved addresses and redirects by default and pin each
  connection to an approved DNS result. The machine-local-only `url_allow_private_networks` boolean
  enables intentional internal access; composed URL candidates require every origin to opt in.
- Transaction swaps now park and verify the exact live source before installation, overlay edits and
  builds use no-follow directory descriptors, and URL cancellation/deadlines shut down live sockets.
  Replaced pack sources remain in completed transaction state until normal retention cleanup, and
  build overlay destinations are pinned and traversed descriptor-relative.
- NAT64/special-use address policy, Windows device aliases, and portable metadata filename collision
  checks now cover direct URL additions and repository validation.

### Overlay Safety (#32, #33)

- Validation, builds, and interactive content editing now share one no-follow overlay policy.
- Content overlays reject every symlink and every Packwiz-owned `pack.toml`, `index.toml`, or
  `*.pw.toml` path. Builds preserve the previous distribution when an invalid overlay is found.

### Template Resolver Integrity (#34, #35)

- Template roots are resolved in isolated temporary Packwiz projects so each root's complete
  dependency closure receives its requested side before closures are merged.
- Shared dependencies union sides across roots. Metadata path, provider identity, and JAR filename
  collisions fail closed when selected candidates cannot all be represented independently.

### Transactional Profiles (#36)

- Multiple profiles now apply in declared order under one project lock and one staged transaction.
- Install, refresh, interruption, or concurrent-source-change failures leave the real pack source
  unchanged; existing provider identities retain the union of their current and requested sides.

### Breaking: Public CLI Boundary (#30)

- `huroshiki` is the interactive interface, `packctl` is the noninteractive interface, and Just is
  development-only.
- All management recipes, `packctl use`, `packctl current`, and the public `MODPACK` environment
  context were removed. Pass an explicit project ID instead. For example, replace
  `just build-for demo` with `packctl build demo` and context-based `just update` with
  `packctl update demo --build`.
- Replace `just template-projects` with `packctl list-templates`, `just serve-for demo 8080` with
  `packctl serve demo --port 8080`, and `just publish-for demo` with `packctl publish demo`.
- The package no longer includes Just or a global `_just` completion. It installs dedicated
  `_packctl` and `_huroshiki` completions; generic Just completion remains untouched.
- See the README's complete Just migration table for every removed recipe.

### Breaking: Legacy Templates Removed (#31)

- `packctl migrate-template`, legacy `source/pack.toml` and `source/*.pw.toml` fallback, and the
  associated migration API were removed.
- Before upgrading a legacy template, manually write `id`, `display_name`, `enabled`, `minecraft`,
  `loader`, `reference_loader_version`, and `mods` into the committed `template.yaml`; remove
  `templates/<id>/source`; then run `packctl validate-template <id>`.
- Any `templates/<id>/source` entry, including a symlink, is now a hard validation error.
- `template.local.yaml` cannot supply missing required fields or define `mods`; MOD entries remain
  committed structural data in `template.yaml`.

### Breaking: Machine-Local Configuration Schema (#37)

- `template.local.yaml` now permits only a positive integer `url_max_jar_size_bytes` and the boolean
  `url_allow_private_networks`. Template
  identity, display name, enablement, Minecraft/loader settings, reference loader version, MODs, and
  unknown keys fail closed in validation, runtime loading, and the TUI.
- `pack.local.yaml` now permits only `distribution.rsync_target`,
  `minecraft_server.{ssh_host,stack_dir,service}`, `url_max_jar_size_bytes`, and
  `url_allow_private_networks`. Identity, Packwiz
  semantic settings, and unknown top-level or nested keys are rejected. The private-network key is
  machine-local-only and is rejected in either committed manifest.
- Template side/delete operations continue to write committed `template.yaml`. Transaction conflict
  detection covers both committed configuration and allowed local URL policy.
