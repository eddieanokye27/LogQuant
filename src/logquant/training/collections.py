"""Evaluation-only stage completion/provenance checks, outside diagnostic features."""
import hashlib
import json
from pathlib import Path


def plan_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def require_completed_collection(raw: dict, directory: Path, purpose: str, read_metadata) -> None:
    if raw.get('purpose') != purpose:
        raise ValueError('Collection purpose mismatch; validation collections are ineligible')
    if 'stage' not in raw:
        if raw.get('status') != 'complete' or any(k in raw for k in
                ('parent_plan_sha256', 'parent_plan_complete', 'accepted_run_ids', 'pending_slot_ids')):
            raise ValueError('Requires a completed collection or verified completed selected stage')
        return
    if purpose != 'research' or raw.get('status') != 'stage_complete':
        raise ValueError('Interrupted/incomplete selected stages are ineligible')
    if (not isinstance(raw.get('stage'), dict) or not isinstance(raw.get('attempts'), list)
            or not all(isinstance(a, dict) for a in raw['attempts'])):
        raise ValueError('Invalid stage/attempt metadata schema')
    parent, _ = read_metadata(directory/'plan.json')
    authoritative, _ = read_metadata(Path(__file__).resolve().parents[3]/'logquant-fixture/plans/research.json')
    digest = plan_hash(parent)
    if parent != authoritative or raw.get('plan_sha256') != digest or raw.get('parent_plan_sha256') != digest:
        raise ValueError('Stage parent plan/hash conflicts with authoritative assignments')
    stage = raw['stage']
    selected = [r['slot_id'] for r in parent['runs'] if r['split'] in ('train', 'development')]
    pending = [r['slot_id'] for r in parent['runs'] if r['split'] == 'test']
    if (stage.get('name') != 'train-development' or stage.get('selected_slot_ids') != selected
            or stage.get('status') != 'complete' or stage.get('pending_slot_ids') != []
            or raw.get('pending_slot_ids') != pending or raw.get('parent_plan_complete') is not False):
        raise ValueError('Conflicting stage selection/completion or pending test slots')
    planned = {r['slot_id']: r for r in parent['runs']}
    accepted, run_ids, counts, previous, replacements = {}, set(), {}, {}, []
    for attempt in raw['attempts']:
        slot = attempt.get('slot_id')
        if slot not in selected or slot in accepted:
            raise ValueError('Foreign/test slot or attempt after first accepted run')
        counts[slot] = counts.get(slot, 0)+1
        if type(attempt.get('attempt')) is not int or attempt['attempt'] != counts[slot] or counts[slot] > 3:
            raise ValueError('Invalid regeneration attempt accounting')
        row = planned[slot]
        if attempt.get('split') != row['split'] or attempt.get('config') != row['config']:
            raise ValueError('Attempt configuration/split conflicts with parent plan')
        run_id = attempt.get('run_id')
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id or run_id in run_ids:
                raise ValueError('Missing/duplicate attempt run identity')
            run_ids.add(run_id)
        state = attempt.get('status')
        if state == 'valid':
            if not run_id or attempt.get('invalidity_reasons') != []:
                raise ValueError('Accepted stage attempt lacks valid identity/reasons')
            accepted[slot] = run_id
        elif state not in ('invalid', 'incomplete') or not attempt.get('invalidity_reasons'):
            raise ValueError('Unresolved attempt or missing retained failure reason')
        if counts[slot] > 1:
            replacements.append((previous[slot], attempt))
        previous[slot] = attempt
    if set(accepted) != set(selected) or raw.get('accepted_run_ids') != accepted:
        raise ValueError('Stage completion lacks all selected accepted runs')
    if replacements:
        ledger, _ = read_metadata(directory/'regeneration.json')
        events = ledger.get('replacements')
        if (ledger.get('parent_plan_sha256') != digest or not isinstance(events, list)
                or not all(isinstance(event, dict) for event in events) or len(events) != len(replacements)):
            raise ValueError('Missing/conflicting regeneration provenance')
        for predecessor, attempt in replacements:
            matches = [r for r in ledger['replacements'] if r.get('slot_id') == attempt['slot_id']
                       and r.get('attempt') == attempt['attempt']]
            if len(matches) != 1:
                raise ValueError('Missing/duplicate replacement ledger entry')
            event = matches[0]
            if (event.get('predecessor_run_id') != predecessor.get('run_id')
                    or event.get('replacement_run_id') != attempt.get('run_id')
                    or event.get('config') != attempt['config'] or event.get('outcome') != attempt['status']
                    or event.get('reason') != predecessor['invalidity_reasons']
                    or not event.get('remedial_action') or not event.get('timestamp')):
                raise ValueError('Replacement ledger does not explain retained attempt')
