# Repository Notes

## Environment and Checks

- Enter the pinned toolchain with `direnv allow` or `nix develop`; the shell adds Just and actionlint for development. The packaged runtime supplies Python with Textual/PyYAML/tomlkit, Packwiz, Java 21, rsync, and SSH, but deliberately excludes Just.
- Run the CI-equivalent checks locally with `nix flake check`, `nix develop --command just test-huroshiki`, `nix develop --command bash -n shared/scripts/huroshiki-launcher.sh`, `nix build .`, and `nix build .#huroshiki`. There is no configured lint, formatter, or typecheck command.
- Run one test with `PYTHONPATH=shared/scripts python -m unittest tests.test_templates.TemplateManifestTest.test_candidate_matching_ignores_loader_version -v`. Bare imports such as `import packctl` require that `PYTHONPATH` outside the Nix-provided `huroshiki` launcher.
- PTY tests are POSIX-only. The suite otherwise uses temporary repositories and mocks Packwiz where needed; it does not require a checked-in pack.

## Code Boundaries

- `shared/scripts/packctl.py` is the CLI and source of truth for project validation, YAML/TOML handling, Packwiz mutations, builds, and deployment.
- `shared/scripts/huroshiki_paths.py` resolves the managed repository as `--root`, then `HUROSHIKI_ROOT`, then the current working directory. Keep this data root separate from the installed Python/CSS source location, and do not change cwd to communicate root state.
- `shared/scripts/huroshiki.py` is only the Textual UI; keep filesystem/process behavior in `huroshiki_core.py`. Interactive Packwiz transport and output interpretation belong in `packwiz_pty.py` and `packwiz_parser.py` respectively.
- `project_locks.py` implements advisory-lock files and owner metadata; `packctl.py` validates project keys, chooses lock paths, and reexports the public lock API.
- `url_artifacts.py` owns self-hosted URL validation, bounded downloads, JAR identity parsing, URL logs, and Packwiz metadata writes; `huroshiki_core.py` reexports that API and coordinates it with transactions and project configuration.
- Profile configuration loading remains in `packctl.py`; `huroshiki_core.apply_profiles` owns transactional profile execution so CLI and future TUI callers share one lock, baseline, refresh, and atomic apply.
- `deploy_support.py` contains the pure rsync change parser/command builder and distribution digest model; deployment configuration, preview execution, confirmation guards, and CLI behavior remain in `packctl.py`.
- A pack lives at `packs/<id>/`: `pack.yaml` is committed configuration, `source/` is the canonical Packwiz project, and `content/common|client|server` overlays distribution content. Ignored `pack.local.yaml` may recursively override only `distribution.rsync_target`, `minecraft_server.ssh_host`, `minecraft_server.stack_dir`, `minecraft_server.service`, and `url_max_jar_size_bytes`; all identity, Packwiz semantic, and unknown keys are invalid.
- A template lives at `templates/<id>/template.yaml` and stores committed identity, Minecraft/loader settings, provider IDs, sides, and a reference loader version. Ignored `template.local.yaml` permits only `url_max_jar_size_bytes`; all other keys are invalid. A template is a creation recipe, not a persistent Packwiz project; resolver projects are temporary. Any `templates/<id>/source` entry, including a symlink, is invalid.

## Generated and Operational State

- Do not edit `packs/*/dist/`; `packctl build <pack>` recreates `dist/client` and `dist/server`, filters `*.pw.toml` by `side`, overlays `common` plus the target side, then runs `packwiz refresh`.
- Every pack metadata file must have `side = "client"`, `"server"`, or `"both"`; builds stop rather than guess when classification is invalid.
- `.huroshiki/` contains ignored transaction copies and PTY logs. TUI additions are staged there and atomically applied only if the real source/template has not changed.
- Use `huroshiki` for interactive management and explicit-ID `packctl` commands for automation. Justfile recipes are development checks only; there is no public `MODPACK` context.
- Do not run `packctl deploy`, `packctl publish`, or `packctl restart` as verification: deployment uses remote `rsync -av --delete`, and restart invokes Docker Compose over SSH using values from the merged pack configuration.
