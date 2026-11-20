"""Queue logging distinguishes submission from an unresolved receipt."""

import asyncio

from pinky_daemon.api import create_api
from tests.test_scheduler_busy_bound import session


async def test_queue_log_waits_for_exact_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "0")
    monkeypatch.setenv("PINKY_STREAMING_TRANSPORT", "tmux")
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    app.state.agents.register("worker", working_dir=str(tmp_path), transport="tmux")
    transport, _ = session(tmp_path)
    receipt = asyncio.get_running_loop().create_future()

    async def send(prompt, *, on_accept=None, busy_deliver_at=None):
        return receipt

    transport.send_scheduler_prompt = send
    app.state.broker._streaming["worker"] = {"main": transport}
    try:
        assert await app.state.scheduler._wake_callback("worker", "main", "work") is receipt
        assert not receipt.done()
        queued = [line for line in capsys.readouterr().err.splitlines()
                  if "scheduler: queued" in line]
        assert len(queued) == 1
        assert "confirmed" not in queued[0]
        assert queued[0] == (
            "scheduler: queued wake for worker via streaming main; awaiting exact receipt"
        )
    finally:
        receipt.cancel()
        await transport.disconnect()
        app.state.agents.close()
