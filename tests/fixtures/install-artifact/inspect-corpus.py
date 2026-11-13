"""Stdlib-only isolated inspector runner for the selected accd/broker Python."""
import hashlib
import importlib.util
import json
import os
import sys
import tarfile
from pathlib import Path

module_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('qualified_install_artifact', module_path)
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)
request = json.load(sys.stdin)
results = []
for candidate in request['cases']:
    try:
        inspected, _ = artifact.inspect(Path(candidate['file']).read_bytes(), request['metadata'])
        results.append({'id': candidate['id'], 'accepted': True, 'manifest': inspected['manifest']})
    except artifact.InvalidArtifact as error:
        results.append({'id': candidate['id'], 'accepted': False, 'reason': error.reason})


def file_hash(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


print(json.dumps({'interpreter': {'executable': sys.executable, 'realpath': os.path.realpath(sys.executable),
    'sha256': file_hash(sys.executable), 'version': sys.version, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
    'isolated': sys.flags.isolated, 'uid': os.geteuid(), 'tarfile_path': tarfile.__file__,
    'tarfile_sha256': file_hash(tarfile.__file__)},
    'inspector': {'path': str(module_path), 'sha256': file_hash(module_path)}, 'results': results}))
