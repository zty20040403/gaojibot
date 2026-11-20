"""Archive host-side nspawn work directories without following guest-controlled paths."""
import os
import re
import stat
import sys
import tarfile
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path


SCOPE = r's[0-9a-f]{6}'
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def opened(path, flags, parent=None):
    fd = os.open(path, flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def mount_id(fd):
    # st_dev alone cannot distinguish a bind mount of a neighbouring scope.
    for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines():
        if line.startswith('mnt_id:'):
            return int(line.split()[1])
    raise RuntimeError('Linux mount ID unavailable')


def check_mount(fd, expected):
    if mount_id(fd) != expected:
        raise ValueError('mount boundary in workspace')


def add_regular(archive, fd, meta, member):
    member.size = meta.st_size
    with open(f'/proc/self/fd/{fd}', 'rb') as source:
        archive.addfile(member, source)
    after = os.fstat(fd)
    if after.st_ctime_ns != meta.st_ctime_ns or after.st_nlink != meta.st_nlink:
        raise ValueError('file changed during snapshot')


def pin(fd, stack):
    pinned = os.dup(fd)
    stack.callback(os.close, pinned)
    return pinned


def archive_tree(archive, directory, prefix, mount, stack, links, directories):
    before = os.fstat(directory).st_ctime_ns
    entries = {}
    for name in os.listdir(directory):
        # O_PATH pins the inode without opening devices, FIFOs or symlink targets.
        with opened(name, os.O_PATH | os.O_NOFOLLOW, directory) as fd:
            check_mount(fd, mount)
            meta = os.fstat(fd)
            member = tarfile.TarInfo(prefix + name)
            member.mode = meta.st_mode & 0o777
            member.mtime = int(meta.st_mtime)
            if stat.S_ISLNK(meta.st_mode):
                member.type = tarfile.SYMTYPE
                member.linkname = os.readlink('', dir_fd=fd)
                archive.addfile(member)
            elif stat.S_ISDIR(meta.st_mode):
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
                with opened('.', DIRECTORY, fd) as child:
                    if archive_tree(archive, child, member.name + '/', mount, stack, links, directories):
                        entries[name] = meta.st_ino
            elif stat.S_ISREG(meta.st_mode):
                if meta.st_nlink == 1:
                    add_regular(archive, fd, meta, member)
                else:
                    key = (meta.st_dev, meta.st_ino)
                    if key not in links:
                        links[key] = (pin(fd, stack), meta, [])
                    links[key][2].append(member)
                    entries[name] = meta.st_ino
    if os.fstat(directory).st_ctime_ns != before:
        raise ValueError('directory changed during snapshot')
    if entries:
        inode = os.fstat(directory).st_ino
        if inode in directories:
            raise ValueError('directory visited twice during snapshot')
        directories[inode] = (pin(directory, stack), before, entries)
    return bool(entries)


def archive_scope(archive, work, mount):
    links, directories = {}, {}
    with ExitStack() as stack:
        archive_tree(archive, work, '', mount, stack, links, directories)

        # Renaming a parent can move a whole subtree without changing the file inode.
        def check_directories():
            for directory, before, entries in directories.values():
                if os.fstat(directory).st_ctime_ns != before:
                    raise ValueError('directory changed during snapshot')
                for name, inode in entries.items():
                    with opened(name, os.O_PATH | os.O_NOFOLLOW, directory) as fd:
                        check_mount(fd, mount)
                        if os.fstat(fd).st_ino != inode:
                            raise ValueError('directory changed during snapshot')

        check_directories()
        for fd, meta, members in links.values():
            current = os.fstat(fd)
            if current.st_ctime_ns != meta.st_ctime_ns or not (len(members) == current.st_nlink == meta.st_nlink):
                raise ValueError('cannot establish workspace ownership of a hard-linked file')
            for member in members:
                add_regular(archive, fd, meta, member)
        check_directories()


def enter_destination(destination):
    # Pin the pre-provisioned host directory, rejecting symlinks even in its parents.
    with ExitStack() as stack:
        fd = stack.enter_context(opened('/', DIRECTORY))
        for component in destination.relative_to('/').parts:
            fd = stack.enter_context(opened(component, DIRECTORY, fd))
        os.fchdir(fd)
    mode = os.stat('.')
    if mode.st_uid != os.geteuid() or stat.S_IMODE(mode.st_mode) != 0o700:
        raise PermissionError('backup directory must be owned by the service user with mode 0700')


def snapshot(source, day):
    os.umask(0o077)
    partial = Path('.pending')
    partial.unlink(missing_ok=True)
    errors = []
    with opened(source, DIRECTORY) as root:
        mount = mount_id(root)
        for scope in sorted(os.listdir(root)):
            if not re.fullmatch(SCOPE, scope):
                continue
            try:
                with opened(scope, DIRECTORY, root) as base:
                    check_mount(base, mount)
                    with opened('work', DIRECTORY, base) as work, partial.open('xb') as out:
                        os.fchmod(out.fileno(), 0o600)
                        check_mount(work, mount)
                        with tarfile.open(fileobj=out, mode='w|') as archive:
                            archive_scope(archive, work, mount)
                        out.flush()
                        os.fsync(out.fileno())
                partial.replace(f'{scope}_{day}.tar')
            except (OSError, ValueError, RecursionError, tarfile.TarError) as error:
                error.add_note(f'workspace: {scope}')
                errors.append(error)
            finally:
                partial.unlink(missing_ok=True)
    oldest = (day - timedelta(days=29)).isoformat()
    for path in Path('.').iterdir():
        match = re.fullmatch(SCOPE + r'_(\d{4}-\d{2}-\d{2})\.tar', path.name)
        if match and match[1] < oldest:
            path.unlink()
    with opened('.', DIRECTORY) as destination:
        os.fsync(destination)
    if errors:
        raise ExceptionGroup('workspace snapshot failed', errors)


if __name__ == '__main__':
    enter_destination(Path(sys.argv[2]))
    snapshot(sys.argv[1], datetime.now(UTC).date())
