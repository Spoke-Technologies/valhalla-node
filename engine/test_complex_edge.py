"""A tiny reproducible red/green test: preserve restricted turns, fix same-edge paths."""
import json
from pathlib import Path
import subprocess

ROOT = Path('/build')
fixture = ROOT / 'fixture'
fixture.mkdir(exist_ok=True)
config = json.loads((ROOT / 'seed-config.json').read_text())
config['mjolnir'].update(tile_dir=str(fixture / 'tiles'), tile_extract='', admin='', timezone='',
                          traffic_extract='', default_speeds_config='', concurrency='1',
                          id_table_size=1000, max_cache_size=33554432, hierarchy=False, shortcuts=False)
config['mjolnir']['data_processing']['use_admin_db'] = False
config['loki']['use_connectivity'] = False
config['loki']['service_defaults']['minimum_reachability'] = 1
config['thor']['clear_reserved_memory'] = True
(fixture / 'config.json').write_text(json.dumps(config))
subprocess.run(['osmium', 'cat', str(ROOT / 'engine/complex-restriction.osm'), '-o', str(fixture / 'map.osm.pbf'), '--overwrite'], check=True)
subprocess.run(['valhalla_build_tiles', '-c', str(fixture / 'config.json'), str(fixture / 'map.osm.pbf')], check=True)

points = {
    'P': {'lat': -27.5, 'lon': 153.0013}, 'Q': {'lat': -27.5, 'lon': 153.0017},
    'A': {'lat': -27.5, 'lon': 153.0}, 'D': {'lat': -27.499, 'lon': 153.002},
}


def action(variant, action, payload):
    result = subprocess.run([str(ROOT / f'valhalla_service-{variant}'), str(fixture / 'config.json'), action, json.dumps(payload)],
                            check=True, capture_output=True, text=True, timeout=15)
    return json.loads(result.stdout)


def matrix(variant, costing, source, target):
    data = action(variant, 'sources_to_targets', {'sources': [points[source]], 'targets': [points[target]], 'costing': costing})
    assert data['algorithm'] == 'costmatrix'
    return data['sources_to_targets'][0][0]


for costing in ('auto', 'bus'):
    located = action('baseline', 'locate', {'locations': [points['P']], 'costing': costing, 'verbose': True})
    assert located[0]['edges'][0]['edge']['part_of_complex_restriction']
    before = matrix('baseline', costing, 'P', 'P')
    after = matrix('fixed', costing, 'P', 'P')
    assert before['time'] > 0 and before['distance'] > .4, before
    assert after['time'] == 0 and after['distance'] == 0, after
    before = matrix('baseline', costing, 'P', 'Q')
    after = matrix('fixed', costing, 'P', 'Q')
    assert before['distance'] > .4 and 0 < after['distance'] < .05, (before, after)
    for source, target, minimum_distance in [('Q', 'P', .3), ('A', 'D', .6)]:
        before = matrix('baseline', costing, source, target)
        after = matrix('fixed', costing, source, target)
        assert before == after and after['distance'] > minimum_distance, (source, target, before, after)
    data = action('fixed', 'sources_to_targets', {'sources': [points['P'], points['Q']],
                  'targets': [points['Q'], points['P'], points['P']], 'costing': costing})
    assert all(data['sources_to_targets'][0][i]['time'] == 0 and data['sources_to_targets'][0][i]['distance'] == 0 for i in (1, 2))
print('PASS: original engine reproduces the bug; patched auto/bus matrices fix same-edge trips and preserve one-way and complex-turn restrictions.')
