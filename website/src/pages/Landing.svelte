<script>
  import { link } from 'svelte-spa-router';
  import { langs, getCurrentLang, setCurrentLang, t } from '../lib/i18n.svelte.js';
  import { getTheme, toggleTheme } from '../lib/theme.svelte.js';
  import { onMount } from 'svelte';

  let currentLang = $derived(getCurrentLang());
  let theme = $derived(getTheme());
  let dropdownOpen = $state(false);
  let copied = $state('');

  function closeDropdown(e) {
    if (!e.target.closest('.prefs-dropdown')) dropdownOpen = false;
  }

  function copy(text, id) {
    navigator.clipboard.writeText(text).then(() => {
      copied = id;
      setTimeout(() => { copied = ''; }, 1800);
    });
  }

  // ── Dot animation ────────────────────────────────────────────────
  const DOT_COUNT = 120;
  const COLS = 12;
  const REPEL_RADIUS = 130;
  const REPEL_STRENGTH = 22;
  const SCROLL_FACTOR = 0.06;

  const dotOpacities = Array.from({ length: DOT_COUNT }, () => Math.random() * 0.45 + 0.2);

  let gridEl = $state(null);
  let dotEls = [];   // direct DOM refs — no Svelte state, no re-renders
  let mouseX = -9999;
  let mouseY = -9999;
  let scrollY = 0;
  let rafId;
  let scheduled = false;

  function updateDots() {
    scheduled = false;
    if (!gridEl) return;
    const rect = gridEl.getBoundingClientRect();
    const rows = Math.ceil(DOT_COUNT / COLS);
    const dotW = rect.width / COLS;
    const dotH = rect.height / rows;

    for (let i = 0; i < dotEls.length; i++) {
      const el = dotEls[i];
      if (!el) continue;
      const col = i % COLS;
      const row = Math.floor(i / COLS);
      const dotX = rect.left + (col + 0.5) * dotW;
      const dotY = rect.top + (row + 0.5) * dotH;

      const dx = dotX - mouseX;
      const dy = dotY - mouseY;
      const dist = Math.sqrt(dx * dx + dy * dy);

      let ox = 0, oy = 0;
      if (dist < REPEL_RADIUS && dist > 0) {
        const force = (1 - dist / REPEL_RADIUS) * REPEL_STRENGTH;
        ox = (dx / dist) * force;
        oy = (dy / dist) * force;
      }
      oy += scrollY * SCROLL_FACTOR * ((row / rows) - 0.5);

      el.style.transform = `translate(${ox}px,${oy}px)`;
    }
  }

  function scheduleUpdate() {
    if (!scheduled) {
      scheduled = true;
      rafId = requestAnimationFrame(updateDots);
    }
  }

  function onHeroMouseMove(e) {
    mouseX = e.clientX;
    mouseY = e.clientY;
    scheduleUpdate();
  }

  function onHeroMouseLeave() {
    mouseX = -9999;
    mouseY = -9999;
    scheduleUpdate();
  }

  function onScroll() {
    scrollY = window.scrollY;
    scheduleUpdate();
  }

  onMount(() => {
    // Collect dot element refs after render
    dotEls = Array.from(gridEl?.querySelectorAll('.grid-dot') ?? []);
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      window.removeEventListener('scroll', onScroll);
      cancelAnimationFrame(rafId);
    };
  });
</script>

<svelte:window onclick={closeDropdown} />

