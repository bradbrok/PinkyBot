"""Directive mutations preserve rows owned by a different path account."""
from __future__ import annotations

import inspect
import json

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import create_api


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PINKY_TOOL_POLICY', 'off')
    app = create_api(db_path=str(tmp_path / 'memory.db'), default_working_dir=str(tmp_path))
    app.state.agents.register('first')
    app.state.agents.register('second')
    client = TestClient(app)
    try:
        yield app, client
    finally:
        client.close()
        app.state.store_catalog.close()


def row_bytes(registry, owner):
    return json.dumps([row.to_dict() for row in registry.get_directives(owner, active_only=False)],
                      sort_keys=True).encode()


@pytest.mark.parametrize('operation', ['toggle', 'delete'])
def test_directive_route_checks_path_owner(application, operation):
    app, client = application
    registry = app.state.agents
    row = registry.add_directive('second', 'Example rule')
    before = row_bytes(registry, 'second')
    path = f'/agents/first/directives/{row.id}'
    response = (client.post(path + '/toggle', params={'active': False})
                if operation == 'toggle' else client.delete(path))
    after = row_bytes(registry, 'second')
    assert response.status_code == 404, response.text
    assert response.json() == {'detail': 'Directive not found'}
    assert after == before

    owned = f'/agents/second/directives/{row.id}'
    response = (client.post(owned + '/toggle', params={'active': False})
                if operation == 'toggle' else client.delete(owned))
    assert response.status_code == 200, response.text
    remaining = registry.get_directives('second', active_only=False)
    if operation == 'delete':
        assert remaining == []
    else:
        assert len(remaining) == 1 and remaining[0].active is False
        assert client.post(owned + '/toggle').status_code == 200
        assert registry.get_directives('second')[0].active is True


@pytest.mark.parametrize('operation', ['toggle', 'delete'])
def test_directive_registry_checks_owner(tmp_path, operation):
    registry = AgentRegistry(str(tmp_path / 'agents.db'))
    try:
        for name in ('first', 'second'):
            registry.register(name)
        row = registry.add_directive('second', 'Example rule')
        before = row_bytes(registry, 'second')
        method = registry.toggle_directive if operation == 'toggle' else registry.remove_directive
        args = (row.id, False) if operation == 'toggle' else (row.id,)
        # Exercise the earlier signature as well so failure observes a row change.
        scoped = 'agent_name' in inspect.signature(method).parameters
        changed = method(*args, **({'agent_name': 'first'} if scoped else {}))
        assert changed is False
        assert row_bytes(registry, 'second') == before
        assert scoped
        parameter = inspect.signature(method).parameters['agent_name']
        assert parameter.kind == inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
        assert method(*args, agent_name='second') is True
    finally:
        registry.close()
