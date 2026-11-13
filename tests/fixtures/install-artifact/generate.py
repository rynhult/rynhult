"""Reconstruct B1 and B2 review scenarios; no package code is executed."""
import gzip
import json
import runpy
import tarfile
from pathlib import Path

PACKAGE = {'name': '@reviewed/sample', 'version': '1.2.3', 'bin': 'cli.js'}


def entry(name, data=b'', *, format=tarfile.USTAR_FORMAT):
    header = tarfile.TarInfo(name)
    header.size = len(data)
    return header.tobuf(format=format) + data + bytes(-len(data) % 512)


def checksum(header):
    header[148:156] = b'        '
    header[148:156] = f'{sum(header):06o}\0 '.encode()
    return header


def build():
    root = Path(__file__).parent
    prefix = entry('package/package.json', json.dumps(PACKAGE).encode())
    invalid = bytearray(tarfile.TarInfo('bad-header').tobuf(format=tarfile.USTAR_FORMAT))
    invalid[0] ^= 1  # Deliberately leave checksum unchanged.
    malicious = entry('package/cli.js', b'malicious\n')
    cases = {
        'bad-checksum-duplicate.tgz': prefix + malicious + invalid + entry('package/cli.js', b'benign\n'),
        'bad-checksum-hidden.tgz': prefix + malicious + invalid + entry('package/hidden.js', b'hidden\n'),
    }
    gnu = bytearray(entry('package/x.js', b'x', format=tarfile.GNU_FORMAT))
    gnu[345:355] = b'other/evil'
    gnu[:512] = checksum(gnu[:512])
    cases['gnu-prefix.tgz'] = (entry('package/package.json', json.dumps(PACKAGE).encode(), format=tarfile.GNU_FORMAT)
                                + entry('package/cli.js', b'cli', format=tarfile.GNU_FORMAT) + gnu)
    corpus = runpy.run_path(str(root.parents[1] / 'install_artifact_corpus.py'))
    b2_cases = corpus['b2_cases']
    (root / 'n1-pax-path-directory.tgz').write_bytes(corpus['n1_archive']())
    for name, blob in b2_cases().items():
        (root / f'{name}.tgz').write_bytes(blob)
    for name, raw in cases.items():
        (root / name).write_bytes(gzip.compress(raw + bytes(1024), mtime=0))


if __name__ == '__main__':
    build()
