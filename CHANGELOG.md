# Changelog

## Unreleased

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

- `template.local.yaml` now permits only a positive integer `url_max_jar_size_bytes`. Template
  identity, display name, enablement, Minecraft/loader settings, reference loader version, MODs, and
  unknown keys fail closed in validation, runtime loading, and the TUI.
- `pack.local.yaml` now permits only `distribution.rsync_target`,
  `minecraft_server.{ssh_host,stack_dir,service}`, and `url_max_jar_size_bytes`. Identity, Packwiz
  semantic settings, and unknown top-level or nested keys are rejected.
- Template side/delete operations continue to write committed `template.yaml`. Template transaction
  conflict detection now covers both committed configuration and the allowed local URL limit.
