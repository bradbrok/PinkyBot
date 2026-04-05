<script>
  import { onMount, onDestroy } from 'svelte';
  import { link, push } from 'svelte-spa-router';

  let daemonUrl = $state('');
  let agents = $state([]);
  let loading = $state(true);
  let error = $state('');
  let pollTimer = null;

  async function loadAgents(url, key) {
    try {
      const res = await fetch(`${url}/agents`, {
        headers: { 'X-API-Key': key },
      });
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json();
      agents = Array.isArray(data) ? data : (data.agents || []);
      error = '';
    } catch (err) {
      error = `Could not reach daemon at ${url}: ${err.message}`;
    }
  }

  onMount(async () => {
    daemonUrl = localStorage.getItem('pinky_daemon_url') || '';
    const apiKey = localStorage.getItem('pinky_api_key') || '';

    if (!daemonUrl || !apiKey) {
      push('/login');
      return;
    }

    await loadAgents(daemonUrl, apiKey);
    loading = false;

    // Poll every 30s to keep statuses fresh
    pollTimer = setInterval(() => loadAgents(daemonUrl, apiKey), 30_000);
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
  });

  function statusClass(agent) {
    if (agent.working_status === 'working') return 'working';
    if (agent.working_status === 'idle') return 'alive';
    return 'offline';
  }

  function statusLabel(agent) {
    if (agent.working_status === 'working') return 'Working';
    if (agent.working_status === 'idle') return 'Online';
    return 'Offline';
  }

  function modelLabel(model) {
    if (!model) return null;
    // Strip "claude-" prefix for display
    return model.replace(/^claude-/, '');
  }

  function timeAgo(iso) {
    if (!iso) return null;
    const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (diff < 5) return 'just now';
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }
</script>

<nav class="nav">
  <a href="/#/" use:link class="nav-logo">Pinky.</a>
  <div class="nav-links">
    <a href="/#/presentations" use:link class="nav-link">Presentations</a>
    <span class="nav-daemon">{daemonUrl || '…'}</span>
  </div>
</nav>

<main class="section">
  <p class="section-eyebrow">Your fleet</p>
  <h1 class="section-title" style="margin-bottom: 2.5rem;">Agents</h1>

  {#if loading}
    <div class="state-message">Loading fleet…</div>
  {:else if error}
    <div class="error-banner">{error}</div>
  {:else if agents.length === 0}
    <div class="state-message">No agents found on this daemon.</div>
  {:else}
    <div class="agents-grid">
      {#each agents as agent}
        <div class="card agent-card">
          <div class="agent-header">
            <div class="agent-name-row">
              {#if agent.working_status === 'working'}
                <span class="pulse-dot" aria-label="working"></span>
              {/if}
              <span class="agent-name">{agent.display_name || agent.name}</span>
            </div>
            <span class="status-pill {statusClass(agent)}">
              {statusLabel(agent)}
            </span>
          </div>

          <div class="agent-meta">
            {#if modelLabel(agent.model)}
              <span class="agent-model">{modelLabel(agent.model)}</span>
            {/if}
            {#if agent.working_status_updated_at}
              <span class="agent-seen">· {timeAgo(agent.working_status_updated_at)}</span>
            {/if}
          </div>

          {#if agent.soul_summary || agent.role}
            <p class="agent-bio">{agent.soul_summary || agent.role}</p>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</main>

<footer class="footer">
  pinkybot.ai &nbsp;·&nbsp; Built with Claude Code
</footer>

<style>
  .agents-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 1.25rem;
  }

  .agent-card {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .agent-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
  }

  .agent-name-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 0;
  }

  .agent-name {
    font-size: 1.1rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Pulsing working indicator */
  .pulse-dot {
    display: inline-block;
    flex-shrink: 0;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--yellow);
    box-shadow: 0 0 6px var(--yellow);
    animation: pulse-work 1.2s ease-in-out infinite;
  }

  @keyframes pulse-work {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.8); }
  }

  .status-pill {
    flex-shrink: 0;
    font-family: var(--mono);
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem;
    border-radius: var(--radius-pill);
  }

  .status-pill.alive {
    background: rgba(74, 222, 128, 0.15);
    color: var(--green);
    border: 1px solid rgba(74, 222, 128, 0.3);
  }

  .status-pill.working {
    background: rgba(249, 216, 73, 0.14);
    color: var(--yellow);
    border: 1px solid rgba(249, 216, 73, 0.3);
  }

  .status-pill.offline {
    background: rgba(148, 163, 184, 0.12);
    color: var(--text-muted);
    border: 1px solid rgba(148, 163, 184, 0.15);
  }

  .agent-meta {
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }

  .agent-model {
    font-family: var(--mono);
    font-size: 0.68rem;
    color: var(--text-muted);
    letter-spacing: 0.03em;
  }

  .agent-seen {
    font-family: var(--mono);
    font-size: 0.68rem;
    color: var(--text-muted);
    letter-spacing: 0.02em;
  }

  .agent-bio {
    font-size: 0.85rem;
    color: var(--text-secondary);
    line-height: 1.55;
  }

  .state-message {
    padding: 3rem;
    text-align: center;
    color: var(--text-muted);
    font-size: 0.9rem;
    background: var(--surface);
    border-radius: var(--radius-xl);
    border: 1px solid rgba(255,255,255,0.04);
  }

  .error-banner {
    background: rgba(248, 113, 113, 0.1);
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: var(--radius-lg);
    padding: 1rem 1.25rem;
    color: #fca5a5;
    font-size: 0.85rem;
  }

  .nav-daemon {
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--text-muted);
    letter-spacing: 0.03em;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
