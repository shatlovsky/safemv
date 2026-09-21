#!/usr/bin/env python3
"""Verified copying and moving via rsync. Python 3.9+, macOS/Linux.

Only mv and rm delete sources, after successful verification. No replacement,
forced unmount, implicit sudo, or cleanup of failed copies.
See README.md for supported storage, approval steps and verification limits.
"""
import argparse
import datetime
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
import uuid


class Stop(Exception):
    pass


class ChecksumError(Stop):
    pass


def require(condition, message):
    if not condition:
        raise Stop(message)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def line(obj):
    # ASCII escapes also preserve undecodable Unix filename bytes (surrogateescape).
    return json.dumps(obj, ensure_ascii=True, sort_keys=True) + '\n'


def command(argv, pass_fds=()):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, pass_fds=pass_fds, check=False)
    if result.returncode:
        raise Stop('Command failed: ' + repr(argv) + '\n' +
                   (result.stderr or result.stdout).decode(errors='replace').strip())
    return result.stdout


def executable(name):
    result = shutil.which(name)
    require(result is not None, 'Required system command is missing: ' + name)
    return result


def absolute(value):
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def within(path, root):
    return path == root or root in path.parents


def clean_path(path):
    """Reject symlink components; allow standard macOS /var and /tmp aliases at CLI entry."""
    for part in [*reversed(path.parents), path]:
        require(not part.is_symlink(), 'Symlink path component refused: ' + repr(str(part)))


def canonical(value):
    path = absolute(value)
    # Resolve only the system aliases, not arbitrary user symlinks.
    for alias in ('/tmp', '/var', '/etc'):
        if platform.system() == 'Darwin' and within(path, Path(alias)):
            path = Path(alias).resolve() / path.relative_to(alias)
            break
    clean_path(path)
    # APFS firmlinks (e.g. /Users) are not symlinks; diskutil reports the Data mount.
    data_root = Path('/System/Volumes/Data')
    if platform.system() == 'Darwin' and data_root.is_dir() and not within(path, data_root):
        parent = existing_parent(path)
        candidate = data_root / str(parent).lstrip('/')
        if (parent.stat().st_dev == data_root.stat().st_dev and candidate.exists()
                and os.path.samefile(parent, candidate)):
            path = candidate / path.relative_to(parent)
    return path


def existing_parent(path):
    while not path.exists():
        require(path.parent != path, 'No existing ancestor: ' + str(path))
        path = path.parent
    return path


def signature(st):
    return dict(size=st.st_size, mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns,
                inode=st.st_ino, device=st.st_dev)


def digest_file(path):
    clean_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode), 'Not an ordinary file: ' + repr(str(path)))
        h = hashlib.sha256()
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                h.update(chunk)
        after = os.fstat(fd)
        require(signature(before) == signature(after) == signature(path.lstat()),
                'File changed while hashing: ' + repr(str(path)))
        return h.hexdigest(), signature(after)
    finally:
        os.close(fd)


def walk_source(root):
    clean_path(root)
    if root.is_file():
        return [], [root]
    require(root.is_dir(), 'Source must be an ordinary file or directory.')
    device = root.stat().st_dev
    directories, files = [], []

    def visit(directory):
        require(directory.stat().st_dev == device, 'Nested source mount refused: ' + str(directory))
        directories.append(directory)
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        for item in entries:
            p = Path(item.path)
            info = item.stat(follow_symlinks=False)
            require(not item.is_symlink(), 'Source symlink refused: ' + repr(str(p)))
            require(info.st_dev == device, 'Nested source mount refused: ' + repr(str(p)))
            if stat.S_ISDIR(info.st_mode):
                visit(p)
            elif stat.S_ISREG(info.st_mode):
                files.append(p)
            else:
                raise Stop('Special source file refused: ' + repr(str(p)))

    visit(root)
    return directories, files


def mac_volume(path):
    tool = executable('diskutil')
    mountpoint = existing_parent(path)
    while not os.path.ismount(mountpoint):
        require(mountpoint.parent != mountpoint, 'No mounted ancestor found.')
        mountpoint = mountpoint.parent
    info = plistlib.loads(command([tool, 'info', '-plist', str(mountpoint)]))
    require(info.get('MountPoint') and info.get('Mounted') is not False, 'Destination is not mounted.')
    require(isinstance(info.get('Internal'), bool), 'Cannot determine whether disk is external.')
    require(info.get('VolumeUUID'), 'Destination has no readable VolumeUUID.')
    require(not info.get('ReadOnlyVolume'), 'Destination volume is read-only.')
    require(not (info.get('VirtualOrPhysical') == 'Virtual' and not info['Internal']),
            'External virtual destination disk is unsupported.')
    return dict(os='Darwin', mount=str(Path(info['MountPoint'])), uuid=info['VolumeUUID'],
                device='/dev/' + info['DeviceIdentifier'], fstype=info.get('FilesystemType', ''),
                external=not info['Internal'])


def tree_nodes(rows):
    for row in rows:
        yield row
        yield from tree_nodes(row.get('children', []))


def linux_volume(path):
    data = json.loads(command([executable('findmnt'), '--json', '--target', str(path),
                               '--output', 'SOURCE,TARGET,FSTYPE,UUID,OPTIONS,FSROOT']))
    rows = data.get('filesystems', [])
    require(len(rows) == 1, 'Destination mount is ambiguous or missing.')
    row = rows[0]
    require(row.get('uuid') and row.get('source', '').startswith('/dev/'),
            'Only local block filesystems with a readable UUID are supported.')
    require(row.get('fsroot') == '/', 'Bind mounts and filesystem subvolumes are unsupported.')
    require('ro' not in row.get('options', '').split(','), 'Destination volume is read-only.')
    device = row['source']
    blocks = json.loads(command([executable('lsblk'), '--json', '--paths', '--inverse',
                                 '--output', 'NAME,TYPE,TRAN,HOTPLUG,RM', device]))
    nodes = list(tree_nodes(blocks.get('blockdevices', [])))
    require(nodes and all(n['type'] in ('disk', 'part') for n in nodes),
            'RAID, LVM, encrypted mapper and loop destinations are unsupported.')
    disks = [n for n in nodes if n['type'] == 'disk']
    require(len(disks) == 1, 'Cannot identify one physical destination disk.')
    disk = disks[0]
    if disk.get('tran') == 'usb':
        external = True
    elif disk.get('tran') in ('sata', 'ata', 'nvme', 'sas') and not disk.get('hotplug') and not disk.get('rm'):
        external = False
    else:
        raise Stop('External/internal status is uncertain; automatic execution refused.')
    return dict(os='Linux', mount=row['target'], uuid=row['uuid'], device=device,
                fstype=row['fstype'], options=row['options'], external=external)


