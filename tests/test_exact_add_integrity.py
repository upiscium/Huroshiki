"""Release-audit regressions for #214 and #215; all Packs are disposable."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib
import unittest
import zipfile

import dependency_equivalence as equivalence
from unittest.mock import patch

import huroshiki_core as core
import tomlkit
from tests import test_add_transaction as add_fixture
from tests import test_mod_version_selection as exact_fixture


def authoritative_roots(source: Path):
    scan = core.scan_pack_migration_source(source, checkpoint=lambda: None)
    return core.extract_pack_migration_roots(
        source, expected_identity=scan.root_identity,
        expected_snapshot_digest=scan.snapshot_digest, checkpoint=lambda: None,
    )


def refresh_index(source: Path) -> None:
    entries = []
    for p in sorted(source.glob('mods/*.pw.toml')):
        entries.append({'file': str(p.relative_to(source)), 'metafile': True,
                        'hash': hashlib.sha256(p.read_bytes()).hexdigest()})
    index = 'hash-format = "sha256"\n'
    for e in entries:
        index += '\n[[files]]\n' + ''.join(
            f'{k} = {json.dumps(v)}\n' for k, v in e.items()
        )
    (source / 'index.toml').write_text(index)
    pack = tomlkit.parse((source / 'pack.toml').read_text())
    pack['index'] = {'file': 'index.toml', 'hash-format': 'sha256',
                     'hash': hashlib.sha256(index.encode()).hexdigest()}
    (source / 'pack.toml').write_text(tomlkit.dumps(pack))


class InstalledExactReaddIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.f = add_fixture.AddTransactionTest('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.source = self.f.source
        (self.source / 'mods/root.pw.toml').write_text(
            add_fixture.metadata('Root', add_fixture.MODRINTH_IDS['root']))
        (self.source / 'mods/dependency.pw.toml').write_text(
            add_fixture.metadata('Dependency', add_fixture.MODRINTH_IDS['dependency'])
            .replace('version = "v1"', 'version = "Exact001"'))
        (self.source / '.packwizignore').write_text(
            '/.huroshiki-roots.json\n/.huroshiki-version-overrides.json\n')
        core.write_pack_root_manifest(self.source, (
            core.PackRootRecord('modrinth', add_fixture.MODRINTH_IDS['root'], 'both'),))
        refresh_index(self.source)

    def add(self, side='client'):
        return core.add_mod_transactionally(
            self.f.key, 'modrinth', 'dependency', side, artifact_id='Exact001')

    def test_subset_readd_records_installed_side_and_remains_authoritative(self):
        before = {p.name: p.read_bytes() for p in self.source.glob('mods/*.pw.toml')}
        with patch.object(core, 'resolve_mod_closure') as automatic, patch.object(
            core, 'resolve_exact_mod_closure'
        ) as exact:
            self.assertEqual(self.add(), 0)
        automatic.assert_not_called()
        exact.assert_not_called()
        roots = authoritative_roots(self.source)
        target = next(r for r in roots if r.project_id == add_fixture.MODRINTH_IDS['dependency'])
        self.assertEqual(target.source_side, 'both')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.glob('mods/*.pw.toml')})

    def test_stale_manifest_rejected_without_live_mutation(self):
        roots = core.read_pack_root_manifest(self.source)
        core.write_pack_root_manifest(self.source, roots + (
            core.PackRootRecord('modrinth', 'Ghost001', 'both'),))
        before = self.f.snapshot()
        with self.assertRaises(core.HuroshikiError):
            self.add()
        self.assertEqual(self.f.snapshot(), before)
        self.f.assert_unlocked()

    def test_root_side_disagreement_rejected_without_live_mutation(self):
        core.write_pack_root_manifest(self.source, (
            core.PackRootRecord('modrinth', add_fixture.MODRINTH_IDS['root'], 'client'),))
        before = self.f.snapshot()
        with self.assertRaises(core.HuroshikiError):
            self.add()
        self.assertEqual(self.f.snapshot(), before)

    def test_duplicate_unrelated_metadata_rejected(self):
        (self.source / 'mods/duplicate.pw.toml').write_bytes(
            (self.source / 'mods/root.pw.toml').read_bytes())
        before = self.f.snapshot()
        with self.assertRaises(core.HuroshikiError):
            self.add()
        self.assertEqual(self.f.snapshot(), before)

    def test_missing_manifest_rejected(self):
        (self.source / '.huroshiki-roots.json').unlink()
        before = self.f.snapshot()
        with self.assertRaises(core.HuroshikiError):
            self.add()
        self.assertEqual(self.f.snapshot(), before)

    def test_mixed_transaction_refreshes_changed_descriptor_hash(self):
        tx = core.PackTransaction.create(self.f.key)
        self.addCleanup(tx.discard)
        tx.set_side(Path('mods/root.pw.toml'), False, True)
        calls = []
        def run(command, *, cwd, **kwargs):
            calls.append(tuple(command))
            self.assertEqual(command, ['packwiz', 'refresh'])
            refresh_index(cwd)
            return core.ResolverProcessResult(0, '', '', False, False)
        with patch.object(core, 'run_resolver_process', side_effect=run):
            self.assertEqual(tx.add_mod_transactionally(
                'modrinth', 'dependency', 'client', artifact_id='Exact001'), 0)
        self.assertEqual(calls, [('packwiz', 'refresh')])
        self.assert_indexes_valid()
        authoritative_roots(self.source)

    def assert_indexes_valid(self):
        index_bytes = (self.source / 'index.toml').read_bytes()
        pack = tomllib.loads((self.source / 'pack.toml').read_text())
        self.assertEqual(pack['index']['hash'], hashlib.sha256(index_bytes).hexdigest())
        for e in tomllib.loads(index_bytes.decode())['files']:
            self.assertEqual(e['hash'], hashlib.sha256((self.source / e['file']).read_bytes()).hexdigest())

    def test_mixed_refresh_failure_preserves_live_pack(self):
        before = self.f.snapshot()
        tx = core.PackTransaction.create(self.f.key)
        self.addCleanup(tx.discard)
        tx.set_side(Path('mods/root.pw.toml'), False, True)
        with patch.object(core, 'run_resolver_process', return_value=
                          core.ResolverProcessResult(2, '', 'refresh failed', False, False)):
            with self.assertRaises(core.HuroshikiError):
                tx.add_mod_transactionally('modrinth', 'dependency', 'client', artifact_id='Exact001')
        self.assertEqual(self.f.snapshot(), before)

    def test_mixed_refresh_cannot_rewrite_selected_artifact(self):
        before = self.f.snapshot()
        tx = core.PackTransaction.create(self.f.key)
        self.addCleanup(tx.discard)
        tx.set_side(Path('mods/root.pw.toml'), False, True)
        def run(command, *, cwd, **kwargs):
            p = cwd / 'mods/dependency.pw.toml'
            p.write_text(p.read_text().replace('https://example.invalid/', 'https://different.invalid/'))
            refresh_index(cwd)
            return core.ResolverProcessResult(0, '', '', False, False)
        with patch.object(core, 'run_resolver_process', side_effect=run):
            with self.assertRaises(core.HuroshikiError):
                tx.add_mod_transactionally('modrinth', 'dependency', 'client', artifact_id='Exact001')
        self.assertEqual(self.f.snapshot(), before)


    def test_repeated_readd_is_idempotent_and_preserves_lock_reason(self):
        core.set_mod_version_override(self.source, core.ModVersionOverride(
            'modrinth', add_fixture.MODRINTH_IDS['dependency'], 'Exact001', True, 'Keep this version'))
        self.assertEqual(self.add(), 0)
        before = self.f.snapshot()
        self.assertEqual(self.add('server'), 0)
        self.assertEqual(self.f.snapshot(), before)
        override = core.get_mod_version_override(self.source, 'modrinth:Depn0001')
        self.assertTrue(override.locked)
        self.assertEqual(override.reason, 'Keep this version')

    def test_same_artifact_jar_required_edge_does_not_gain_runtime_side(self):
        # Verify a real JAR dependency declaration; this operation deliberately
        # preserves, and does not certify or repair, the installed distribution.
        jars = self.f.root / 'local-jars'
        jars.mkdir()
        parent = jars / 'parent.jar'
        child = jars / 'child.jar'
        with zipfile.ZipFile(parent, 'w') as jar:
            jar.writestr('META-INF/neoforge.mods.toml',
                '[[mods]]\nmodId="parent"\nversion="1.0"\n'
                '[[dependencies.parent]]\nmodId="child"\ntype="required"\n'
                'versionRange="[1,)"\nside="BOTH"\n')
        with zipfile.ZipFile(child, 'w') as jar:
            jar.writestr('META-INF/neoforge.mods.toml',
                '[[mods]]\nmodId="child"\nversion="1.0"\n')
        requirements = equivalence.parse_loader_dependency_requirements(parent, 'neoforge')
        self.assertEqual(requirements, (equivalence.LoaderDependencyRequirement('child', '[1,)'),))
        self.assertEqual(equivalence.parse_semantic_jar(child, 'neoforge').members, (('child', '1.0'),))
        p = self.source / 'mods/dependency.pw.toml'
        d = tomlkit.parse(p.read_text())
        d['download']['hash'] = hashlib.sha256(parent.read_bytes()).hexdigest()
        p.write_text(tomlkit.dumps(d))
        (self.source / 'mods/child.pw.toml').write_text(
            add_fixture.metadata('Child', 'Child001', side='client').replace(
                'hash = "00"', f'hash = "{hashlib.sha256(child.read_bytes()).hexdigest()}"'))
        refresh_index(self.source)
        before = {p.name: p.read_bytes() for p in self.source.glob('mods/*.pw.toml')}
        with patch.object(core, 'materialize_provider_artifact') as materialize, patch.object(
            core, 'resolve_exact_mod_closure'
        ) as resolve:
            self.assertEqual(self.add('both'), 0)
        materialize.assert_not_called()
        resolve.assert_not_called()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.glob('mods/*.pw.toml')})
        self.assertEqual(core.read_mod(self.source, Path('mods/child.pw.toml')).side, 'client')
        authoritative_roots(self.source)
        self.assert_indexes_valid()

    def test_mixed_refresh_cannot_rebind_stale_index_to_pack(self):
        before = self.f.snapshot()
        tx = core.PackTransaction.create(self.f.key)
        self.addCleanup(tx.discard)
        tx.set_side(Path('mods/root.pw.toml'), False, True)
        def run(command, *, cwd, **kwargs):
            # Keep the old descriptor hashes but make pack.toml's index hash valid.
            d = tomlkit.parse((cwd / 'pack.toml').read_text())
            d['index']['hash'] = hashlib.sha256((cwd / 'index.toml').read_bytes()).hexdigest()
            (cwd / 'pack.toml').write_text(tomlkit.dumps(d))
            return core.ResolverProcessResult(0, '', '', False, False)
        with patch.object(core, 'run_resolver_process', side_effect=run):
            with self.assertRaises(core.HuroshikiError):
                tx.add_mod_transactionally('modrinth', 'dependency', 'client', artifact_id='Exact001')
        self.assertEqual(self.f.snapshot(), before)


    def test_refresh_index_rejects_missing_duplicate_or_non_metafile_entries(self):
        before = core._file_content_snapshot(self.source)
        for kind in ('missing', 'duplicate', 'non-metafile'):
            with self.subTest(kind=kind):
                refresh_index(self.source)
                index = tomlkit.parse((self.source / 'index.toml').read_text())
                if kind == 'missing':
                    index['files'].pop()
                elif kind == 'duplicate':
                    index['files'].append(dict(index['files'][0]))
                else:
                    index['files'][0]['metafile'] = False
                raw = tomlkit.dumps(index).encode()
                (self.source / 'index.toml').write_bytes(raw)
                pack = tomlkit.parse((self.source / 'pack.toml').read_text())
                pack['index']['hash'] = hashlib.sha256(raw).hexdigest()
                (self.source / 'pack.toml').write_text(tomlkit.dumps(pack))
                with self.assertRaises(core.HuroshikiError):
                    core._assert_exact_refresh_postconditions(self.source, before, lambda: None)

    def test_formatting_change_with_correct_index_bindings_is_accepted(self):
        before = core._file_content_snapshot(self.source)
        p = self.source / 'mods/dependency.pw.toml'
        p.write_text('# allowed format-only change\n' + p.read_text())
        refresh_index(self.source)
        core._assert_exact_refresh_postconditions(self.source, before, lambda: None)
        self.assert_indexes_valid()


class ExactPromotionRefreshIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.f = exact_fixture.ExactModVersionSelectionTest('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def prepare(self, mutation=None, *, promote_dependency=False, reject=True, mutation_on=2, child_explicit=False):
        f = self.f
        roots = (('root','r1','server'), ('root-b','b1','both'))
        dependencies = (('dependency','d1'),)
        if child_explicit:
            roots += (('dependency','d1','server'),)
            dependencies = ()
        tx = f.write_graph_pack(roots, dependencies)
        self.addCleanup(tx.discard)
        if promote_dependency:
            p = tx.source / 'mods/dependency.pw.toml'
            p.write_text(f.metadata('modrinth', 'dependency', 'd1', side='server'))
        before = core._file_content_snapshot(tx.source)
        live_before = core._file_content_snapshot(f.source)
        closures = {
            ('modrinth','root','r1'): f.graph_closure('root','r1',('dependency','d1')),
            ('modrinth','root-b','b1'): f.graph_closure('root-b','b1'),
        }
        if child_explicit:
            closures[('modrinth','dependency','d1')] = f.graph_closure('dependency','d1')
        materializer = f.dependency_graph_materializer(
            {'root': (('dependency','>=1.0'),)}, {'r1':'1.0','b1':'1.0','d1':'1.0'})
        original_refresh = core._exact_run_refresh
        refreshes = []
        def refresh(source, **kwargs):
            original_refresh(source, **kwargs)
            refreshes.append(source)
            if len(refreshes) == mutation_on and mutation is not None:
                mutation(source)
        project, version = ('dependency','d1') if promote_dependency else ('root','r1')
        selection = f.selection('modrinth', exact_fixture.branded_project(project),
                                exact_fixture.branded_version(version))
        with patch.object(core,'resolve_exact_mod_closure',side_effect=
                          lambda sel, **kw: closures[exact_fixture.selection_fixture_key(sel)]), patch.object(
                              core,'materialize_provider_artifact',side_effect=materializer), patch.object(
                                  core,'_exact_run_refresh',side_effect=refresh):
            if mutation is None or not reject:
                tx.prepare_exact_mod_version(selection, promote_to_root_side='client' if promote_dependency else 'both')
            else:
                with self.assertRaises(core.HuroshikiError):
                    tx.prepare_exact_mod_version(selection, promote_to_root_side='both')
                self.assertEqual(core._file_content_snapshot(tx.source), before)
                self.assertEqual(core._file_content_snapshot(f.source), live_before)
                self.assertFalse(tx._exact_selection_prepared)
                return
        tx.apply()
        authoritative_roots(f.source)
        return tx

    def test_promoted_dependency_root_side_matches_metadata(self):
        self.prepare(promote_dependency=True)

    def test_ordinary_promotion_remains_valid(self):
        self.prepare()

    def test_refresh_cannot_change_dependency_download(self):
        def change(s):
            p=s/'mods/dependency.pw.toml'; d=tomlkit.parse(p.read_text())
            d['download']['url']='https://example.invalid/unverified.jar'
            d['download']['hash']='c'*64; p.write_text(tomlkit.dumps(d))
        self.prepare(change)

    def test_refresh_cannot_drop_dependency_side(self):
        def change(s):
            p=s/'mods/dependency.pw.toml'; d=tomlkit.parse(p.read_text())
            d['side']='server'; p.write_text(tomlkit.dumps(d))
        self.prepare(change)

    def test_refresh_cannot_remove_unrelated_root(self):
        self.prepare(lambda s: core.write_pack_root_manifest(s, tuple(
            r for r in core.read_pack_root_manifest(s)
            if r.project_id != exact_fixture.mr_project('root-b'))))

    def test_refresh_cannot_change_version_lock(self):
        def change(s):
            p=s/'.huroshiki-version-overrides.json'; d=json.loads(p.read_text())
            # The fixture creates one unlocked selection; changing the boolean must fail.
            p.write_text(p.read_text().replace('false', 'true'))
        self.prepare(change)

    def test_refresh_cannot_drop_ignore_rule(self):
        self.prepare(lambda s: (s/'.packwizignore').write_text('/.huroshiki-roots.json\n'))

    def test_refresh_cannot_change_other_pack_file(self):
        self.prepare(lambda s: (s/'unexpected.txt').write_text('not authorized'))

    def test_refresh_cannot_change_non_runtime_pack_setting(self):
        def change(s):
            p=s/'pack.toml'; d=tomlkit.parse(p.read_text()); d['name']='Unapproved'; p.write_text(tomlkit.dumps(d))
        self.prepare(change)


    def test_format_only_refresh_is_accepted(self):
        self.prepare(lambda source: (source / 'mods/dependency.pw.toml').write_text(
            '# formatting only\n' + (source / 'mods/dependency.pw.toml').read_text()), reject=False)

    def test_first_refresh_cannot_change_other_pack_file(self):
        self.prepare(lambda source: (source / 'not-authorized.txt').write_text('unexpected'), mutation_on=1)

    def test_explicit_dependency_side_expands_consistently(self):
        tx = self.prepare(child_explicit=True)
        roots = authoritative_roots(self.f.source)
        dependency = next(r for r in roots if r.project_id == exact_fixture.mr_project('dependency'))
        self.assertEqual(dependency.source_side, 'both')
        self.assertEqual(core.read_mod(self.f.source, Path('mods/dependency.pw.toml')).side, 'both')
