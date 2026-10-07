"""Copy one validated attachment; this standalone child imports only stdlib."""

import fcntl
import json
import os
import stat
import sys
from pathlib import Path


def path_for_fd(fd):
    if sys.platform == "darwin" and hasattr(fcntl, "F_GETPATH"):
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        return Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    if sys.platform.startswith("linux"):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    raise OSError("opened file path lookup is unavailable")


def copy_attachment(payload):
    fd = os.open(
        payload["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("attachment must be a regular file")
        if info.st_nlink != 1:
            raise ValueError("attachment must have one link")
        opened_path = path_for_fd(fd)
        if not opened_path.is_relative_to(Path(payload["working_dir"])):
            raise ValueError("opened attachment is outside the caller's working directory")
        if str(opened_path).lower().endswith((".db", ".db-wal", ".db-shm", ".db-journal")):
            raise ValueError("SQLite databases and sidecars cannot be sent as attachments")
        if (info.st_dev, info.st_ino) in {tuple(pair) for pair in payload["live_sqlite"]}:
            raise ValueError("attachment is a live SQLite inode")
        cap = payload["size_cap"]
        if info.st_size > cap:
            raise ValueError("attachment exceeds the upload size limit")
        target_fd = os.open(
            payload["target"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
        )
        copied = 0
        with os.fdopen(target_fd, "wb") as output:
            while chunk := os.read(fd, min(65536, cap - copied + 1)):
                copied += len(chunk)
                if copied > cap:
                    raise ValueError("attachment exceeds the upload size limit")
                output.write(chunk)
        return {"ok": True, "size": copied, "dev": info.st_dev, "ino": info.st_ino}
    finally:
        os.close(fd)


def main():
    try:
        result = copy_attachment(json.load(sys.stdin))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"ok": False, "reason": str(error)}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
