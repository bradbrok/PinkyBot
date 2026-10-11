"""Read-only, process-wide startup diagnostics for local launch state."""

import errno
from collections.abc import Callable
from threading import Lock, Thread

from pinky_daemon import tmux_launch_env

_START_LOCK = Lock()


def warn_state_directory(log: Callable[[str], None]) -> None:
    """Start one best-effort diagnostic worker without delaying startup."""
    try:
        if _START_LOCK.acquire(blocking=False):
            Thread(
                target=_warn_state_directory, args=(log,),
                name="launch-env-state-directory", daemon=True,
            ).start()
    except Exception:
        pass


def _warn_state_directory(log: Callable[[str], None]) -> None:
    try:
        with tmux_launch_env._state_directory(create=False):
            pass
    except FileNotFoundError:
        return
    except tmux_launch_env.UnsafeDirectoryError as exc:
        if exc.rule == "owner":
            hint = f"{exc.label} must be owned by uid {exc.euid}"
        else:
            remove = "g-rwx,o-rwx" if exc.rule == "group/other access" else "g-w,o-w"
            hint = f"chmod {remove} {exc.label}"
        message = (
            f"WARNING launch environment state directory is unsafe: {exc}; "
            f"agent panes will fail to launch until fixed: {hint}"
        )
    except OSError as exc:
        symbol = errno.errorcode.get(exc.errno, "UNKNOWN")
        message = f"WARNING launch environment state directory check failed: {symbol}"
    except Exception:
        message = "WARNING launch environment state directory check failed: UNKNOWN"
    else:
        return
    try:
        log(message)
    except Exception:
        pass