def volume(path):
    clean_path(path)
    system = platform.system()
    require(system in ('Darwin', 'Linux'), 'Only macOS and Linux are supported.')
    result = mac_volume(path) if system == 'Darwin' else linux_volume(path)
    require(result['fstype'], 'Filesystem type is unknown.')
    mount = Path(result['mount'])
    require(os.path.ismount(mount), 'Destination mount disappeared.')
    require(within(path, mount), 'Path does not belong to the detected mount.')
    return result


def same_volume(expected, actual):
    for key in ('os', 'mount', 'uuid', 'fstype', 'external'):
        require(expected[key] == actual[key], 'Destination identity changed: ' + key)


def check_volume(header):
    dest = Path(header['destination_root'])
    actual = volume(existing_parent(dest))
    same_volume(header['volume'], actual)
    require(existing_parent(dest).stat().st_dev == Path(actual['mount']).stat().st_dev,
            'Destination is on an unexpected nested mount.')
    return actual


def check_manifest_location(path, source, dest, vol):
    clean_path(path)
    require(not within(path, source) and not within(path, dest),
            'Manifest/log must be outside source and destination directories.')
    require(path.parent.is_dir(), 'Manifest/log parent directory must already exist.')
    if vol['external']:
        require(path.parent.stat().st_dev != Path(vol['mount']).stat().st_dev,
                'Manifest/log must be on a different volume from the external destination.')


def separate_source(source, vol):
    if vol['external']:
        require(source.stat().st_dev != Path(vol['mount']).stat().st_dev,
                'Source is on the external destination volume; remount would interrupt it.')


def check_entries(header, entries):
    require(header.get('type') == 'plan' and header.get('version') == 1, 'Unknown manifest format.')
    source, dest = Path(header['source_root']), Path(header['destination_root'])
    require(source.is_absolute() and dest.is_absolute() and source != dest,
            'Manifest roots must be distinct absolute paths.')
    require(source == absolute(source) and dest == absolute(dest), 'Manifest roots are not normalized.')
    require(not within(dest, source) and not within(source, dest), 'Overlapping roots refused.')
    dirs, files, names, total = set(), set(), set(), 0
    for item in entries:
        require(item.get('type') in ('file', 'directory'), 'Unknown manifest entry.')
        src, dst = Path(item['source']), Path(item['destination'])
        require(src.is_absolute() and dst.is_absolute(), 'Manifest paths must be absolute.')
        require('..' not in src.parts and '..' not in dst.parts, 'Parent traversal refused.')
        require(within(src, source) and within(dst, dest), 'Entry escapes manifest roots.')
        require(src.relative_to(source) == dst.relative_to(dest), 'Source/destination mapping changed.')
        normalized = unicodedata.normalize('NFC', str(dst)).casefold()
        require(normalized not in names, 'Duplicate or case/Unicode filename collision: ' + repr(str(dst)))
        names.add(normalized)
        if item['type'] == 'directory':
            dirs.add(src)
        else:
            require(re.fullmatch('[0-9a-f]{64}', item['sha256']) is not None, 'Invalid SHA-256.')
            require(isinstance(item['size'], int) and item['size'] >= 0, 'Invalid file size.')
            for key in ('mtime_ns', 'ctime_ns', 'inode', 'device'):
                require(isinstance(item[key], int), 'Missing file signature: ' + key)
            files.add(src)
            total += item['size']
    if header.get('source_kind', 'directory') == 'file':
        require(not dirs and files == {source}, 'A file plan must contain exactly its source file.')
    else:
        require(source in dirs and all(p.parent in dirs for p in (dirs | files) if p != source),
                'Missing directory entries.')
    require(len(files) == header['files'] and len(dirs) == header['directories'] and total == header['bytes'],
            'Manifest is incomplete or its counts changed.')
    require(files or dirs, 'Empty manifest refused.')


def prepare(args, existing_destination=False):
    source, dest, manifest = map(canonical, (args.source, args.destination, args.manifest))
    if existing_destination:
        require(dest.exists(), 'Destination does not exist: ' + repr(str(dest)))
        require(not os.path.samefile(source, dest), 'Source and destination refer to the same object.')
        require(source.is_dir() == dest.is_dir() and source.is_file() == dest.is_file(),
                'Source and destination types do not match.')
    else:
        require(not os.path.lexists(dest), 'Destination root must not exist; choose a new batch directory.')
    require(not within(dest, source) and not within(source, dest), 'Overlapping roots refused.')
    vol = volume(existing_parent(dest))
    check_manifest_location(manifest, source, dest, vol)
    if not existing_destination:
        separate_source(source, vol)
    dirs, files = walk_source(source)
    entries = [dict(type='directory', source=str(p), destination=str(dest / p.relative_to(source))) for p in dirs]
    for index, path in enumerate(files, 1):
        print('Hashing {}/{}: {}'.format(index, len(files), repr(str(path))), flush=True)
        checksum, sig = digest_file(path)
        entries.append(dict(type='file', source=str(path), destination=str(dest / path.relative_to(source)),
                            sha256=checksum, **sig))
    header = dict(type='plan', version=1, id=str(uuid.uuid4()), created_utc=now(),
                  source_root=str(source), destination_root=str(dest), volume=vol,
                  source_kind='directory' if source.is_dir() else 'file',
                  files=len(files), directories=len(dirs), bytes=sum(x['size'] for x in entries if x['type'] == 'file'))
    if getattr(args, 'move', False):
        for item in entries:
            path = Path(item['source'])
            require(item['type'] != 'file' or path.stat().st_nlink == 1,
                    'Source removal does not support hard-linked files: ' + repr(str(path)))
            item['move_metadata'] = move_metadata(path)
            item['source_identity'] = [path.stat().st_dev, path.stat().st_ino]
    check_entries(header, entries)
    # Re-enumerate after hashing: a changing source must not silently yield an incomplete plan.
    check_source(header, entries, hashes=False)
    check_volume(header)
    data = (line(header) + ''.join(map(line, entries))).encode('utf-8')
    with manifest.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    print('Manifest: ' + str(manifest))
    print('Manifest SHA-256: ' + hashlib.sha256(data).hexdigest())
    print('Files: {}; bytes: {}; destination UUID: {}; external: {}'.format(
        header['files'], header['bytes'], vol['uuid'], vol['external']))
    print('No destination files were created.')


