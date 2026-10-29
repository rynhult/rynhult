#!/usr/bin/python3 -I
"""Verify a reviewed single-image OCI archive before root imports it. Never extracts files."""
import ast
import hashlib
import io
import json
import posixpath
import re
import sys
import tarfile
import zlib
from pathlib import Path

WHITEOUT, OPAQUE = '.wh.', '.wh..wh..opq'
MAX_LAYER_BYTES = 4 << 30  # all layers together, uncompressed; the session image is about 1.5 GiB


CONSTANTS = ('MOUNT_DIRECTORIES', 'MOUNT_FILES', 'MOUNT_LINKS', 'STICKY')


def rootfs_constants(source=None):
    """slim_rootfs's mount targets and sticky folders (beside this file in the reviewed checkout). Root runs this, so
    the file is parsed, never executed: each constant must be one plain literal assignment."""
    source = Path(__file__).with_name('slim_rootfs.py') if source is None else source
    found = {}
    for node in ast.parse(source.read_text(), filename=str(source)).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in CONSTANTS):
            if node.targets[0].id in found:
                raise ValueError(f'{node.targets[0].id} is assigned twice in {source.name}')
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                raise ValueError(f'{node.targets[0].id} in {source.name} is not a literal') from None
    directories, files, links, sticky = (found.get(name) for name in CONSTANTS)
    if not (isinstance(directories, tuple) and isinstance(files, tuple) and isinstance(links, dict)
            and isinstance(sticky, tuple)
            and all(isinstance(n, str) for n in (*directories, *files, *links, *links.values(), *sticky))):
        raise ValueError(f'{source.name} must assign MOUNT_DIRECTORIES, MOUNT_FILES and STICKY (tuples of strings) and '
                         'MOUNT_LINKS (a dict of strings) once each')
    return found


def mount_targets(source=None):
    """Each mount target with what crun needs it to be on the read-only rootfs: ('dir', None), ('file', None) or
    ('symlink', its exact target)."""
    found = rootfs_constants(source)
    return {**{name: ('dir', None) for name in found['MOUNT_DIRECTORIES']},
            **{name: ('file', None) for name in found['MOUNT_FILES']},
            **{name: ('symlink', target) for name, target in found['MOUNT_LINKS'].items()}}


def gunzip(data, room):
    """Decompress every gzip member, refusing to produce more than `room` bytes."""
    out = bytearray()
    while data:
        stream = zlib.decompressobj(wbits=31)
        while not stream.eof:
            chunk = stream.decompress(data, room - len(out) + 1)
            out += chunk
            if len(out) > room:
                raise ValueError(f'layers decompress to more than {MAX_LAYER_BYTES >> 20} MiB in total')
            data = stream.unconsumed_tail
            if not chunk and not data:
                raise ValueError('truncated gzip layer')
        data = stream.unused_data
    return bytes(out)


def unsafe(member, path, sticky):
    """What root must not import: device nodes and other special files, set-id bits, and world-writable entries other
    than slim_rootfs's STICKY folders at exactly 1777, which a tmpfs covers at run time (slim_rootfs.unsafe_entries
    refuses the same at build)."""
    if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
        return 'device node or special file'
    if member.issym():
        return None  # a link's mode is meaningless
    if member.mode & 0o6000:
        return 'set-id bit'
    if member.mode & 0o002 and not (path in sticky and member.isdir() and member.mode & 0o7777 == 0o1777):
        return 'world-writable'
    return None


