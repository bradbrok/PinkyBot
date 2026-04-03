<script>
  import { link } from 'svelte-spa-router';
</script>

<!-- Nav -->
<nav class="nav">
  <span class="nav-logo">Pinky.</span>
  <div class="nav-links">
    <a href="#features" class="nav-link">Features</a>
    <a href="#how-it-works" class="nav-link">How it works</a>
    <a href="/#/presentations" use:link class="nav-link">Gallery</a>
    <a href="/#/login" use:link class="btn btn-primary btn-sm nav-link nav-cta">Connect →</a>
  </div>
</nav>

<!-- Hero -->
<section class="hero-section">
  <div class="hero-inner">
    <div class="hero-badge">
      <span class="status-dot"></span>
      <span>Personal AI Infrastructure</span>
    </div>

    <h1 class="hero-title">
      PINKY<span class="hero-dot">.</span>
    </h1>

    <p class="hero-tagline">
      Your personal AI agent fleet.<br />
      Research, build, ship — while you sleep.
    </p>

    <div class="hero-actions">
      <a href="/#/login" use:link class="btn btn-primary btn-lg">
        Connect Your Agent →
      </a>
      <a href="#how-it-works" class="btn btn-ghost btn-lg">
        See how it works
      </a>
    </div>

    <div class="hero-grid-accent" aria-hidden="true">
      {#each Array(120) as _, i}
        <span class="grid-dot" style="opacity: {Math.random() * 0.3 + 0.05}"></span>
      {/each}
    </div>
  </div>
</section>

<!-- Features -->
<section id="features" class="section">
  <p class="section-eyebrow">What Pinky does</p>
  <h2 class="section-title" style="margin-bottom: 3.5rem;">
    A fleet that works<br />while you don't.
  </h2>

  <div class="features-grid">
    <div class="card feature-card">
      <div class="feature-icon">🤖</div>
      <h3 class="feature-title">Multi-Agent Fleet</h3>
      <p class="feature-body">
        Specialized agents that collaborate, delegate, and coordinate.
        Each with persistent identity, memory, and skills — none of the repetition.
      </p>
    </div>

    <div class="card feature-card">
      <div class="feature-icon">🔬</div>
      <h3 class="feature-title">Research Pipeline</h3>
      <p class="feature-body">
        Agents research topics overnight, write peer-reviewed briefs,
        and surface action items automatically. Wake up to answers.
      </p>
    </div>

    <div class="card feature-card">
      <div class="feature-icon">🎯</div>
      <h3 class="feature-title">Presentations</h3>
      <p class="feature-body">
        Publish polished slide decks from research in one command.
        Share with anyone via link. No PowerPoint, no fussing.
      </p>
    </div>
  </div>
</section>

<!-- How it works -->
<section id="how-it-works" class="section how-section">
  <p class="section-eyebrow">Getting started</p>
  <h2 class="section-title" style="margin-bottom: 3.5rem;">
    Three steps to your fleet.
  </h2>

  <div class="steps">
    <div class="step">
      <div class="step-number">01</div>
      <div class="step-content">
        <h3 class="step-title">Connect your daemon</h3>
        <p class="step-body">
          Point this site at your Pinky daemon with an API key.
          Runs on any machine — your home server, Mac Mini, or cloud box.
        </p>
      </div>
    </div>

    <div class="step-connector" aria-hidden="true"></div>

    <div class="step">
      <div class="step-number">02</div>
      <div class="step-content">
        <h3 class="step-title">Agents get to work</h3>
        <p class="step-body">
          Your agents research, publish briefs, and create presentations —
          storing everything in long-term memory for future reference.
        </p>
      </div>
    </div>

    <div class="step-connector" aria-hidden="true"></div>

    <div class="step">
      <div class="step-number">03</div>
      <div class="step-content">
        <h3 class="step-title">Access from anywhere</h3>
        <p class="step-body">
          View your fleet, browse research, and share presentations
          from any machine, on any device. Your AI, always with you.
        </p>
      </div>
    </div>
  </div>
</section>

<!-- CTA band -->
<section class="cta-band">
  <div class="cta-inner">
    <h2 class="cta-title">Ready to ship while you sleep?</h2>
    <p class="cta-sub">Connect your Pinky daemon and your agents will handle the rest.</p>
    <a href="/#/login" use:link class="btn btn-primary btn-lg">
      Connect Your Agent →
    </a>
  </div>
</section>

<!-- Footer -->
<footer class="footer">
  pinkybot.ai &nbsp;·&nbsp; Built with Claude Code
</footer>

<style>
  /* Hero */
  .hero-section {
    min-height: 88vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 4rem 2rem;
    position: relative;
    overflow: hidden;
  }

  .hero-inner {
    text-align: center;
    max-width: 900px;
    position: relative;
    z-index: 1;
  }

  .hero-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-secondary);
    background: var(--surface);
    border: 1px solid rgba(255,255,255,0.06);
    padding: 0.4rem 0.9rem;
    border-radius: var(--radius-pill);
    margin-bottom: 2rem;
  }

  .hero-title {
    font-size: clamp(5rem, 18vw, 14rem);
    font-weight: 900;
    letter-spacing: -0.05em;
    line-height: 0.9;
    color: var(--yellow);
    margin-bottom: 1.5rem;
    text-shadow: 0 0 120px rgba(247, 197, 106, 0.25);
  }

  .hero-dot {
    color: var(--text-muted);
  }

  .hero-tagline {
    font-size: clamp(1.1rem, 2.5vw, 1.5rem);
    color: var(--text-secondary);
    line-height: 1.5;
    margin-bottom: 2.5rem;
    font-weight: 400;
  }

  .hero-actions {
    display: flex;
    gap: 1rem;
    justify-content: center;
    flex-wrap: wrap;
    margin-bottom: 1rem;
  }

  /* Dot grid decoration */
  .hero-grid-accent {
    position: absolute;
    inset: 0;
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 2.5rem;
    pointer-events: none;
    z-index: -1;
    padding: 2rem;
    align-items: center;
  }

  .grid-dot {
    width: 3px;
    height: 3px;
    border-radius: 50%;
    background: var(--yellow);
    display: block;
  }

  /* Features grid */
  .features-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1.25rem;
  }

  .feature-card {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
  }

  .feature-icon {
    font-size: 2rem;
    line-height: 1;
  }

  .feature-title {
    font-size: 1.05rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--text);
  }

  .feature-body {
    font-size: 0.9rem;
    color: var(--text-secondary);
    line-height: 1.65;
  }

  /* How it works — stepped layout */
  .how-section {
    background: var(--surface);
    border-radius: var(--radius-xl);
    margin: 0 1rem;
    padding: 5rem 4rem;
    max-width: none;
  }

  @media (min-width: 1200px) {
    .how-section {
      margin: 0 auto;
      max-width: 1160px;
    }
  }

  .steps {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .step {
    display: flex;
    gap: 2rem;
    align-items: flex-start;
  }

  .step-number {
    font-family: var(--mono);
    font-size: 0.7rem;
    font-weight: 700;
    color: var(--yellow);
    background: var(--yellow-dim);
    padding: 0.4rem 0.7rem;
    border-radius: var(--radius-lg);
    flex-shrink: 0;
    margin-top: 0.15rem;
    letter-spacing: 0.04em;
  }

  .step-content {
    padding-bottom: 2.5rem;
  }

  .step-title {
    font-size: 1.1rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin-bottom: 0.5rem;
    color: var(--text);
  }

  .step-body {
    font-size: 0.9rem;
    color: var(--text-secondary);
    line-height: 1.65;
    max-width: 52ch;
  }

  .step-connector {
    width: 1px;
    height: 2rem;
    background: linear-gradient(to bottom, var(--yellow-dim), transparent);
    margin-left: 2.5rem;
  }

  /* CTA band */
  .cta-band {
    padding: 6rem 2rem;
    text-align: center;
    background: radial-gradient(ellipse 60% 50% at 50% 50%, rgba(247, 197, 106, 0.07) 0%, transparent 70%);
  }

  .cta-inner {
    max-width: 600px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1.25rem;
  }

  .cta-title {
    font-size: clamp(1.8rem, 4vw, 2.8rem);
    font-weight: 900;
    letter-spacing: -0.04em;
    color: var(--text);
  }

  .cta-sub {
    font-size: 1rem;
    color: var(--text-secondary);
    line-height: 1.65;
  }

  /* Small button variant for nav */
  :global(.btn-sm) {
    font-size: 0.72rem;
    padding: 0.42rem 0.9rem;
  }

  /* Responsive */
  @media (max-width: 768px) {
    .how-section {
      padding: 3rem 1.5rem;
      margin: 0 0.75rem;
      border-radius: var(--radius-lg);
    }

    .step {
      gap: 1.25rem;
    }

    .step-connector {
      margin-left: 2rem;
    }
  }
</style>
