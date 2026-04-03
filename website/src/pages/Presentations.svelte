<script>
  import { link } from 'svelte-spa-router';

  // Placeholder — will connect to daemon's presentations API
  const mockPresentations = [
    { id: 1, title: 'Q1 Research Brief', agent: 'Barsik', date: '2026-03-28', slides: 12 },
    { id: 2, title: 'AI Infrastructure Overview', agent: 'Pushok', date: '2026-03-25', slides: 8 },
    { id: 3, title: 'Product Roadmap 2026', agent: 'Barsik', date: '2026-03-20', slides: 16 },
  ];
</script>

<nav class="nav">
  <a href="/#/" use:link class="nav-logo">Pinky.</a>
  <div class="nav-links">
    <a href="/#/" use:link class="nav-link">Home</a>
    <a href="/#/login" use:link class="btn btn-primary btn-sm nav-cta">Connect →</a>
  </div>
</nav>

<main class="section">
  <p class="section-eyebrow">Published by your agents</p>
  <h1 class="section-title" style="margin-bottom: 2.5rem;">Presentations</h1>

  <div class="gallery-grid">
    {#each mockPresentations as deck}
      <div class="card deck-card">
        <div class="deck-thumb">
          <span class="deck-count">{deck.slides} slides</span>
        </div>
        <div class="deck-meta">
          <h2 class="deck-title">{deck.title}</h2>
          <div class="deck-info">
            <span class="deck-agent">by {deck.agent}</span>
            <span class="deck-date">{deck.date}</span>
          </div>
        </div>
        <button class="btn btn-ghost btn-sm" disabled>View deck →</button>
      </div>
    {/each}
  </div>

  <div class="gallery-empty-note">
    <p>Connect your daemon to see your agents' published presentations.</p>
    <a href="/#/login" use:link class="btn btn-primary" style="margin-top: 1rem;">Connect →</a>
  </div>
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
    opacity: 0.4;
    pointer-events: none;
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

  .deck-count {
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

  .deck-info {
    display: flex;
    gap: 0.75rem;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .deck-agent { color: var(--yellow); }

  .gallery-empty-note {
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
