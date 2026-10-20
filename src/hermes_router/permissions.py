"""Owner-only files for local application data; never follow data-file symlinks."""
import os
from pathlib import Path


def private_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("application data file cannot be a symlink")
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    if path.stat().st_uid != os.getuid():
        raise ValueError("application data file must belong to the service user")
    path.chmod(0o600)
