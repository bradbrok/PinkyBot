"""Prova mirata: una riga in quarantena può affamare una riga viva più vecchia.

Isolato dal resto della suite apposta — serve a decidere se ripristinare la
guardia `if pending.parked_at != 0: continue` nel replay di `scheduler.py`.

Il cron è ORARIO di proposito: con `* * * * *` la finestra di replay vale 60s
(`_pending_wake_replay_max_age` = prossima ricorrenza) e la riga vecchia verrebbe
scartata come stale prima di poter dimostrare qualsiasi cosa. Con `0 * * * *` la
finestra è 3600s ed entrambe le righe sono vive dentro la finestra.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.scheduler import AgentScheduler


@pytest.fixture
def registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    reg = AgentRegistry(db_path=path)
    yield reg
    reg.close()
    os.unlink(path)


@pytest.mark.asyncio
async def test_parked_newer_row_must_not_collapse_a_live_older_row(
    registry, monkeypatch
):
    """La riga parcheggiata non deve né essere consegnata né far collassare.

    `list_pending_schedule_wakes(..., include_parked=True)` restituisce anche le
    righe terminali. Una riga parcheggiata il cui schedule è ancora vivo non
    produce `zombie_reason`, quindi supera l'unico `continue` che guarda
    `parked_at` e finisce in `pending_wakes`. Lì diventa `newest_recurring` per
    il suo schedule e fa RECURRENCE_COLLAPSED sulla riga viva più vecchia — che
    è l'unica che avrebbe potuto essere consegnata.
    """
    registry.register("worker")
    # Cron ORARIO: vedi docstring del modulo.
    schedule = registry.add_schedule(
        "worker", "0 * * * *", name="ricorrente", prompt="prompt vivo"
    )
    # 1_800_000_000 == 2027-01-15 00:00:00 -0800, esattamente sul minuto :00,
    # quindi la prossima ricorrenza è +3600s e la finestra di replay è piena.
    older_fired_at = 1_800_000_000.0
    older, _ = registry.persist_schedule_wake(
        schedule.id,
        agent_name="worker",
        schedule_name=schedule.name,
        prompt=schedule.prompt,
        fired_at=older_fired_at,
    )
    newer, _ = registry.persist_schedule_wake(
        schedule.id,
        agent_name="worker",
        schedule_name=schedule.name,
        prompt=schedule.prompt,
        fired_at=older_fired_at + 60.0,
    )
    # La più NUOVA finisce in quarantena.
    assert registry.park_pending_schedule_wake(
        newer.id, reason="terminal replay policy: drain-extension budget"
    )

    submissions: list[str] = []

    async def confirmed(agent_name, session_id, prompt):
        del agent_name, session_id
        submissions.append(prompt)
        return True

    scheduler = AgentScheduler(registry, wake_callback=confirmed)
    monkeypatch.setattr(
        "pinky_daemon.scheduler.time.time", lambda: older_fired_at + 120.0
    )
    await scheduler._replay_pending_locked("worker")

    by_id = {row.id: row for row in registry.list_schedule_wake_ledger("worker")}
    # La riga viva più vecchia deve essere stata consegnata...
    assert submissions == ["prompt vivo"]
    # ...e NON collassata dentro una riga terminale.
    assert "recurrence collapsed" not in (by_id[older.id].last_error or "").lower()
