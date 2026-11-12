"""Physical, append-only collector slices. Provenance belongs to evaluation only."""
import hashlib
import json
import math
import os
from pathlib import Path

SOURCES = ('application', 'postgresql')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(application: Path, database: Path) -> dict:
    if application.is_symlink() or database.is_symlink() or not database.is_dir():
        raise ValueError('Invalid/symlink collector path')
    paths = {'application': [application], 'postgresql': sorted(database.glob('*.json'))}
    result = {}
    identities = set()
    for source, files in paths.items():
        result[source] = {}
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise ValueError('Missing or symlink collector file')
            with path.open('rb') as stream:
                before = os.fstat(stream.fileno());data = stream.read();after = os.fstat(stream.fileno())
            current=path.stat()
            if (current.st_dev,current.st_ino)!=(after.st_dev,after.st_ino):
                raise ValueError('Collector replaced during snapshot')
            if (before.st_dev,before.st_ino,before.st_size) != (after.st_dev,after.st_ino,after.st_size) or len(data)!=after.st_size:
                raise ValueError('Collector changed during snapshot')
            if data and not data.endswith(b'\n'):
                raise ValueError('Partial collector line at boundary')
            identity = (after.st_dev,after.st_ino)
            if identity in identities:
                raise ValueError('Duplicate collector file identity')
            identities.add(identity)
            result[source][str(path)] = {'device':after.st_dev,'inode':after.st_ino,
                'size':len(data),'sha256':digest(data)}
    return result


def export_ranges(before: dict, after: dict, directory: Path) -> dict:
    """Recheck prefix/identity and copy original bytes, without UTC filtering."""
    manifest = {'version':1,'sources':{}}
    for source in SOURCES:
        start,end = before[source],after[source]
        if not set(start)<=set(end):
            raise ValueError('Collector disappeared during run')
        chunks = [];parts = [];offset = 0;count = 0
        for name, last in sorted(end.items()):
            path = Path(name)
            if path.is_symlink() or not path.is_file():
                raise ValueError('Collector path changed before export')
            st = path.stat();data = path.read_bytes()
            current=path.stat()
            if ((st.st_dev,st.st_ino,len(data)) != (last['device'],last['inode'],last['size'])
                    or (current.st_dev,current.st_ino,current.st_size)!=(st.st_dev,st.st_ino,len(data))
                    or digest(data)!=last['sha256']):
                raise ValueError('Unexpected collector write/replacement after boundary')
            first = start.get(name)
            lo = first['size'] if first else 0
            if first and ((first['device'],first['inode'])!=(last['device'],last['inode']) or len(data)<lo or digest(data[:lo])!=first['sha256']):
                raise ValueError('Collector replaced, truncated or earlier bytes changed')
            part = data[lo:]
            if part and not part.endswith(b'\n'):
                raise ValueError('Partial collector range')
            for line in part.splitlines():
                if not isinstance(json.loads(line),dict):
                    raise ValueError('Collector JSON line is not an object')
            parts.append(part);count += len(part.splitlines())
            chunks.append({'path':name,'device':last['device'],'inode':last['inode'],
                'start':lo,'end':len(data),'prefix_sha256':first['sha256'] if first else digest(b''),
                'end_prefix_sha256':last['sha256'],'range_sha256':digest(part),
                'output_start':offset,'output_end':offset+len(part),'new_file':first is None})
            offset += len(part)
        payload = b''.join(parts)
        destination = directory/(source+'.jsonl')
        with destination.open('xb') as stream:
            stream.write(payload)
        manifest['sources'][source] = {'before':start,'after':end,'ranges':chunks,
            'bytes':len(payload),'records':count,'sha256':digest(payload)}
    return manifest


