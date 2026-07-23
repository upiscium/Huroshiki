set shell := ["bash", "-euo", "pipefail", "-c"]
ctl := "python shared/scripts/packctl.py"

default:
    @just --list

# List all managed packs.
packs:
    {{ctl}} list

# Enter a child shell scoped to one MODPACK. Run `exit` to leave it.
use pack:
    {{ctl}} use "{{pack}}"

# Show the currently selected MODPACK.
current:
    {{ctl}} current

# Show the selected pack.
show:
    {{ctl}} show "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}"

# Show an explicitly named pack.
show-for pack:
    {{ctl}} show "{{pack}}"

# Open the huroshiki MODPACK manager.
huroshiki:
    huroshiki

# Open huroshiki directly in an explicitly named MODPACK.
huroshiki-for pack:
    huroshiki --pack "{{pack}}"

# Open huroshiki directly in a template project.
huroshiki-template template:
    huroshiki --template "{{template}}"

# List recoverable deleted projects.
trash-list:
    {{ctl}} trash-list

# Restore one trash entry shown by `just trash-list`.
trash-restore entry:
    {{ctl}} trash-restore "{{entry}}"

# Permanently remove one trash entry.
trash-purge entry:
    {{ctl}} trash-purge "{{entry}}"

# Preview state retention cleanup; pass filters after `--`.
clean-huroshiki-state *args:
    {{ctl}} clean-huroshiki-state {{args}}

# Apply state retention cleanup; pass filters after `--`.
purge-huroshiki-state *args:
    {{ctl}} clean-huroshiki-state --apply {{args}}

# Backward-compatible alias: open the selected MODPACK directly.
tui:
    huroshiki --pack "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}"

# Backward-compatible alias for an explicitly named MODPACK.
tui-for pack:
    huroshiki --pack "{{pack}}"

# Run huroshiki parser and PTY integration tests.
test-huroshiki:
    PYTHONPATH=shared/scripts python -m unittest discover -s tests -v

# Create a new MODPACK Packwiz project.
new pack display_name minecraft loader loader_version:
    {{ctl}} new "{{pack}}" "{{display_name}}" "{{minecraft}}" "{{loader}}" "{{loader_version}}"

# Create a new MOD-list template.
new-template template display_name minecraft loader loader_version:
    {{ctl}} new-template "{{template}}" "{{display_name}}" "{{minecraft}}" "{{loader}}" "{{loader_version}}"

# List template IDs.
template-projects:
    {{ctl}} complete templates

# Validate a template manifest.
validate-template template:
    {{ctl}} validate-template "{{template}}"

# Validate all packs and templates without changing them.
validate:
    {{ctl}} validate

# Validate one pack without changing it.
validate-for pack:
    {{ctl}} validate-for "{{pack}}"

# Convert a legacy Packwiz template project into the MOD-list manifest format.
migrate-template template:
    {{ctl}} migrate-template "{{template}}"

# Select a provider, then let Packwiz search, choose and install.
# Skip provider selection with mr:kubejs, cf:238222, or a project URL.
add query side:
    {{ctl}} add "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}" "{{query}}" "{{side}}"

# Add to an explicitly named pack.
add-for pack query side:
    {{ctl}} add "{{pack}}" "{{query}}" "{{side}}"

# Remove a mod from the selected pack.
# Use the .pw.toml filename without the .pw.toml suffix.
remove mod:
    {{ctl}} remove "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}" "{{mod}}"

# Remove a mod from an explicitly named pack.
remove-for pack mod:
    {{ctl}} remove "{{pack}}" "{{mod}}"

# Change side in the selected pack.
side metadata_file side:
    {{ctl}} side "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}" "{{metadata_file}}" "{{side}}"

# Change side in an explicitly named pack.
side-for pack metadata_file side:
    {{ctl}} side "{{pack}}" "{{metadata_file}}" "{{side}}"

# Apply profiles to the selected pack.
profile *names:
    {{ctl}} profile "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}" {{names}}

# Apply profiles to an explicitly named pack.
profile-for pack *names:
    {{ctl}} profile "{{pack}}" {{names}}

# Update the selected pack, then rebuild.
update:
    {{ctl}} update "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}"
    {{ctl}} build "$MODPACK"

# Update an explicitly named pack.
update-for pack:
    {{ctl}} update "{{pack}}"
    {{ctl}} build "{{pack}}"

# Build the selected pack.
build:
    {{ctl}} build "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}"

# Build an explicitly named pack.
build-for pack:
    {{ctl}} build "{{pack}}"

# Build all enabled packs.
build-all:
    {{ctl}} build-all

# Serve the selected pack locally.
serve port="8080": build
    python -m http.server "{{port}}" --directory "packs/${MODPACK:?No active MODPACK. Run: just use <MODPACK>}/dist"

# Serve an explicitly named pack.
serve-for pack port="8080": (build-for pack)
    python -m http.server "{{port}}" --directory "packs/{{pack}}/dist"

# Build and rsync the selected pack.
deploy: build
    {{ctl}} deploy "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}"

# Build and rsync an explicitly named pack.
deploy-for pack: (build-for pack)
    {{ctl}} deploy "{{pack}}"

# Build and preview rsync changes for the selected pack.
deploy-dry-run: build
    {{ctl}} deploy-dry-run "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}"

# Build and preview rsync changes for an explicitly named pack.
deploy-dry-run-for pack: (build-for pack)
    {{ctl}} deploy-dry-run "{{pack}}"

# Restart the selected pack's Compose service.
restart:
    {{ctl}} restart "${MODPACK:?No active MODPACK. Run: just use <MODPACK>}"

# Restart an explicitly named pack's Compose service.
restart-for pack:
    {{ctl}} restart "{{pack}}"

# Build, upload and restart the selected pack.
publish:
    just deploy
    just restart

# Build, upload and restart an explicitly named pack.
publish-for pack:
    just deploy-for "{{pack}}"
    just restart-for "{{pack}}"

# Build and deploy all enabled packs without restarting servers.
deploy-all:
    {{ctl}} deploy-all
