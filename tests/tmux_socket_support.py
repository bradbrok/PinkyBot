"""Disposable tmux sockets with paths independent of pytest node names."""

import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

SOCKET_PATH_LENGTHS: list[int] = []


@contextmanager
def private_socket():
    # Gate TMPDIR may need a private ancestor chain for storage tests. Socket
    # directories have their own short, private lifetime and contain no DBs.
    with TemporaryDirectory(prefix="pt-", dir="/tmp") as directory:
        path = Path(directory) / "tmux.sock"
        length = max(len(os.fsencode(path)), len(os.fsencode(path.resolve())))
        assert length < 100, "private tmux socket exceeds the portable path budget"
        assert path.parent.stat().st_mode & 0o777 == 0o700
        SOCKET_PATH_LENGTHS.append(length)
        yield str(path)
