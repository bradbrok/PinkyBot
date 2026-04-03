<script>
  import { onMount } from 'svelte';
  import { link } from 'svelte-spa-router';

  let daemonUrl = '';
  let apiKey = '';
  let presentations = [];
  let loading = true;
  let error = '';

  onMount(async () => {
    daemonUrl = localStorage.getItem('pinky_daemon_url') || '';
    apiKey = localStorage.getItem('pinky_api_key') || '';

    if (!daemonUrl || !apiKey) {
      loading = false;
      return;
    }

    try {
      const res = await fetch(`${daemonUrl}/presentations`, {
        headers: { 'X-API-Key': apiKey },
      });
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json();
      presentations = data.presentations || [];
    } catch (err) {
      error = `Could not load presentations: ${err.message}`;
    } finally {
      loading = false;
    }
  });

  function shareUrl(p) {
    return `${daemonUrl}/p/${p.share_token}`;
  }

  function timeAgo(iso) {
    if (!iso) return '';
    const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }
</script>

<nav class="nav">
  <a href="/#/" use:link class="nav-logo">Pinky.</a>
  <div class="nav-links">
    <a href="/#/" use:link class="nav-link">Home</a>
    <a href="/#/fleet" use:link class="nav-link">Fleet</a>
    <a href="/#/login" use:link class="btn btn-primary btn-sm nav-cta">Connect →</a>
  </div>
</nav>

<main class="section">
  <p class="section-eyebrow">Published by your agents</p>
  <h1 class="section-title" style="margin-bottom: 2.5rem;">Presentations</h1>

  {#if loading}
    <div class="state-msg">Loading…</div>
  {:else if !daemonUrl}
    <div class="connect-prompt">
      <p>Connect your Pinky daemon to see published presentations.</p>
      <a href="/#/login" use:link class="btn btn-primary" style="margin-top: 1rem;">Connect →</a>
    </div>
  {:else if error}
    <div class="error-banner">{error}</div>
  {:else if presentations.length === 0}
    <div class="connect-prompt">
      <p>No presentations yet. Your agents can create them via the MCP tools.</p>
    </div>
  {:else}
    <div class="gallery-grid">
      {#each presentations as p}
        <div class="card deck-card">
          <div class="deck-thumb">
            <span class="deck-version">v{p.current_version}</span>
          </div>
          <div class="deck-meta">
            <h2 class="deck-title">{p.title}</h2>
            {#if p.description}
              <p class="deck-desc">{p.description}</p>
            {/if}
            <div class="deck-info">
              <span class="deck-agent">by {p.created_by}</span>
              <span class="deck-date">{timeAgo(p.updated_at)}</span>
            </div>
            {#if p.tags?.length}
              <div class="deck-tags">
                {#each p.tags as tag}
                  <span class="tag">{tag}</span>
                {/each}
              </div>
            {/if}
          </div>
          {#if p.share_token}
            <a href={shareUrl(p)} target="_blank" rel="noopener noreferrer" class="btn btn-ghost btn-sm">View deck →</a>
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
  .gallery-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 1.25rem;
    margin-bottom: 3rem;
  }

  .deck-card {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .deck-thumb {
    height: 140px;
    background: linear-gradient(135deg, var(--surface-2), var(--surface-3));
    border-radius: var(--radius-lg);
    display: flex;
    align-items: flex-end;
    padding: 0.75rem;
  }

  .deck-version {
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--text-muted);
    letter-spacing: 0.06em;
  }

  .deck-meta {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .deck-title {
    font-size: 1rem;
    font-weight: 700;
    letter-spacing: -0.02em;
  }

  .deck-desc {
    font-size: 0.82rem;
    color: var(--text-secondary);
    line-height: 1.45;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .deck-info {
    display: flex;
    gap: 0.75rem;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .deck-agent { color: var(--yellow); }

  .deck-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
  }

  .tag {
    font-family: var(--mono);
    font-size: 0.62rem;
    color: var(--text-muted);
    background: var(--surface-3);
    padding: 0.15rem 0.45rem;
    border-radius: var(--radius-pill);
    letter-spacing: 0.03em;
  }

  .state-msg {
    padding: 3rem;
    text-align: center;
    color: var(--text-muted);
    font-size: 0.9rem;
  }

  .error-banner {
    background: rgba(248, 113, 113, 0.1);
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: var(--radius-lg);
    padding: 1rem 1.25rem;
    color: #fca5a5;
    font-size: 0.85rem;
    margin-bottom: 2rem;
  }

  .connect-prompt {
    text-align: center;
    padding: 3rem;
    background: var(--surface);
    border-radius: var(--radius-xl);
    border: 1px solid rgba(255,255,255,0.05);
    color: var(--text-secondary);
    font-size: 0.9rem;
    display: flex;
    flex-direction: column;
    align-items: center;
  }
</style>
