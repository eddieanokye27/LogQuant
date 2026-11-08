"""POSIX directory-relative access: no directory or file symlinks are followed."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
from collections.abc import Iterator


@contextmanager
def directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("Secure ingestion requires POSIX O_NOFOLLOW and O_DIRECTORY support")
    absolute = path.absolute()  # Do not resolve symlinks or collapse '..' before walking.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def open_source(directory: int, filename: str) -> int:
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("Input must be a regular file, not a directory, device or FIFO")
    return fd