def load_manifest(path):
    clean_path(path)
    require(path.is_file(), 'Manifest is not an ordinary file.')
    data = path.read_bytes()
    rows = [json.loads(row) for row in data.decode('utf-8').splitlines()]
    require(rows, 'Empty manifest.')
    header, entries = rows[0], rows[1:]
    check_entries(header, entries)
    return header, entries, hashlib.sha256(data).hexdigest()


def check_source(header, entries, hashes):
    directories, files = walk_source(Path(header['source_root']))
    require(set(directories) == {Path(x['source']) for x in entries if x['type'] == 'directory'} and
            set(files) == {Path(x['source']) for x in entries if x['type'] == 'file'},
            'Source tree changed since manifest preparation.')
    for item in entries:
        if item['type'] != 'file':
            continue
        path = Path(item['source'])
        require(signature(path.lstat()) == {key: item[key] for key in signature(path.lstat())},
                'Source metadata changed: ' + repr(str(path)))
        if hashes:
            checksum, _ = digest_file(path)
            if checksum != item['sha256']:
                raise ChecksumError('Source SHA-256 changed: ' + repr(str(path)))


class Journal:
    def __init__(self, path):
        self.stream = path.open('x', encoding='utf-8')

    def event(self, status, **data):
        self.stream.write(line(dict(time_utc=now(), event=status, **data)))
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self):
        self.stream.close()


def file_attributes(fd, updates=None):
    """Use fd-based xattr APIs; macOS Python does not expose os.*xattr."""
    if platform.system() != 'Darwin':
        if updates is not None:
            for name, value in updates.items():
                os.setxattr(fd, name, value)
            return
        return {name: os.getxattr(fd, name) for name in os.listxattr(fd)}
    libc = ctypes.CDLL(None, use_errno=True)
    libc.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.flistxattr.restype = ctypes.c_ssize_t
    libc.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    libc.fgetxattr.restype = ctypes.c_ssize_t
    libc.fsetxattr.argtypes = libc.fgetxattr.argtypes
    libc.fsetxattr.restype = ctypes.c_int

    def checked(value):
        if value < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return value

    if updates is not None:
        for name, value in updates.items():
            buf = ctypes.create_string_buffer(value)
            checked(libc.fsetxattr(fd, os.fsencode(name), buf, len(value), 0, 0))
        return
    try:
        size = checked(libc.flistxattr(fd, None, 0, 0))
    except OSError as exc:
        if exc.errno == errno.ENOTSUP:
            return {}
        raise
    names = ctypes.create_string_buffer(size)
    used = checked(libc.flistxattr(fd, names, size, 0))
    result = {}
    for name in names.raw[:used].split(b'\0'):
        if name:
            size = checked(libc.fgetxattr(fd, name, None, 0, 0, 0))
            value = ctypes.create_string_buffer(size)
            used = checked(libc.fgetxattr(fd, name, value, size, 0, 0))
            result[os.fsdecode(name)] = value.raw[:used]
    return result


def move_metadata(path):
    clean_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        attributes = file_attributes(fd)
        require(not any(name.startswith('system.posix_acl_') for name in attributes),
                'mv does not support extended ACLs: ' + repr(str(path)))
        if platform.system() == 'Darwin':
            listing = command([executable('ls'), '-lde', str(path)]).decode(errors='replace')
            require(not re.search(r'^\s+\d+: ', listing, re.MULTILINE),
                    'mv does not support extended ACLs: ' + repr(str(path)))
        return {name: hashlib.sha256(value).hexdigest() for name, value in attributes.items()}
    finally:
        os.close(fd)


def destination_mtime_resolution(vol):
    # macOS ufsd_NTFS can report a cached fractional mtime that is lost on
    # remount. Write whole seconds before hashing; never tolerate later drift.
    return 1_000_000_000 if vol['os'] == 'Darwin' and vol['fstype'].lower() == 'ufsd_ntfs' else 1


def destination_times(info, mtime_resolution_ns):
    return (info.st_atime_ns, info.st_mtime_ns - info.st_mtime_ns % mtime_resolution_ns)


def system_managed_attributes():
    # macOS assigns provenance to the creating/modifying application. Copies
    # need not share it with originals; leave the OS-managed value untouched.
    return {'com.apple.provenance'} if platform.system() == 'Darwin' else set()


def check_destination_metadata(path, expected, actual, journal, *, phase):
    differences = {name: dict(expected_sha256=value, actual_sha256=actual.get(name))
                   for name, value in expected.items() if actual.get(name) != value}
    ignored = {k: v for k, v in differences.items() if k in system_managed_attributes()}
    rejected = {k: v for k, v in differences.items() if k not in ignored}
    if differences:
        journal.event('destination_metadata_difference', destination=str(path), phase=phase,
                      ignored_system_attributes=ignored, rejected_attributes=rejected)
    require(not rejected, 'Destination metadata does not match: ' + repr(str(path)) +
            '; phase=' + phase + '; attributes=' + json.dumps(rejected, sort_keys=True))


