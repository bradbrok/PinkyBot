"""Uncertain delivery stays explicit at the final send and HTTP response boundaries."""

import json
import urllib.error
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pinky_outreach.telegram import TelegramAdapter, TelegramError
from tests.test_api_delivery_unknown import _assert_unknown, _file, _quote, _text
from tests.test_api_delivery_unknown import sender as sender
from tests.test_messaging_delivery_unknown import SEND_CASES, _tools


@pytest.mark.parametrize('error', [
    httpx.ReadTimeout('Telegram reply timed out'),
    httpx.ReadError('Telegram connection lost'),
    json.JSONDecodeError('bad Telegram response', '<html>', 0),
], ids=['timeout', 'transport', 'non-json'])
def test_gif_send_unknown_is_not_plain_502(sender, error):
    search = {'data': [{'images': {'original': {'url': 'https://media.invalid/x.gif'}}}]}
    with (
        patch('urllib.request.urlopen', side_effect=[
            BytesIO(json.dumps(search).encode()), BytesIO(b'synthetic GIF'),
        ]) as upstream,
        patch.object(TelegramAdapter, 'send_animation', side_effect=error) as send,
    ):
        response = sender.client.post('/broker/send-gif', json={
            'agent_name': 'sender', 'platform': 'telegram', 'chat_id': '12345',
            'query': 'synthetic',
        })
    assert upstream.call_count == 2
    assert send.call_count == 1
    _assert_unknown(sender, response)


@pytest.mark.parametrize('stage', ['search', 'download'])
def test_gif_presend_failure_keeps_502(sender, stage):
    search = {'data': [{'images': {'original': {'url': 'https://media.invalid/x.gif'}}}]}
    replies = [TimeoutError('Giphy unavailable')] if stage == 'search' else [
        BytesIO(json.dumps(search).encode()), TimeoutError('download unavailable'),
    ]
    with (
        patch('urllib.request.urlopen', side_effect=replies),
        patch.object(TelegramAdapter, 'send_animation') as send,
    ):
        response = sender.client.post('/broker/send-gif', json={
            'agent_name': 'sender', 'platform': 'telegram', 'chat_id': '12345',
            'query': 'synthetic',
        })
    send.assert_not_called()
    assert response.status_code == 502, response.text
    assert 'delivery unknown' not in response.text


@pytest.mark.parametrize('name,kwargs', SEND_CASES, ids=[case[0] for case in SEND_CASES])
def test_mcp_timeout_reading_http_error_body_is_unknown(name, kwargs):
    body = MagicMock()
    body.read.side_effect = TimeoutError('error body read timed out')
    error = urllib.error.HTTPError('http://test', 504, 'Gateway Timeout', {}, body)
    with patch('urllib.request.urlopen', side_effect=error) as request:
        result = json.loads(_tools()[name](**kwargs))
    request.assert_called_once()
    assert result.get('delivery') == 'unknown', result
    assert result['error'].startswith('delivery unknown:')
    assert 'do not resend' in result['error']
    assert 'check the chat first' in result['error']


@pytest.mark.parametrize('name,kwargs', SEND_CASES, ids=[case[0] for case in SEND_CASES])
def test_mcp_timeout_reading_success_body_is_unknown(name, kwargs):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.side_effect = TimeoutError('success body read timed out')
    with patch('urllib.request.urlopen', return_value=response) as request:
        result = json.loads(_tools()[name](**kwargs))
    request.assert_called_once()
    assert result.get('delivery') == 'unknown', result


@pytest.mark.parametrize('kind', ['text', 'photo', 'document', 'video', 'animation'])
@pytest.mark.parametrize('failure', ['timeout', 'protocol', 'non-json'])
def test_real_adapter_http_boundary_is_unknown(sender, monkeypatch, kind, failure):
    requests = []

    def respond(request):
        requests.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('slow response', request=request)
        if failure == 'protocol':
            raise httpx.RemoteProtocolError('connection lost', request=request)
        return httpx.Response(200, content=b'<html>bad gateway</html>')

    original_init = TelegramAdapter.__init__

    def initialize(adapter, *args, **kwargs):
        original_init(adapter, *args, **kwargs)
        adapter._client.close()
        adapter._client = httpx.Client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(TelegramAdapter, '__init__', initialize)
    response = _text(sender) if kind == 'text' else _file(sender, kind)
    assert len(requests) == 1
    _assert_unknown(sender, response)


def test_quote_plain_transport_error_does_not_degrade_again(sender):
    with patch.object(TelegramAdapter, 'send_message', side_effect=[
        TelegramError("Bad Request: can't parse entities: quote markup", 400), httpx.ReadTimeout('plain quote timed out'),
    ]) as send:
        response = _quote(sender)
    assert send.call_count == 2
    assert all(call.kwargs['reply_parameters'] is not None for call in send.call_args_list)
    _assert_unknown(sender, response)


def test_broadcast_group_loop_continues_once(sender, monkeypatch):
    monkeypatch.setattr(sender.app.state.agents, 'list_approved_users', lambda _: [])
    monkeypatch.setattr(sender.app.state.agents, 'list_group_chats', lambda _: [
        {'platform': 'telegram', 'chat_id': chat} for chat in ('11111', '22222', '33333')
    ])

    def respond(chat_id, *args, **kwargs):
        if chat_id == '22222':
            raise httpx.ReadTimeout('slow middle group')
        return SimpleNamespace(message_id='message-' + chat_id)

    with patch.object(TelegramAdapter, 'send_message', side_effect=respond) as send:
        response = sender.client.post('/broker/broadcast', json={
            'agent_name': 'sender', 'content': 'synthetic broadcast',
        })
    assert [call.args[0] for call in send.call_args_list] == ['11111', '22222', '33333']
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['count'] == 2
    assert body['delivery_unknown'] == ['22222']
    middle = next(item for item in body['deliveries'] if item['chat_id'] == '22222')
    assert middle['delivery'] == 'unknown'
    assert middle['error'].startswith('delivery unknown:')
    assert 'do not resend' in middle['error']
    assert 'check the chat first' in middle['error']
    entries = {item['chat_id']: item for item in body['deliveries']}
    assert entries['11111']['sent'] is True
    assert entries['33333']['sent'] is True
    assert [call.args for call in sender.stopped.call_args_list] == [
        ('sender', '11111'), ('sender', '22222'), ('sender', '33333'),
    ]
    assert any('outcome=error_upstream' in line and 'error=delivery_unknown:' in line
               for line in sender.logs)
