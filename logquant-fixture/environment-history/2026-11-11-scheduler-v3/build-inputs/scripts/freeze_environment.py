"""Freeze locally built image identities; no credentials are inspected or saved."""
import hashlib
import json
from pathlib import Path
import subprocess


def output(args):
    return subprocess.check_output(args, text=True, timeout=60).strip()


images = {}
for service, name in [('app','logquant-fixture-app'),('harness','logquant-fixture-harness'),
                      ('db','postgres:17.2@sha256:3267c505060a0052e5aa6e5175a7b41ab6b04da2f8c4540fc6e98a37210aa2d3')]:
    info = json.loads(output(['docker','image','inspect',name]))[0]
    digests = info.get('RepoDigests',[])
    if not digests: raise RuntimeError('Image has no immutable local repository digest: '+service)
    images[service] = {'reference': digests[0], 'id': info['Id'],
                       'platform':info['Os']+'/'+info['Architecture']}
lock = {'schema_version':1,'images':images,
        'dependency_lock_sha256':hashlib.sha256(Path('requirements.lock').read_bytes()).hexdigest(),
        'python_base':'python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159',
        'docker_version':output(['docker','version','--format','{{.Server.Version}}']),
        'compose_version':output(['docker','compose','version','--short'])}
Path('environment.lock.json').write_text(json.dumps(lock,indent=2)+'\n')
Path('compose.lock.yaml').write_text('services:\n'+''.join(
    '  '+service+':\n    image: '+info['reference']+'\n' for service,info in images.items()))
print('Frozen image identities and dependency lock; fingerprint: '+hashlib.sha256(
    Path('environment.lock.json').read_bytes()).hexdigest())
