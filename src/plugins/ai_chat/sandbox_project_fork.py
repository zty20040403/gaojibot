"""Copy an explicit group project into a job-specific working directory using Linux reflinks."""
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


# linux/fs.h FICLONE, also used by GNU cp --reflink=always.
FICLONE = 0x40049409
DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def fork_project(work, project, job_id):
    if not re.fullmatch(r'[\w-]{1,80}', project) or job_id <= 0:
        raise ValueError('请指定 /work/projects 下的单个项目名和有效任务编号。')
    parent = work / '.gaoji' / 'jobs' / f'job-{job_id}'
    dest = parent / 'project'
    if parent.resolve(strict=True) != parent:
        raise ValueError('分叉目标不是当前工作区的任务目录。')
    if dest.exists() or dest.is_symlink():
        raise FileExistsError('分叉目录已存在，不覆盖或合并已有结果。')
    remaining = 4096

    def clone_tree(directory, target):
        with os.scandir(directory) as entries:
            clone_entries(directory, target, entries)

    def clone_entries(directory, target, entries):
        nonlocal remaining
        for entry in entries:
            name = entry.name
            remaining -= 1
            if remaining < 0 or name.startswith('.'):
                raise ValueError('分叉只接受最多 4096 项的显式项目资料，不复制隐藏文件。')
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if info.st_dev != source_dev:
                raise ValueError('分叉不跨文件系统。')
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, DIR, dir_fd=directory)
                try:
                    (target / name).mkdir()
                    clone_tree(child, target / name)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                with os.fdopen(os.open(name, flags, dir_fd=directory), 'rb') as source, (target / name).open('xb') as out:
                    fcntl.ioctl(out.fileno(), FICLONE, source.fileno())
                    os.fchmod(out.fileno(), 0o700 if info.st_mode & 0o111 else 0o600)
            else:
                raise ValueError('分叉项目不接受链接、设备或套接字。')

    projects = os.open(work / 'projects', DIR)
    try:
        source = os.open(project, DIR, dir_fd=projects)
        try:
            source_dev = os.fstat(source).st_dev
            with tempfile.TemporaryDirectory(dir=parent, prefix='.fork-') as tmp:
                tree = Path(tmp) / 'project'
                tree.mkdir()
                clone_tree(source, tree)
                tree.rename(dest)
        finally:
            os.close(source)
    finally:
        os.close(projects)
    return dest


if __name__ == '__main__':
    print(fork_project(Path('/work'), sys.argv[1], int(sys.argv[2])))
