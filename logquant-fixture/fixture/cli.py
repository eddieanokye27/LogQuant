import argparse
import asyncio
from dataclasses import asdict
import fcntl
import json
from pathlib import Path
import signal
import sys
import uuid
from fixture.core import CATEGORIES, Config, save
from fixture.control import Control
from fixture.runner import experiment
from fixture.plans import validate_plan, plan_hash, plan_summary


def parser():
    p = argparse.ArgumentParser(description='LogQuant local controlled evaluation fixture')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('reset')
    run = sub.add_parser('run')
    run.add_argument('--category', choices=CATEGORIES)
    run.add_argument('--smoke', action='store_true')
    for name in ('baseline','exposure','recovery','rate','timeout','poll','verify_timeout'):
        run.add_argument('--'+name.replace('_','-'), type=float)
    run.add_argument('--seed',type=int)
    run.add_argument('--concurrency',type=int)
    batch = sub.add_parser('batch')
    batch.add_argument('config', type=Path)
    check = sub.add_parser('check-plan')
    check.add_argument('config', type=Path)
    sub.add_parser('integration')
    return p

async def run_batch(items, manifest_path):
    manifest = []
    # Validate the whole batch before modifying any fixture state.
    configs = [(Config(**item['config']),item.get('split','unassigned')) for item in items]
    if any(split not in ('train','development','test','unassigned') for _,split in configs):
        raise ValueError('Invalid evaluation split')
    if any(split=='train' and cfg.category for cfg,split in configs):
        raise ValueError('Training runs must be healthy for the proposed detector')
    for cfg, split in configs:
        directory, data = await experiment(cfg)
        manifest.append({'run_id':data['run_id'], 'split':split, 'valid':data['valid'], 'config':asdict(cfg)})
        save(manifest_path,manifest)
        print(f'{directory}: {"VALID" if data["valid"] else "INVALID"}', flush=True)
        if not data['valid']:
            return 1
    return 0

async def integration():
    # Real fault/recovery/evidence checks; not detector evaluation.
    from fixture.control import Control
    cases = [None]
    for category in CATEGORIES:
        cases.extend([category,None])
    for category in cases:
        directory,data = await experiment(Config(baseline=3,exposure=5,recovery=3,category=category))
        if not data['valid']:
            raise RuntimeError(f'Integration failed: {directory}: {data["invalidity_reasons"]}')
        print(f'PASS {category or "healthy"}: {directory}',flush=True)
    class NoOpInjection(Control):
        def inject(self, category):
            pass
    directory,data = await experiment(Config(baseline=3,exposure=5,recovery=3,category=CATEGORIES[3]), control=NoOpInjection())
    if data['valid'] or not data['invalidity_reasons']:
        raise RuntimeError('No-op injection was not rejected')
    print(f'PASS invalid injection recorded: {directory}')
    return 0


async def run_plan(payload):
    rows = validate_plan(payload)  # Reject the entire plan before state changes.
    collection = Path(payload['purpose'] + '-runs') / uuid.uuid4().hex
    collection.mkdir(parents=True)
    save(collection/'plan.json', payload)
    environment = Path('environment.lock.json')
    if environment.is_file():
        save(collection/'environment.lock.json', json.loads(environment.read_text()))
    manifest = {'schema_version': 1, 'purpose': payload['purpose'],
                'plan_sha256': plan_hash(payload), 'status': 'running', 'attempts': []}
    path = collection/'manifest.json'
    save(path, manifest)
    print(f'Collection: {collection}', flush=True)
    for item, cfg in rows:
        entry = {'slot_id': item['slot_id'], 'split': item['split'], 'attempt': 1,
                 'status': 'running', 'config': asdict(cfg)}
        manifest['attempts'].append(entry)
        save(path, manifest)  # Leave an explicit pending entry after hard interruption.
        try:
            directory, data = await experiment(cfg, root=collection)
        except BaseException:
            entry['status'] = 'incomplete'
            manifest['status'] = 'stopped'
            save(path, manifest)
            raise
        entry.update(run_id=data['run_id'], status='valid' if data['valid'] else 'invalid',
                     invalidity_reasons=data['invalidity_reasons'])
        manifest['status'] = 'running' if data['valid'] else 'stopped'
        save(path, manifest)
        print(f'{entry["status"].upper()} {directory}', flush=True)
        if not data['valid']:
            return 1  # Recovery/review is manual; never retry silently.
    manifest['status'] = 'complete'
    save(path, manifest)
    return 0

async def main_async(args):
    # SIGTERM follows the same cancellation/finalization path as Ctrl+C.
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    if args.command=='check-plan':
        print(json.dumps(plan_summary(json.loads(args.config.read_text())), indent=2))
        return 0
    if args.command=='reset':
        with Path('/evidence/run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            Control().reset()
        return 0
    if args.command=='integration':
        return await integration()
    if args.command=='batch':
        payload = json.loads(args.config.read_text())
        if 'purpose' in payload:
            return await run_plan(payload)
        return await run_batch(payload['runs'], Path('runs')/f'manifest-{uuid.uuid4().hex}.json')
    options = {k:v for k,v in vars(args).items() if k not in ('command','smoke') and v is not None}
    if args.smoke:
        for key,value in [('baseline',3),('exposure',5),('recovery',3)]:
            options.setdefault(key,value)
    directory,data = await experiment(Config(**options))
    print(f'{directory}: {"VALID" if data["valid"] else "INVALID"}')
    if not data['valid']:
        print(json.dumps(data['invalidity_reasons']))
    return 0 if data['valid'] else 1

if __name__=='__main__':
    try:
        sys.exit(asyncio.run(main_async(parser().parse_args())))
    except (Exception,KeyboardInterrupt) as exc:
        print(f'Fixture failed: {type(exc).__name__}: {exc}',file=sys.stderr)
        sys.exit(1)