def preserve_move_metadata(entries, mtime_resolution_ns=1):
    # Set directory metadata last, after children have been created.
    for item in sorted(entries, key=lambda x: len(Path(x['destination']).parts), reverse=True):
        src, dst = Path(item['source']), Path(item['destination'])
        require(move_metadata(src) == item['move_metadata'], 'Source metadata changed: ' + repr(str(src)))
        clean_path(dst)
        source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        target_fd = None
        try:
            target_fd = os.open(dst, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            original, current = file_attributes(source_fd), file_attributes(target_fd)
            file_attributes(target_fd, {k: v for k, v in original.items()
                                       if k not in system_managed_attributes() and current.get(k) != v})
            st = os.fstat(source_fd)
            os.fchmod(target_fd, stat.S_IMODE(st.st_mode) & 0o777)
            os.utime(target_fd, ns=destination_times(st, mtime_resolution_ns))
        finally:
            os.close(source_fd)
            if target_fd is not None:
                os.close(target_fd)
    os.sync()


def check_removal_sources(header, entries, journal):
    """Check the entire source batch and metadata before removing any entry."""
    check_source(header, entries, hashes=True)
    for item in entries:
        src, dst = Path(item['source']), Path(item['destination'])
        require([src.stat().st_dev, src.stat().st_ino] == item['source_identity'],
                'Source entry was replaced: ' + repr(str(src)))
        expected = item['move_metadata']
        require(move_metadata(src) == expected, 'Source metadata changed: ' + repr(str(src)))
        actual = move_metadata(dst)
        check_destination_metadata(dst, expected, actual, journal, phase='before_source_removal')


def remove_verified_source(header, entries, journal, deletion):
    """Delete only manifest entries. Never recursively remove unknown files."""
    source = Path(header['source_root'])
    check_removal_sources(header, entries, journal)
    # Reuse the single checksum pass; subsequent checks inspect file state only.
    check_verified_destination(header, entries, deletion['verified_destinations'], journal,
                               phase='before_source_removal')
    clean_path(source.parent)
    parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    mount = Path(header['volume']['mount'])
    mount_fd = None
    try:
        mount_fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        check_volume(header)  # Holding this FD prevents normal unmount during deletion.
        journal.event('source_removal_start', source=str(source), files=header['files'])
        deletion['started'] = True
        for item in (x for x in entries if x['type'] == 'file'):
            src, dst = Path(item['source']), Path(item['destination'])
            src_parent = directory_fd(parent_fd, src.parent.relative_to(source.parent))
            dst_parent = directory_fd(mount_fd, dst.parent.relative_to(mount))
            try:
                # Check again immediately before deletion; an error stops subsequent deletions.
                checksum, sig = digest_file(src)
                require(checksum == item['sha256'] and sig == {k: item[k] for k in sig},
                        'Source changed before removal: ' + repr(str(src)))
                dest_sig = deletion['verified_destinations'][str(dst)]
                require(move_metadata(src) == item['move_metadata'], 'Source metadata changed before removal.')
                target_meta = move_metadata(dst)
                check_destination_metadata(dst, item['move_metadata'], target_meta, journal,
                                           phase='before_file_removal')
                st = os.stat(src.name, dir_fd=src_parent, follow_symlinks=False)
                require(signature(st) == sig, 'Source name was replaced before removal.')
                target_stat = os.stat(dst.name, dir_fd=dst_parent, follow_symlinks=False)
                check_destination_state(item, dest_sig, target_stat, os.fstat(mount_fd).st_dev,
                                        journal, phase='before_file_removal')
                journal.event('file_removal_start', source=str(src))
                os.unlink(src.name, dir_fd=src_parent)
                deletion['files'] += 1
                journal.event('file_removed', source=str(src))
            finally:
                os.close(src_parent)
                os.close(dst_parent)
        for item in sorted((x for x in entries if x['type'] == 'directory'),
                           key=lambda x: len(Path(x['source']).parts), reverse=True):
            src = Path(item['source'])
            fd = directory_fd(parent_fd, src.parent.relative_to(source.parent))
            try:
                st = os.stat(src.name, dir_fd=fd, follow_symlinks=False)
                require(stat.S_ISDIR(st.st_mode) and [st.st_dev, st.st_ino] == item['source_identity'],
                        'Source directory was replaced before removal.')
                require(move_metadata(src) == item['move_metadata'], 'Source directory metadata changed.')
                os.rmdir(src.name, dir_fd=fd)  # Fails on new/unlisted contents instead of deleting them.
                journal.event('directory_removed', source=str(src))
            finally:
                os.close(fd)
        os.sync()
        journal.event('moved', files=deletion['files'], source=str(source), destination=header['destination_root'])
    finally:
        os.close(parent_fd)
        if mount_fd is not None:
            os.close(mount_fd)


def verify_files(header, entries, journal):
    check_volume(header)
    expected_device = Path(header['volume']['mount']).stat().st_dev
    checked = 0
    verified = {}
    for item in entries:
        path = Path(item['destination'])
        clean_path(path)
        require(path.exists() and path.stat().st_dev == expected_device,
                'Missing destination or nested mount: ' + repr(str(path)))
        if item['type'] == 'directory':
            require(path.is_dir(), 'Destination directory was replaced: ' + repr(str(path)))
            verified[str(path)] = signature(path.lstat())
            journal.event('destination_snapshot', destination=str(path), object_type='directory',
                          signature=verified[str(path)])
            continue
        print('Verifying {}/{}: {}'.format(checked + 1, header['files'], repr(str(path))), flush=True)
        checksum, sig = digest_file(path)
        if checksum != item['sha256'] or sig['size'] != item['size']:
            journal.event('checksum_mismatch', destination=str(path), expected=item['sha256'], actual=checksum)
            raise ChecksumError('DESTINATION CHECKSUM MISMATCH: ' + repr(str(path)))
        checked += 1
        verified[str(path)] = sig
        journal.event('file_verified', destination=str(path), sha256=checksum, signature=sig)
    check_volume(header)
    require(checked == header['files'], 'Not all manifest files were verified.')
    return verified


def check_destination_state(item, expected, info, mount_device, journal, *, phase,
                            after_remount=False):
    """Allow only the mount-local device number to change across a remount."""
    actual = signature(info)
    differences = {key: dict(before=value, after=actual[key])
                   for key, value in expected.items() if value != actual[key]}
    valid_type = stat.S_ISDIR(info.st_mode) if item['type'] == 'directory' else stat.S_ISREG(info.st_mode)
    if not valid_type:
        differences['object_type'] = dict(before=item['type'], after=stat.S_IFMT(info.st_mode))
    if info.st_dev != mount_device:
        differences['mount_device'] = dict(before=mount_device, after=info.st_dev)
    # The caller has checked the UUID, mount path, filesystem type and every
    # object's membership in the current mount. Inode/time/size changes remain fatal.
    allowed = {'device'} if after_remount else set()
    rejected = sorted(set(differences) - allowed)
    journal.event('destination_state_checked', destination=item['destination'],
                  object_type=item['type'], phase=phase, expected=expected, actual=actual,
                  mount_device=mount_device, differences=differences,
                  accepted=not rejected, allowed_changes=sorted(set(differences) & allowed))
    require(not rejected, 'Destination changed since checksum verification: ' +
            repr(item['destination']) + '; phase=' + phase + '; differences=' +
            json.dumps(differences, ensure_ascii=True, sort_keys=True))
    return actual


def check_verified_destination(header, entries, verified, journal, *, after_remount=False,
                               phase='within_mount'):
    """Validate all entries, then return a baseline for strict checks in this mount."""
    check_volume(header)
    mount = Path(header['volume']['mount'])
    mount_device = mount.stat().st_dev
    observed = {}
    for item in entries:
        path = Path(item['destination'])
        try:
            clean_path(path)
            info = path.lstat()
        except (OSError, Stop) as exc:
            journal.event('destination_state_unavailable', destination=str(path), phase=phase,
                          expected=verified[str(path)], error=str(exc))
            raise
        observed[str(path)] = check_destination_state(
            item, verified[str(path)], info, mount_device, journal,
            phase=phase, after_remount=after_remount)
    check_volume(header)
    require(mount.stat().st_dev == mount_device, 'Destination mount changed during state checks.')
    return observed


def remount_commands(vol, sudo=False, linux_method='system'):
    require(vol['external'] is True, 'Internal volume must never be unmounted.')
    require(vol['mount'] not in ('/', '/System/Volumes/Data', '/boot', '/home', '/usr', '/var'),
            'System mount must never be unmounted.')
    if vol['os'] == 'Darwin':
        require(not sudo, '--sudo-remount is Linux-only.')
        tool = executable('diskutil')
        # Disk Arbitration can remove /Volumes/<name> on unmount. A custom
        # -mountPoint requires an existing directory; let macOS recreate its
        # standard mountpoint, then validate the UUID and path below.
        return ([tool, 'unmount', vol['device']],
                [tool, 'mount', vol['device']])
    require(vol['os'] == 'Linux', 'Unsupported remount platform.')
    if linux_method == 'udisks':
        require(not sudo, '--sudo-remount cannot be combined with UDisks.')
        tool = executable('udisksctl')
        return ([tool, 'unmount', '--block-device', vol['device'], '--no-user-interaction'],
                [tool, 'mount', '--block-device', vol['device'], '--no-user-interaction'])
    # Never infer a new filesystem driver, alter options, force, or lazy-unmount.
    require(vol['fstype'] in ('ext2', 'ext3', 'ext4', 'xfs', 'ntfs3', 'vfat', 'exfat'),
            'Automatic Linux remount supports native simple block filesystems only; FUSE is refused.')
    prefix = [executable('sudo'), '-n', '--'] if sudo else []
    return (prefix + [executable('umount'), '--', vol['mount']],
            prefix + [executable('mount'), '--types', vol['fstype'], '--options', vol['options'],
                      '--source', 'UUID=' + vol['uuid'], '--target', vol['mount']])


def confirm_remount(vol, journal):
    journal.event('remount_confirmation_requested', mount=vol['mount'], uuid=vol['uuid'], device=vol['device'])
    print('The entire destination volume will be briefly unavailable.', flush=True)
    prompt = 'Confirm remount of volume {} (UUID {}, device {})? [y/N]: '.format(
        repr(vol['mount']), vol['uuid'], vol['device'])
    while True:
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, OSError):
            journal.event('remount_declined', reason='input unavailable')
            raise Stop('No remount confirmation received. Copies retained; volume was not unmounted.')
        if answer == 'y':
            journal.event('remount_confirmed', uuid=vol['uuid'])
            return
        if answer in ('n', ''):
            journal.event('remount_declined', reason='user declined')
            raise Stop('Remount declined. Copies retained; volume was not unmounted. Verification not completed.')
        print('Please enter y or n. Enter defaults to n.', flush=True)


