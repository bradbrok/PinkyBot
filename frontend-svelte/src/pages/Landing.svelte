<script>
    // Landing/marketing page - standalone layout (no shared header)
</script>

<div class="landing">
    <nav class="nav">
        <div class="nav-logo">PinkyBot</div>
        <div class="nav-links">
            <a href="#features">Features</a>
            <a href="#architecture">Architecture</a>
            <a href="#quickstart">Quick Start</a>
            <a href="https://github.com/bradbrok/PinkyBot" class="nav-cta">GitHub</a>
        </div>
    </nav>

    <header class="hero">
        <div class="hero-content">
            <h1 class="hero-title">
                YOUR AGENTS.<br>
                THEIR <span class="accent">SOULS</span>.<br>
                ONE TEAM.
            </h1>
            <p class="hero-subtitle">
                A multi-agent framework where each AI has persistent memory,
                its own personality, and the ability to talk to the others.
                They grow with you -- and with each other.
            </p>
            <div class="hero-actions">
                <code class="hero-install">curl -fsSL https://pinkybot.ai/install.sh | bash</code>
                <a href="#quickstart" class="btn-l btn-l-primary">Get Started</a>
                <a href="https://github.com/bradbrok/PinkyBot" class="btn-l btn-l-secondary">View Source</a>
            </div>
        </div>
        <div class="hero-ascii">
<pre>
    ┌─────────────────────────┐
    │      Claude Code        │
    │       (brain)           │
    ├─────────────────────────┤
    │     CLAUDE.md (soul)    │
    ├────────┬────────┬───────┤
    │ Memory │  Chat  │ Tools │
    │  MCP   │  MCP   │  MCP  │
    └────────┴────────┴───────┘
         x agents, one team
</pre>
        </div>
    </header>

    <section id="features" class="section-l">
        <h2 class="section-l-title">WHAT IT DOES<span class="accent">.</span></h2>
        <div class="feature-grid">
            {#each [
                ['[ TEAM ]', 'Multi-Agent Teams', 'Not one AI -- a team. Each agent has its own identity, skills, and context. They delegate to each other and share what they learn.'],
                ['[ SOUL ]', 'Souls, Not Prompts', 'CLAUDE.md defines personality, values, and boundaries. Version-controlled identity that evolves over time.'],
                ['[ MEM ]', 'Persistent Memory', 'Semantic long-term memory with vector search. Your agents remember conversations, decisions, and context across sessions.'],
                ['[ GROW ]', 'Grows With You', 'Auto-generated skills from experience. The longer your agents run, the more capable they become.'],
                ['[ MSG ]', 'Every Platform', 'Telegram, Discord, Slack, iMessage, email, and web dashboard. Start on your phone, pick up on desktop.'],
                ['[ WAKE ]', 'Scheduled Autonomy', 'Cron-based wake schedules, proactive check-ins, background research. Your agents work while you sleep.'],
                ['[ UI ]', 'Web Dashboard', 'Real-time Svelte SPA for managing agents, reviewing memory, monitoring tasks, and chatting.'],
                ['[ ZERO ]', 'Zero Infrastructure', 'SQLite for storage. Markdown for config. One curl command and you are running.'],
            ] as [icon, title, desc]}
                <div class="feature-card">
                    <div class="feature-icon">{icon}</div>
                    <h3>{title}</h3>
                    <p>{desc}</p>
                </div>
            {/each}
        </div>
    </section>

    <section id="architecture" class="section-l section-dark">
        <h2 class="section-l-title">HOW IT WORKS<span class="accent">.</span></h2>
        <div class="arch-container">