<!-- Nav -->
<nav class="nav">
  <span class="nav-logo">Pinky.</span>
  <div class="nav-links">
    <a href="#features" class="nav-link">{t('nav.features')}</a>
    <a href="#install" class="nav-link">{t('nav.install')}</a>
    <a href="/#/docs" class="nav-link nav-docs">{t('nav.docs')}</a>
    <a href="/#/login" use:link class="btn btn-primary btn-sm nav-cta">Connect →</a>
  </div>

  <div class="nav-controls">
  <button class="theme-toggle-btn" onclick={toggleTheme} aria-label="Toggle theme">
    {theme === 'dark' ? t('theme.light') : t('theme.dark')}
  </button>
  <div class="prefs-dropdown">
    <button class="prefs-trigger" onclick={(e) => { e.stopPropagation(); dropdownOpen = !dropdownOpen; }}>
      {currentLang.toUpperCase()} <span class="chevron" class:open={dropdownOpen}>▾</span>
    </button>

    {#if dropdownOpen}
      <div class="prefs-menu" onclick={(e) => e.stopPropagation()}>
        <div class="prefs-section-label">Language</div>
        {#each langs as lang}
          <button
            class="prefs-item"
            class:prefs-item-active={currentLang === lang.code}
            onclick={() => { setCurrentLang(lang.code); }}
          >
            <span class="prefs-item-code">{lang.label}</span>
            <span class="prefs-item-name">{lang.name}</span>
          </button>
        {/each}
      </div>
    {/if}
  </div>
  </div>
</nav>

<!-- Hero -->
<section class="hero-section" onmousemove={onHeroMouseMove} onmouseleave={onHeroMouseLeave}>
  <div class="hero-inner">
    <div class="hero-badge">
      <span class="status-dot"></span>
      <span>{t('hero.badge')}</span>
    </div>

    <div class="title-dot-wrap">
      <div class="hero-grid-accent" bind:this={gridEl} aria-hidden="true">
        {#each dotOpacities as opacity}
          <span class="grid-dot" style="opacity: {opacity};"></span>
        {/each}
      </div>
      <h1 class="hero-title">
        PINKY<span class="hero-dot">.</span>
      </h1>
    </div>

    <p class="hero-tagline">
      {#each t('hero.tagline').split('\n') as line, i}
        {line}{#if i < t('hero.tagline').split('\n').length - 1}<br />{/if}
      {/each}
    </p>

    <div class="hero-pill">
      {t('hero.pill')}
    </div>

    <div class="hero-actions">
      <a href="#install" class="btn btn-ghost btn-lg">
        {t('hero.install')}
      </a>
    </div>

  </div>
</section>

<!-- Features -->
<section id="features" class="section">
  <p class="section-eyebrow">{t('features.eyebrow')}</p>
  <h2 class="section-title" style="margin-bottom: 3.5rem;">
    {#each t('features.title').split('\n') as line, i}
      {line}{#if i < t('features.title').split('\n').length - 1}<br />{/if}
    {/each}
  </h2>

  <div class="features-grid">
    <div class="card feature-card">

      <h3 class="feature-title">{t('features.01.title')}</h3>
      <p class="feature-body">{t('features.01.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.02.title')}</h3>
      <p class="feature-body">{t('features.02.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.03.title')}</h3>
      <p class="feature-body">{t('features.03.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.04.title')}</h3>
      <p class="feature-body">{t('features.04.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.05.title')}</h3>
      <p class="feature-body">{t('features.05.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.06.title')}</h3>
      <p class="feature-body">{t('features.06.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.07.title')}</h3>
      <p class="feature-body">{t('features.07.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.08.title')}</h3>
      <p class="feature-body">{t('features.08.body')}</p>
    </div>

    <div class="card feature-card">

      <h3 class="feature-title">{t('features.09.title')}</h3>
      <p class="feature-body">{t('features.09.body')}</p>
    </div>

  </div>
</section>

<!-- Use Cases -->
<div class="usecases-wrapper">
<section class="section usecases-section">
  <p class="section-eyebrow">Real world</p>
  <h2 class="section-title">What people build with Pinky.</h2>

  <div class="usecases-grid">

    <div class="usecase-card">
      <div class="usecase-header">
        <span class="usecase-role">Developer</span>
      </div>
      <p class="usecase-hook">Push a branch before bed. Wake up to a PR with tests written, lint fixed, and a summary in Telegram.</p>
      <p class="usecase-body">Pinky monitors CI, runs fixes autonomously, and only pings you when it needs a decision. It knows your codebase from memory.</p>
    </div>

    <div class="usecase-card">
      <div class="usecase-header">
        <span class="usecase-role">Founder</span>
      </div>
      <p class="usecase-hook">Every morning: what competitors shipped, what's trending, and three things to act on — delivered before you're out of bed.</p>
      <p class="usecase-body">Nightly research pipeline across changelogs, Hacker News, and product announcements. Tight digest in Telegram, no noise.</p>
    </div>

    <div class="usecase-card">
      <div class="usecase-header">
        <span class="usecase-role">Researcher</span>
      </div>
      <p class="usecase-hook">Hand it 40 papers. It summarizes them, finds the contradictions, and builds a knowledge base you can query in plain English.</p>
      <p class="usecase-body">Persistent vector memory means you can ask questions across everything it's read — weeks or months later, no re-uploading.</p>
    </div>

    <div class="usecase-card">
      <div class="usecase-header">
        <span class="usecase-role">Business Owner</span>
      </div>
      <p class="usecase-hook">Website live in a weekend. Knows your competitors. Wrote the about page, the pricing, and the emails. You just had the idea.</p>
      <p class="usecase-body">Pinky researches your market, figures out what to say and who to say it to, builds the site, and keeps improving it — while you run the actual business.</p>
    </div>

    <div class="usecase-card">
      <div class="usecase-header">
        <span class="usecase-role">Creator</span>
      </div>
      <p class="usecase-hook">Dump raw voice notes into a channel. By morning it's shaped them into drafts and flagged which ones are worth publishing.</p>
      <p class="usecase-body">Pulls context from months of your past work to match your voice. Suggests angles. Cuts the ones that won't land.</p>
    </div>

    <div class="usecase-card">
      <div class="usecase-header">
        <span class="usecase-role">Student</span>
      </div>
      <p class="usecase-hook">Studying for an exam in six weeks. It builds the schedule, quizzes you on Telegram each evening, and adapts when you fall behind.</p>
      <p class="usecase-body">Spaced-repetition, tracked across sessions. Shows up at the right time with the right questions. No app to remember to open.</p>
    </div>

  </div>
</section>
</div>

<!-- Install -->
<section id="install" class="section how-section">
  <p class="section-eyebrow">{t('install.eyebrow')}</p>
  <h2 class="section-title" style="margin-bottom: 3.5rem;">
    {t('install.title')}
  </h2>

  <div class="steps">
    <div class="step">
      <div class="step-number">01</div>
      <div class="step-content">
        <h3 class="step-title">{t('install.01.title')}</h3>
        <p class="step-body">
          {t('install.01.body')}
          <a href="https://code.claude.com/docs/en/quickstart" target="_blank" rel="noreferrer" class="step-link">{t('install.01.link')}</a>
        </p>
        <div class="code-wrap">
          <span class="code-prompt">$</span>
          <span class="code-text">curl -fsSL https://claude.ai/install.sh | bash</span>
          <button class="copy-btn" onclick={() => copy('curl -fsSL https://claude.ai/install.sh | bash', 'step1')}>
            {copied === 'step1' ? '✓' : '⧉'}
          </button>
        </div>
      </div>
    </div>

    <div class="step-connector" aria-hidden="true"></div>

    <div class="step">
      <div class="step-number">02</div>
      <div class="step-content">
        <h3 class="step-title">{t('install.02.title')}</h3>
        <p class="step-body">
          {t('install.02.body')}
        </p>
        <div class="code-wrap">
          <span class="code-prompt">$</span>
          <span class="code-text">curl -fsSL https://pinkybot.ai/install.sh | bash</span>
          <button class="copy-btn" onclick={() => copy('curl -fsSL https://pinkybot.ai/install.sh | bash', 'step2')}>
            {copied === 'step2' ? '✓' : '⧉'}
          </button>
        </div>
      </div>
    </div>

    <div class="step-connector" aria-hidden="true"></div>

    <div class="step">
      <div class="step-number">03</div>
      <div class="step-content">
        <h3 class="step-title">{t('install.03.title')}</h3>
        <p class="step-body">
          {t('install.03.body')}
        </p>
        <div class="code-wrap">
          <span class="code-prompt">$</span>
          <span class="code-text">open http://localhost:8888</span>
          <button class="copy-btn" onclick={() => copy('open http://localhost:8888', 'step3')}>
            {copied === 'step3' ? '✓' : '⧉'}
          </button>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- Footer -->
<footer class="footer">
  © PinkyBot 2026 &nbsp;·&nbsp; <a href="https://brockmanlabs.com" target="_blank" rel="noreferrer">brockmanlabs.com</a>
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
    margin-bottom: 0;
    text-shadow: 0 0 120px rgba(249, 216, 73, 0.25);
    position: relative;
    z-index: 1;
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

  .hero-pill {
    display: inline-block;
    font-family: var(--mono);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--yellow);
    background: var(--yellow-dim);
    border: 1px solid rgba(249, 216, 73, 0.2);
    padding: 0.4rem 1rem;
    border-radius: var(--radius-pill);
    margin-bottom: 2rem;
  }

  .hero-actions {
    display: flex;
    gap: 1rem;
    justify-content: center;
    flex-wrap: wrap;
    margin-bottom: 1rem;
  }

  /* Title + dot grid wrapper */
  .title-dot-wrap {
    position: relative;
    padding: 2.5rem 3.5rem;
    display: inline-block;
    align-self: center;
  }

  /* Dot grid decoration */
  .hero-grid-accent {
    position: absolute;
    inset: 0;
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 1.8rem;
    pointer-events: none;
    z-index: 0;
    padding: 1rem;
    align-items: center;
    mask-image: radial-gradient(ellipse 75% 70% at 50% 50%, black 35%, transparent 100%);
    -webkit-mask-image: radial-gradient(ellipse 75% 70% at 50% 50%, black 35%, transparent 100%);
  }

  .grid-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #ff69b4;
    display: block;
    will-change: transform;
    transition: transform 0.4s cubic-bezier(0.25, 0.46, 0.45, 0.94);
    box-shadow: 0 0 6px 1px rgba(255, 105, 180, 0.4);
  }

  /* Features grid */
  .features-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1.25rem;
  }

  .feature-card-accent {
    border-color: rgba(249, 216, 73, 0.2);
    background: linear-gradient(135deg, var(--surface) 60%, rgba(249, 216, 73, 0.05));
  }

  .feature-card {
    display: flex;
    flex-direction: column;
    gap: 0.9rem;
  }

  .feature-icon {
    font-family: var(--mono);
    font-size: 0.7rem;
    font-weight: 700;
    color: var(--yellow);
    background: var(--yellow-dim);
    padding: 0.4rem 0.7rem;
    border-radius: var(--radius-lg);
    display: inline-block;
    letter-spacing: 0.04em;
    align-self: flex-start;
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
    min-width: 0;
    flex: 1;
    overflow: hidden;
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

  .step-link {
    color: var(--yellow);
    font-size: 0.82rem;
    white-space: nowrap;
  }

  .code-wrap {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 0.75rem;
    background: #0d0d0f;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: var(--radius-lg);
    padding: 0.75rem 1rem;
    max-width: min(560px, 100%);
    overflow-x: auto;
    box-sizing: border-box;
  }

  .code-lines {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    flex: 1;
    min-width: 0;
  }

  .code-line {
    display: flex;
    gap: 0.6rem;
    align-items: baseline;
  }

  .code-prompt {
    font-family: var(--mono);
    font-size: 0.78rem;
    color: var(--text-muted);
    flex-shrink: 0;
    user-select: none;
  }

  .code-text {
    font-family: var(--mono);
    font-size: 0.78rem;
    color: #e8e8f0;
    line-height: 1.6;
    white-space: nowrap;
  }

  .copy-btn {
    font-family: var(--mono);
    font-size: 0.85rem;
    color: var(--text-muted);
    background: none;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: var(--radius);
    padding: 0.25rem 0.45rem;
    cursor: pointer;
    flex-shrink: 0;
    margin-left: auto;
    transition: color 0.15s, border-color 0.15s;
    line-height: 1;
  }

  .copy-btn:hover {
    color: var(--yellow);
    border-color: rgba(249, 216, 73, 0.3);
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
    background: radial-gradient(ellipse 60% 50% at 50% 50%, rgba(249, 216, 73, 0.07) 0%, transparent 70%);
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

  /* Use Cases */
  .usecases-wrapper {
    background: var(--surface-inverse);
  }

  .usecases-section {
    color: var(--text-inverse);
  }

  .usecases-section .section-eyebrow {
    color: #ff69b4;
  }

  .usecases-section .section-title {
    color: var(--text-inverse);
    margin-bottom: 2.5rem;
  }

  .usecases-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    background: var(--surface-inverse-border);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .usecase-card {
    background: var(--surface-inverse);
    padding: 2rem;
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
    transition: background 0.15s;
  }

  .usecase-card:hover {
    background: var(--surface-inverse-hover);
  }

  .usecase-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }

  .usecase-icon {
    font-size: 1.1rem;
    line-height: 1;
  }

  .usecase-role {
    font-family: var(--mono);
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #ff69b4;
  }

  .usecase-hook {
    font-size: 0.92rem;
    font-weight: 600;
    color: var(--text-inverse);
    line-height: 1.55;
    margin: 0;
  }

  .usecase-body {
    font-size: 0.82rem;
    color: var(--text-inverse-muted);
    line-height: 1.7;
    margin: 0;
  }

  @media (max-width: 900px) {
    .usecases-grid { grid-template-columns: repeat(2, 1fr); }
  }

  @media (max-width: 600px) {
    .usecases-grid { grid-template-columns: 1fr; }
  }

  /* Small button variant for nav */
  :global(.btn-sm) {
    font-size: 0.72rem;
    padding: 0.42rem 0.9rem;
  }

  /* Nav controls group */
  .nav-controls {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  /* Prefs dropdown */
  .prefs-dropdown {
    position: relative;
    margin-left: 0.5rem;
  }

  .prefs-trigger {
    font-family: var(--mono);
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--text-secondary);
    background: var(--surface);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: var(--radius-lg);
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.3rem;
    transition: color 0.15s, border-color 0.15s;
  }

  .prefs-trigger:hover {
    color: var(--text);
    border-color: rgba(255,255,255,0.12);
  }

  .chevron {
    font-size: 0.6rem;
    transition: transform 0.15s;
    display: inline-block;
  }

  .chevron.open {
    transform: rotate(180deg);
  }

  .prefs-menu {
    position: absolute;
    top: calc(100% + 0.5rem);
    right: 0;
    background: var(--surface);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: var(--radius-xl);
    padding: 0.5rem;
    min-width: 160px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    z-index: 200;
  }

  .prefs-section-label {
    font-family: var(--mono);
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-muted);
    padding: 0.3rem 0.6rem 0.2rem;
  }

  .prefs-item {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.4rem 0.6rem;
    border-radius: var(--radius-lg);
    transition: background 0.12s;
  }

  .prefs-item:hover {
    background: var(--surface-2);
  }

  .prefs-item-active {
    background: var(--yellow-dim);
  }

  .prefs-item-code {
    font-family: var(--mono);
    font-size: 0.68rem;
    font-weight: 700;
    color: var(--yellow);
    min-width: 1.6rem;
  }

  .prefs-item-name {
    font-size: 0.82rem;
    color: var(--text-secondary);
  }

  .prefs-divider {
    height: 1px;
    background: rgba(255,255,255,0.05);
    margin: 0.35rem 0.4rem;
  }

  .theme-toggle-btn {
    font-family: var(--mono);
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    background: var(--surface);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: var(--radius-lg);
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
    white-space: nowrap;
  }

  .theme-toggle-btn:hover {
    color: var(--text);
    border-color: rgba(255,255,255,0.12);
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

    .prefs-menu {
      right: -0.5rem;
    }
  }
</style>
