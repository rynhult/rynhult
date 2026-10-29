"""The owner importer authenticates every OCI blob before Podman sees it."""
import gzip
import hashlib
import importlib.util
import io
import json
import posixpath
import tarfile
from pathlib import Path

import pytest


def verifier():
    spec = importlib.util.spec_from_file_location('image_archive', Path(__file__).parents[1] / 'deploy/containers/verify_image_archive.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def layer_tar(names):
    """'x/' a folder, 'x -> y' a symlink, 'x => y' a hard link, otherwise an empty file."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as layer:
        for name in names:
            name, arrow, target = name.partition(' -> ') if ' -> ' in name else name.partition(' => ')
            info = tarfile.TarInfo(name)
            info.type = (tarfile.SYMTYPE if arrow == ' -> ' else tarfile.LNKTYPE if arrow else
                         tarfile.DIRTYPE if name.endswith('/') else tarfile.REGTYPE)
            info.linkname = target
            layer.addfile(info, io.BytesIO(b''))
    return stream.getvalue()


def slim_rootfs():
    spec = importlib.util.spec_from_file_location('slim_rootfs', Path(__file__).parents[1] / 'deploy/containers/slim_rootfs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mount_target_names():
    """What slim_rootfs.mount_targets() leaves in the runtime tree, from its own constants: every image ships these."""
    slim = slim_rootfs()
    folders = set()
    for name in (*slim.MOUNT_DIRECTORIES, *(posixpath.dirname(n) for n in (*slim.MOUNT_FILES, *slim.MOUNT_LINKS))):
        while name:
            folders.add(name)
            name = posixpath.dirname(name)
    return ([f + '/' for f in sorted(folders)] + list(slim.MOUNT_FILES)
            + [f'{name} -> {target}' for name, target in slim.MOUNT_LINKS.items()])


MOUNT_TARGETS = mount_target_names()


def archive_of(tmp_path, layers, gzipped=False):
    blobs = {}

    def blob(data):
        digest = hashlib.sha256(data).hexdigest()
        blobs['blobs/sha256/' + digest] = data
        return {'digest': 'sha256:' + digest, 'size': len(data)}

    diff_ids = ['sha256:' + hashlib.sha256(layer).hexdigest() for layer in layers]
    if gzipped:
        descriptors = [{**blob(gzip.compress(layer, mtime=0)), 'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip'}
                       for layer in layers]
    else:
        descriptors = [blob(layer) for layer in layers]
    config = blob(json.dumps({'rootfs': {'type': 'layers', 'diff_ids': diff_ids}}).encode())
    blobs['index.json'] = json.dumps({'manifests': [blob(json.dumps({'config': config, 'layers': descriptors}).encode())]}).encode()
    archive = tmp_path / 'image.tar'
    with tarfile.open(archive, 'w') as stream:
        for name, data in blobs.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest(), config['digest']


def test_archive_verifies_layers_and_config(tmp_path):
    module = verifier()
    blobs = {}

    def blob(data):
        digest = hashlib.sha256(data).hexdigest()
        blobs['blobs/sha256/' + digest] = data
        return {'digest': 'sha256:' + digest, 'size': len(data)}

    layer = blob(layer_tar([*MOUNT_TARGETS, 'usr/', 'usr/bin/', 'usr/bin/true']))
    config = blob(json.dumps({'rootfs': {'type': 'layers', 'diff_ids': [layer['digest']]}}).encode())
    manifest = blob(json.dumps({'config': config, 'layers': [layer]}).encode())
    blobs['index.json'] = json.dumps({'manifests': [manifest]}).encode()
    archive = tmp_path / 'image.tar'

    def write():
        with tarfile.open(archive, 'w') as stream:
            for name, data in blobs.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                stream.addfile(info, io.BytesIO(data))

    write()
    module.verify(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), config['digest'])
    blobs['blobs/sha256/' + layer['digest'][7:]] = b'evil bytes!'
    write()
    with pytest.raises(ValueError):
        module.verify(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), config['digest'])


# Qualification attempt 10: the final Containerfile stage's RUN committed run/.wh..containerenv, deleting the read-only
# rootfs's mount target (crun: open rootfs/run/.containerenv: No such file or directory).
ATTEMPT_10_FINAL_LAYER = ['run/', 'run/.wh..containerenv', 'tmp/']


@pytest.mark.parametrize('names,deleted', [
    (ATTEMPT_10_FINAL_LAYER, '/run/.containerenv'),
    (['etc/', 'etc/.wh.hosts'], '/etc/hosts'),
    (['etc/', 'etc/.wh.mtab'], '/etc/mtab'),                                   # MOUNT_LINKS
    (['srv/acc/', 'srv/acc/.wh.workspaces'], '/srv/acc/workspaces'),          # MOUNT_DIRECTORIES
    (['run/', 'run/.wh.acc-container'], '/run/acc-container, /run/acc-container/contract.json'),  # a folder above
    (['./run/', './run/.wh..wh..opq'], '/run/.containerenv'),                  # an opaque folder hides them all
])
def test_a_layer_that_deletes_a_mount_target_is_refused_naming_it(tmp_path, names, deleted):
    module = verifier()
    archive, checksum, image = archive_of(tmp_path, [layer_tar(MOUNT_TARGETS), layer_tar(names)])
    with pytest.raises(ValueError) as refused:
        module.verify(archive, checksum, image)
    message = str(refused.value)
    assert message.startswith('layer 2 deletes or replaces mount targets the read-only rootfs needs: ') and deleted in message


def test_other_whiteouts_are_not_mount_targets(tmp_path):
    module = verifier()
    layer = layer_tar(['usr/', 'usr/.wh.games', 'etc/', 'etc/.wh.hostsfile', 'opt/acc/', 'opt/acc/.wh..wh..opq'])
    archive, checksum, image = archive_of(tmp_path, [layer_tar(MOUNT_TARGETS), layer])
    assert module.verify(archive, checksum, image) == image


# Review of 73cc94e7 (M1): whiteouts were the only check, so a later layer could replace a target or a folder above it.
@pytest.mark.parametrize('names,lost', [
    (['run -> /tmp'], '/run/.containerenv'),                                     # a folder above becomes a link
    (['etc -> /srv/acc/workspaces'], '/etc/hosts'),
    (['run'], '/run/acc-container/contract.json'),                               # ... or a file
    (['etc/', 'etc/hosts -> /proc/self/environ'], '/etc/hosts'),                 # a file target becomes a link
    (['run/', 'run/.containerenv/'], '/run/.containerenv'),                      # ... or a folder
    (['etc/', 'etc/resolv.conf => usr/bin/true'], '/etc/resolv.conf'),           # ... or a hard link
    (['tmp -> /dev/shm'], '/tmp'),                                               # a folder target becomes a link
    (['etc/', 'etc/mtab -> /proc/1/mounts'], '/etc/mtab'),                       # a link target points elsewhere
    (['./run//', './run//.wh..containerenv'], '/run/.containerenv'),            # spelling does not hide a whiteout
    (['.wh..wh..opq'], '/proc'),                                                 # an opaque root hides everything
])
def test_a_layer_that_replaces_a_mount_target_or_a_folder_above_it_is_refused(tmp_path, names, lost):
    module = verifier()
    archive, checksum, image = archive_of(tmp_path, [layer_tar([*MOUNT_TARGETS, 'usr/', 'usr/bin/', 'usr/bin/true']),
                                                     layer_tar(names)])
    with pytest.raises(ValueError, match='layer 2 deletes or replaces mount targets') as refused:
        module.verify(archive, checksum, image)
    assert lost in str(refused.value)


def test_a_whiteout_then_re_adding_the_target_in_a_later_layer_still_counts_as_lost(tmp_path):
    module = verifier()
    archive, checksum, image = archive_of(tmp_path, [layer_tar(MOUNT_TARGETS), layer_tar(['run/', 'run/.wh..containerenv']),
                                                     layer_tar(['run/', 'run/.containerenv'])])
    with pytest.raises(ValueError, match='layer 2 deletes or replaces'):
        module.verify(archive, checksum, image)


def test_an_image_without_a_mount_target_is_refused(tmp_path):
    module = verifier()
    archive, checksum, image = archive_of(tmp_path, [layer_tar([t for t in MOUNT_TARGETS if t != 'run/.containerenv'])])
    with pytest.raises(ValueError, match=r'lacks mount targets .*: /run/\.containerenv$'):
        module.verify(archive, checksum, image)


def test_layers_that_add_inside_targets_or_unrelated_links_pass(tmp_path):
    module = verifier()
    archive, checksum, image = archive_of(tmp_path, [layer_tar(MOUNT_TARGETS), layer_tar(
        ['run/', 'run/acc-container/', 'run/acc-container/extra', 'usr/', 'usr/lib -> /opt', 'srv/acc/bin -> /usr/local/bin'])])
    assert module.verify(archive, checksum, image) == image


@pytest.mark.parametrize('gap', [b'\xff' * 512, b'\0' * 512], ids=['bad-header', 'lone-zero-block'])
def test_a_whiteout_behind_a_header_tarfile_stops_at_is_refused(tmp_path, gap):
    """tarfile ends quietly at a bad header or a lone zero block; the whiteout behind it must not go unseen."""
    module = verifier()
    folder = layer_tar(['usr/'])[:512]  # one header block (an empty folder), without the end-of-archive blocks
    layer = folder + gap + layer_tar(['run/', 'run/.wh..containerenv'])
    archive, checksum, image = archive_of(tmp_path, [layer_tar(MOUNT_TARGETS), layer])
    with pytest.raises(ValueError, match='data after an unreadable header'):
        module.verify(archive, checksum, image)


@pytest.mark.parametrize('names,error', [
    (['usr/', 'usr/../run/.wh..containerenv'], '".."'),
    (['usr/', './usr/'], 'duplicate'),
    (['run -> /tmp', 'run/.containerenv'], 'below a non-directory'),
    (['run/', 'run/.wh..containerenv', 'run/.containerenv'], 'both deletes and adds'),
])
def test_layers_an_importer_could_read_differently_are_refused(tmp_path, names, error):
    module = verifier()
    archive, checksum, image = archive_of(tmp_path, [layer_tar(MOUNT_TARGETS), layer_tar(names)])
    with pytest.raises(ValueError, match=error):
        module.verify(archive, checksum, image)


# Review follow-up (L4): root runs the verifier, so slim_rootfs.py's constants are parsed, never executed.
def test_mount_targets_are_parsed_from_literals_without_running_slim_rootfs(tmp_path):
    module = verifier()
    marker = tmp_path / 'executed'
    source = tmp_path / 'slim_rootfs.py'
    source.write_text(f"open({str(marker)!r}, 'w').close()\n"
                      "MOUNT_DIRECTORIES = ('run', 'tmp')\nMOUNT_FILES = ('run/.containerenv',)\n"
                      "MOUNT_LINKS = {'etc/mtab': '../proc/self/mounts'}\nSTICKY = ('tmp',)\n")
    assert module.mount_targets(source) == {'run': ('dir', None), 'tmp': ('dir', None), 'run/.containerenv': ('file', None),
                                            'etc/mtab': ('symlink', '../proc/self/mounts')}
    assert not marker.exists()
    for bad, error in [("MOUNT_FILES = tuple(['run/.containerenv'])", 'not a literal'),
                       ("MOUNT_FILES = ['run/.containerenv']", 'tuples of strings'),
                       ("MOUNT_FILES = ('run/.containerenv',)\nMOUNT_FILES = ()", 'assigned twice')]:
        source.write_text("MOUNT_DIRECTORIES = ('run',)\nMOUNT_LINKS = {}\nSTICKY = ()\n" + bad + "\n")
        with pytest.raises(ValueError, match=error):
            module.mount_targets(source)
    # The real file still parses, to the targets the layout tests pin.
    assert module.mount_targets()['run/.containerenv'] == ('file', None)


def entry(name, kind=tarfile.REGTYPE, mode=0o644):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as layer:
        info = tarfile.TarInfo(name)
        info.type, info.mode = kind, mode
        if kind == tarfile.SYMTYPE:
            info.linkname = '/usr/bin/true'
        layer.addfile(info, io.BytesIO(b''))
    return stream.getvalue()


# Review follow-up (L5): what root must never import, whatever the reviewed build did.
@pytest.mark.parametrize('name,kind,mode,refused', [
    ('dev/sda', tarfile.BLKTYPE, 0o660, 'device node'),
    ('opt/acc/tty', tarfile.CHRTYPE, 0o666, 'device node'),
    ('opt/acc/pipe', tarfile.FIFOTYPE, 0o644, 'special file'),
    ('usr/bin/su', tarfile.REGTYPE, 0o4755, 'set-id'),
    ('usr/lib/shared/', tarfile.DIRTYPE, 0o2755, 'set-id'),
    ('etc/profile', tarfile.REGTYPE, 0o666, 'world-writable'),
    ('opt/acc/drop/', tarfile.DIRTYPE, 0o1777, 'world-writable'),   # sticky, but not /tmp
    ('tmp/', tarfile.DIRTYPE, 0o777, 'world-writable'),             # /tmp without the sticky bit
    ('tmp/', tarfile.DIRTYPE, 0o1777, None),                         # slim_rootfs.STICKY, as it ships them
    ('var/tmp/', tarfile.DIRTYPE, 0o1777, None),
    ('var/tmp/', tarfile.DIRTYPE, 0o777, 'world-writable'),         # ... but only at exactly 1777
    ('var/tmp/x/', tarfile.DIRTYPE, 0o1777, 'world-writable'),      # ... and only those folders
    ('usr/bin/sh', tarfile.SYMTYPE, 0o777, None),                    # link modes mean nothing
])
def test_device_nodes_set_id_and_world_writable_entries_are_refused(tmp_path, name, kind, mode, refused):
    module = verifier()
    archive, checksum, image = archive_of(tmp_path, [layer_tar(MOUNT_TARGETS), entry(name, kind, mode)])
    if refused is None:
        assert module.verify(archive, checksum, image) == image
    else:
        with pytest.raises(ValueError, match=f'layer entry refused \\(.*{refused}.*\\): {name.rstrip("/")}$'):
            module.verify(archive, checksum, image)


@pytest.mark.parametrize('gzipped', [True, False], ids=['gzip', 'uncompressed'])
def test_layers_larger_than_the_cap_are_refused_before_parsing(tmp_path, monkeypatch, gzipped):
    module = verifier()
    base = layer_tar(MOUNT_TARGETS)
    big = io.BytesIO()
    with tarfile.open(fileobj=big, mode='w') as layer:
        info = tarfile.TarInfo('opt/zeros')
        info.size = 1 << 20
        layer.addfile(info, io.BytesIO(bytes(info.size)))  # compresses to about 1 KiB
    archive, checksum, image = archive_of(tmp_path, [base, big.getvalue()], gzipped=gzipped)
    assert module.verify(archive, checksum, image) == image
    monkeypatch.setattr(module, 'MAX_LAYER_BYTES', len(base) + (1 << 19))
    with pytest.raises(ValueError, match=r'layers decompress to more than 0 MiB in total'):
        module.verify(archive, checksum, image)


def rootfs_layer(root):
    """A layer of `root` as an image build writes one: every entry relative, the root folder itself left out."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as layer:
        for path in sorted(root.rglob('*')):
            layer.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    return stream.getvalue()


def test_the_verifier_accepts_what_slim_rootfs_builds_and_refuses_only_the_attempt_10_whiteout(tmp_path):
    """Attempt 10's real archive was refused for var/tmp (world-writable): the verifier allowed only tmp while
    slim_rootfs ships STICKY = (tmp, var/tmp) at 1777. Build the mount targets with slim_rootfs itself so the two cannot
    drift again, then add attempt 10's final-stage layer."""
    slim = slim_rootfs()
    root = tmp_path / 'rootfs'
    (root / 'etc').mkdir(parents=True)
    slim.mount_targets(root)
    slim.normalize(root)
    assert slim.unsafe_entries(root) == []  # what the build itself accepts
    assert slim.STICKY and all((root / name).stat().st_mode & 0o7777 == 0o1777 for name in slim.STICKY)
    module = verifier()
    base = rootfs_layer(root)
    archive, checksum, image = archive_of(tmp_path, [base])
    assert module.verify(archive, checksum, image) == image
    archive, checksum, image = archive_of(tmp_path, [base, layer_tar(ATTEMPT_10_FINAL_LAYER)])
    with pytest.raises(ValueError) as refused:
        module.verify(archive, checksum, image)
    assert str(refused.value) == ('layer 2 deletes or replaces mount targets the read-only rootfs needs: '
                                  '/run/.containerenv (a RUN in the final Containerfile stage?)')


def test_sticky_folders_come_from_slim_rootfs_not_a_second_list(tmp_path):
    module = verifier()
    assert set(module.rootfs_constants()['STICKY']) == set(slim_rootfs().STICKY)
    source = tmp_path / 'slim_rootfs.py'
    source.write_text("MOUNT_DIRECTORIES = ('tmp',)\nMOUNT_FILES = ()\nMOUNT_LINKS = {}\n")
    with pytest.raises(ValueError, match='STICKY'):
        module.rootfs_constants(source)  # no STICKY: refused, not a fallback
