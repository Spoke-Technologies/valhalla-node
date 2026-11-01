import contextlib
import fcntl
import hashlib
import io
import json
from pathlib import Path
import subprocess
import signal
import shutil
import sys
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
        self.assertFalse((self.root / 'activation.json').exists())

    def interrupted_activation(self, first_install=False):
        if first_install:
            (self.root / 'current').unlink()
        (self.new / 'custom_files').mkdir(exist_ok=True)
        (self.new / 'custom_files/valhalla_tiles.tar').touch()
        program = '''import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import node
node.ROOT = Path(sys.argv[2])
def interrupted(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGTERM)
node.run = interrupted
node.activate(node.ROOT / 'releases/new', {}, {})
'''
        result = subprocess.run([sys.executable, '-c', program, str(Path(node.__file__).parent), str(self.root)])
        self.assertEqual(result.returncode, -signal.SIGTERM)
        self.assertTrue((self.root / 'activation.json').exists())

    def boot_recovery(self):
        program = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import node
node.ROOT = Path(sys.argv[2])
node.os.geteuid = lambda: 0
sys.argv = ['node.py', '_recover']
node.main()
'''
        return subprocess.run([sys.executable, '-c', program, str(Path(node.__file__).parent), str(self.root)], capture_output=True)

    def test_termination_preserves_fallback_and_boot_recovers_history(self):
        earlier = self.root / 'releases/earlier'
        earlier.mkdir()
        node.set_link('previous', earlier)
        (self.old / 'custom_files').mkdir()
        (self.old / 'custom_files/valhalla_tiles.tar').touch()
        self.interrupted_activation()
        self.assertEqual((self.root / 'current').resolve(), self.new)
        self.assertEqual((self.root / 'previous').resolve(), self.old)
        self.assertEqual(self.boot_recovery().returncode, 0)
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertEqual((self.root / 'previous').resolve(), earlier)
        self.assertFalse((self.root / 'activation.json').exists())
        self.assertFalse(node.recover_activation())

    def test_boot_recovery_skips_an_activation_that_still_holds_the_lock(self):
        self.interrupted_activation()
        with (self.root / 'update.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.boot_recovery().returncode, 0)
        self.assertEqual((self.root / 'current').resolve(), self.new)
        self.assertTrue((self.root / 'activation.json').exists())

    def test_interrupted_first_install_stays_stopped_after_recovery(self):
        self.interrupted_activation(first_install=True)
        self.assertEqual(self.boot_recovery().returncode, 1)
        self.assertFalse((self.root / 'current').is_symlink())
        self.assertFalse((self.root / 'activation.json').exists())

    def test_rollback_recovers_interruption_without_rolling_back_twice(self):
        self.interrupted_activation()
        with patch.object(sys, 'argv', ['node.py', 'rollback']), patch.object(node.os, 'geteuid', return_value=0), patch.object(node, 'run') as run, patch.object(node, 'wait_healthy'), patch.object(node, 'validate_candidate') as validate, contextlib.redirect_stdout(io.StringIO()):
            node.main()
        self.assertEqual((self.root / 'current').resolve(), self.old)
        run.assert_called_once_with('systemctl', 'restart', node.SERVICE)
        validate.assert_not_called()

    def test_failed_activation_restores_existing_previous_pointer(self):
        earlier = self.root / 'releases/earlier'
        earlier.mkdir()
        node.set_link('previous', earlier)
        with patch.object(node, 'run'), patch.object(node, 'wait_healthy', side_effect=[RuntimeError('failed'), {}]):
            with self.assertRaises(RuntimeError):
                node.activate(self.new, {}, {})
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertEqual((self.root / 'previous').resolve(), earlier)
        self.assertFalse((self.root / 'activation.json').exists())

    @unittest.skipUnless(shutil.which('flock'), 'The installer requires Linux flock')
    def test_installer_lock_blocks_mutation_during_an_update(self):
        source = (Path(node.__file__).parent / 'setup.sh').read_text()
        guard = source.split('exec 9>/opt/valhalla-node/update.lock', 1)[1].split('export DEBIAN_FRONTEND', 1)[0]
        guard = 'exec 9>"$1/update.lock"\n' + guard + '\nprintf installed > "$1/mutated"\n'
        with (self.root / 'update.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = subprocess.run(['bash', '-ec', guard, 'setup-test', str(self.root)], capture_output=True)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertFalse((self.root / 'mutated').exists())
        allowed = subprocess.run(['bash', '-ec', guard, 'setup-test', str(self.root)], capture_output=True)
        self.assertEqual(allowed.returncode, 0)
        self.assertEqual((self.root / 'mutated').read_text(), 'installed')

    def test_invalid_status_fields_are_rejected(self):
        good = {'version': '3.8.3', 'tileset_last_modified': 123}
        self.assertEqual(node.validate_status(good), good)
        for invalid in ({}, [], {'version': '3.8.3'}, dict(good, version='other'),
                        dict(good, tileset_last_modified=0), dict(good, tileset_last_modified=True),
                        dict(good, tileset_last_modified='123')):
            with self.subTest(status=invalid), self.assertRaises(ValueError):
                node.validate_status(invalid)

    def run_public_probe(self, responder):
        source = (Path(node.__file__).parent / 'cloudflare-setup.sh').read_text()
        program = source.split('python3 - "$NODE_HOSTNAME" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        output = io.StringIO()
        with patch.object(sys, 'argv', ['probe', 'node.example']), patch.object(sys, 'path', sys.path.copy()), patch.object(node, 'request_json', side_effect=responder), patch.object(node.time, 'sleep'), contextlib.redirect_stdout(output):
            exec(compile(program, 'cloudflare-public-probe', 'exec'), {})
        return output.getvalue()

    @staticmethod
    def public_response(base_url, path, payload=None):
        if path == 'status':
            return {'version': '3.8.3', 'tileset_last_modified': 123}
        count = len(payload['sources'])
        return {'sources_to_targets': [[{
            'from_index': source, 'to_index': target,
            'time': 0 if source == target else 5,
            'distance': 0 if source == target else 1,
        } for target in range(count)] for source in range(count)]}

    def test_public_probe_rejects_empty_local_status(self):
        with self.assertRaisesRegex(ValueError, 'healthy Valhalla'):
            self.run_public_probe(lambda *args: {})

    def test_public_probe_rejects_empty_remote_status(self):
        def response(base_url, path, payload=None):
            return {} if base_url.startswith('https:') else self.public_response(base_url, path, payload)
        with self.assertRaisesRegex(ValueError, 'healthy Valhalla'):
            self.run_public_probe(response)

    def test_public_probe_rejects_different_remote_matrices(self):
        def response(base_url, path, payload=None):
            result = self.public_response(base_url, path, payload)
            if base_url.startswith('https:') and path != 'status':
                result['sources_to_targets'][0][1]['time'] = 99
            return result
        with self.assertRaisesRegex(ValueError, 'matrices differ'):
            self.run_public_probe(response)

    def test_public_probe_accepts_matching_valid_matrices(self):
        self.assertIn('Public endpoint passed', self.run_public_probe(self.public_response))

    def test_https_matrix_request_retains_url_and_payload(self):
        with patch.object(node.urllib.request, 'urlopen', return_value=io.BytesIO(b'{}')) as fetch:
            node.request_json('https://node.example', 'sources_to_targets', {'costing': 'bus'})
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, 'https://node.example/sources_to_targets')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), {'costing': 'bus'})

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
