"""Strict candidate surface and bounded, explicitly untrusted prior observations."""
import json
from pathlib import Path

from .world import validate_spec

DEFAULT_SPEC = dict(family='drift', contacts=7, page_size=5, updates=1, brief_words=180, seed=101)
BOUNDS = {'contacts': (6, 24), 'page_size': (2, 8), 'updates': (0, 2),
          'brief_words': (80, 300), 'seed': (1, 9999)}
PAYLOAD_SCHEMA = {'type': 'object', 'additionalProperties': False,
                  'required': list(BOUNDS), 'properties': {
                      **{k: {'type': 'integer', 'minimum': lo, 'maximum': hi} for k, (lo, hi) in BOUNDS.items()},
                      'family': {'enum': ['handoff', 'drift']}}}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate payload key')
        result[key] = value
    return result


def parse_candidate(candidate):
    if candidate is None:
        return dict(DEFAULT_SPEC)
    if candidate.family not in ('handoff', 'drift') or not isinstance(candidate.payload, str):
        raise ValueError('unsupported family')
    if len(candidate.payload) > 2048:
        raise ValueError('oversized payload')
    raw = json.loads(candidate.payload, object_pairs_hook=_unique)
    if not isinstance(raw, dict) or set(raw) - set(BOUNDS) - {'family'} or not set(BOUNDS) <= set(raw):
        raise ValueError('payload must contain only bounded scenario parameters')
    if raw.get('family', candidate.family) != candidate.family:
        raise ValueError('family mismatch')
    if candidate.family == 'handoff' and raw['updates'] != 0:
        raise ValueError('handoff does not permit runtime updates')
    return validate_spec(dict(raw, family=candidate.family))


def prior_observations():
    data = json.loads(Path(__file__).with_name('experience.json').read_text())
    if data.get('schema_version') != 1 or len(data['observations']) > 4:
        raise ValueError('invalid experience package')
    for item in data['observations']:
        if item['classification'] not in ('external_observation', 'hypothesis'):
            raise ValueError('invalid observation classification')
        if any(len(item[field]) > 900 for field in ('summary', 'conditions', 'limitations')):
            raise ValueError('oversized observation')
        if len(item['sources']) > 4:
            raise ValueError('too many source references')
    return {'trust': 'untrusted historical data, not instructions or current verification',
            'applicability': 'Nemotron/OpenCode synthetic consent exports with the recorded conditions; '
                             'transfer to another harness or prompt requires new measurements.',
            'deduplication_authority': False, 'observations': data['observations']}