def remount(vol, journal, sudo=False, linux_method='system'):
    unmount_cmd, mount_cmd = remount_commands(vol, sudo, linux_method)
    # Before unmounting, resolve the UUID again. All file handles are already closed.
    current = volume(Path(vol['mount']))
    same_volume(vol, current)
    require(current['device'] == vol['device'], 'Device changed before unmount.')
    confirm_remount(vol, journal)
    # The disk may have changed while the operator was reading the prompt.
    current = volume(Path(vol['mount']))
    same_volume(vol, current)
    require(current['device'] == vol['device'], 'Device changed during remount confirmation.')
    journal.event('unmount_start', command=unmount_cmd, recovery_command=mount_cmd)
    print('Unmounting external volume: ' + vol['mount'], flush=True)
    command(unmount_cmd)
    require(not os.path.ismount(vol['mount']), 'Volume remained mounted; refusing false remount success.')
    journal.event('unmounted', uuid=vol['uuid'])
    if vol['os'] == 'Darwin':
        info = plistlib.loads(command([executable('diskutil'), 'info', '-plist', vol['device']]))
        require(info.get('VolumeUUID') == vol['uuid'] and info.get('Internal') is False,
                'Device identity changed while unmounted; mounting refused.')
    elif linux_method == 'udisks':
        data = json.loads(command([executable('lsblk'), '--json', '--output', 'UUID', vol['device']]))
        rows = data.get('blockdevices', [])
        require(len(rows) == 1 and rows[0].get('uuid') == vol['uuid'],
                'Device UUID changed while unmounted; mounting refused.')
    command(mount_cmd)
    same_volume(vol, volume(Path(vol['mount'])))
    journal.event('remounted', uuid=vol['uuid'])


def directory_fd(root_fd, relative, create=False):
    """Walk only beneath a held mount FD: never fall back to the system disk."""
    fd = os.dup(root_fd)
    expected_device = os.fstat(root_fd).st_dev
    try:
        for part in relative.parts:
            require(part not in ('..', '/') and part, 'Unsafe relative directory.')
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            require(os.fstat(fd).st_dev == expected_device, 'Nested destination mount refused.')
        return fd
    except BaseException:
        os.close(fd)
        raise


