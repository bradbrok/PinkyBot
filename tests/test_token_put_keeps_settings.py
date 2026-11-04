"""Token updates preserve omitted settings and replace explicitly supplied settings."""

from __future__ import annotations

import pytest

from pinky_daemon import pollers
from tests.test_token_settings_redaction import request
from tests.test_token_settings_redaction import service as service

pytestmark = pytest.mark.real_auth

OLD_TOKEN = "xoxb-FAKE-OLD"
NEW_TOKEN = "xoxb-FAKE-NEW"
APP_TOKEN = "xapp-FAKE"
OTHER_APP_TOKEN = "xapp-OTHER"


def _assert_redacted(response):
    assert response.status_code == 200, response.text
    for value in (OLD_TOKEN, NEW_TOKEN, APP_TOKEN, OTHER_APP_TOKEN):
        assert value not in response.text


@pytest.mark.parametrize("update", [None, {}, {"app_token": OTHER_APP_TOKEN}],
                         ids=["omitted", "empty", "replace"])
def test_slack_token_put_preserves_only_omitted_settings(service, update):
    original = {"app_token": APP_TOKEN}
    service.registry.set_token(
        "tenant", "slack", OLD_TOKEN, settings=original, enabled=False, token_ref="previous-ref",
    )
    body = {"token": NEW_TOKEN, "token_ref": ""}
    if update is not None:
        body["settings"] = update

    response = request(service, "PUT", "/agents/tenant/tokens/slack", caller="operator", body=body)

    _assert_redacted(response)
    stored = service.registry.get_token("tenant", "slack")
    assert stored.settings == (original if update is None else update)
    assert service.registry.get_raw_token("tenant", "slack") == NEW_TOKEN
    assert stored.enabled is True
    assert stored.token_ref == ""
    assert service.started == []


def test_token_put_without_existing_row_stores_empty_settings(service):
    assert service.registry.get_token("tenant", "slack") is None

    response = request(
        service, "PUT", "/agents/tenant/tokens/slack", caller="operator",
        body={"token": NEW_TOKEN, "token_ref": ""},
    )

    _assert_redacted(response)
    assert service.registry.get_token("tenant", "slack").settings == {}
    assert service.registry.get_raw_token("tenant", "slack") == NEW_TOKEN


def test_discord_token_only_put_restarts_with_stored_settings(service):
    settings = {"poll_interval_sec": 2.5, "watched_channels": ["FAKE-CHANNEL"]}
    service.registry.set_token("tenant", "discord", "FAKE-OLD-DISCORD", settings=settings)
    try:
        response = request(
            service, "PUT", "/agents/tenant/tokens/discord", caller="operator",
            body={"token": "FAKE-NEW-DISCORD", "token_ref": ""},
        )

        _assert_redacted(response)
        assert len(service.started) == 1
        restarted = service.started[0]
        assert isinstance(restarted, pollers.BrokerDiscordPoller)
        assert restarted._poll_interval == 2.5
        assert restarted._configured_channels == ["FAKE-CHANNEL"]
        assert service.registry.get_token("tenant", "discord").settings == settings
        assert service.registry.get_raw_token("tenant", "discord") == "FAKE-NEW-DISCORD"
        assert "FAKE-NEW-DISCORD" not in response.text
    finally:
        for restarted in service.started:
            restarted.stop()
            restarted._adapter.close()
