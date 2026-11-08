import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from fixture.plans import validate_plan, plan_summary


def load(name):
    return json.loads((Path('plans') / (name + '.json')).read_text())


def test_complete_research_manifest_and_validation_separation():
    research, validation = load('research'), load('validation')
    summary = plan_summary(research)
    assert summary['runs'] == 42 and summary['healthy'] == 12
    assert summary['splits'] == {'train': 6, 'development': 13, 'test': 23}
    assert summary['scheduled_seconds'] == 12600
    assert plan_summary(validation)['scheduled_seconds'] == 1800
    assert {r['config']['seed'] for r in research['runs']}.isdisjoint(
        r['config']['seed'] for r in validation['runs'])
    for split in ('development', 'test'):
        categories = {r['config']['category'] for r in research['runs'] if r['split'] == split}
        for category in categories:
            rates = {r['config']['rate'] for r in research['runs']
                     if r['split'] == split and r['config']['category'] == category}
            assert {4, 6} <= rates  # Workload extremes are not unique to a category.


@pytest.mark.parametrize('change', ['count', 'faulty_training', 'duplicate_seed', 'duplicate_slot',
                                  'short', 'validation_split', 'attempt_limit'])
def test_plan_rejects_protocol_drift(change):
    plan = deepcopy(load('validation' if change == 'validation_split' else 'research'))
    rows = plan['runs']
    if change == 'count': rows.pop()
    elif change == 'faulty_training': rows[0]['config']['category'] = 'Database unavailable'
    elif change == 'duplicate_seed': rows[1]['config']['seed'] = rows[0]['config']['seed']
    elif change == 'duplicate_slot': rows[1]['slot_id'] = rows[0]['slot_id']
    elif change == 'short': rows[0]['config']['baseline'] = 3
    elif change == 'validation_split': rows[0]['split'] = 'development'
    elif change == 'attempt_limit': plan['max_attempts_per_slot'] = 100
    with pytest.raises(ValueError): validate_plan(plan)


def test_validation_plan_stops_on_invalid_and_preserves_attempt(tmp_path, monkeypatch):
    from fixture.cli import run_plan
    monkeypatch.chdir(tmp_path)
    plan = load_from_repository_validation()
    calls = []
    async def fake_experiment(cfg, root):
        calls.append(root)
        return root/'opaque', {'run_id': 'opaque', 'valid': False,
                              'invalidity_reasons': ['Recovery verification failed']}
    with patch('fixture.cli.experiment', side_effect=fake_experiment):
        assert asyncio.run(run_plan(plan)) == 1
    assert len(calls) == 1
    manifest = json.loads(next((tmp_path/'validation-runs').glob('*/manifest.json')).read_text())
    assert manifest['status'] == 'stopped'
    assert manifest['attempts'][0]['invalidity_reasons'] == ['Recovery verification failed']
    assert not (tmp_path/'research-runs').exists()


def load_from_repository_validation():
    return json.loads((Path(__file__).resolve().parents[1]/'plans/validation.json').read_text())
