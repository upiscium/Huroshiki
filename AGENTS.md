# Repository Notes

## Environment and Checks

- Enter the pinned toolchain with `direnv allow` or `nix develop`; `flake.nix` supplies Python with Textual/PyYAML/tomlkit, Packwiz, Java 21, Just, rsync, and SSH.
- Run the CI-equivalent checks locally with `nix flake check`, `nix develop --command just test-huroshiki`, and `nix develop --command bash -n shared/scripts/huroshiki-launcher.sh`. There is no configured lint, formatter, or typecheck command.
- Run one test with `PYTHONPATH=shared/scripts python -m unittest tests.test_templates.TemplateManifestTest.test_candidate_matching_ignores_loader_version -v`. Bare imports such as `import packctl` require that `PYTHONPATH` outside the Nix-provided `huroshiki` launcher.
- PTY tests are POSIX-only. The suite otherwise uses temporary repositories and mocks Packwiz where needed; it does not require a checked-in pack.

## Code Boundaries

- `shared/scripts/packctl.py` is the CLI and source of truth for project validation, YAML/TOML handling, Packwiz mutations, builds, and deployment.
- `shared/scripts/huroshiki.py` is only the Textual UI; keep filesystem/process behavior in `huroshiki_core.py`. Interactive Packwiz transport and output interpretation belong in `packwiz_pty.py` and `packwiz_parser.py` respectively.
- `project_locks.py` implements advisory-lock files and owner metadata; `packctl.py` validates project keys, chooses lock paths, and reexports the public lock API.
- `url_artifacts.py` owns self-hosted URL validation, bounded downloads, JAR identity parsing, URL logs, and Packwiz metadata writes; `huroshiki_core.py` reexports that API and coordinates it with transactions and project configuration.
- `deploy_support.py` contains the pure rsync change parser/command builder and distribution digest model; deployment configuration, preview execution, confirmation guards, and CLI behavior remain in `packctl.py`.
- A pack lives at `packs/<id>/`: `pack.yaml` is committed configuration, ignored `pack.local.yaml` recursively overrides it, `source/` is the canonical Packwiz project, and `content/common|client|server` overlays distribution content.
- A template lives at `templates/<id>/template.yaml` and stores provider IDs, sides, and a reference loader version. It is a creation recipe, not a persistent Packwiz project; resolver projects are temporary. Legacy template `source/` trees are read only for migration and are not deleted by `just migrate-template <id>`.

## Generated and Operational State

- Do not edit `packs/*/dist/`; `just build-for <pack>` recreates `dist/client` and `dist/server`, filters `*.pw.toml` by `side`, overlays `common` plus the target side, then runs `packwiz refresh`.
- Every pack metadata file must have `side = "client"`, `"server"`, or `"both"`; builds stop rather than guess when classification is invalid.
- `.huroshiki/` contains ignored transaction copies and PTY logs. TUI additions are staged there and atomically applied only if the real source/template has not changed.
- Prefer explicit `*-for <pack>` Just recipes in automation. Recipes without `-for` require the `MODPACK` environment created by the interactive subshell `just use <pack>`.
- Do not run `deploy*`, `publish*`, or `restart*` as verification: deployment uses remote `rsync -av --delete`, and restart invokes Docker Compose over SSH using values from the merged pack configuration.