def verify_ranges(manifest: dict, directory: Path) -> dict[str,int]:
    """Independently validate retained slice identity, partitions and checksums."""
    if manifest.get('version')!=1 or set(manifest.get('sources',{}))!=set(SOURCES):
        raise ValueError('Missing collector range manifest')
    counts = {}
    for source in SOURCES:
        saved = manifest['sources'][source];path = directory/(source+'.jsonl')
        if path.is_symlink() or not path.is_file():
            raise ValueError('Invalid diagnostic export file')
        data = path.read_bytes();position = 0
        if saved['bytes']!=len(data) or saved['sha256']!=digest(data):
            raise ValueError('Diagnostic slice checksum/length mismatch')
        before,after = saved['before'],saved['after']
        if not set(before)<=set(after) or [r['path'] for r in saved['ranges']]!=sorted(after):
            raise ValueError('Missing/duplicate collector ranges')
        for r in saved['ranges']:
            initial = before.get(r['path']);end = after[r['path']]
            if (r['device'],r['inode'],r['end'],r['end_prefix_sha256'])!=(end['device'],end['inode'],end['size'],end['sha256']):
                raise ValueError('Collector end identity mismatch')
            if r['new_file']!=(initial is None) or r['start']!=(initial['size'] if initial else 0):
                raise ValueError('Collector start offset mismatch')
            if initial and ((r['device'],r['inode'])!=(initial['device'],initial['inode']) or r['prefix_sha256']!=initial['sha256']):
                raise ValueError('Collector prefix identity mismatch')
            if initial is None and r['prefix_sha256']!=digest(b''):
                raise ValueError('New collector prefix must be empty')
            if not 0<=r['start']<=r['end'] or r['output_start']!=position or r['output_end']-position!=r['end']-r['start']:
                raise ValueError('Incomplete/overlapping physical slice partition')
            part = data[position:r['output_end']]
            if digest(part)!=r['range_sha256'] or (part and not part.endswith(b'\n')):
                raise ValueError('Invalid/partial collector range')
            for line in part.splitlines():json.loads(line)
            position = r['output_end']
        counts[source] = len(data.splitlines())
        if position!=len(data) or counts[source]!=saved['records']:
            raise ValueError('Unreconciled collector export')
    return counts


def verify_nonoverlap(manifests: list[dict]) -> None:
    ends = {};identities = {}
    for manifest in manifests:
        for source in SOURCES:
            for r in manifest['sources'][source]['ranges']:
                key = (source,r['path']);identity = (r['device'],r['inode'])
                if key in identities and identities[key]!=identity:
                    raise ValueError('Collector identity changed across collection')
                if r['start']<ends.get(key,0):
                    raise ValueError('Overlapping physical collector ranges')
                identities[key]=identity;ends[key]=r['end']


def verify_collection_ranges(root: Path, current: dict) -> None:
    """Reject cohort overlap before this run's validity is saved or next case starts."""
    path=root/'manifest.json'
    if not path.is_file():return
    previous=[]
    for attempt in json.loads(path.read_text())['attempts']:
        if not attempt.get('run_id'):continue
        ground=json.loads((root/attempt['run_id']/'evaluation/ground_truth.json').read_text())
        if 'collector_ranges' in ground:previous.append(ground['collector_ranges'])
    verify_nonoverlap([*previous,current])


def verify_representation(directory: Path, monotonic_seconds: float) -> dict:
    from fixture.core import timestamp
    from fixture.scheduling import bucket
    from datetime import datetime
    occupied = set()
    if not math.isfinite(monotonic_seconds) or monotonic_seconds<=0:
        raise ValueError('Invalid monotonic observation duration')
    for source in SOURCES:
        for line in (directory/(source+'.jsonl')).read_text().splitlines():
            row = json.loads(line);value = timestamp(row['timestamp'])
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('Diagnostic timestamp lacks timezone')
            occupied.add(bucket(value.isoformat()))
    if not occupied:raise ValueError('Missing diagnostic observations')
    first,last = map(datetime.fromisoformat,(min(occupied),max(occupied)))
    n = int((last-first).total_seconds()/30)+1
    limit = math.ceil(monotonic_seconds/30)+2
    interior = len(occupied-{min(occupied),max(occupied)})
    if n>limit or interior<2:
        raise ValueError('Diagnostic grid exceeds representation budget or lacks two nonempty interiors')
    return {'observed_windows':n,'maximum_windows':limit,'nonempty_interior_windows':interior}


def verify_request_links(directory: Path, rows: list[dict]) -> dict:
    records = [json.loads(s) for s in (directory/'application.jsonl').read_text().splitlines()]
    if len(records)!=len(rows):raise ValueError('Application count differs from tracked requests')
    by_id = {}
    for r in records:
        if r['request_id'] in by_id:raise ValueError('Duplicate application request identity')
        by_id[r['request_id']]=r
    seen = set();unknown = 0
    for r in rows:
        rid = r.get('request_id')
        if rid is None:unknown += 1;continue
        if rid in seen or rid not in by_id:raise ValueError('Missing/duplicate returned request ID')
        seen.add(rid);record = by_id[rid]
        if (record['method'],record['route'],record['status'])!=(r['method'],r['path'],r['status']):
            raise ValueError('Request identity evidence mismatch')
    return {'linked':len(seen),'unknown':unknown,'requests':len(rows)}
