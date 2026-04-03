<script>
  import { link } from 'svelte-spa-router';
  import { langs, getCurrentLang, setCurrentLang, t } from '../lib/i18n.svelte.js';
  import { getTheme, toggleTheme } from '../lib/theme.svelte.js';

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
</script>

<svelte:window onclick={closeDropdown} />

<!-- Nav -->
<nav class="nav">
  <span class="nav-logo">Pinky.</span>
  <div class="nav-links">
    <a href="#features" class="nav-link">{t('nav.features')}</a>
    <a href="#install" class="nav-link">{t('nav.install')}</a>
    <a href="/#/docs" class="nav-link">{t('nav.docs')}</a>
    <a href="/#/login" use:link class="btn btn-primary btn-sm nav-link nav-cta">{t('nav.connect')}</a>
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
<section class="hero-section">
  <div class="hero-inner">
    <div class="hero-badge">
      <span class="status-dot"></span>
      <span>{t('hero.badge')}</span>
    </div>

    <h1 class="hero-title">
      PINKY<span class="hero-dot">.</span>
    </h1>

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

    <div class="hero-grid-accent" aria-hidden="true">
      {#each Array(120) as _, i}
        <span class="grid-dot" style="opacity: {Math.random() * 0.3 + 0.05}"></span>
      {/each}
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
          <div class="code-lines">
            <div class="code-line"><span class="code-prompt">$</span><span class="code-text">git clone https://github.com/bradbrok/PinkyBot</span></div>
            <div class="code-line"><span class="code-prompt">$</span><span class="code-text">cd PinkyBot && python -m pinky_daemon</span></div>
          </div>
          <button class="copy-btn" onclick={() => copy('git clone https://github.com/bradbrok/PinkyBot\ncd PinkyBot && python -m pinky_daemon', 'step2')}>
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
          <span class="code-text">open http://localhost:5173</span>
          <button class="copy-btn" onclick={() => copy('open http://localhost:5173', 'step3')}>
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
    margin-bottom: 1.5rem;
    text-shadow: 0 0 120px rgba(249, 216, 73, 0.25);
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
    max-width: 560px;
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
    word-break: break-all;
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
