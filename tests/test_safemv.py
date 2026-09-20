"""Tests use only newly generated temporary files; artifacts are retained."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'safemv.py'
spec = importlib.util.spec_from_file_location('safemv', SCRIPT)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
ARTIFACTS = Path(tempfile.mkdtemp(prefix='safemv-tests-')).resolve()


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.cwd = Path.cwd()
        self.base = Path(tempfile.mkdtemp(prefix=self._testMethodName + '-', dir=ARTIFACTS))
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'data.txt').write_bytes(b'original bytes\x00\xff')
        self.destination = self.base / 'destination'
        self.manifest = self.base / 'list.jsonl'
        self.log = self.base / 'run.jsonl'
        self.vol = dict(os='Darwin', mount=str(t.canonical(self.base)), uuid='TEST-UUID',
                        device='/dev/test-only', fstype='apfs', external=False)
        self.mock_volume = mock.patch.object(t, 'volume', side_effect=lambda p: dict(self.vol))
        self.mock_volume.start()
        # sync is system-wide; avoid unrelated flushes during disposable unit tests.
        self.mock_sync = mock.patch.object(t.os, 'sync')
        self.mock_sync.start()

    def tearDown(self):
        os.chdir(self.cwd)
        self.mock_volume.stop()
        self.mock_sync.stop()

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return t.main(list(map(str, args)))

    def plan(self):
        return self.cli('plan', '--source', self.source, '--destination', self.destination,
                        '--manifest', self.manifest)

    def run_copy(self, **extra):
        args = ['run', '--manifest', self.manifest, '--log', self.log]
        for key, val in extra.items():
            args.extend(['--' + key.replace('_', '-'), val])
        return self.cli(*args)

    def verify(self):
        return self.cli('verify', '--manifest', self.manifest, '--log', self.base / 'verify.jsonl')

    def events(self):
        return [json.loads(row) for row in self.log.read_text().splitlines()]

    def auto(self, action='cp', sources=None, target=None, extra=()):
        return self.cli(action, '--state-dir', self.base / 'jobs', *extra,
                        *(sources or [self.source]), target or self.destination)

    def test_cp_directory_into_existing_directory(self):
        self.destination.mkdir()
        self.assertEqual(self.auto(), 0)
        self.assertEqual((self.destination / self.source.name / 'data.txt').read_bytes(),
                         (self.source / 'data.txt').read_bytes())
        self.assertTrue(self.source.exists())
        self.assertEqual(len(list((self.base / 'jobs').glob('*/plan.log'))), 1)

    def test_cp_directory_to_new_name(self):
        self.assertEqual(self.auto(), 0)
        self.assertTrue((self.destination / 'data.txt').is_file())
        self.assertFalse((self.destination / self.source.name).exists())

    def test_cp_single_file_into_directory(self):
        self.destination.mkdir()
        self.assertEqual(self.auto(sources=[self.source / 'data.txt']), 0)
        self.assertEqual((self.destination / 'data.txt').read_bytes(), b'original bytes\x00\xff')

    def test_cp_file_to_new_filename(self):
        self.assertEqual(self.auto(sources=[self.source / 'data.txt']), 0)
        self.assertTrue(self.destination.is_file())
        self.assertEqual(self.destination.read_bytes(), b'original bytes\x00\xff')

    def test_cp_multiple_sources_require_existing_directory(self):
        second = self.base / 'second.txt'; second.write_bytes(b'second')
        self.assertEqual(self.auto(sources=[self.source, second]), 2)
        self.assertFalse(self.destination.exists())
        self.destination.mkdir()
        self.assertEqual(self.auto(sources=[self.source, second]), 0)
        self.assertTrue((self.destination / self.source.name / 'data.txt').is_file())
        self.assertEqual((self.destination / 'second.txt').read_bytes(), b'second')

    def test_cp_empty_directory(self):
        empty = self.base / 'empty'; empty.mkdir()
        self.assertEqual(self.auto(sources=[empty]), 0)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_cp_refuses_overwrite_or_directory_merge(self):
        self.destination.mkdir()
        child = self.destination / self.source.name; child.mkdir()
        sentinel = child / 'keep'; sentinel.write_bytes(b'keep')
        self.assertEqual(self.auto(), 2)
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertFalse((child / 'data.txt').exists())

    def test_cp_source_dot_copies_contents(self):
        self.destination.mkdir()
        self.assertEqual(self.auto(sources=[str(self.source) + '/.']), 0)
        self.assertTrue((self.destination / 'data.txt').is_file())
        self.assertFalse((self.destination / self.source.name).exists())

    def test_mac_cp_source_trailing_slash_copies_contents(self):
        if t.platform.system() != 'Darwin':
            self.skipTest('BSD cp trailing-slash semantics')
        self.destination.mkdir()
        self.assertEqual(self.auto(sources=[str(self.source) + '/']), 0)
        self.assertTrue((self.destination / 'data.txt').is_file())

    def test_missing_destination_parent_is_refused(self):
        self.assertEqual(self.auto(target=self.base / 'missing-parent/new'), 2)
        self.assertFalse((self.base / 'missing-parent').exists())

    def test_mv_directory_removes_only_verified_source(self):
        (self.source / 'empty').mkdir()
        (self.source / '.hidden').write_bytes(b'hidden')
        self.destination.mkdir()
        self.assertEqual(self.auto('mv'), 0)
        self.assertFalse(self.source.exists())
        result = self.destination / self.source.name
        self.assertEqual((result / 'data.txt').read_bytes(), b'original bytes\x00\xff')
        self.assertEqual((result / '.hidden').read_bytes(), b'hidden')
        self.assertTrue((result / 'empty').is_dir())
        rows = [json.loads(row) for row in next((self.base / 'jobs').glob('*/plan.log')).read_text().splitlines()]
        events = [r['event'] for r in rows]
        self.assertLess(events.index('verified'), events.index('source_removal_start'))
        self.assertEqual(events[-1], 'moved')

    def test_mv_single_file_to_new_name(self):
        src = self.source / 'data.txt'
        self.assertEqual(self.auto('mv', sources=[src]), 0)
        self.assertFalse(src.exists())
        self.assertTrue(self.source.is_dir())
        self.assertEqual(self.destination.read_bytes(), b'original bytes\x00\xff')

    def test_mv_preserves_extended_attribute(self):
        src = self.source / 'data.txt'
        fd = os.open(src, os.O_RDONLY)
        name = 'com.example.safe-transfer' if t.platform.system() == 'Darwin' else 'user.safe_transfer'
        try:
            t.file_attributes(fd, {name: b'attribute-content\x00'})
        finally:
            os.close(fd)
        self.assertEqual(self.auto('mv'), 0)
        fd = os.open(self.destination / 'data.txt', os.O_RDONLY)
        try:
            self.assertEqual(t.file_attributes(fd)[name], b'attribute-content\x00')
        finally:
            os.close(fd)

    def test_mv_corrupt_destination_never_deletes_source(self):
        original = t.verify_files
        def corrupt(header, entries, journal):
            Path(entries[-1]['destination']).write_bytes(b'corrupt')
            return original(header, entries, journal)
        with mock.patch.object(t, 'verify_files', side_effect=corrupt):
            self.assertEqual(self.auto('mv'), 3)
        self.assertEqual((self.source / 'data.txt').read_bytes(), b'original bytes\x00\xff')

    def test_mv_changed_source_never_deletes_it(self):
        original = t.remove_verified_source
        def changed(header, entries, journal, deletion):
            (self.source / 'data.txt').write_bytes(b'changed')
            return original(header, entries, journal, deletion)
        with mock.patch.object(t, 'remove_verified_source', side_effect=changed):
            self.assertEqual(self.auto('mv'), 2)
        self.assertEqual((self.source / 'data.txt').read_bytes(), b'changed')

    def test_mv_added_file_is_not_deleted(self):
        original = t.remove_verified_source
        def changed(header, entries, journal, deletion):
            (self.source / 'new-file').write_bytes(b'new')
            return original(header, entries, journal, deletion)
        with mock.patch.object(t, 'remove_verified_source', side_effect=changed):
            self.assertEqual(self.auto('mv'), 2)
        self.assertTrue((self.source / 'data.txt').is_file())
        self.assertEqual((self.source / 'new-file').read_bytes(), b'new')

    def test_mv_copy_error_never_deletes_source(self):
        with mock.patch.object(t, 'copy_one', side_effect=t.Stop('copy failed')):
            self.assertEqual(self.auto('mv'), 2)
        self.assertTrue((self.source / 'data.txt').is_file())

    def test_mv_external_decline_never_deletes_source(self):
        self.vol['external'] = True
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), \
             mock.patch.object(t, 'remount', side_effect=lambda v, j, *a: t.confirm_remount(v, j)), \
             mock.patch('builtins.input', return_value='n'):
            self.assertEqual(self.auto('mv'), 2)
        self.assertTrue((self.source / 'data.txt').is_file())

    def test_mv_hardlink_refused_before_copy(self):
        os.link(self.source / 'data.txt', self.source / 'linked.txt')
        self.assertEqual(self.auto('mv'), 2)
        self.assertTrue((self.source / 'data.txt').is_file())
        self.assertFalse(self.destination.exists())

    def test_mv_partial_removal_is_reported_truthfully(self):
        (self.source / 'second.txt').write_bytes(b'second')
        original = t.os.unlink
        calls = []
        def fail_second(path, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError('simulated removal refusal')
            return original(path, **kwargs)
        with mock.patch.object(t.os, 'unlink', side_effect=fail_second):
            self.assertEqual(self.auto('mv'), 2)
        rows = [json.loads(row) for row in next((self.base / 'jobs').glob('*/plan.log')).read_text().splitlines()]
        self.assertEqual(rows[-1]['event'], 'failed')
        self.assertTrue(rows[-1]['sources_deleted'])
        self.assertEqual(rows[-1]['source_files_removed'], 1)
        self.assertEqual(len(list(self.source.iterdir())), 1)
        self.assertEqual((self.destination / 'data.txt').read_bytes(), b'original bytes\x00\xff')
        self.assertEqual((self.destination / 'second.txt').read_bytes(), b'second')

    def test_mv_destination_changes_after_verification(self):
        original = t.remove_verified_source
        def changed(header, entries, journal, deletion):
            (self.destination / 'data.txt').write_bytes(b'corrupted after verification')
            return original(header, entries, journal, deletion)
        with mock.patch.object(t, 'remove_verified_source', side_effect=changed):
            self.assertEqual(self.auto('mv'), 2)
        self.assertTrue((self.source / 'data.txt').is_file())

    def test_mv_dot_operand_refused(self):
        self.assertEqual(self.auto('mv', sources=[str(self.source) + '/.']), 2)
        self.assertTrue((self.source / 'data.txt').is_file())
        self.assertFalse(self.destination.exists())

    def test_directory_to_missing_destination_with_trailing_slash(self):
        self.assertEqual(self.auto(target=str(self.destination) + '/'), 0)
        self.assertTrue((self.destination / 'data.txt').is_file())

    def test_actual_rsync_roundtrip_and_internal_no_remount(self):
        for name in ['space name.txt', 'quote"name', 'new\nline', 'tab\tname', '-option', 'caf\u00e9.jpg', '.hidden', 'zero']:
            (self.source / name).write_bytes(b'' if name == 'zero' else name.encode())
        (self.source / 'empty folder').mkdir()
        self.assertEqual(self.plan(), 0)
        self.assertFalse(self.destination.exists())
        with mock.patch.object(t, 'remount') as remount, mock.patch('builtins.input') as question:
            self.assertEqual(self.run_copy(), 0)
            remount.assert_not_called()
            question.assert_not_called()
        self.assertEqual(self.verify(), 0)
        for item in self.source.iterdir():
            if item.is_file():
                self.assertEqual(item.read_bytes(), (self.destination / item.name).read_bytes())
                self.assertEqual(item.stat().st_mode & 0o777, (self.destination / item.name).stat().st_mode & 0o777)
                self.assertEqual(item.stat().st_mtime_ns, (self.destination / item.name).stat().st_mtime_ns)
        self.assertTrue((self.destination / 'empty folder').is_dir())
        self.assertEqual(self.events()[-1]['event'], 'verified')
        self.assertFalse(self.events()[-1]['sources_deleted'])
        self.assertFalse(self.events()[-1]['remounted_in_this_run'])

    def test_removed_approval_flags_are_not_in_cli(self):
        for flag in ('--approve-copy', '--approve-remount'):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    t.parser().parse_args(['run', '--manifest', 'plan', '--log', 'log', flag, 'value'])
                self.assertEqual(result.exception.code, 2)

    def test_run_default_log_replaces_last_extension(self):
        self.manifest = self.base / 'batch list.part.jsonl'
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.cli('run', '--manifest', self.manifest), 0)
        default_log = self.base / 'batch list.part.log'
        self.assertTrue(default_log.is_file())
        self.assertEqual(json.loads(default_log.read_text().splitlines()[-1])['event'], 'verified')
        self.assertFalse(self.log.exists())

    def test_verify_default_log_for_manifest_without_extension(self):
        self.manifest = self.base / 'manifest'
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.run_copy(), 0)  # Explicit log still overrides the default.
        self.assertEqual(self.cli('verify', '--manifest', self.manifest), 0)
        default_log = self.base / 'manifest.log'
        events = [json.loads(row) for row in default_log.read_text().splitlines()]
        self.assertEqual(events[0]['mode'], 'verify')
        self.assertEqual(events[-1]['event'], 'verified')
        self.assertTrue(self.log.is_file())

    def test_existing_default_log_is_never_overwritten(self):
        self.assertEqual(self.plan(), 0)
        default_log = self.manifest.with_suffix('.log')
        default_log.write_bytes(b'previous record')
        self.assertEqual(self.cli('run', '--manifest', self.manifest), 2)
        self.assertEqual(default_log.read_bytes(), b'previous record')
        self.assertFalse(self.destination.exists())

    def test_default_log_cannot_replace_manifest_named_log(self):
        self.manifest = self.base / 'manifest.log'
        self.assertEqual(self.plan(), 0)
        before = self.manifest.read_bytes()
        self.assertEqual(self.cli('run', '--manifest', self.manifest), 2)
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_preserved(self):
        self.assertEqual(self.plan(), 0)
        self.destination.mkdir()
        sentinel = self.destination / 'keep'; sentinel.write_bytes(b'keep')
        self.assertEqual(self.run_copy(), 2)
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertEqual(list(self.destination.iterdir()), [sentinel])

    def test_changed_source_aborts_before_copy(self):
        self.assertEqual(self.plan(), 0)
        (self.source / 'data.txt').write_bytes(b'changed bytes')
        self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.events()[-1]['event'], 'failed')

    def test_added_source_aborts_before_copy(self):
        self.assertEqual(self.plan(), 0)
        (self.source / 'added').write_bytes(b'new')
        self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())

    def test_corruption_stops_first_mismatch_with_exit_3(self):
        (self.source / 'z-last').write_bytes(b'must not be verified')
        self.assertEqual(self.plan(), 0)
        original_verify = t.verify_files
        def corrupt_then_verify(header, entries, journal):
            (self.destination / 'data.txt').write_bytes(b'corrupted')
            return original_verify(header, entries, journal)
        with mock.patch.object(t, 'verify_files', side_effect=corrupt_then_verify):
            self.assertEqual(self.run_copy(), 3)
        events = self.events()
        self.assertEqual(events[-2]['event'], 'checksum_mismatch')
        self.assertEqual(events[-1]['event'], 'failed')
        self.assertFalse(any(e['event'] == 'verified' for e in events))
        self.assertEqual((self.source / 'data.txt').read_bytes(), b'original bytes\x00\xff')

    def test_missing_destination_aborts_verify(self):
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.run_copy(), 0)
        (self.destination / 'data.txt').rename(self.destination / 'retained-for-test')
        self.assertEqual(self.verify(), 2)

    def test_truncated_manifest_refused(self):
        self.assertEqual(self.plan(), 0)
        rows = self.manifest.read_text().splitlines()
        self.manifest.write_text('\n'.join(rows[:-1]) + '\n')
        self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())

    def test_manifest_path_escape_refused(self):
        self.assertEqual(self.plan(), 0)
        rows = [json.loads(x) for x in self.manifest.read_text().splitlines()]
        rows[-1]['destination'] = str(self.base / 'escaped')
        self.manifest.write_text(''.join(map(t.line, rows)))
        self.assertEqual(self.run_copy(), 2)
        self.assertFalse((self.base / 'escaped').exists())

    def test_duplicate_filename_manifest_refused(self):
        self.assertEqual(self.plan(), 0)
        rows = self.manifest.read_text().splitlines()
        self.manifest.write_text('\n'.join(rows + [rows[-1]]) + '\n')
        self.assertEqual(self.run_copy(), 2)

    def test_case_collision_refused_in_manifest(self):
        self.assertEqual(self.plan(), 0)
        rows = [json.loads(x) for x in self.manifest.read_text().splitlines()]
        extra = dict(rows[-1])
        extra['source'] = extra['source'].replace('data.txt', 'DATA.TXT')
        extra['destination'] = extra['destination'].replace('data.txt', 'DATA.TXT')
        rows.append(extra)
        rows[0]['files'] += 1
        rows[0]['bytes'] += extra['size']
        self.manifest.write_text(''.join(map(t.line, rows)))
        self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())

    def test_existing_individual_file_is_never_replaced(self):
        self.assertEqual(self.plan(), 0)
        self.destination.mkdir()
        target = self.destination / 'data.txt'; target.write_bytes(b'valuable existing file')
        header, entries, _ = t.load_manifest(self.manifest)
        fd = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(t.Stop):
                t.copy_one(entries[-1], fd, Path(self.vol['mount']), shutil.which('rsync'))
        finally:
            os.close(fd)
        self.assertEqual(target.read_bytes(), b'valuable existing file')

    def test_replaced_destination_directory_cannot_redirect_copy(self):
        (self.source / 'sub').mkdir()
        (self.source / 'sub/file').write_bytes(b'safe')
        self.assertEqual(self.plan(), 0)
        self.destination.mkdir()
        outside = self.base / 'outside'; outside.mkdir()
        (self.destination / 'sub').symlink_to(outside, target_is_directory=True)
        _, entries, _ = t.load_manifest(self.manifest)
        item = next(x for x in entries if x['type'] == 'file' and x['source'].endswith('/sub/file'))
        fd = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(OSError):
                t.copy_one(item, fd, Path(self.vol['mount']), shutil.which('rsync'))
        finally:
            os.close(fd)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_source_refused(self):
        (self.source / 'link').symlink_to(self.source / 'data.txt')
        self.assertEqual(self.plan(), 2)
        self.assertFalse(self.manifest.exists())

    def test_special_file_refused(self):
        os.mkfifo(self.source / 'fifo')
        self.assertEqual(self.plan(), 2)

    def test_symlink_destination_parent_refused(self):
        outside = self.base / 'outside'; outside.mkdir()
        link = self.base / 'link'; link.symlink_to(outside, target_is_directory=True)
        self.destination = link / 'batch'
        self.assertEqual(self.plan(), 2)
        self.assertEqual(list(outside.iterdir()), [])

    def test_volume_uuid_substitution_refused(self):
        self.assertEqual(self.plan(), 0)
        self.vol['uuid'] = 'WRONG-UUID'
        self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())

    def test_volume_unavailable_refused(self):
        self.assertEqual(self.plan(), 0)
        with mock.patch.object(t, 'volume', side_effect=t.Stop('unmounted')):
            self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())

    def test_insufficient_space_before_destination_creation(self):
        self.assertEqual(self.plan(), 0)
        with mock.patch.object(t.shutil, 'disk_usage', return_value=mock.Mock(free=0)):
            self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())

    def test_rsync_failure_keeps_source_and_records_failure(self):
        self.assertEqual(self.plan(), 0)
        with mock.patch.object(t, 'command', side_effect=t.Stop('rsync write failure')):
            self.assertEqual(self.run_copy(), 2)
        self.assertTrue((self.source / 'data.txt').is_file())
        self.assertEqual(self.events()[-1]['event'], 'failed')

    def test_rsync_backend_and_safe_arguments(self):
        self.assertEqual(self.plan(), 0)
        original = t.command
        calls = []
        def record(argv, **kwargs):
            calls.append(argv)
            return original(argv, **kwargs)
        with mock.patch.object(t, 'command', side_effect=record):
            self.assertEqual(self.run_copy(), 0)
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(Path(argv[0]).name, 'rsync')
        self.assertIn('--ignore-existing', argv)
        self.assertIn('--partial', argv)
        self.assertIn('--whole-file', argv)
        self.assertNotIn('--remove-source-files', argv)
        self.assertNotIn('--delete', argv)
        self.assertNotIn('--checksum', argv)
        self.assertEqual(self.events()[0]['copy_backend'], argv[0])

    def test_rsync_skipped_destination_is_not_accepted_or_modified(self):
        self.assertEqual(self.plan(), 0)
        original = t.command
        target = self.destination / 'data.txt'
        def create_conflict(argv, **kwargs):
            target.write_bytes(b'valuable existing bytes')
            os.chmod(target, 0o600)
            return original(argv, **kwargs)
        with mock.patch.object(t, 'command', side_effect=create_conflict):
            self.assertEqual(self.run_copy(), 2)
        self.assertEqual(target.read_bytes(), b'valuable existing bytes')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.source / 'data.txt').exists())
        self.assertNotIn('copy_complete', [row['event'] for row in self.events()])

    def test_missing_rsync_stops_before_destination_creation(self):
        self.assertEqual(self.plan(), 0)
        original = t.shutil.which
        with mock.patch.object(t.shutil, 'which', side_effect=lambda name: None if name == 'rsync' else original(name)):
            self.assertEqual(self.run_copy(), 2)
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.source / 'data.txt').exists())

    def test_source_change_during_copy_refused(self):
        self.assertEqual(self.plan(), 0)
        original = t.command
        def mutate(args, **kwargs):
            result = original(args, **kwargs)
            (self.source / 'data.txt').write_bytes(b'mutated during copy')
            return result
        with mock.patch.object(t, 'command', side_effect=mutate):
            self.assertEqual(self.run_copy(), 2)
        self.assertFalse(any(e['event'] == 'verified' for e in self.events()))

    def external_manifest(self):
        self.assertEqual(self.plan(), 0)
        rows = [json.loads(x) for x in self.manifest.read_text().splitlines()]
        self.vol['external'] = True
        rows[0]['volume']['external'] = True
        self.manifest.write_text(''.join(map(t.line, rows)))

    def test_external_decline_keeps_verified_copies_and_stops_before_unmount(self):
        self.external_manifest()
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), \
             mock.patch.object(t, 'remount', side_effect=lambda v, j, *a: t.confirm_remount(v, j)), \
             mock.patch('builtins.input', return_value='n'), \
             mock.patch.object(t, 'verify_files', wraps=t.verify_files) as verify:
            self.assertEqual(self.run_copy(), 2)
            verify.assert_called_once()
        self.assertEqual((self.destination / 'data.txt').read_bytes(), (self.source / 'data.txt').read_bytes())
        events = [e['event'] for e in self.events()]
        self.assertLess(events.index('copy_complete'), events.index('remount_confirmation_requested'))
        self.assertLess(events.index('content_verified'), events.index('remount_confirmation_requested'))
        self.assertIn('remount_declined', events)
        self.assertNotIn('unmount_start', events)
        self.assertNotIn('verified', events)

    def test_external_copy_verify_remount_order(self):
        self.external_manifest()
        order = []
        original_copy, original_verify = t.copy_files, t.verify_files
        def do_copy(*args):
            order.append('copy'); return original_copy(*args)
        def do_verify(*args):
            order.append('verify'); return original_verify(*args)
        def do_remount(vol, journal, *args):
            order.append('question')
            t.confirm_remount(vol, journal)
            order.append('remount')
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), \
             mock.patch.object(t, 'copy_files', side_effect=do_copy), \
             mock.patch.object(t, 'remount', side_effect=do_remount), \
             mock.patch('builtins.input', return_value='y') as question, \
             mock.patch.object(t, 'verify_files', side_effect=do_verify):
            self.assertEqual(self.run_copy(), 0)
            self.assertIn('TEST-UUID', question.call_args.args[0])
            self.assertIn(self.vol['mount'], question.call_args.args[0])
        self.assertEqual(order, ['copy', 'verify', 'question', 'remount'])
        self.assertTrue(self.events()[-1]['remounted_in_this_run'])
        self.assertEqual(self.events()[-1]['checksum_verification_phase'], 'before_remount')
        self.assertFalse(self.events()[-1]['content_rechecked_after_remount'])

    def test_remount_failure_after_verification_prevents_success(self):
        self.external_manifest()
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), \
             mock.patch.object(t, 'remount', side_effect=t.Stop('busy')), \
             mock.patch.object(t, 'verify_files', wraps=t.verify_files) as verify:
            self.assertEqual(self.run_copy(), 2)
            verify.assert_called_once()
        self.assertEqual(self.events()[-1]['event'], 'failed')

    def test_mv_hashes_destination_once_before_remount(self):
        self.vol['external'] = True
        original = t.digest_file
        reads = []
        remounted = []
        def digest(path):
            if t.within(path, t.canonical(self.destination)):
                self.assertFalse(remounted, 'Destination contents read after remount')
                reads.append(path)
            return original(path)
        def remount(*args):
            self.assertEqual(len(reads), 1)
            remounted.append(True)
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), mock.patch.object(t, 'remount', side_effect=remount), \
             mock.patch.object(t, 'digest_file', side_effect=digest):
            self.assertEqual(self.auto('mv'), 0)
        self.assertEqual(len(reads), 1)
        self.assertFalse(self.source.exists())

    def test_mv_changed_destination_during_remount_blocks_removal(self):
        self.vol['external'] = True
        def remount(*args):
            (self.destination / 'data.txt').write_bytes(b'changed during remount')
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), mock.patch.object(t, 'remount', side_effect=remount):
            self.assertEqual(self.auto('mv'), 2)
        self.assertTrue((self.source / 'data.txt').exists())

    def test_mv_remount_error_blocks_removal_after_verification(self):
        self.vol['external'] = True
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), \
             mock.patch.object(t, 'remount', side_effect=t.Stop('mount failed')), \
             mock.patch.object(t, 'verify_files', wraps=t.verify_files) as verify:
            self.assertEqual(self.auto('mv'), 2)
            verify.assert_called_once()
        self.assertTrue((self.source / 'data.txt').exists())

    def test_corrupt_copy_blocks_remount(self):
        self.external_manifest()
        original = t.copy_files
        def copy(*args):
            original(*args)
            (self.destination / 'data.txt').write_bytes(b'bad copy')
        with mock.patch.object(t, 'check_manifest_location'), mock.patch.object(t, 'separate_source'), \
             mock.patch.object(t, 'remount_commands'), mock.patch.object(t, 'copy_files', side_effect=copy), \
             mock.patch.object(t, 'remount') as remount:
            self.assertEqual(self.run_copy(), 3)
            remount.assert_not_called()
        self.assertTrue((self.source / 'data.txt').exists())


class PlatformTests(unittest.TestCase):
    def test_macos_external_plist(self):
        data = dict(MountPoint='/Volumes/External', Internal=False, VolumeUUID='UUID',
                    DeviceIdentifier='disk9s1', FilesystemType='ntfs')
        with mock.patch.object(t, 'executable', return_value='/usr/sbin/diskutil'), \
             mock.patch.object(t, 'command', return_value=plistlib.dumps(data)):
            result = t.mac_volume(Path('/Volumes/External'))
        self.assertTrue(result['external'])
        self.assertEqual(result['device'], '/dev/disk9s1')

    def linux_data(self, transport='usb', hotplug=True, fsroot='/'):
        mount = {'filesystems': [dict(source='/dev/sdb1', target='/media/External', fstype='ntfs3',
                                     uuid='UUID', options='rw,nosuid,nodev', fsroot=fsroot)]}
        blocks = {'blockdevices': [dict(name='/dev/sdb1', type='part', children=[
            dict(name='/dev/sdb', type='disk', tran=transport, hotplug=hotplug, rm=False)])]}
        return [json.dumps(mount).encode(), json.dumps(blocks).encode()]

    def test_linux_usb_detection(self):
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'command', side_effect=self.linux_data()):
            self.assertTrue(t.linux_volume(Path('/media/External'))['external'])

    def test_linux_internal_detection(self):
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'command', side_effect=self.linux_data('nvme', False)):
            self.assertFalse(t.linux_volume(Path('/media/External'))['external'])

    def test_linux_uncertain_hotplug_refused(self):
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'command', side_effect=self.linux_data('sata', True)):
            with self.assertRaises(t.Stop): t.linux_volume(Path('/media/External'))

    def test_linux_bind_mount_refused(self):
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'command', side_effect=self.linux_data(fsroot='/subtree')):
            with self.assertRaises(t.Stop): t.linux_volume(Path('/media/External'))

    def test_remount_commands_never_force_or_eject(self):
        vol = dict(os='Darwin', external=True, mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID')
        with mock.patch.object(t, 'executable', side_effect=lambda n: n):
            unmount, mount = t.remount_commands(vol)
            self.assertEqual(unmount, ['diskutil', 'unmount', '/dev/disk9s1'])
            self.assertEqual(mount, ['diskutil', 'mount', '/dev/disk9s1'])
            vol.update(os='Linux', mount='/media/External', device='/dev/sdb1', fstype='ntfs3', options='rw,nosuid,nodev')
            unmount, mount = t.remount_commands(vol, sudo=True)
            self.assertEqual(unmount, ['sudo', '-n', '--', 'umount', '--', '/media/External'])
            self.assertIn('UUID=UUID', mount)
            self.assertNotIn('remount', mount)
            self.assertNotIn('-f', unmount)
            self.assertNotIn('-l', unmount)

    def test_internal_remount_refused(self):
        with self.assertRaises(t.Stop):
            t.remount_commands(dict(external=False))

    def test_fuse_requires_udisks(self):
        vol = dict(os='Linux', external=True, mount='/media/External', device='/dev/sdb1', uuid='UUID',
                   fstype='fuseblk', options='rw')
        with mock.patch.object(t, 'executable', side_effect=lambda n: n):
            with self.assertRaises(t.Stop): t.remount_commands(vol)
            commands = t.remount_commands(vol, linux_method='udisks')
            self.assertEqual(commands[0][0], 'udisksctl')
            self.assertIn('--no-user-interaction', commands[0])

    def test_busy_unmount_does_not_try_mount(self):
        vol = dict(os='Darwin', external=True, mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID', fstype='ntfs')
        journal = mock.Mock()
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'volume', return_value=vol), \
             mock.patch('builtins.input', return_value='y'), \
             mock.patch.object(t, 'command', side_effect=t.Stop('resource busy')) as cmd:
            with self.assertRaises(t.Stop): t.remount(vol, journal)
            self.assertEqual(cmd.call_count, 1)
            self.assertEqual(cmd.call_args.args[0][1], 'unmount')

    def test_wrong_uuid_after_remount_refused(self):
        vol = dict(os='Darwin', external=True, mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID', fstype='ntfs')
        wrong = dict(vol, uuid='WRONG')
        info = plistlib.dumps(dict(VolumeUUID='UUID', Internal=False))
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'volume', side_effect=[vol, vol, wrong]), \
             mock.patch('builtins.input', return_value='y'), \
             mock.patch.object(t.os.path, 'ismount', return_value=False), \
             mock.patch.object(t, 'command', side_effect=[b'', info, b'']) as cmd:
            with self.assertRaises(t.Stop): t.remount(vol, mock.Mock())
            self.assertEqual(cmd.call_count, 3)

    def test_mounted_volume_must_actually_disappear(self):
        vol = dict(os='Darwin', external=True, mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID', fstype='ntfs')
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'volume', return_value=vol), \
             mock.patch('builtins.input', return_value='y'), \
             mock.patch.object(t.os.path, 'ismount', return_value=True), \
             mock.patch.object(t, 'command', return_value=b'') as cmd:
            with self.assertRaises(t.Stop): t.remount(vol, mock.Mock())
            self.assertEqual(cmd.call_count, 1)

    def test_no_unmount_without_explicit_y(self):
        vol = dict(os='Darwin', external=True, mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID', fstype='ntfs')
        for answer in ('n', '', EOFError(), OSError('no input')):
            with self.subTest(answer=repr(answer)), \
                 mock.patch.object(t, 'executable', side_effect=lambda n: n), \
                 mock.patch.object(t, 'volume', return_value=vol), \
                 mock.patch('builtins.input', side_effect=[answer]), \
                 mock.patch.object(t, 'command') as cmd, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(t.Stop): t.remount(vol, mock.Mock())
                cmd.assert_not_called()

    def test_invalid_answer_repeats_and_y_confirms(self):
        vol = dict(mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID')
        journal = mock.Mock()
        with mock.patch('builtins.input', side_effect=['maybe', 'yes', ' Y ']) as question, \
             contextlib.redirect_stdout(io.StringIO()):
            t.confirm_remount(vol, journal)
        self.assertEqual(question.call_count, 3)
        journal.event.assert_called_with('remount_confirmed', uuid='UUID')

    def test_volume_changed_during_prompt_is_not_unmounted(self):
        vol = dict(os='Darwin', external=True, mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID', fstype='ntfs')
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'volume', side_effect=[vol, dict(vol, uuid='CHANGED')]), \
             mock.patch('builtins.input', return_value='y'), \
             mock.patch.object(t, 'command') as cmd, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(t.Stop): t.remount(vol, mock.Mock())
            cmd.assert_not_called()

    def test_macos_remount_does_not_require_old_mountpoint_directory(self):
        missing = ARTIFACTS / 'removed-by-disk-arbitration'
        self.assertFalse(missing.exists())
        vol = dict(os='Darwin', external=True, mount=str(missing), device='/dev/disk9s1', uuid='UUID', fstype='ntfs')
        info = plistlib.dumps(dict(VolumeUUID='UUID', Internal=False))
        journal = mock.Mock()
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'volume', return_value=vol), \
             mock.patch.object(t.os.path, 'ismount', return_value=False), \
             mock.patch('builtins.input', return_value='y'), \
             mock.patch.object(t, 'command', side_effect=[b'', info, b'']) as cmd, \
             contextlib.redirect_stdout(io.StringIO()):
            t.remount(vol, journal)
        self.assertEqual(cmd.call_args_list[-1].args[0], ['diskutil', 'mount', '/dev/disk9s1'])
        self.assertFalse(missing.exists())
        journal.event.assert_called_with('remounted', uuid='UUID')

    def test_changed_automatic_mountpoint_is_not_accepted(self):
        vol = dict(os='Darwin', external=True, mount='/Volumes/External', device='/dev/disk9s1', uuid='UUID', fstype='ntfs')
        moved = dict(vol, mount='/Volumes/External 1')
        info = plistlib.dumps(dict(VolumeUUID='UUID', Internal=False))
        journal = mock.Mock()
        with mock.patch.object(t, 'executable', side_effect=lambda n: n), \
             mock.patch.object(t, 'volume', side_effect=[vol, vol, moved]), \
             mock.patch.object(t.os.path, 'ismount', return_value=False), \
             mock.patch('builtins.input', return_value='y'), \
             mock.patch.object(t, 'command', side_effect=[b'', info, b'']), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(t.Stop, 'Destination identity changed: mount'):
                t.remount(vol, journal)
        self.assertFalse(any(call.args[0] == 'remounted' for call in journal.event.call_args_list))


if __name__ == '__main__':
    print('Retained test artifacts:', ARTIFACTS, flush=True)
    unittest.main(verbosity=2)
