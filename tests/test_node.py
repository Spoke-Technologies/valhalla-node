import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import node


class Response(io.BytesIO):
    def geturl(self):
        return 'https://download.geofabrik.de/australia-oceania/australia-261031.osm.pbf'


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.patcher = patch.object(node, 'ROOT', self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.old = self.root / 'releases/old'
        self.new = self.root / 'releases/new'
        self.old.mkdir(parents=True)
        self.new.mkdir()
        node.set_link('current', self.old)

    def test_download_uses_dated_checksum_and_verifies_content(self):
        content = b'Australian map fixture'
        checksum = hashlib.md5(content).hexdigest().encode() + b'  australia.osm.pbf\n'
        with patch.object(node, 'MIN_MAP_BYTES', 1), patch.object(
            node.urllib.request, 'urlopen', side_effect=[Response(content), Response(checksum)]
        ) as fetch:
            result = node.download_map(self.new)
        self.assertEqual(fetch.call_args_list[1].args[0], Response().geturl() + '.md5')
        self.assertEqual(result['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual((self.new / 'australia-latest.osm.pbf').read_bytes(), content)

    def test_bad_download_never_gets_promoted(self):
        with patch.object(node.urllib.request, 'urlopen', side_effect=[Response(b'broken'), Response(b'0' * 32)]):
            with self.assertRaisesRegex(RuntimeError, 'MD5 mismatch'):
                node.download_map(self.new)
        self.assertFalse((self.new / 'australia-latest.osm.pbf').exists())
        self.assertEqual((self.root / 'current').resolve(), self.old)

    def test_successful_activation_keeps_previous_release(self):
        with patch.object(node, 'run'), patch.object(node, 'wait_healthy', return_value={'version': '3.8.3'}), patch.object(node, 'matrices', return_value={'matrix': []}):
            node.activate(self.new, {'version': '3.8.3'}, {'matrix': []})
        self.assertEqual((self.root / 'current').resolve(), self.new)
        self.assertEqual((self.root / 'previous').resolve(), self.old)

    def test_failed_activation_restores_old_release(self):
        failure = RuntimeError('new maps not healthy')
        with patch.object(node, 'run') as run, patch.object(node, 'wait_healthy', side_effect=[failure, {'version': '3.8.3'}]):
            with self.assertRaisesRegex(RuntimeError, 'new maps not healthy'):
                node.activate(self.new, {}, {})
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertEqual(sum(call.args == ('systemctl', 'restart', node.SERVICE) for call in run.call_args_list), 2)

    def test_bad_matrix_after_start_also_rolls_back(self):
        with patch.object(node, 'run'), patch.object(node, 'wait_healthy', return_value={'version': '3.8.3'}), patch.object(node, 'matrices', return_value={'wrong': []}):
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                node.activate(self.new, {'version': '3.8.3'}, {'expected': []})
        self.assertEqual((self.root / 'current').resolve(), self.old)

    def test_failed_first_install_leaves_no_active_release(self):
        (self.root / 'current').unlink()
        with patch.object(node, 'run') as run, patch.object(node, 'wait_healthy', side_effect=RuntimeError('not healthy')):
            with self.assertRaises(RuntimeError):
                node.activate(self.new, {}, {})
        self.assertFalse((self.root / 'current').is_symlink())
        run.assert_any_call('systemctl', 'stop', node.SERVICE)

    def test_invalid_matrix_is_rejected(self):
        cell = {'from_index': 0, 'to_index': 0, 'time': 0, 'distance': 0}
        self.assertEqual(node.check_matrix({'sources_to_targets': [[cell]]}, 1), [[cell]])
        for key, value in [('time', None), ('time', -1), ('distance', float('nan')), ('to_index', 1)]:
            bad = dict(cell, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                node.check_matrix({'sources_to_targets': [[bad]]}, 1)
        with self.assertRaises(ValueError):
            node.check_matrix({'sources_to_targets': []}, 1)

    def test_failed_candidate_does_not_switch_active_maps(self):
        directory = self.old / 'custom_files'
        directory.mkdir()
        original = json.loads((Path(__file__).resolve().parents[1] / 'valhalla.json').read_text())
        node.write_json(directory / 'valhalla.json', original)
        commands = []

        def fake_run(*args, **kwargs):
            commands.append(args)
            if args[:2] == ('docker', 'run'):
                mount = args[args.index('--mount') + 1]
                built = Path(mount.split('src=', 1)[1].split(',', 1)[0])
                self.assertEqual(json.loads((built / 'valhalla.json').read_text())['mjolnir']['max_cache_size'], 134217728)
                self.assertIn('--memory=1g', args)
                self.assertIn('--memory-swap=1g', args)
                self.assertIn('server_threads=1', args)
                (built / 'valhalla_tiles.tar').write_bytes(b'')
                with (built / 'valhalla_tiles.tar').open('r+b') as stream:
                    stream.truncate(1_000_001)
                for filename in ('admins.sqlite', 'timezones.sqlite', 'default_speeds.json'):
                    (built / filename).write_text('fixture')
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([{'State': {'OOMKilled': False, 'ExitCode': 0}}]))

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(node.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 50 * 1024**3})()))
            stack.enter_context(patch.object(node, 'check_port'))
            stack.enter_context(patch.object(node, 'download_map', return_value={'md5': 'fixture'}))
            stack.enter_context(patch.object(node, 'remove_stopped'))
            stack.enter_context(patch.object(node, 'run', side_effect=fake_run))
            stack.enter_context(patch.object(node.subprocess, 'run'))
            stack.enter_context(patch.object(node, 'validate_candidate', side_effect=RuntimeError('matrix failed')))
            with self.assertRaisesRegex(RuntimeError, 'matrix failed'):
                node.update()
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertEqual(json.loads((directory / 'valhalla.json').read_text()), original)
        self.assertFalse(any(command[0] == 'systemctl' for command in commands))


if __name__ == '__main__':
    unittest.main()