<pre class="arch-diagram">
┌──────────────────────────────────────────────────┐
│                  CLAUDE CODE                      │
│          LLM brain -- agent loop,                 │
│     context management, tool execution            │
├──────────────────────────────────────────────────┤
│               CLAUDE.md (Soul)                    │
│      Identity, personality, boundaries,           │
│      user profiles, behavioral rules              │
├─────────────┬──────────────┬─────────────────────┤
│   Memory    │   Messaging  │    Self / Custom     │
│    MCP      │     MCP      │       MCP Servers    │
│   Server    │    Server    │                      │
└─────────────┴──────────────┴─────────────────────┘
</pre>
            <div class="arch-text">
                <p><strong>Claude Code</strong> is the brain. Each agent runs its own session with full tool access.</p>
                <p><strong>CLAUDE.md</strong> is the soul. Edit it like any file -- personality, values, and ethics.</p>
                <p><strong>MCP Servers</strong> provide capabilities. Memory, messaging, self-awareness, and more.</p>
                <p><strong>Inter-agent comms</strong> let agents delegate, research, and collaborate as a team.</p>
            </div>
        </div>
    </section>

    <section id="quickstart" class="section-l">
        <h2 class="section-l-title">RUNNING IN 60 SECONDS<span class="accent">.</span></h2>
        <div class="quickstart-steps">
            {#each [
                ['01', 'Install', 'curl -fsSL https://pinkybot.ai/install.sh | bash', 'Sets up Pinky, Claude Code, and all dependencies.'],
                ['02', 'Start', 'pinky --mode api --port 8888', 'Runs the daemon with web dashboard.'],
                ['03', 'Dashboard', 'http://localhost:8888', 'Create agents, connect platforms, manage everything.'],
                ['04', 'Create', 'Give it a name, a role, and a soul', 'Edit CLAUDE.md to define personality, values, and boundaries.'],
                ['05', 'Grow', 'Your agents learn, remember, and improve', 'The longer they run, the more capable they become.'],
            ] as [num, title, cmd, desc]}
                <div class="step">
                    <div class="step-number">{num}</div>
                    <div class="step-content">
                        <h3>{title}</h3>
                        <code>{cmd}</code>
                        {#if desc}<p>{desc}</p>{/if}
                    </div>
                </div>
            {/each}
        </div>
    </section>

    <footer class="footer">
        <div class="footer-content">
            <div class="footer-logo">PINKY<span class="accent">.</span></div>
            <p>Built by <a href="https://brockmanlabs.com">Brockman Labs</a></p>
            <p class="footer-small">MIT License -- Open Source</p>
        </div>
    </footer>
</div>

<style>
    .landing { background: var(--app-bg); color: var(--text-primary); }

    .nav { display: flex; justify-content: space-between; align-items: center; padding: 1rem 3rem; position: sticky; top: 0; background: var(--surface-container-lowest, var(--app-bg)); z-index: 100; }
    .nav-logo { font-family: var(--font-grotesk); font-size: 1.1rem; font-weight: 900; background: var(--primary-container); color: #000; padding: 0.2rem 0.7rem; border-radius: var(--radius); }
    .nav-links { display: flex; gap: 1.5rem; align-items: center; font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
    .nav-links a:hover { color: var(--primary-container); }
    .nav-cta { background: var(--primary-container); color: var(--on-primary-container) !important; padding: 0.5rem 1.2rem; border: none; border-radius: var(--radius-lg); font-weight: 700; transition: all 0.1s; box-shadow: 4px 4px 0px var(--primary); }
    .nav-cta:hover { background: var(--primary); color: #fff !important; box-shadow: 2px 2px 0px var(--primary); }

    .hero { display: grid; grid-template-columns: 1fr 1fr; min-height: 80vh; }
    .hero-content { padding: 5rem 3rem; display: flex; flex-direction: column; justify-content: center; }
    .hero-title { font-family: var(--font-grotesk); font-size: 4rem; font-weight: 900; line-height: 1.05; margin-bottom: 1.5rem; letter-spacing: -0.03em; }
    .hero-subtitle { font-size: 1.1rem; color: var(--text-secondary); margin-bottom: 2.5rem; max-width: 500px; line-height: 1.7; }
    .hero-actions { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
    .hero-install { font-family: monospace; background: var(--surface-inverse); color: var(--accent); font-size: 1rem; padding: 0.7rem 1.2rem; border-radius: var(--radius-lg); }
    .hero-ascii { background: var(--surface-inverse); color: var(--accent); display: flex; align-items: center; justify-content: center; padding: 3rem; border-radius: 0 var(--radius-xl) var(--radius-xl) 0; }
    .hero-ascii pre { font-family: monospace; font-size: 1rem; }

    .btn-l { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; text-transform: uppercase; padding: 0.7rem 1.5rem; border: none; border-radius: var(--radius-lg); cursor: pointer; transition: all 0.1s; display: inline-block; text-decoration: none; color: var(--text-primary); }
    .btn-l:active { transform: scale(0.98); }
    .btn-l-primary { background: var(--primary-container); color: var(--on-primary-container); box-shadow: 4px 4px 0px var(--primary); }
    .btn-l-primary:hover { background: var(--primary); color: #fff; box-shadow: 2px 2px 0px var(--primary); }
    .btn-l-secondary { background: var(--surface-1); }
    .btn-l-secondary:hover { background: var(--surface-2); }

    .section-l { padding: 5rem 3rem; }
    .section-dark { background: var(--surface-inverse); color: var(--text-inverse); border-radius: var(--radius-xl); margin: 1rem 3rem; }
    .section-l-title { font-family: var(--font-grotesk); font-size: 2rem; font-weight: 900; margin-bottom: 3rem; letter-spacing: -0.02em; }

    .feature-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; }
    .feature-card { padding: 2rem; background: var(--surface-1); border-radius: var(--radius-lg); transition: all 0.15s; }
    .feature-card:hover { background: var(--surface-2); transform: translateY(-2px); }
    .feature-icon { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; color: var(--primary-container); margin-bottom: 1rem; }
    .feature-card:hover .feature-icon { color: var(--primary); }
    .feature-card h3 { font-family: var(--font-grotesk); font-size: 1.1rem; margin-bottom: 0.75rem; }
    .feature-card p { font-size: 0.9rem; color: var(--text-secondary); line-height: 1.6; }
    .feature-card:hover p { color: var(--text-primary); }

    .arch-container { display: grid; grid-template-columns: 1fr 1fr; gap: 3rem; align-items: center; }
    .arch-diagram { color: var(--accent); font-family: monospace; font-size: 0.9rem; padding: 2rem; background: rgba(255, 255, 255, 0.05); border-radius: var(--radius-lg); }
    .arch-text p { margin-bottom: 1.5rem; font-size: 1rem; line-height: 1.7; color: var(--text-muted); }
    .arch-text strong { color: var(--accent); font-family: var(--font-grotesk); }

    .quickstart-steps { max-width: 700px; }
    .step { display: flex; gap: 2rem; padding: 2rem 0; }
    .step + .step { border-top: 1px solid var(--outline-variant, rgba(208,198,171,0.2)); }
    .step-number { font-family: var(--font-grotesk); font-size: 2rem; font-weight: 900; color: var(--on-primary-container); background: var(--primary-container); width: 4rem; height: 4rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; border-radius: var(--radius-lg); }
    .step-content h3 { font-family: var(--font-grotesk); font-size: 1.1rem; margin-bottom: 0.5rem; text-transform: uppercase; }
    .step-content code { font-family: monospace; background: var(--surface-inverse); color: var(--accent); padding: 0.2em 0.5em; display: inline-block; margin-bottom: 0.5rem; border-radius: var(--radius); }
    .step-content p { font-size: 0.9rem; color: var(--text-secondary); }

    .footer { padding: 3rem; background: var(--surface-inverse); color: var(--text-inverse); border-radius: var(--radius-xl) var(--radius-xl) 0 0; }
    .footer-content { display: flex; align-items: center; gap: 2rem; }
    .footer-logo { font-family: var(--font-grotesk); font-size: 1.1rem; font-weight: 900; background: var(--primary-container); color: #000; padding: 0.15rem 0.5rem; border-radius: var(--radius); }
    .footer a { color: var(--accent); }
    .footer-small { color: var(--text-muted); font-size: 0.8rem; margin-left: auto; }

    @media (max-width: 900px) {
        .hero { grid-template-columns: 1fr; }
        .hero-ascii { border-radius: 0 0 var(--radius-xl) var(--radius-xl); min-height: 300px; }
        .hero-title { font-size: 2.5rem; }
        .feature-grid { grid-template-columns: 1fr; }
        .arch-container { grid-template-columns: 1fr; }
        .nav { padding: 0.8rem 1.5rem; }
        .nav-links { gap: 1rem; font-size: 0.75rem; }
        .section-l { padding: 3rem 1.5rem; }
        .section-dark { margin: 0.5rem; }
        .footer-content { flex-direction: column; align-items: flex-start; }
        .footer-small { margin-left: 0; }
    }
    @media (max-width: 1100px) {
        .feature-grid { grid-template-columns: repeat(2, 1fr); }
    }
</style>
