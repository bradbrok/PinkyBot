"""Run Pinky daemon or API server.

Usage:
    # API server (stateful sessions via HTTP)
    python -m pinky_daemon --mode api --port 8888

    # Polling daemon (auto-processes inbound messages)
    python -m pinky_daemon --mode poll --config pinky.yaml

    # Inspect startup imports and storage layout without opening stores
    python -m pinky_daemon --mode check-startup --db-path data/conversations.db

    # Default is API mode
    python -m pinky_daemon
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import io
import json
import os
import signal
import sys
import time
from pathlib import Path


def _install_faulthandler() -> None:
    """Wire SIGUSR2 to dump Python tracebacks of every thread to stderr.

    On-demand stack dump for diagnosing wedged daemons. macOS `sample` captures
    C frames only; `py-spy` requires sudo. faulthandler.register gives us
    per-thread Python tracebacks with no special perms.

    Usage:
        kill -USR2 <pid>     # prints tracebacks to stderr (-> api.log)

    Cost: stdlib only, no overhead until the signal fires. Idempotent.
    SIGUSR2 is reserved for application use; not consumed by Python or uvicorn.
    """
    try:
        faulthandler.register(signal.SIGUSR2, all_threads=True, chain=False)
    except (AttributeError, RuntimeError):
        # SIGUSR2 is Unix-only; chain=False is 3.5+. Both hold on our targets,
        # but degrade gracefully if a future runtime drops either.
        pass


def _load_dotenv() -> None:
    """Load .env file from working directory if it exists."""
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:  # Don't override existing env vars
                os.environ[key] = value


def _quiet_http_client_loggers() -> None:
    """Drop httpx/httpcore loggers to WARNING so they never log request URLs.

    httpx logs every request line at INFO, including the full URL. For the
    Telegram/Discord polling loops that URL embeds the bot token in the path
    (``api.telegram.org/bot<token>/getUpdates``). The daemon's stdout/stderr is
    redirected to ``logs/api.log`` (launchd ``StandardOutPath``), so at INFO
    this wrote live bot tokens to the log in cleartext on every poll — millions
    of lines. Raising both loggers to WARNING filters the request lines (and any
    DEBUG-level ``Authorization`` headers) at the logger before they reach a
    handler, regardless of what uvicorn/root installs. Errors still surface.

    Runs for both api and poll modes, before any HTTP client starts.
    """
    import logging

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _start_api_guard(args):
    if args.mode != "api":
        return None
    from pinky_daemon.deploy_guard import begin_start

    args._deploy_guard = begin_start(
        str(Path(__file__).resolve().parents[2]),
        Path(os.path.realpath(args.db_path)).parent / "deploy",
    )
    return args._deploy_guard


def _minimal_start_args():
    mode_parser = argparse.ArgumentParser(add_help=False)
    mode_parser.add_argument("--mode", default="api")
    mode_parser.add_argument("--db-path", default="data/conversations.db")
    mode, _ = mode_parser.parse_known_args()
    return mode


def _record_bootstrap_failure(args, exc):
    guard = _start_api_guard(args)
    if guard is not None:
        try:
            guard.failed(exc)
        finally:
            guard.close()


def main() -> None:
    _install_faulthandler()
    try:
        _load_dotenv()
    except (Exception, SystemExit) as exc:
        mode = _minimal_start_args()
        if mode.mode == "api":
            _record_bootstrap_failure(mode, exc)
        if mode.mode != "check-startup":
            raise
        sys.exit(_startup_check_incomplete(exc))
    _quiet_http_client_loggers()
    parser = argparse.ArgumentParser(description="Pinky — headless Claude Code")
    parser.add_argument(
        "--mode",
        choices=["api", "poll", "check-startup"],
        default="api",
        help="Run mode: api (HTTP server), poll (message polling), or check-startup (inspection)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="API server host")
    parser.add_argument("--port", type=int, default=8888, help="API server port")
    parser.add_argument(
        "--config",
        default=os.environ.get("PINKY_CONFIG", "pinky.yaml"),
        help="Config file (poll mode)",
    )
    parser.add_argument(
        "--working-dir",
        default=".",
        help="Working directory (where CLAUDE.md lives)",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=50,
        help="Max concurrent sessions (api mode)",
    )
    parser.add_argument(
        "--db-path",
        default="data/conversations.db",
        help="Canonical conversations DB path (api mode)",
    )
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        if exc.code:
            _record_bootstrap_failure(_minimal_start_args(), exc)
        raise
    guard = _start_api_guard(args)

    if args.mode == "check-startup":
        sys.exit(_run_startup_check(args))
    elif args.mode == "api":
        try:
            _run_api(args)
        except BaseException as exc:
            guard.failed(exc)
            raise
        finally:
            guard.close()
    elif args.mode == "poll":
        _run_poll(args)


def _startup_check_incomplete(exc: BaseException) -> int:
    detail = " ".join(str(exc).splitlines())
    print(f"startup check could not complete: {type(exc).__name__}: {detail}", file=sys.stderr)
    return 2


def _run_startup_check(args) -> int:
    """Inspect the API import and main storage layout without opening stores."""
    try:
        # Keep an incomplete check to one diagnostic, including noisy imports.
        diagnostics = io.StringIO()
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            from pinky_daemon import api
            from pinky_daemon.store_catalog import (
                inspect_filesystem_layout,
                read_store_identities,
            )

            db_path = os.path.realpath(args.db_path)
            root = Path(db_path).parent
            manifest = api._derive_api_store_manifest(db_path)
            identities = read_store_identities(os.path.realpath(target.path) for target in manifest.values())
            inspection = inspect_filesystem_layout(root, identities)
            from pinky_daemon import deploy_guard

            deploy_guard.selftest()
            report = json.dumps({
                "ok": not inspection.findings,
                "root": str(root),
                "checked": ["import", "layout", "guard_selftest"],
                "findings": inspection.findings,
                "warnings_count": len(inspection.warnings),
                "warnings": inspection.warnings[:20],
            })
    except (Exception, SystemExit) as exc:
        return _startup_check_incomplete(exc)

    sys.stderr.write(diagnostics.getvalue())
    print(report)
    return 1 if inspection.findings else 0


def _run_api(args) -> None:
    """Start the stateful API server under the lifetime store-authority lock."""
    args._startup_started_at = time.monotonic()
    from pinky_daemon.store_authority import store_authority_lock

    with store_authority_lock(args.db_path):
        guard = getattr(args, "_deploy_guard", None)
        if guard is not None:
            guard.activate()
        _run_api_with_authority(args)


def _run_api_with_authority(args) -> None:
    """Construct and serve the API while the caller holds store authority."""
    import uvicorn

    from pinky_daemon.api import create_api
    from pinky_daemon.api_readiness import ApiReadiness
    from pinky_daemon.routes.triggers import uvicorn_log_config

    # The access log prints raw request paths; /hooks/<token> carries a
    # credential in the path, so every server start gets the redacting config.
    log_config = uvicorn_log_config()

    working_dir = os.path.abspath(args.working_dir)

    print(
        f"[pinky] Starting API server\n"
        f"  Host: {args.host}:{args.port}\n"
        f"  Working dir: {working_dir}\n"
        f"  Max sessions: {args.max_sessions}",
        file=sys.stderr,
    )

    app = create_api(
        max_sessions=args.max_sessions,
        default_working_dir=working_dir,
        db_path=args.db_path,
    )
    if hasattr(args, "_startup_started_at"):
        app.state.startup_started_at = args._startup_started_at

    readiness = getattr(app.state, "api_readiness", None)
    if not isinstance(readiness, ApiReadiness):
        message = "daemon API readiness barrier missing; refusing to start"
        print(f"ERROR {message}", file=sys.stderr)
        raise RuntimeError(message)

    main_server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_config=log_config)
    )
    readiness.attach(main_server)
    original_serve = main_server.serve

    async def _serve_main(*args, **kwargs):
        try:
            await original_serve(*args, **kwargs)
        finally:
            await readiness.close("main server stopped")

    main_server.serve = _serve_main

    def _run_single_listener():
        from uvicorn.main import STARTUP_FAILURE

        try:
            main_server.run()
        except KeyboardInterrupt:
            pass
        if not main_server.started:
            sys.exit(STARTUP_FAILURE)

    from pinky_daemon.ferry.config import FerryConfig
    from pinky_daemon.ferry.listener import FerryListenerState, serve_ferry_with_retry

    ferry_cfg = FerryConfig.from_env()
    listener_state = getattr(app.state, "ferry_listener", None)
    if not isinstance(listener_state, FerryListenerState):
        listener_state = FerryListenerState.from_config(ferry_cfg)
        app.state.ferry_listener = listener_state
    bind = (
        f"{ferry_cfg.bind_host}:{ferry_cfg.bind_port}"
        if ferry_cfg.bind_host
        else ""
    )
    if not ferry_cfg.enabled:
        # Default: one listener. If the
        # operator tried to turn ferry ON but the config is incomplete or the
        # bind host is unsafe, say why (fail-closed — we never bind publicly).
        enabled_requested = (
            os.environ.get("PINKYBOT_FERRY_ENABLED") or ""
        ).strip().lower() in (
            "1", "true", "yes", "on",
        )
        disabled_error = ferry_cfg.why_disabled() if enabled_requested else ""
        listener_state.update(
            "disabled",
            bind=bind,
            last_error=disabled_error,
            retry_count=0,
        )
        if enabled_requested:
            print(f"[pinky] Ferry disabled: {ferry_cfg.why_disabled()}", file=sys.stderr)
        _run_single_listener()
        return

    # Ferry enabled: run the main API + a dedicated ferry listener bound to the
    # Tailscale IP only, so the cross-fleet surface is one authed endpoint and
    # the main API keeps its existing bind. Both servers share one event loop
    # (so HostPinky.deliver dispatches on the same loop the broker runs on).
    from pinky_daemon.ferry.inbound_server import build_ferry_app

    host_pinky = getattr(app.state, "host_pinky", None)
    if host_pinky is None:
        listener_state.update(
            "dead",
            bind=bind,
            last_error="ferry enabled but host_pinky missing",
            retry_count=0,
        )
        print(
            "[pinky] ferry enabled but host_pinky missing — starting API only",
            file=sys.stderr,
        )
        _run_single_listener()
        return

    ferry_app = build_ferry_app(host_pinky=host_pinky, config=ferry_cfg)
    print(
        f"[pinky] Ferry listener: {ferry_cfg.bind_host}:{ferry_cfg.bind_port} "
        f"(fleet={ferry_cfg.fleet_name})",
        file=sys.stderr,
    )

    ferry_server = uvicorn.Server(
        uvicorn.Config(
            ferry_app,
            host=ferry_cfg.bind_host,
            port=ferry_cfg.bind_port,
            log_config=log_config,
        )
    )
    # uvicorn's serve() does `with self.capture_signals():` — a context manager
    # that installs signal.signal(...) handlers. With two servers on one loop the
    # second would clobber the first's handler, leaving one server un-stoppable
    # on SIGINT/SIGTERM. Replace it with a no-op CONTEXT MANAGER (nullcontext) —
    # NOT `lambda: None`, which makes `with None:` raise — so neither server
    # installs handlers; we install ONE loop-level handler that stops both.
    main_server.capture_signals = lambda: contextlib.nullcontext()
    ferry_server.capture_signals = lambda: contextlib.nullcontext()

    async def _serve_both() -> None:
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        async def _wait_for_retry(delay: float) -> None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            except TimeoutError:
                pass

        def _shutdown(*_a) -> None:
            main_server.should_exit = True
            ferry_server.should_exit = True
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except (NotImplementedError, RuntimeError):
                pass
        # main API failures propagate (core); the ferry side is best-effort.
        await asyncio.gather(
            main_server.serve(),
            serve_ferry_with_retry(
                ferry_server,
                listener_state,
                wait=_wait_for_retry,
            ),
        )

    asyncio.run(_serve_both())


def _run_poll(args) -> None:
    """Start the polling daemon."""
    from pinky_daemon.daemon import Daemon, DaemonConfig

    config = DaemonConfig.from_yaml(args.config)
    config.working_dir = os.path.abspath(args.working_dir)

    # Fallback to env vars
    if not config.telegram_token:
        config.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not config.discord_token:
        config.discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not config.slack_token:
        config.slack_token = os.environ.get("SLACK_BOT_TOKEN", "")

    print(
        f"[pinky] Starting polling daemon\n"
        f"  Config: {args.config}\n"
        f"  Working dir: {config.working_dir}\n"
        f"  Telegram: {'yes' if config.telegram_token else 'no'}\n"
        f"  Discord: {'yes' if config.discord_token else 'no'}\n"
        f"  Slack: {'yes' if config.slack_token else 'no'}",
        file=sys.stderr,
    )

    daemon = Daemon(config)
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        print("\n[pinky] Interrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
