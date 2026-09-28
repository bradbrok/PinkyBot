"""Synthetic inputs for the JSON launch boundary contract."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from pinky_daemon import tmux_launch_env, tmux_launch_env_loader

NONCE = "a1" * 16
OTHER_NONCE = "b2" * 16
SCOPE = "c3" * 32
SECRET = "synthetic-json-launch-value-82e6"


def stage(home, *, nonce=NONCE, scope=SCOPE, env=None, deadline=None):
    return tmux_launch_env.stage_env(
        {"SECRET": SECRET} if env is None else env,
        scope,
        nonce,
        deadline=time.time() + 30 if deadline is None else deadline,
    )


def cancel(*, nonce=NONCE, scope=SCOPE):
    cleanup = getattr(tmux_launch_env, "cancel_env", None)
    assert callable(cleanup), "exact-nonce cancellation must be available in the target namespace"
    return cleanup(scope, nonce)


def payload(home, *, nonce=NONCE, filename=None, env=None):
    directory = home / ".local/state/pinkybot/tmux-launch-env" / SCOPE
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / (filename or f"env-{nonce}.json")
    path.write_text(json.dumps({"nonce": nonce, "env": env or {"SECRET": SECRET}}))
    path.chmod(0o600)
    return path


def probe_command():
    code = "import json,os;print(json.dumps(dict(os.environ)))"
    return shlex.join([sys.executable, "-I", "-c", code])


def run_loader(path, *, nonce=NONCE, command=None):
    source = Path(tmux_launch_env_loader.__file__).read_text()
    return subprocess.run(
        [sys.executable, "-I", "-c", source, str(path), nonce, command or probe_command()],
        input=b"", capture_output=True, timeout=10,
        env={"HOME": str(path.parents[5]), "PATH": os.defpath},
    )
