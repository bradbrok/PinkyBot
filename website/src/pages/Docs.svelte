<script>
  import { getTheme, toggleTheme } from '../lib/theme.svelte.js';
  let theme = $derived(getTheme());
  let copied = $state('');

  function copy(text, id) {
    navigator.clipboard.writeText(text).then(() => {
      copied = id;
      setTimeout(() => { copied = ''; }, 1800);
    });
  }

  const sections = [
    {
      group: 'Getting Started',
      items: [
        { id: 'what-is-pinky', label: 'What is Pinky?' },
        { id: 'installation', label: 'Installation' },
        { id: 'first-agent', label: 'Your first agent' },
      ]
    },
    {
      group: 'Core Concepts',
      items: [
        { id: 'agents-and-soul', label: 'Agents & Soul' },
        { id: 'memory', label: 'Memory & Dreams' },
        { id: 'skills', label: 'Skills' },
        { id: 'directives', label: 'Directives' },
      ]
    },
    {
      group: 'Features',
      items: [
        { id: 'research', label: 'Research Pipeline' },
        { id: 'presentations', label: 'Presentations' },
        { id: 'project-management', label: 'Project Management' },
        { id: 'inter-agent', label: 'Inter-Agent Comms' },
        { id: 'voice', label: 'Voice Chat' },
        { id: 'channels', label: 'Multi-Channel' },
      ]
    },
    {
      group: 'Configuration',
      items: [
        { id: 'daemon-setup', label: 'Daemon Setup' },
        { id: 'agent-config', label: 'Agent Config' },
        { id: 'hooks', label: 'Claude Code Hooks' },
      ]
    },
    {
      group: 'API Reference',
      items: [
        { id: 'api-reference', label: 'REST API' },
      ]
    },
  ];

  let activeSection = $state('what-is-pinky');

  function scrollTo(id) {
    activeSection = id;
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
</script>

<nav class="nav">
  <a href="/#/" class="nav-logo">Pinky.</a>
  <div class="nav-links">
    <a href="/#/" class="nav-link">Home</a>
    <a href="/#/login" class="btn btn-primary btn-sm nav-link nav-cta">Connect →</a>
    <button class="theme-btn" onclick={toggleTheme} aria-label="Toggle theme">
      {theme === 'dark' ? 'light' : 'dark'}
    </button>
  </div>
</nav>

<div class="docs-layout">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-inner">
      {#each sections as section}
        <div class="nav-group">
          <div class="nav-group-label">{section.group}</div>
          {#each section.items as item}
            <button
              class="nav-item {activeSection === item.id ? 'active' : ''}"
              onclick={() => scrollTo(item.id)}
            >
              {item.label}
            </button>
          {/each}
        </div>
      {/each}
    </div>
  </aside>

  <!-- Main content -->
  <main class="docs-content">

    <!-- Getting Started -->
    <section id="what-is-pinky" class="doc-section">
      <p class="section-eyebrow">Getting Started</p>
      <h1 class="doc-h1">What is Pinky?</h1>
      <p class="doc-p">
        Pinky is your personal AI agent — not a chatbot, not a search engine.
        It's a persistent agent that lives on your machine, knows who you are,
        remembers your work, and gets things done while you're away.
      </p>
      <p class="doc-p">
        Built on <strong>Claude Code</strong>, Pinky uses your existing Claude subscription —
        no API keys, no per-token bills. It runs locally, your data stays yours,
        and it connects to you over Telegram, Slack, Discord, iMessage, or SMS.
      </p>
      <div class="callout">
        Pinky is self-hosted. You own the server, the data, and the agent.
      </div>
    </section>

    <div class="doc-divider"></div>

    <section id="installation" class="doc-section">
      <h2 class="doc-h2">Installation</h2>
      <p class="doc-p">You need a Claude subscription (Pro or higher) and a machine to run Pinky on — a Mac, Linux box, or home server works great.</p>

      <h3 class="doc-h3">1. Install Claude Code</h3>
      <div class="code-wrap">
        <span class="code-prompt">$</span>
        <span class="code-text">curl -fsSL https://claude.ai/install.sh | bash</span>
        <button class="copy-btn" onclick={() => copy('curl -fsSL https://claude.ai/install.sh | bash', 'd1')}>{copied === 'd1' ? '✓' : '⧉'}</button>
      </div>
      <p class="doc-p doc-p-sm">Need help? <a href="https://code.claude.com/docs/en/quickstart" target="_blank" rel="noreferrer" class="doc-link">Claude Code quickstart →</a></p>

      <h3 class="doc-h3">2. Clone PinkyBot</h3>
      <div class="code-wrap">
        <div class="code-lines">
          <div class="code-line"><span class="code-prompt">$</span><span class="code-text">git clone https://github.com/bradbrok/PinkyBot</span></div>
          <div class="code-line"><span class="code-prompt">$</span><span class="code-text">cd PinkyBot && pip install -e .</span></div>
        </div>
        <button class="copy-btn" onclick={() => copy('git clone https://github.com/bradbrok/PinkyBot\ncd PinkyBot && pip install -e .', 'd2')}>{copied === 'd2' ? '✓' : '⧉'}</button>
      </div>

      <h3 class="doc-h3">3. Start the daemon</h3>
      <div class="code-wrap">
        <span class="code-prompt">$</span>
        <span class="code-text">python -m pinky_daemon --mode api --port 8888</span>
        <button class="copy-btn" onclick={() => copy('python -m pinky_daemon --mode api --port 8888', 'd3')}>{copied === 'd3' ? '✓' : '⧉'}</button>
      </div>

      <h3 class="doc-h3">4. Open the dashboard</h3>
      <div class="code-wrap">
        <span class="code-prompt">$</span>
        <span class="code-text">open http://localhost:5173</span>
        <button class="copy-btn" onclick={() => copy('open http://localhost:5173', 'd4')}>{copied === 'd4' ? '✓' : '⧉'}</button>
      </div>
    </section>

    <div class="doc-divider"></div>

    <section id="first-agent" class="doc-section">
      <h2 class="doc-h2">Your first agent</h2>
      <p class="doc-p">
        Once the daemon is running, open the dashboard and create your first agent.
        Give it a name, write a short soul (personality), and hit start.
        Your agent will boot up, connect to Claude Code, and be ready to chat.
      </p>
      <p class="doc-p">
        By default, every agent is a full coding agent — it can read and write files,
        run commands, search the web, and remember things across sessions.
      </p>
    </section>

    <div class="doc-divider"></div>

    <!-- Core Concepts -->
    <section id="agents-and-soul" class="doc-section">
      <p class="section-eyebrow">Core Concepts</p>
      <h2 class="doc-h2">Agents & Soul</h2>
      <p class="doc-p">
        An <strong>agent</strong> is a named, persistent AI entity with its own identity, memory, tools, and permissions.
        Each agent runs as a long-lived Claude Code session managed by the PinkyBot daemon.
      </p>
      <p class="doc-p">
        The <strong>soul</strong> is the agent's personality — a short description of who it is, how it thinks,
        and what it cares about. Combined with boundaries, directives, and skills, it's compiled into
        a <code class="inline-code">CLAUDE.md</code> file that Claude Code reads on each session.
      </p>
    </section>

    <div class="doc-divider"></div>

    <section id="memory" class="doc-section">
      <h2 class="doc-h2">Memory & Dreams</h2>
      <p class="doc-p">
        Pinky has two layers of memory:
      </p>
      <ul class="doc-list">
        <li><strong>Working memory</strong> — active project state in <code class="inline-code">MEMORY.md</code> files. Fast, in-context, editable.</li>
        <li><strong>Long-term memory</strong> — semantic vector store. Agents <code class="inline-code">reflect()</code> to store learnings and <code class="inline-code">recall()</code> to search them across sessions.</li>
      </ul>
      <p class="doc-p">
        <strong>Dreams</strong> run on a nightly cron (default 3 AM). While you sleep, your agent reviews the day,
        consolidates memories, connects dots across projects, and surfaces things worth knowing in the morning.
      </p>
    </section>

    <div class="doc-divider"></div>

    <section id="skills" class="doc-section">
      <h2 class="doc-h2">Skills</h2>
      <p class="doc-p">
        Skills are capability plugins — a <code class="inline-code">SKILL.md</code> file that gets injected into the agent's
        system prompt, plus optional MCP servers that add new tools.
        Agents can have multiple skills assigned; each contributes instructions and capabilities.
      </p>
      <p class="doc-p">Built-in skills include: file access, long-term memory, messaging, self-management, Google Workspace, and more.</p>
    </section>

    <div class="doc-divider"></div>

    <section id="directives" class="doc-section">
      <h2 class="doc-h2">Directives</h2>
      <p class="doc-p">
        Directives are priority-ordered instructions injected into an agent's system prompt.
        Use them to add standing rules, behavioral guidelines, or task-specific context
        without editing the soul directly.
      </p>
    </section>

    <div class="doc-divider"></div>

    <!-- Features -->
    <section id="research" class="doc-section">
      <p class="section-eyebrow">Features</p>
      <h2 class="doc-h2">Research Pipeline</h2>
      <p class="doc-p">
        Agents can own research topics — questions or subjects they investigate autonomously.
        Multiple agents contribute findings, peer-review each other's briefs, and publish
        a final report. Wake up to structured answers with sources.
      </p>
    </section>

    <div class="doc-divider"></div>

    <section id="presentations" class="doc-section">
      <h2 class="doc-h2">Presentations</h2>
      <p class="doc-p">
        Agents can create and publish polished HTML slide decks directly from research or any topic.
        Every presentation is versioned — you can restore any previous version.
        Share via a public link, optionally password-protected.
      </p>
    </section>

    <div class="doc-divider"></div>

    <section id="project-management" class="doc-section">
      <h2 class="doc-h2">Project Management</h2>
      <p class="doc-p">
        Pinky manages projects with tasks, sprints, and milestones. Agents create tasks,
        claim them, and mark them complete — tracked in a persistent task store.
        The dashboard shows burndown, progress, and fleet-wide task counts.
      </p>
    </section>

    <div class="doc-divider"></div>

    <section id="inter-agent" class="doc-section">
      <h2 class="doc-h2">Inter-Agent Comms</h2>
      <p class="doc-p">
        Agents can message each other directly via <code class="inline-code">send_to_agent()</code>.
        Messages are delivered in real-time if the agent is online, or queued in their inbox
        for when they wake up. Agents can delegate tasks, share research, and coordinate work.
      </p>
    </section>

    <div class="doc-divider"></div>

    <section id="voice" class="doc-section">
      <h2 class="doc-h2">Voice Chat</h2>
      <p class="doc-p">
        Talk to your agent with voice — push to talk, voice response.
        Available in the dashboard chat view. Same memory, same tools, just hands-free.
      </p>
    </section>

    <div class="doc-divider"></div>

    <section id="channels" class="doc-section">
      <h2 class="doc-h2">Multi-Channel</h2>
      <p class="doc-p">
        Pinky meets you wherever you are. Connect your agent to Telegram, Slack, Discord,
        iMessage, or SMS — and it responds from the same persistent session with the same memory.
      </p>
    </section>

    <div class="doc-divider"></div>

    <!-- Config -->
    <section id="daemon-setup" class="doc-section">
      <p class="section-eyebrow">Configuration</p>
      <h2 class="doc-h2">Daemon Setup</h2>
      <p class="doc-p">
        The daemon is a FastAPI server that manages agents, sessions, and scheduling.
        Start it with:
      </p>
      <div class="code-wrap">
        <span class="code-prompt">$</span>
        <span class="code-text">python -m pinky_daemon --mode api --port 8888</span>
        <button class="copy-btn" onclick={() => copy('python -m pinky_daemon --mode api --port 8888', 'd5')}>{copied === 'd5' ? '✓' : '⧉'}</button>
      </div>
      <p class="doc-p">Configure via environment variables or a <code class="inline-code">.env</code> file in the project root.</p>
    </section>

    <div class="doc-divider"></div>

    <section id="agent-config" class="doc-section">
      <h2 class="doc-h2">Agent Config</h2>
      <p class="doc-p">
        Agents are configured through the dashboard or API. Key fields:
      </p>
      <ul class="doc-list">
        <li><strong>Soul</strong> — personality and identity</li>
        <li><strong>Model</strong> — which Claude model to use</li>
        <li><strong>Skills</strong> — capability plugins to load</li>
        <li><strong>Wake interval</strong> — how often to check in autonomously</li>
        <li><strong>Auto-start</strong> — whether to boot on daemon startup</li>
      </ul>
    </section>

    <div class="doc-divider"></div>

    <section id="hooks" class="doc-section">
      <h2 class="doc-h2">Claude Code Hooks</h2>
      <p class="doc-p">
        Pinky uses Claude Code hooks to track agent activity. Hooks fire on tool use
        and session stop, updating the agent's working/idle status in real-time.
        Hook configs live at <code class="inline-code">data/agents/{"{name}"}/.claude/settings.json</code>.
      </p>
    </section>

    <div class="doc-divider"></div>

    <!-- API -->
    <section id="api-reference" class="doc-section">
      <p class="section-eyebrow">API Reference</p>
      <h2 class="doc-h2">REST API</h2>
      <p class="doc-p">
        The PinkyBot daemon exposes a full REST API on port 8888.
        Full reference coming soon — in the meantime, explore <code class="inline-code">http://localhost:8888/docs</code> for the auto-generated OpenAPI spec.
      </p>
      <div class="code-wrap">
        <span class="code-prompt">$</span>
        <span class="code-text">open http://localhost:8888/docs</span>
        <button class="copy-btn" onclick={() => copy('open http://localhost:8888/docs', 'd6')}>{copied === 'd6' ? '✓' : '⧉'}</button>
      </div>
    </section>

  </main>
</div>

<footer class="footer">
  pinkybot.ai &nbsp;·&nbsp; Built with Claude Code
</footer>

<style>
  .docs-layout {
    display: grid;
    grid-template-columns: 240px 1fr;
    min-height: calc(100vh - 60px);
    max-width: 1200px;
    margin: 0 auto;
    padding: 2rem 1.5rem;
    gap: 3rem;
  }

  /* Sidebar */
  .sidebar {
    position: sticky;
    top: 5rem;
    height: fit-content;
    max-height: calc(100vh - 6rem);
    overflow-y: auto;
  }

  .sidebar-inner {
    display: flex;
    flex-direction: column;
    gap: 1.75rem;
  }

  .nav-group {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .nav-group-label {
    font-family: var(--mono);
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-muted);
    padding: 0 0.6rem;
    margin-bottom: 0.35rem;
  }

  .nav-item {
    display: block;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    cursor: pointer;
    font-family: var(--sans);
    font-size: 0.84rem;
    color: var(--text-secondary);
    padding: 0.35rem 0.6rem;
    border-radius: var(--radius-md);
    transition: color 0.15s, background 0.15s;
  }

  .nav-item:hover {
    color: var(--text);
    background: var(--surface);
  }

  .nav-item.active {
    color: var(--yellow);
    background: var(--yellow-dim);
  }

  /* Content */
  .docs-content {
    min-width: 0;
    padding-bottom: 6rem;
  }

  .doc-section {
    padding-top: 1rem;
    scroll-margin-top: 5rem;
  }

  .doc-h1 {
    font-size: clamp(1.8rem, 3vw, 2.4rem);
    font-weight: 900;
    letter-spacing: -0.04em;
    color: var(--text);
    margin-bottom: 1.25rem;
    line-height: 1.1;
  }

  .doc-h2 {
    font-size: 1.4rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--text);
    margin-bottom: 1rem;
    line-height: 1.2;
  }

  .doc-h3 {
    font-size: 0.95rem;
    font-weight: 700;
    color: var(--text);
    margin: 1.5rem 0 0.5rem;
    letter-spacing: -0.01em;
  }

  .doc-p {
    font-size: 0.92rem;
    color: var(--text-secondary);
    line-height: 1.75;
    margin-bottom: 0.9rem;
    max-width: 68ch;
  }

  .doc-p-sm {
    font-size: 0.82rem;
    margin-top: -0.25rem;
  }

  .doc-link {
    color: var(--yellow);
  }

  .doc-list {
    list-style: none;
    padding: 0;
    margin: 0 0 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .doc-list li {
    font-size: 0.92rem;
    color: var(--text-secondary);
    line-height: 1.65;
    padding-left: 1.1rem;
    position: relative;
    max-width: 68ch;
  }

  .doc-list li::before {
    content: '—';
    position: absolute;
    left: 0;
    color: var(--yellow);
    font-family: var(--mono);
    font-size: 0.75rem;
    top: 0.15em;
  }

  .inline-code {
    font-family: var(--mono);
    font-size: 0.82em;
    color: var(--yellow);
    background: var(--yellow-dim);
    padding: 0.1em 0.4em;
    border-radius: 4px;
  }

  .code-wrap {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin: 0.5rem 0 1rem;
    background: #0d0d0f;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: var(--radius-lg);
    padding: 0.75rem 1rem;
    max-width: 600px;
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
    color: #5a5a72;
    flex-shrink: 0;
    user-select: none;
  }

  .code-text {
    font-family: var(--mono);
    font-size: 0.78rem;
    color: #e8e8f0;
    line-height: 1.6;
    word-break: break-all;
    flex: 1;
  }

  .copy-btn {
    font-family: var(--mono);
    font-size: 0.85rem;
    color: #5a5a72;
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
    color: #F9D849;
    border-color: rgba(249, 216, 73, 0.3);
  }

  .callout {
    background: var(--surface);
    border-left: 2px solid var(--yellow);
    border-radius: 0 var(--radius-lg) var(--radius-lg) 0;
    padding: 0.85rem 1.1rem;
    font-size: 0.88rem;
    color: var(--text-secondary);
    line-height: 1.65;
    margin: 1rem 0;
    max-width: 600px;
  }

  .doc-divider {
    height: 1px;
    background: rgba(255,255,255,0.05);
    margin: 3rem 0;
  }

  .theme-btn {
    font-family: var(--mono);
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.3rem 0.5rem;
    border-radius: var(--radius);
    transition: color 0.15s;
    margin-left: 0.5rem;
  }

  .theme-btn:hover {
    color: var(--text-secondary);
  }

  @media (max-width: 768px) {
    .docs-layout {
      grid-template-columns: 1fr;
    }

    .sidebar {
      position: static;
      max-height: none;
    }
  }
</style>