def copy_one(item, mount_fd, mount, rsync, mtime_resolution_ns=1):
    src, dst = Path(item['source']), Path(item['destination'])
    clean_path(src)
    source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    parent_fd = cwd_fd = None
    try:
        before = os.fstat(source_fd)
        require(stat.S_ISREG(before.st_mode) and signature(before) ==
                {key: item[key] for key in signature(before)}, 'Source changed before copy: ' + repr(str(src)))
        parent_fd = directory_fd(mount_fd, dst.parent.relative_to(mount))
        try:
            os.stat(dst.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Stop('Destination file already exists: ' + repr(str(dst)))
        # A held directory FD prevents normal unmount and avoids pathname redirection.
        # This program is intentionally single-threaded; restore cwd even if rsync fails.
        cwd_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
        os.fchdir(parent_fd)
        try:
            # rsync needs an ordinary pathname: macOS /dev/fd entries are devices.
            # Retain the open source FD and reject identity/state changes afterwards.
            clean_path(src)
            output = command([rsync, '--whole-file', '--ignore-existing', '--partial',
                              '--itemize-changes', '--out-format=SAFE_TRANSFER_FILE:%i',
                              '--', str(src), './' + dst.name])
            require(any(row.startswith(b'SAFE_TRANSFER_FILE:>f') for row in output.splitlines()),
                    'rsync did not report a copied regular file; refusing a skipped destination: ' + repr(str(dst)))
        finally:
            os.fchdir(cwd_fd)
        require(signature(os.fstat(source_fd)) == signature(before) == signature(src.lstat()),
                'Source changed during copy: ' + repr(str(src)))
        fd = os.open(dst.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_size == item['size'] and
                    info.st_dev == os.fstat(mount_fd).st_dev,
                    'Copied destination has unexpected type, size or filesystem: ' + repr(str(dst)))
            # Metadata policy stays in this wrapper, independent of rsync's defaults.
            os.fchmod(fd, stat.S_IMODE(before.st_mode) & 0o777)
            os.utime(fd, ns=destination_times(before, mtime_resolution_ns))
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        for fd in (source_fd, parent_fd, cwd_fd):
            if fd is not None:
                os.close(fd)


def copy_files(header, entries, journal, rsync):
    dest = Path(header['destination_root'])
    require(not os.path.lexists(dest), 'Destination already exists; no automatic resume or replacement.')
    check_volume(header)
    require(shutil.disk_usage(existing_parent(dest)).free >= header['bytes'] + max(64 * 1024 * 1024, header['bytes'] // 100),
            'Insufficient free space including a minimal working margin.')
    mount = Path(header['volume']['mount'])
    mount_fd = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    count = 0
    mtime_resolution_ns = destination_mtime_resolution(header['volume'])
    try:
        check_volume(header)
        require(os.fstat(mount_fd).st_dev == mount.stat().st_dev, 'Mount changed while opening it.')
        if header.get('source_kind') == 'file':
            fd = directory_fd(mount_fd, dest.parent.relative_to(mount), create=True)
            os.close(fd)
        for item in sorted((x for x in entries if x['type'] == 'directory'),
                           key=lambda x: len(Path(x['destination']).parts)):
            path = Path(item['destination'])
            parent_fd = directory_fd(mount_fd, path.parent.relative_to(mount), create=True)
            try:
                os.mkdir(path.name, dir_fd=parent_fd)  # Every declared destination is new.
            finally:
                os.close(parent_fd)
        for item in entries:
            if item['type'] != 'file':
                continue
            print('Copying {}/{}: {}'.format(count + 1, header['files'], repr(item['source'])), flush=True)
            copy_one(item, mount_fd, mount, rsync, mtime_resolution_ns)
            count += 1
            journal.event('file_copied', source=item['source'], destination=item['destination'], size=item['size'])
        if mtime_resolution_ns > 1:
            for item in (x for x in entries if x['type'] == 'directory'):
                path = Path(item['destination'])
                fd = directory_fd(mount_fd, path.relative_to(mount))
                try:
                    info = os.fstat(fd)
                    times = destination_times(info, mtime_resolution_ns)
                    os.utime(fd, ns=times)
                    journal.event('directory_timestamp_normalized', destination=str(path),
                                  before_mtime_ns=info.st_mtime_ns, requested_mtime_ns=times[1])
                finally:
                    os.close(fd)
    finally:
        os.close(mount_fd)
    check_source(header, entries, hashes=False)
    os.sync()
    check_volume(header)
    journal.event('copy_complete', files=count)


def execute(args, verify_only=False):
    manifest = canonical(args.manifest)
    log = canonical(args.log) if args.log is not None else manifest.with_suffix('.log')
    require(log != manifest, 'Log path matches the manifest; choose a different --log filename.')
    header, entries, manifest_sha = load_manifest(manifest)
    source, dest = Path(header['source_root']), Path(header['destination_root'])
    check_manifest_location(manifest, source, dest, header['volume'])
    check_manifest_location(log, source, dest, header['volume'])
    actual = check_volume(header)
    require(not os.path.lexists(log), 'Log already exists; choose a new filename with --log: ' + str(log))
    if not verify_only:
        if actual['external']:
            separate_source(source, actual)
            remount_commands(actual, args.sudo_remount, args.linux_remount)
        else:
            require(not args.sudo_remount, '--sudo-remount must not be supplied for an internal volume.')
        require(not os.path.lexists(dest), 'Destination root already exists; automatic resume is disabled.')
        rsync = executable('rsync')
    journal = Journal(log)
    moving = getattr(args, 'move', False) and not verify_only
    deletion = {'started': False, 'files': 0}
    try:
        journal.event('start', mode='verify' if verify_only else ('mv' if moving else 'run'), manifest=str(manifest),
                      manifest_sha256=manifest_sha, volume=actual, files=header['files'],
                      copy_backend=None if verify_only else rsync)
        print('Log: ' + str(log), flush=True)
        # Keep the process working directory off the external disk.
        os.chdir('/')
        if not verify_only:
            mtime_resolution_ns = destination_mtime_resolution(actual)
            journal.event('destination_timestamp_policy', mtime_resolution_ns=mtime_resolution_ns,
                          rounding='floor', applies_to='new_destination_files_and_directories',
                          filesystem=actual['fstype'])
            if mtime_resolution_ns > 1:
                print('Destination driver compatibility: copy mtime uses whole seconds; '
                      'post-verification timestamp checks remain exact.', flush=True)
            print('Checking all sources against the manifest...', flush=True)
            check_source(header, entries, hashes=True)
            check_volume(header)
            print('Copy backend: ' + rsync, flush=True)
            copy_files(header, entries, journal, rsync)
            if moving:
                preserve_move_metadata(entries, mtime_resolution_ns)
        verified = verify_files(header, entries, journal)
        journal.event('content_verified', files=header['files'], bytes=header['bytes'],
                      phase='standalone' if verify_only else 'before_remount')
        if not verify_only:
            check_verified_destination(header, entries, verified, journal, phase='before_remount')
            if actual['external']:
                remount(actual, journal, args.sudo_remount, args.linux_remount)
            else:
                journal.event('remount_skipped', reason='internal volume')
            verified = check_verified_destination(
                header, entries, verified, journal, after_remount=actual['external'],
                phase='after_remount' if actual['external'] else 'within_mount')
        journal.event('verified', files=header['files'], bytes=header['bytes'],
                      remounted_in_this_run=not verify_only and actual['external'], sources_deleted=False,
                      checksum_verification_phase='standalone' if verify_only else 'before_remount',
                      content_rechecked_after_remount=False)
        if moving:
            deletion['verified_destinations'] = verified
            remove_verified_source(header, entries, journal, deletion)
            print('MOVED: {} files, {} bytes. Verified source removed.'.format(header['files'], header['bytes']))
        else:
            print('VERIFIED: {} files, {} bytes. Source files were not deleted.'.format(header['files'], header['bytes']))
    except BaseException as exc:
        try:
            journal.event('failed', error=str(exc), error_type=type(exc).__name__,
                          sources_deleted=bool(deletion['files']), source_deletion_started=deletion['started'],
                          source_files_removed=deletion['files'])
        except OSError:
            pass
        raise
    finally:
        journal.close()


def confirm_source_removal(header, journal):
    source, dest = header['source_root'], header['destination_root']
    journal.event('source_removal_confirmation_requested', source=source, destination=dest,
                  files=header['files'], bytes=header['bytes'])
    print('Verified destination: ' + repr(dest), flush=True)
    print('Remove {} source files ({} bytes) and their empty directories from {}.'.format(
        header['files'], header['bytes'], repr(source)), flush=True)
    print('Removal bypasses Trash and is not atomic. Recovery requires copying back from '
          'the verified destination; a failure may leave a partially removed source tree.', flush=True)
    try:
        answer = input('Confirm removal of these verified originals? [y/N]: ').strip().lower()
    except (EOFError, OSError):
        answer = ''
    if answer != 'y':
        journal.event('source_removal_declined', source=source)
        raise Stop('Source removal not confirmed. No originals were removed.')
    journal.event('source_removal_confirmed', source=source)


def remove_existing_sources(args):
    """Verify existing copies and remove originals, without copying or remounting."""
    manifest = canonical(args.manifest)
    log = canonical(args.log) if args.log is not None else manifest.with_suffix('.rm.log')
    require(log != manifest, 'Log path matches the manifest; choose a different --log filename.')
    header, entries, manifest_sha = load_manifest(manifest)
    source, dest = Path(header['source_root']), Path(header['destination_root'])
    clean_path(source)
    require(not os.path.samefile(source, dest), 'Source and destination refer to the same object.')
    require(source.name and not os.path.ismount(source) and source != canonical(Path.home()),
            'Filesystem roots and the home directory cannot be removed as a whole.')
    check_manifest_location(manifest, source, dest, header['volume'])
    check_manifest_location(log, source, dest, header['volume'])
    actual = check_volume(header)
    require(not os.path.lexists(log), 'Log already exists; choose a new filename with --log: ' + str(log))
    journal = Journal(log)
    deletion = {'started': False, 'files': 0}
    try:
        journal.event('start', mode='rm', manifest=str(manifest), manifest_sha256=manifest_sha,
                      volume=actual, files=header['files'], copy_backend=None)
        print('Log: ' + str(log), flush=True)
        os.chdir('/')
        print('Checking source state against the manifest...', flush=True)
        check_source(header, entries, hashes=False)
        for item in entries:
            path = Path(item['source'])
            info = path.lstat()
            require(item['type'] != 'file' or info.st_nlink == 1,
                    'rm does not support hard-linked source files: ' + repr(str(path)))
            # cp/run plans lack move metadata. Capture it now and require the
            # destination to contain it too; never repair copies in this command.
            if 'source_identity' not in item:
                item['source_identity'] = [info.st_dev, info.st_ino]
            if 'move_metadata' not in item:
                item['move_metadata'] = move_metadata(path)
            journal.event('removal_source_snapshot', source=str(path),
                          source_identity=item['source_identity'], move_metadata=item['move_metadata'])
        verified = verify_files(header, entries, journal)
        journal.event('content_verified', files=header['files'], bytes=header['bytes'],
                      phase='before_source_removal')
        check_removal_sources(header, entries, journal)
        check_verified_destination(header, entries, verified, journal, phase='before_removal_confirmation')
        confirm_source_removal(header, journal)
        deletion['verified_destinations'] = verified
        # Recheck after the prompt; files may have changed while it was open.
        remove_verified_source(header, entries, journal, deletion)
        print('REMOVED: {} files, {} bytes. Verified originals removed.'.format(header['files'], header['bytes']))
    except BaseException as exc:
        try:
            journal.event('failed', error=str(exc), error_type=type(exc).__name__,
                      sources_deleted=bool(deletion['files']), source_deletion_started=deletion['started'],
                      source_files_removed=deletion['files'])
        except OSError:
            pass
        raise
    finally:
        journal.close()


def automatic_transfer(args):
    require(len(args.paths) >= 2, 'Specify SOURCE... DESTINATION.')
    destination = canonical(args.paths[-1])
    sources = []
    for operand in args.paths[:-1]:
        leaf = operand.rstrip(os.sep).rsplit(os.sep, 1)[-1]
        require(args.action == 'cp' or leaf not in ('.', '..'), 'Source removal cannot use . or .. operands.')
        source = canonical(operand)
        if operand.endswith(os.sep):
            require(source.is_dir(), 'A source ending in / must be a directory.')
        contents = (args.action == 'cp' and source.is_dir() and destination.is_dir() and
                    (leaf == '.' or (platform.system() == 'Darwin' and operand.endswith('/'))))
        if contents:
            sources.extend(sorted(source.iterdir()))
        else:
            sources.append(source)
    if not sources:
        print('Nothing to copy: source directory is empty.')
        return
    require(len(sources) == 1 or destination.is_dir(), 'Multiple sources require an existing destination directory.')
    require(destination.parent.is_dir(), 'Destination parent directory does not exist.')
    if args.paths[-1].endswith(os.sep):
        require(destination.is_dir() or (not destination.exists() and len(sources) == 1 and sources[0].is_dir()),
                'A destination ending in / must be a directory.')
    jobs = []
    used = set()
    for source in sources:
        require(source.is_file() or source.is_dir(), 'Source is not an ordinary file or directory: ' + str(source))
        require(source.name and not os.path.ismount(source) and source != canonical(Path.home()),
                'Filesystem roots and the home directory cannot be transferred as a whole.')
        target = destination / source.name if destination.is_dir() else destination
        if args.action == 'rm':
            clean_path(target)
            require(target.exists(), 'Destination does not exist: ' + repr(str(target)))
            require(not os.path.samefile(source, target), 'Source and destination refer to the same object.')
            require(source.is_dir() == target.is_dir() and source.is_file() == target.is_file(),
                    'Source and destination types do not match: ' + repr(str(target)))
        else:
            require(not os.path.lexists(target), 'Destination already exists; refusing overwrite or merge: ' + str(target))
        require(not any(within(target, p) or within(p, target) for p in sources), 'Source and destination overlap.')
        require(not any(source != p and (within(source, p) or within(p, source)) for p in sources),
                'Overlapping sources are not supported.')
        name = unicodedata.normalize('NFC', str(target)).casefold()
        require(name not in used, 'Multiple sources have conflicting destination names.')
        used.add(name)
        jobs.append((source, target))
    state = canonical(args.state_dir or (Path.home() / '.local/state/safe-transfer'))
    require(not args.log or len(jobs) == 1, '--log may only be used with one transfer; otherwise each plan gets its own log.')
    require(not any(within(state, p) or within(p, state) for pair in jobs for p in pair),
            'State directory must be separate from all transfer paths.')
    state.mkdir(parents=True, exist_ok=True)
    for source, target in jobs:
        job = state / (datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:12])
        job.mkdir()
        task = argparse.Namespace(source=str(source), destination=str(target),
                                  manifest=str(job / 'plan.jsonl'), log=args.log,
                                  move=args.action in ('mv', 'rm'), sudo_remount=getattr(args, 'sudo_remount', False),
                                  linux_remount=getattr(args, 'linux_remount', 'system'))
        print('{}: {} -> {}'.format(args.action.upper(), repr(str(source)), repr(str(target))), flush=True)
        prepare(task, existing_destination=args.action == 'rm')
        if args.action == 'rm':
            # This is a new automatic job, so the normal plan.log is available.
            task.log = task.log or str(Path(task.manifest).with_suffix('.log'))
            remove_existing_sources(task)
        else:
            execute(task)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest='action', required=True)
    for name in ('cp', 'mv'):
        part = sub.add_parser(name, help='Automatically plan, copy and verify' +
                             (', then remove the verified source.' if name == 'mv' else '.'))
        part.add_argument('paths', nargs='+', metavar='PATH', help='SOURCE... DESTINATION, as with cp/mv.')
        part.add_argument('--state-dir', help='Manifest/log storage (default: ~/.local/state/safe-transfer).')
        part.add_argument('--log', help='Optional custom log for a single transfer; default: generated plan path with .log extension.')
        part.add_argument('--sudo-remount', action='store_true', help='Linux only: sudo -n for umount/mount.')
        part.add_argument('--linux-remount', choices=('system', 'udisks'), default='system')
        part.add_argument('-r', '-R', '--recursive', action='store_true', help='Accepted for cp compatibility; directories are always recursive.')
    plan = sub.add_parser('plan', help='Read sources and create a new reviewable JSONL manifest.')
    plan.add_argument('--source', required=True, help='Source file or directory.')
    plan.add_argument('--destination', required=True, help='Exact new, nonexistent destination path.')
    plan.add_argument('--manifest', required=True, help='New JSONL file outside source/destination.')
    for name in ('run', 'verify'):
        part = sub.add_parser(name, help='Copy, verify, then remount external volume.' if name == 'run' else 'Read-only checksum verification; no remount.')
        part.add_argument('--manifest', required=True)
        part.add_argument('--log', help='New log file (JSONL content); default: manifest path with its extension replaced by .log.')
        if name == 'run':
            part.add_argument('--sudo-remount', action='store_true', help='Linux only: explicitly use sudo -n for umount/mount only.')
            part.add_argument('--linux-remount', choices=('system', 'udisks'), default='system',
                              help='Linux: system mount/umount, or UDisks for desktop/FUSE volumes.')
    remove = sub.add_parser('rm', help='Verify existing copies, confirm, then remove originals; no copy or remount.')
    remove.add_argument('paths', nargs='*', metavar='PATH', help='SOURCE... DESTINATION, as with mv; copies must already exist.')
    remove.add_argument('--state-dir', help='Automatic manifest/log storage (default: ~/.local/state/safe-transfer).')
    remove.add_argument('-r', '-R', '--recursive', action='store_true', help='Accepted for compatibility; directories are always recursive.')
    remove.add_argument('--manifest', help='Alternatively use an existing plan instead of SOURCE... DESTINATION.')
    remove.add_argument('--log', help='New event log; default: plan.log for paths or plan.rm.log for --manifest.')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action in ('cp', 'mv'):
            automatic_transfer(args)
        elif args.action == 'plan':
            prepare(args)
        elif args.action == 'rm':
            require(not (args.paths and args.manifest), 'Use SOURCE... DESTINATION or --manifest, not both.')
            if args.paths:
                automatic_transfer(args)
            else:
                require(args.manifest is not None, 'Specify SOURCE... DESTINATION.')
                require(args.state_dir is None, '--state-dir applies only to SOURCE... DESTINATION.')
                remove_existing_sources(args)
        else:
            execute(args, verify_only=args.action == 'verify')
        return 0
    except ChecksumError as exc:
        print('FATAL: ' + str(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print('INTERRUPTED: copies retained. Check the journal for source removal status if using mv or rm.', file=sys.stderr)
        return 130
    except (Stop, OSError, ValueError, KeyError, TypeError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
