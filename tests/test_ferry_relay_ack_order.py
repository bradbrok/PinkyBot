"""Acknowledgement observes committed terminal metadata, never a pending claim."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.test_ferry_relay_inbound import receiver as receiver
from tests.test_ferry_relay_inbound import sealed_row


async def test_ack_observes_a_terminal_row_not_an_absent_claim(receiver):
    observed = []

    async def ack(ids):
        record = receiver.replay._db.execute("SELECT state FROM relay_claims").fetchone()
        observed.append((ids, record[0] if record is not None else None))

    client = SimpleNamespace(poll=AsyncMock(return_value=[sealed_row(receiver)]), ack=ack)
    await receiver.run_once(client)
    assert observed == [(["row-one"], "DELIVERED")]
    assert receiver.host.deliver.await_count == 1
