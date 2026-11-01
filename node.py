#!/usr/bin/env python3
"""Australia-only Valhalla installation and staged map updates; stdlib only."""
import argparse
import copy
import datetime
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path('/opt/valhalla-node')
LIB = Path('/usr/local/lib/valhalla-node')
IMAGE = 'ghcr.io/valhalla/valhalla-scripted@sha256:24ef7955899dececb94e26c6dfb89d64fabfae875f980432694b0261eb6c251b'
MAP_URL = 'https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf'
MIN_MAP_BYTES = 100_000_000
SERVICE = 'valhalla-node.service'
BUILD = 'spoke-valhalla-build'
VERIFY = 'spoke-valhalla-verify'
AREAS = {
    'Brisbane': [(-27.4698, 153.0251), (-27.4990, 152.9740), (-27.3843, 153.0323), (-27.4810, 153.0220)],
    'Gold Coast': [(-28.0015, 153.4280), (-28.0720, 153.4189), (-27.9678, 153.4138)],
    'Sydney': [(-33.8688, 151.2093), (-33.8151, 151.0010), (-33.8977, 151.1792)],
    'Melbourne': [(-37.8136, 144.9631), (-37.8182, 144.9983), (-37.8000, 144.9000)],
}


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def digest(path):
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def download_map(directory):
    partial = directory / 'australia-latest.osm.pbf.part'
    md5, sha = hashlib.md5(), hashlib.sha256()
    with urllib.request.urlopen(MAP_URL, timeout=120) as response, partial.open('wb') as output:
        effective_url = response.geturl()
        parsed = urllib.parse.urlparse(effective_url)
        if parsed.scheme != 'https' or parsed.hostname != 'download.geofabrik.de':
            raise RuntimeError('Unexpected map download origin')
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            output.write(chunk)
            md5.update(chunk)
            sha.update(chunk)
    # Use the resolved dated URL so a midnight latest-file rollover fails safely.
    with urllib.request.urlopen(effective_url + '.md5', timeout=30) as response:
        checksum = response.read(4096).decode().split()[0].lower()
    if not re.fullmatch(r'[0-9a-f]{32}', checksum) or checksum != md5.hexdigest():
        raise RuntimeError('Geofabrik MD5 mismatch; current maps have not been changed. Retry the update.')
    if partial.stat().st_size < MIN_MAP_BYTES:
        raise RuntimeError('Australia download is unexpectedly small')
    partial.rename(directory / 'australia-latest.osm.pbf')
    return {'url': effective_url, 'md5': checksum, 'sha256': sha.hexdigest(),
            'downloaded_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}


def request_json(port, path, payload=None):
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/{path}',
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=45 if payload else 3) as response:
        return json.load(response)


def wait_healthy(port):
    for attempt in range(120):
        try:
            status = request_json(port, 'status')
            if status.get('version') == '3.8.3' and status.get('tileset_last_modified', 0) > 0:
                return status
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(f'Valhalla on port {port} did not become healthy')


def check_matrix(data, count):
    rows = data['sources_to_targets']
    if len(rows) != count or any(len(row) != count for row in rows):
        raise ValueError('Incomplete routing matrix')
    for source, row in enumerate(rows):
        for target, cell in enumerate(row):
            if cell.get('from_index') != source or cell.get('to_index') != target:
                raise ValueError('Incorrect matrix indices')
            for key in ('time', 'distance'):
                value = cell.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError('Unreachable or invalid matrix result')
                if source == target and value != 0:
                    raise ValueError('Nonzero matrix diagonal')
    return rows


def matrices(port):
    results = {}
    for area, coordinates in AREAS.items():
        locations = [{'lat': lat, 'lon': lon} for lat, lon in coordinates]
        for costing in ('auto', 'bus'):
            data = request_json(port, 'sources_to_targets', {
                'sources': locations, 'targets': locations, 'costing': costing, 'units': 'kilometers',
            })
            results[f'{area}/{costing}'] = check_matrix(data, len(locations))
    return results


def check_port(port):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))


def container_args(name, directory):
    return ['docker', 'run', '--name', name, '--restart=no',
            '--env-file', str(LIB / 'valhalla.env'),
            '--mount', f'type=bind,src={directory},dst=/custom_files']


def remove_stopped(name):
    result = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True)
    if result.returncode == 0:
        if json.loads(result.stdout)[0]['State']['Running']:
            raise RuntimeError(f'{name} is already running; inspect it before retrying')
        run('docker', 'rm', name, stdout=subprocess.DEVNULL)


