"""Run Pinky daemon or API server.

Usage:
    # API server (stateful sessions via HTTP)
    python -m pinky_daemon --mode api --port 8888

    # Polling daemon (auto-processes inbound messages)
    python -m pinky_daemon --mode poll --config pinky.yaml

    # Default is API mode
    python -m pinky_daemon
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import os
import signal
import sys


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


def main() -> None:
    _install_faulthandler()
    _load_dotenv()
    _quiet_http_client_loggers()
    parser = argparse.ArgumentParser(description="Pinky — headless Claude Code")
    parser.add_argument(
        "--mode",
        choices=["api", "poll"],
        default="api",
        help="Run mode: api (HTTP server) or poll (message polling daemon)",
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
    args = parser.parse_args()

    if args.mode == "api":
        _run_api(args)
    elif args.mode == "poll":
        _run_poll(args)


def _run_api(args) -> None:
    """Start the stateful API server under the lifetime store-authority lock."""
    from pinky_daemon.store_authority import store_authority_lock

    with store_authority_lock(args.db_path):
        _run_api_with_authority(args)


def _run_api_with_authority(args) -> None:
    """Construct and serve the API while the caller holds store authority."""
    import uvicorn

    from pinky_daemon.api import create_api

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
        # Default / current prod: single server, unchanged behavior. If the
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
        uvicorn.run(app, host=args.host, port=args.port)
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
        uvicorn.run(app, host=args.host, port=args.port)
        return

    ferry_app = build_ferry_app(host_pinky=host_pinky, config=ferry_cfg)
    print(
        f"[pinky] Ferry listener: {ferry_cfg.bind_host}:{ferry_cfg.bind_port} "
        f"(fleet={ferry_cfg.fleet_name})",
        file=sys.stderr,
    )

    main_server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port))
    ferry_server = uvicorn.Server(
        uvicorn.Config(ferry_app, host=ferry_cfg.bind_host, port=ferry_cfg.bind_port)
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
