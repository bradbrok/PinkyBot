"""Disposable tmux sockets with paths independent of pytest node names."""

import os
import shlex
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

SOCKET_PATH_LENGTHS: list[int] = []
_SOCKETS: set[str] = set()
_LABELS: set[tuple[str, str]] = set()


def _shell_mentions_tmux(command):
    # Inspect shell tokens, not substrings of the JSON loader's Python source
    # (which legitimately mentions tmux-launch-env paths).
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return "tmux" in command
    return any(os.path.basename(token.strip("`")) == "tmux" for token in tokens)


def check_tmux_argv(args, env=None, *, shell=False):
    """Refuse real clients unless a live fixture owns their complete route.

    Mocked runners never reach this boundary. Parse global options only: a
    capture-pane -S is scrollback, not a socket selector. The registry is
    populated only by helpers which allocate and retain private directories.
    """
    error = "test tmux command requires an explicit private socket"
    if isinstance(args, (str, bytes)):
        if _shell_mentions_tmux(os.fsdecode(args)):
            raise RuntimeError(error)
        return
    argv = [os.fsdecode(a) for a in args]
    if not argv:
        return
    if shell and any(_shell_mentions_tmux(a) for a in argv):
        raise RuntimeError(error)
    effective = dict(os.environ if env is None else env)
    program = os.path.basename(argv[0])
    if program in {"sh", "bash", "zsh"} and any(_shell_mentions_tmux(a) for a in argv[1:]):
        raise RuntimeError(error)
    if program in {"env", "runuser", "sudo", "docker", "podman"}:
        indices = [i for i, arg in enumerate(argv) if os.path.basename(arg) == "tmux"]
        if not indices:
            return
        index = indices[0]
        if program == "env":
            if "-i" in argv[:index] or "--ignore-environment" in argv[:index]:
                effective = {}
            for arg in argv[1:index]:
                if "=" in arg and not arg.startswith("-"):
                    key, value = arg.split("=", 1)
                    effective[key] = value
        # A wrapped real process cannot demonstrate ownership in another uid
        # or container. Wrapper contract tests use a recording inner runner.
        elif program != "env":
            raise RuntimeError(error)
        argv = argv[index:]
    if os.path.basename(argv[0]) != "tmux":
        return
    argv = argv[1:]
    if argv == ["-V"]:
        return
    label = socket = None
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        flag = argv[index]
        if flag in {"-L", "-S", "-f"}:
            if index + 1 == len(argv):
                raise RuntimeError(error)
            value = argv[index + 1]
            index += 2
        elif flag.startswith(("-L", "-S", "-f")):
            value, flag = flag[2:], flag[:2]
            index += 1
        else:
            index += 1
            continue
        if flag == "-L":
            label = value
        elif flag == "-S":
            socket = value
    if label == "pinkybot":
        raise RuntimeError(error)
    if socket is not None:
        if socket not in _SOCKETS:
            raise RuntimeError(error)
    elif (effective.get("TMUX_TMPDIR", ""), label) not in _LABELS:
        raise RuntimeError(error)


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
        _SOCKETS.add(str(path))
        try:
            yield str(path)
        finally:
            _SOCKETS.remove(str(path))


@contextmanager
def private_labels(*labels):
    assert labels and all(label != "pinkybot" for label in labels)
    assert all(label and shlex.quote(label) == label and "/" not in label for label in labels)
    with TemporaryDirectory(prefix="pl-", dir="/tmp") as directory:
        assert Path(directory).stat().st_mode & 0o777 == 0o700
        for label in labels:
            path = Path(directory) / f"tmux-{os.getuid()}" / label
            assert len(os.fsencode(path.resolve())) < 100
            _LABELS.add((directory, label))
        try:
            yield directory
        finally:
            for label in labels:
                _LABELS.remove((directory, label))
