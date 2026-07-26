# Template-to-pack comparison design

Status: design accepted for a staged implementation; this document does not add synchronization behavior.  
Tracks: [GitHub Issue #18](https://github.com/upiscium/Huroshiki/issues/18)

## Decision summary

- A pack remains an independent Packwiz project. A template is never inherited at build time and is not an authoritative desired state.
- A pack may persist one optional template association and a comparison baseline. Packs without that state behave exactly as they do today.
- Comparison is three-way: the accepted template baseline, the current template, and the current pack. Nothing is applied automatically.
- Only template manifest entries are considered explicit template MODs. Pack-only metadata is unmanaged because current Packwiz metadata cannot reliably distinguish a direct install from a dependency.
- Every proposed operation starts unselected. Applying selected operations uses the existing staged-copy model and is all-or-nothing.
- Creation-time multiple-template composition is implemented independently of this optional comparison model. Automatic dependency cleanup remains outside the first comparison implementation.

## Relationship and persisted state

When a pack is successfully created from a template, it should gain `packs/<pack>/template-comparison.yaml`. An existing pack may create the same state through an explicit **Compare with template** / **Adopt comparison baseline** action. The file is committed pack state, not local configuration:

```yaml
schema_version: 1
template_id: base
baseline:
  template_digest: sha256:<canonical-manifest-digest>
  mods:
    - name: Create
      provider: modrinth
      project_id: LNytGWDc
      side: both
    - name: Private MOD
      provider: url
      project_id: private-mod
      url: https://minecraft.example/mods/private-mod.jar
      side: server
```

`template_id` is a comparison preference and creation provenance, not an ownership claim. The baseline stores the last accepted semantic state per entry; `name` is retained for readable reviews but is not part of identity. The canonical digest covers normalized template compatibility fields and normalized MOD entries and is diagnostic, not a remote revision or authority token.

Keeping this state separate from `pack.yaml` avoids interaction with recursive `pack.local.yaml` overrides and avoids rewriting operational configuration merely to acknowledge a comparison. Missing state is schema version 0 and requires no migration. Unknown schema versions must fail comparison with a clear upgrade error while leaving normal pack actions available.

Only one template may be associated with a pack for this comparison schema. Comparing another template is allowed as a read-only, baseline-free preview, and replacing the association requires confirmation and creates a fresh baseline. This does not limit creation: a pack may be created from an ordered list of templates using the composition rules below; comparison v1 simply does not persist or synchronize that ordered origin list.

## Creation-time composition

Before creating a destination, every selected template is validated against the requested Minecraft version and loader. Input order is preserved and duplicate or empty selections are rejected.

- Exact normalized `(provider, project_id)` identities merge at their first position and union sides; `client + server`, `client + both`, and `server + both` become `both`.
- Names are compared after trimming and Unicode case folding. A normalized name with distinct source identities forms one conflict group, including groups with three or more candidates.
- URL entries use `project_id` as the logical MOD ID. Equal IDs and URLs merge; differing URLs for one logical ID require explicit conflict resolution even if the display names differ.
- Every conflict resolution retains a non-empty candidate subset. Retaining multiple sources requires duplicate-risk acknowledgement and emits a warning. Missing conflicts and unknown, stale, duplicate, or empty candidate selections abort before project creation. Multiple selected candidates are accepted only when each has a distinct final metadata representation; provider/project mismatches, metadata-path collisions, URL MOD ID collisions, and JAR filename collisions require A/B re-selection.
- Resolved candidates keep their original global order. Each non-URL root is resolved in an isolated temporary Packwiz project before destination creation, so its requested root is distinguished from its complete dependency closure even when another root has the same dependency. The root side is applied to its entire closure, closures merge by normalized actual provider/project identity, and shared identities union sides. Exact composed root identity/selector duplicates resolve once. URL roots keep the bounded downloader path and do not gain implicit Packwiz dependencies.
- Resolution remains best-effort per explicit root unless a multi-source conflict cannot preserve every selected candidate. The report records ordered templates, conflict selections and warnings, failures, and the actual identity, metadata path, and filename retained for every successful candidate; it never reports an absent candidate as installed.

## Identity and inputs

A MOD identity is `(provider, project_id)` after provider normalization. For `url`, `project_id` remains the MOD ID; changing `url` is a selector change for that identity. A name change is display-only. Pack file names and resolved file versions are not identities and do not produce diffs, because templates intentionally resolve compatible versions rather than pinning them.

Comparison is permitted only when pack and template Minecraft versions and loader types match, using the same compatibility rule as pack creation. Loader version remains irrelevant. Duplicate pack metadata for one identity, an unsupported provider, a missing provider ID, or an unreadable side is a conflict rather than a guessed match. Metadata with no recoverable identity remains visible as unmanaged pack content.

The three inputs are:

- `B`: entries accepted in `template-comparison.yaml`.
- `T`: the selected template's current normalized manifest.
- `P`: identities, selectors, and sides read from the pack's `source/**/*.pw.toml`.

## Diff model

The comparison produces stable identity-keyed records with `baseline`, `template`, and `pack` values, plus one classification and an optional operation:

| State | Classification | Offered resolution |
| --- | --- | --- |
| In `T`, absent from `B` and `P` | template addition | add, or accept as locally omitted |
| In `T` and `P`, absent from `B` | unbaselined match | accept match; mismatched fields are conflicts |
| In `B` and `T`, absent from `P` | pack-local removal | keep absent by default; re-add only explicitly |
| In `B` and `P`, absent from `T` | template removal | remove, or retain as unmanaged pack content |
| Absent from `B` and `T`, present in `P` | pack-local/unmanaged | preserve; no removal operation |
| Present in all three | field comparison | rules below |

Side and URL selector fields use ordinary three-way rules independently:

- `T == P`: aligned; no operation.
- `T != B` and `P == B`: offer the template change.
- `P != B` and `T == B`: pack-local override; preserve it and offer no operation.
- `T == P != B`: aligned change; allow baseline acknowledgement without a pack mutation.
- `T != B`, `P != B`, and `T != P`: conflict; require **use template**, **keep pack**, or defer.
- With no baseline, a field mismatch is a conflict; it is never resolved in favor of the template automatically.

All changes and conflict resolutions default to deferred. A deferred record keeps its old baseline so it appears again. **Keep pack** advances only that identity/field to `T`, making the difference an acknowledged local override or omission. A successfully selected template operation also advances only its resolved baseline fields. Initial adoption may automatically baseline exact `T == P` matches; additions, omissions, and mismatches require an explicit resolution.

## Explicit MODs and dependencies

An entry in `T` or `B` is explicit template intent. A matching pack entry is compared even if Packwiz originally installed it as a dependency, effectively promoting that identity while it remains in the template.

All other pack metadata is unmanaged. It may be a pack-local direct addition or a transitive dependency; the current repository has no durable evidence to distinguish those cases, so the comparison must not guess. In particular:

- Pack-only metadata is never an automatic or preselected removal candidate.
- Adding an explicit template entry may stage required dependencies, but those dependencies do not enter the baseline.
- Removing a template entry targets only that identity. Orphan dependency cleanup is not part of template comparison.
- The final staged review labels the requested explicit change separately from Packwiz-created dependency changes.

This conservative model preserves both pack-local additions and dependencies. A pack-local removal of a still-present template entry remains absent until the user explicitly chooses to re-add it.

## Application transaction

Comparison is read-only until the user selects resolutions and confirms them. Application should follow these semantics:

1. Capture digests of the real pack source, comparison state, and selected template manifest.
2. Copy the Packwiz source and comparison state under `.huroshiki/transactions/`.
3. Perform selected adds, replacements, removals, and side edits in the copy; run `packwiz refresh` there.
4. Show the actual staged metadata changes, including dependencies, before final confirmation.
5. Before commit, reject the transaction if the real source, comparison state, or template changed since step 1.
6. Replace the real source and comparison state as one logical commit using backups. If either replacement fails, restore both; retain recovery diagnostics if rollback itself fails.

Any selected operation, resolution, refresh, validation, or final replacement failure leaves both the real source and baseline unchanged. Cancel/discard deletes only staged state. The baseline advances only for resolutions included in the successful transaction. Builds, deploys, and ordinary pack edits never trigger comparison or mutate comparison state.

Creation is similar: write comparison state only after the destination pack transaction succeeds. Baseline entries include template identities successfully installed or already present with the accepted side; failed template installs remain unbaselined additions so a later comparison can offer them again.

## Compatibility and schema evolution

- Existing packs and packs created without a template have no comparison file and retain current independent-pack behavior.
- Existing packs are not assigned an origin by inference. A user must select and adopt a template.
- Deleting the comparison file detaches the preference without changing any Packwiz metadata.
- Deleting or disabling the referenced template produces an unavailable-association warning; all ordinary pack operations continue.
- `schema_version` is mandatory once the file exists. Readers normalize and validate before diffing; writers use atomic replacement and preserve no unknown fields for a schema version they do not understand.
- Future schema changes require a pure, explicit migration with fixture tests. No migration may edit `source/` or silently change a baseline decision.

## Staged rollout

1. Add schema parsing/validation, creation provenance, baseline-free read-only comparison, and adoption. No apply action.
2. Add transactional application for additions, side changes, and explicit conflict resolutions, with dependency-aware staged review.
3. Add template-removal and URL-selector replacement operations after rollback and provenance behavior have integration coverage.
4. Evaluate, in a separate design, whether users need orphan dependency cleanup or an opt-in authoritative policy. Neither should be inferred from this optional model; creation-time composition does not imply ongoing inheritance.

Each stage must leave packs with no comparison state untouched and keep comparison unavailable rather than partially enabled when its schema cannot be read.

## Test strategy

- Schema fixtures: missing state, valid v1, unknown version, malformed/duplicate identities, URL entries, unavailable templates, and legacy packs.
- Diff table tests: every presence row above, each three-way side/URL rule, initial adoption, local additions/removals, duplicate pack identities, and deferred versus acknowledged baselines.
- Dependency tests: one explicit add producing dependencies, an already-installed dependency promoted by template intent, and pack-only dependencies never offered for deletion.
- Transaction tests: add/remove/side/URL success; operation and refresh failures; cancellation; source, template, and state concurrency conflicts; commit failure and rollback of both source and state.
- Compatibility tests: build, update, deploy configuration lookup, and ordinary pack editing are identical with absent, present, or unavailable comparison state.
- End-to-end TUI tests: no preselected changes, conflict resolution, actual staged dependency review, final confirmation, and detach/replace-association confirmation.

The complete existing `just test-huroshiki` suite remains the regression gate at every stage.

## Deferred questions

These do not block the optional single-template model:

- Should a retained template removal later be labelable as a known pack-local direct MOD, rather than the conservative `unmanaged` label?
- Should orphan dependency reporting be a separate read-only diagnostic before any cleanup feature is considered?
- Is there demand for a separately designed authoritative mode with policy suitable for non-interactive automation?
