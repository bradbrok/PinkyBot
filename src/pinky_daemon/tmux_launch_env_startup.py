"""Read-only, process-wide startup diagnostics for local launch state."""

import errno
import threading
from collections.abc import Callable

from pinky_daemon import tmux_launch_env

_CHECKED = False
_CHECK_LOCK = threading.Lock()


def warn_state_directory(log: Callable[[str], None]) -> None:
    """Check the local service user's state once through the normal daemon sink."""
    global _CHECKED
    with _CHECK_LOCK:
        if _CHECKED:
            return
        _CHECKED = True
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
