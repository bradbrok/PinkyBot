"""Own scratch-only checks for descriptor validation outside ordinary polling."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.codex_home import codex_home_for
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.tmux_transcript import TmuxTranscriptTailer, claude_project_slug
from pinky_identity import live_sqlite
from tests.isolated_policy_support import closure, replace_cell, signed
from tests.isolated_policy_support import daemon as daemon

pytestmark = pytest.mark.real_auth

def replace_path(own, peer, swap):
    if swap == 'file':
        own.unlink()
        own.symlink_to(peer)
    else:
        own.parent.rename(own.parent.with_name(own.parent.name+'-parked'))
        own.parent.symlink_to(peer.parent,target_is_directory=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('per_agent',[False,True])
@pytest.mark.parametrize('swap',['file','directory'])
async def test_codex_discovery_to_bind_swap_never_dispatches_peer(daemon,monkeypatch,per_agent,swap):
    d=daemon()
    monkeypatch.setenv('PINKY_CODEX_PER_AGENT_HOME','1' if per_agent else '0')
    config=StreamingSessionConfig(agent_name='tenant',working_dir=str(d.root/'tenant'))
    own_root=codex_home_for(d.agents.get('tenant'))/'sessions'
    peer_root=codex_home_for(d.agents.get('peer'))/'sessions'
    own=own_root/(d.root.parent.name+'-owned')/'rollout-owned.jsonl'
    peer=peer_root/(d.root.parent.name+'-peer')/own.name
    own.parent.mkdir(parents=True)
    peer.parent.mkdir(parents=True)
    own.write_text(json.dumps({'type':'session_meta','payload':{'id':'own','cwd':config.working_dir}})+'\n')
    peer_entry={'type':'event_msg','payload':{'type':'user_message','message':'foreign harmless fixture'}}
    peer.write_text(json.dumps({'type':'session_meta','payload':{'id':'peer','cwd':str(d.root/'peer')}})+'\n'+json.dumps(peer_entry)+'\n')
    session=CodexTmuxSession(config,registry=d.agents)
    discover=session._discover_transcript_path
    checked=[]
    def swap_after_real_discovery():
        selected=discover()
        assert selected==own
        checked.append(selected)
        replace_path(own,peer,swap)
        return selected
    monkeypatch.setattr(session,'_discover_transcript_path',swap_after_real_discovery)
    async def defer(self):
        pass
    monkeypatch.setattr(TmuxSession,'_start_tailer',defer)
    await session._start_tailer()
    tailer=session._tailer
    entries=[]
    tailer._on_entry=entries.append
    tailer.set_offset(0)
    consumed=await tailer.read_once()
    assert checked==[own]
    assert (consumed,tailer.offset,entries)==(0,0,[]), (getattr(tailer,'_owned_path',None),getattr(tailer,'_owned_root',None),entries)

@pytest.mark.asyncio
@pytest.mark.parametrize('swap',['file','directory'])
async def test_claude_paste_ticket_does_not_read_rejected_peer(daemon,swap):
    d=daemon()
    d.agents.update('tenant',runtime='claude_sdk',transport='tmux')
    session=await closure(d.app,'_prepare_streaming_session')('tenant')
    own_dir=Path.home()/'.claude/projects'/claude_project_slug(d.root/'tenant')
    peer_dir=Path.home()/'.claude/projects'/claude_project_slug(d.root/'peer')
    own=own_dir/'fixture.jsonl'
    peer=peer_dir/own.name
    own_dir.mkdir(parents=True)
    peer_dir.mkdir(parents=True)
    own.write_text('{}\n')
    foreign_bytes=b'foreign harmless fixture\n'
    peer.write_bytes(foreign_bytes)
    session._tailer=TmuxTranscriptTailer(own,lambda turn:None,owned_projects=session._transcript_ownership)
    session._tailer.set_offset(3)
    replace_path(own,peer,swap)
    assert await session._tailer.read_once()==0
    assert session._tailer.offset==3
    ticket=session._capture_transcript_occurrence_ticket()
    assert ticket.anchor is None, ticket

@pytest.mark.parametrize('mode',['off','shadow','enforce'])
def test_sqlite_rejection_preserves_existing_writer_lock(daemon,monkeypatch,mode):
    d=daemon(mode)
    own=d.root/'tenant'/'fixture.db'
    connection=sqlite3.connect(own,factory=live_sqlite.LiveSQLiteConnection)
    live_sqlite.track_sqlite_connection(connection,own)
    connection.execute('CREATE TABLE fixture(value TEXT)').close()
    connection.commit()
    connection.execute('BEGIN IMMEDIATE').close()
    script=("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); "
            "c.execute('BEGIN IMMEDIATE'); c.rollback(); c.close()")
    def contender():
        return subprocess.run([sys.executable,'-c',script,str(own)],capture_output=True,text=True)
    effects=[]
    replace_cell(monkeypatch,closure(d.app,'_send_file_message'),'_get_platform_adapter',
                 lambda *args:SimpleNamespace(send_document=lambda *a,**kw:effects.append(a)))
    before=contender()
    assert before.returncode!=0 and 'database is locked' in before.stderr
    route='/broker/send-document'
    client=TestClient(d.app)
    try:
        response=client.post(route,headers=signed(d,'POST',route),json={
            'agent_name':'tenant','chat_id':'fixture','file_path':str(own)})
        assert (response.status_code,effects)==(400,[]),response.text
        after=contender()
        assert after.returncode!=0 and 'database is locked' in after.stderr, (
            response.status_code,after.returncode,after.stdout,after.stderr)
    finally:
        client.close()
        connection.rollback()
        connection.close()