def validate_candidate(release, config):
    directory = release / 'custom_files'
    remove_stopped(VERIFY)
    try:
        run(*container_args(VERIFY, directory), '-d', '--cpus=1', '--memory=512m',
            '--memory-swap=512m', '-p', '127.0.0.1:18002:8002', IMAGE, 'build_tiles',
            stdout=subprocess.DEVNULL)
        health = wait_healthy(18002)
        results = matrices(18002)
        state = json.loads(run('docker', 'inspect', VERIFY, capture_output=True, text=True).stdout)[0]['State']
        if state['OOMKilled'] or not state['Running']:
            raise RuntimeError('Candidate container failed')
        if json.loads((directory / 'valhalla.json').read_text()) != config:
            raise RuntimeError('Container changed the routing config')
        return health, results
    finally:
        logs = subprocess.run(['docker', 'logs', VERIFY], capture_output=True)
        (release / 'validation.log').write_bytes(logs.stdout + logs.stderr)
        subprocess.run(['docker', 'rm', '-f', VERIFY], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_link(name, target):
    temporary = ROOT / (name + '.new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(ROOT / name)


def activate(release, expected_health, expected_matrices):
    current = ROOT / 'current'
    old = current.resolve() if current.is_symlink() else None
    try:
        set_link('current', release)
        run('systemctl', 'enable', SERVICE, stdout=subprocess.DEVNULL)
        run('systemctl', 'restart', SERVICE)
        if wait_healthy(8002) != expected_health or matrices(8002) != expected_matrices:
            raise RuntimeError('Activated service differs from validated candidate')
    except BaseException:
        if old:
            set_link('current', old)
            run('systemctl', 'restart', SERVICE)
            wait_healthy(8002)
            logging.error('Activation failed; previous maps restored')
        else:
            run('systemctl', 'stop', SERVICE)
            current.unlink(missing_ok=True)
        raise
    if old and old != release:
        set_link('previous', old)


def update():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / 'update.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(ROOT).free < 20 * 1024**3:
            raise RuntimeError('Need at least 20 GiB free disk space for a staged Australia build')
        current_config = ROOT / 'current/custom_files/valhalla.json'
        config_path = current_config if current_config.exists() else LIB / 'valhalla.json'
        original_hash = digest(config_path)
        config = json.loads(config_path.read_text())
        if not current_config.exists():
            check_port(8002)
        check_port(18002)
        run('docker', 'pull', IMAGE)
        name = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        release = ROOT / 'releases' / name
        directory = release / 'custom_files'
        directory.mkdir(parents=True)
        logging.info('Building release %s; existing service stays online until activation', name)
        source = download_map(directory)
        write_json(release / 'source.json', source)
        build_config = copy.deepcopy(config)
        build_config['mjolnir']['max_cache_size'] = 134217728
        build_config['mjolnir']['concurrency'] = '1'
        write_json(directory / 'valhalla.json', build_config)
        speeds = ROOT / 'current/custom_files/default_speeds.json'
        if speeds.exists():
            shutil.copy2(speeds, directory / speeds.name)
        remove_stopped(BUILD)
        try:
            with (release / 'build.log').open('wb') as log:
                logging.info('Build output: %s', release / 'build.log')
                run(*container_args(BUILD, directory), '--cpus=1', '--memory=1g', '--memory-swap=1g',
                    '--env', 'serve_tiles=False', '--env', 'use_tiles_ignore_pbf=False',
                    '--env', 'server_threads=1', IMAGE, 'build_tiles', stdout=log, stderr=subprocess.STDOUT)
            state = json.loads(run('docker', 'inspect', BUILD, capture_output=True, text=True).stdout)[0]['State']
            if state['OOMKilled'] or state['ExitCode'] != 0:
                raise RuntimeError('Map build failed or exceeded memory limit')
        finally:
            subprocess.run(['docker', 'rm', '-f', BUILD], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        archive = directory / 'valhalla_tiles.tar'
        if not archive.exists() or archive.stat().st_size < 1_000_000:
            raise RuntimeError('Missing or incomplete routing tiles')
        for required in ('admins.sqlite', 'timezones.sqlite', 'default_speeds.json'):
            if not (directory / required).is_file() or (directory / required).stat().st_size == 0:
                raise RuntimeError(f'Missing required build data: {required}')
        write_json(directory / 'valhalla.json', config)
        logging.info('Testing eight auto/bus matrix cases across QLD, NSW and VIC')
        health, results = validate_candidate(release, config)
        if digest(config_path) != original_hash:
            raise RuntimeError('Active configuration changed during build; refusing to overwrite it')
        report = {'source': source, 'image': IMAGE, 'health': health, 'matrices': results,
                  'archive_sha256': digest(archive), 'activated': False}
        write_json(release / 'verification.json', report)
        activate(release, health, results)
        report['activated'] = True
        write_json(release / 'verification.json', report)
        logging.info('Update passed and activated: %s. Previous maps retained.', name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['update', 'status', 'test', 'rollback', 'cloudflare', 'image', '_update'])
    command = parser.parse_args().command
    if command == 'image':
        print(IMAGE)
        return
    if command == 'test':
        print(json.dumps(matrices(8002), indent=2))
        return
    if command == 'status':
        print('Current:', (ROOT / 'current').resolve())
        print('Previous:', (ROOT / 'previous').resolve())
        print(json.dumps(request_json(8002, 'status'), indent=2))
        return
    if os.geteuid() != 0:
        parser.error('Run this command with sudo')
    if command == 'cloudflare':
        run('bash', LIB / 'cloudflare-setup.sh')
    elif command == 'update':
        run('systemd-run', '--unit=valhalla-map-update', '--collect', '--no-block',
            '--property=Type=oneshot', '/usr/bin/python3', LIB / 'node.py', '_update')
        print('Update started; it continues if SSH disconnects. Follow: sudo journalctl -fu valhalla-map-update')
        print('Detailed build logs: /opt/valhalla-node/releases/<release>/build.log')
    elif command == 'rollback':
        with (ROOT / 'update.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = ROOT / 'previous'
            if not previous.is_symlink():
                raise RuntimeError('No previous release to restore')
            release = previous.resolve()
            config = json.loads((release / 'custom_files/valhalla.json').read_text())
            check_port(18002)
            health, results = validate_candidate(release, config)
            activate(release, health, results)
            print('Previous release restored and tested')
    else:
        update()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        main()
    except Exception:
        logging.exception('Command failed')
        sys.exit(1)
