<script>
  import { push } from 'svelte-spa-router';

  let daemonUrl = $state('');
  let apiKey = $state('');
  let loading = $state(false);
  let error = $state('');
  let connected = $state(null); // { label, url }

  const HUB_URL = 'http://localhost:8889';

  async function connect() {
    error = '';
    if (!daemonUrl || !apiKey) {
      error = 'Both fields are required.';
      return;
    }

    loading = true;
    try {
      const res = await fetch(`${HUB_URL}/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: daemonUrl, api_key: apiKey }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Server returned ${res.status}`);
      }

      const data = await res.json();
      connected = { label: data.label || daemonUrl, url: daemonUrl };

      // Persist to localStorage
      localStorage.setItem('pinky_daemon_url', daemonUrl);
      localStorage.setItem('pinky_api_key', apiKey);
      localStorage.setItem('pinky_label', connected.label);
    } catch (err) {
      error = err.message || 'Failed to connect. Check the URL and key.';
    } finally {
      loading = false;
    }
  }

  function goToFleet() {
    push('/fleet');
  }
</script>

<nav class="nav">
  <a href="/#/" class="nav-logo">Pinky.</a>
  <div class="nav-links">
    <a href="/#/" class="btn btn-ghost btn-sm">← Back</a>
  </div>
</nav>

<main class="login-page">
  <div class="login-card">
    {#if connected}
      <div class="success-state">
        <div class="success-icon">✓</div>
        <h1 class="login-title">Connected!</h1>
        <p class="login-sub">
          Linked to <strong class="accent">{connected.label}</strong>
        </p>
        <button class="btn btn-primary btn-lg" onclick={goToFleet}>
          Go to Fleet →
        </button>
      </div>
    {:else}
      <div class="login-header">
        <div class="login-eyebrow">Agent Access</div>
        <h1 class="login-title">Connect Your Pinky</h1>
        <p class="login-sub">
          Point this interface at your running Pinky daemon.
        </p>
      </div>

      <form class="login-form" onsubmit={(e) => { e.preventDefault(); connect(); }}>
        <div class="form-group">
          <label class="form-label" for="daemon-url">Daemon URL</label>
          <input
            id="daemon-url"
            class="form-input"
            type="url"
            placeholder="http://your-server:8888"
            bind:value={daemonUrl}
            disabled={loading}
            autocomplete="url"
          />
        </div>

        <div class="form-group">
          <label class="form-label" for="api-key">API Key</label>
          <input
            id="api-key"
            class="form-input"
            type="password"
            placeholder="sk-pinky-••••••••"
            bind:value={apiKey}
            disabled={loading}
            autocomplete="current-password"
          />
        </div>

        {#if error}
          <div class="error-banner">{error}</div>
        {/if}

        <button type="submit" class="btn btn-primary btn-lg submit-btn" disabled={loading}>
          {#if loading}
            Connecting…
          {:else}
            Connect →
          {/if}
        </button>
      </form>

      <p class="login-note">
        Don't have a daemon? Pinky runs on any machine.
        <a href="https://github.com/pinkybot" target="_blank" rel="noreferrer">
          Get started →
        </a>
      </p>
    {/if}
  </div>
</main>

<footer class="footer">
  pinkybot.ai &nbsp;·&nbsp; Built with Claude Code
</footer>

<style>
  .login-page {
    min-height: calc(100vh - 140px);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 3rem 1.25rem;
    background: radial-gradient(ellipse 50% 60% at 50% 20%, rgba(247, 197, 106, 0.06) 0%, transparent 70%);
  }

  .login-card {
    width: 100%;
    max-width: 420px;
    background: var(--surface);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: var(--radius-xl);
    padding: 2.5rem;
    display: flex;
    flex-direction: column;
    gap: 2rem;
    box-shadow: 0 8px 48px rgba(0,0,0,0.4);
  }

  .login-header {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .login-eyebrow {
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--yellow);
  }

  .login-title {
    font-size: 1.75rem;
    font-weight: 900;
    letter-spacing: -0.04em;
    color: var(--text);
  }

  .login-sub {
    font-size: 0.88rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .login-form {
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
  }

  .submit-btn {
    width: 100%;
    justify-content: center;
    margin-top: 0.25rem;
  }
  .submit-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
    box-shadow: 4px 4px 0 #9c7a1a;
  }

  .error-banner {
    background: rgba(248, 113, 113, 0.12);
    border: 1px solid rgba(248, 113, 113, 0.3);
    border-radius: var(--radius-lg);
    padding: 0.7rem 0.9rem;
    font-size: 0.82rem;
    color: #fca5a5;
    line-height: 1.5;
  }

  .login-note {
    font-size: 0.78rem;
    color: var(--text-muted);
    text-align: center;
  }
  .login-note a {
    color: var(--yellow);
  }

  /* Success state */
  .success-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1rem;
    text-align: center;
    padding: 1rem 0;
  }

  .success-icon {
    width: 3.5rem;
    height: 3.5rem;
    border-radius: 50%;
    background: rgba(74, 222, 128, 0.15);
    color: var(--green);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.5rem;
    font-weight: 700;
    border: 1px solid rgba(74, 222, 128, 0.3);
  }

  .success-state .btn {
    width: 100%;
    justify-content: center;
  }
</style>