def layer_entries(data, sticky=()):
    """(path, kind, link target) per member. Refuses what an importer could read differently: '..', duplicates, an
    entry below a non-directory of the same layer, and anything after a header tarfile stops at (tarfile ends quietly
    at a bad or single zero block, so a whiteout behind one would go unseen)."""
    entries, kinds = [], {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as layer:
        for member in layer:
            if '..' in member.name.split('/'):
                raise ValueError(f'layer entry with "..": {member.name}')
            path = posixpath.normpath('/' + member.name).lstrip('/')
            if not path or path in kinds:
                raise ValueError(f'duplicate or root layer entry: {member.name}')
            problem = unsafe(member, path, sticky)
            if problem:
                raise ValueError(f'layer entry refused ({problem}): {member.name}')
            kind = 'dir' if member.isdir() else 'file' if member.isreg() else 'symlink' if member.issym() else 'hardlink'
            kinds[path] = kind
            entries.append((path, kind, member.linkname if member.issym() else None))
        end = layer.offset
    if any(data[end:]):
        raise ValueError('layer has data after an unreadable header or a lone end-of-archive block')
    for path in kinds:
        folder = posixpath.dirname(path)
        while folder:
            if kinds.get(folder, 'dir') != 'dir':
                raise ValueError(f'layer entry below a non-directory: {path}')
            folder = posixpath.dirname(folder)
    return entries


def below(path, folder):
    return not folder or path.startswith(folder + '/')


def apply_layer(state, entries, targets):
    """Overlay one layer onto `state` (mount target -> (kind, link target)): whiteouts and opaque folders hide lower
    layers, a non-directory hides everything below it, and the layer's own entries win."""
    added = {path for path, _, _ in entries if not posixpath.basename(path).startswith(WHITEOUT)}
    for path, _, _ in entries:
        folder, name = posixpath.split(path)
        if name == OPAQUE:
            for target in [t for t in state if below(t, folder)]:
                del state[target]
        elif name.startswith(WHITEOUT):
            gone = posixpath.join(folder, name[len(WHITEOUT):])
            if gone in added:
                raise ValueError(f'layer both deletes and adds {gone}')
            for target in [t for t in state if t == gone or below(t, gone)]:
                del state[target]
    for path, kind, link in entries:
        if posixpath.basename(path).startswith(WHITEOUT):
            continue
        if kind != 'dir':
            for target in [t for t in state if below(t, path)]:
                del state[target]
        if path in targets:
            state[path] = (kind, link)


def check_mount_targets(layers, targets):
    """Every mount target must be what crun needs once all layers are applied. A RUN in the final Containerfile stage
    whited out run/.containerenv (qualification attempt 10); a later layer could equally replace one with a link or a
    file, or replace a folder above it."""
    state = {}
    for number, entries in enumerate(layers, 1):
        before = {t for t in targets if state.get(t) == targets[t]}
        apply_layer(state, entries, targets)
        lost = sorted(t for t in before if state.get(t) != targets[t])
        if lost:
            raise ValueError(f'layer {number} deletes or replaces mount targets the read-only rootfs needs: '
                             f'{", ".join("/" + t for t in lost)} (a RUN in the final Containerfile stage?)')
    missing = sorted(t for t in targets if state.get(t) != targets[t])
    if missing:
        raise ValueError('the image lacks mount targets the read-only rootfs needs, or has them as the wrong kind: '
                         + ', '.join('/' + t for t in missing))


def verify(path, checksum, image):
    with open(path, 'rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != checksum:
            raise ValueError('archive checksum mismatch')
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:') as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)) or any(not (m.isfile() or m.isdir()) for m in members):
                raise ValueError('duplicate or nonregular archive member')

            def read(name):
                member = archive.getmember(name)
                if not member.isfile():
                    raise ValueError('not a regular blob')
                return archive.extractfile(member).read()

            def blob(descriptor):
                digest = descriptor['digest']
                if not re.fullmatch('sha256:[0-9a-f]{64}', digest):
                    raise ValueError('unsupported digest')
                data = read('blobs/sha256/' + digest[7:])
                if len(data) != descriptor['size'] or hashlib.sha256(data).hexdigest() != digest[7:]:
                    raise ValueError('blob digest or size mismatch')
                return data

            index = json.loads(read('index.json'))
            if len(index['manifests']) != 1:
                raise ValueError('requires exactly one reviewed image')
            manifest = json.loads(blob(index['manifests'][0]))
            config = json.loads(blob(manifest['config']))
            if manifest['config']['digest'] != image:
                raise ValueError('image ID mismatch')
            diffs = config['rootfs']['diff_ids']
            if len(diffs) != len(manifest['layers']):
                raise ValueError('layer count mismatch')
            targets, sticky, layers, total = mount_targets(), set(rootfs_constants()['STICKY']), [], 0
            for layer, expected in zip(manifest['layers'], diffs):
                data = blob(layer)
                media = layer.get('mediaType', 'application/vnd.oci.image.layer.v1.tar')
                if media.endswith('+gzip'):
                    data = gunzip(data, MAX_LAYER_BYTES - total)
                elif media != 'application/vnd.oci.image.layer.v1.tar':
                    raise ValueError('unsupported layer compression; use uncompressed or gzip OCI')
                total += len(data)
                if total > MAX_LAYER_BYTES:
                    raise ValueError(f'layers decompress to more than {MAX_LAYER_BYTES >> 20} MiB in total')
                if 'sha256:' + hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError('uncompressed layer digest mismatch')
                layers.append(layer_entries(data, sticky))
            # Every layer is read before deciding; nothing is imported until verify() returns.
            check_mount_targets(layers, targets)
    return image


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('usage: verify_image_archive.py ARCHIVE ARCHIVE_SHA256 IMAGE_ID')
    print(verify(*sys.argv[1:]))
