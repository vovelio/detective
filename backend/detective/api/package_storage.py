"""Immutable digest-addressed packages, published only after a durable atomic install."""

import hashlib
import os
import tempfile
from pathlib import Path

from detective.scenarios.validation import MAX_ZIP_BYTES


def verify(path: Path, checksum: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES or hashlib.sha256(data).hexdigest() != checksum:
        raise OSError("Invalid stored package")


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(directory: Path, data: bytes, checksum: str) -> tuple[Path, bool]:
    """No overwrite, no partially visible final file; never use a client filename."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"{checksum}.detective"
    fd, staged = tempfile.mkstemp(prefix=".upload-", dir=directory)
    created = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staged, target)
            created = True
        except FileExistsError:
            verify(target, checksum)
        sync_directory(directory)
        return target, created
    finally:
        os.unlink(staged)
