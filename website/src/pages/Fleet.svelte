<script>
  import { onMount } from 'svelte';
  import { link, push } from 'svelte-spa-router';

  let daemonUrl = $state('');
  let agents = $state([]);
  let loading = $state(true);
  let error = $state('');

  onMount(async () => {
    daemonUrl = localStorage.getItem('pinky_daemon_url') || '';
    const apiKey = localStorage.getItem('pinky_api_key') || '';

    if (!daemonUrl || !apiKey) {
      push('/login');
      return;
    }

    try {
      const res = await fetch(`${daemonUrl}/agents`, {
        headers: { 'X-API-Key': apiKey },
      });
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json();
      agents = Array.isArray(data) ? data : (data.agents || []);
    } catch (err) {
      error = `Could not reach daemon at ${daemonUrl}: ${err.message}`;
    } finally {
      loading = false;
    }
  });

  function statusClass(agent) {
    if (agent.alive) return 'alive';
    if (agent.status === 'error') return 'error';
    return 'offline';
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
            <span class="agent-name">{agent.name}</span>
            <span class="status-pill {statusClass(agent)}">
              {agent.alive ? 'Online' : 'Offline'}
            </span>
          </div>
          {#if agent.model}
            <span class="agent-model">{agent.model}</span>
          {/if}
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

  .agent-name {
    font-size: 1.1rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--text);
  }

  .status-pill {
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

  .status-pill.offline {
    background: rgba(148, 163, 184, 0.12);
    color: var(--text-muted);
    border: 1px solid rgba(148, 163, 184, 0.15);
  }

  .status-pill.error {
    background: rgba(248, 113, 113, 0.12);
    color: var(--red);
    border: 1px solid rgba(248, 113, 113, 0.25);
  }

  .agent-model {
    font-family: var(--mono);
    font-size: 0.68rem;
    color: var(--text-muted);
    letter-spacing: 0.03em;
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
