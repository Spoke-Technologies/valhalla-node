"""Rebuild only the affected matrix object against the pinned image's installed SDK."""
import hashlib
from pathlib import Path
import shlex
import shutil
import subprocess
import urllib.request

ROOT = Path('/build')
SOURCE = ROOT / 'upstream'
REV = 'a60c7cbfc83e073f50887cd27e0109d02e6b64e5'
FILES = {
    'src/thor/costmatrix.cc': (f'valhalla/valhalla/{REV}/src/thor/costmatrix.cc', '9d9da786de91a2e52544e74bd4e1552ee07a4e37ed3d80368bfcbb4aff2c4a7f'),
    'src/valhalla_service.cc': (f'valhalla/valhalla/{REV}/src/valhalla_service.cc', 'e2cdb345190d2f427a1b51269bd8c0cced6034c6f0c2baf765c773e0568fc404'),
    'src/argparse_utils.h': (f'valhalla/valhalla/{REV}/src/argparse_utils.h', 'dc6f82bc3980290a53e6a69fdf11bc866b9b95de3120060f490e06106ccd0585'),
    'include/ankerl/unordered_dense.h': ('martinus/unordered_dense/73f3cbb237e84d483afafc743f1f14ec53e12314/include/ankerl/unordered_dense.h', 'b5f67c6895a58e059273cf24bae16cb07e6c72f0e3f88bbebecaddd987b102b3'),
    'include/cxxopts.hpp': ('jarro2783/cxxopts/63d1b65a694cfceafc20863afa75df49dfbe6b2a/include/cxxopts.hpp', '61ffa0e5b56ab602ef487e54724c094e57fa87f191ce3ed28a5f43ae5c3d79cc'),
}


def run(args):
    print(shlex.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, cwd=ROOT)


for path, (url, expected) in FILES.items():
    target = SOURCE / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen('https://raw.githubusercontent.com/' + url, timeout=60) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != expected:
        raise RuntimeError(f'Unexpected upstream source: {path}')
    target.write_bytes(content)

cflags = ['-std=c++20', '-O2', '-DNDEBUG', '-DENABLE_SERVICES', '-pthread',
          '-I' + str(SOURCE / 'include'), '-I/usr/local/include/valhalla']
cflags += shlex.split(subprocess.check_output(['pkg-config', '--cflags', 'libvalhalla'], text=True))
packages = ['libvalhalla', 'libczmq', 'spatialite', 'sqlite3', 'luajit', 'geos', 'openssl', 'libcurl', 'libtiff-4', 'liblz4']
libs = [x for x in shlex.split(subprocess.check_output(['pkg-config', '--libs', *packages], text=True)) if x != '-lvalhalla']
libs += ['-l:libgeotiff.so.5', '-pthread']
run(['g++', *cflags, '-c', SOURCE / 'src/valhalla_service.cc', '-o', 'service.o'])
for variant in ('baseline', 'fixed'):
    if variant == 'fixed':
        run(['patch', '--batch', '-d', SOURCE, '-p1', '-i', ROOT / 'engine/costmatrix-complex-restriction.patch'])
    run(['g++', *cflags, '-c', SOURCE / 'src/thor/costmatrix.cc', '-o', 'costmatrix.cc.o'])
    library = ROOT / f'libvalhalla-{variant}.a'
    shutil.copyfile('/usr/local/lib/libvalhalla.a', library)
    run(['ar', 'r', library, 'costmatrix.cc.o'])
    run(['g++', 'service.o', library, *libs, '-o', ROOT / f'valhalla_service-{variant}'])
