<script>
    // Landing/marketing page - standalone layout (no shared header)
</script>

<div class="landing">
    <nav class="nav">
        <div class="nav-logo">PINKY<span class="accent">.</span></div>
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
                GIVE YOUR AI<br>
                A <span class="accent">SOUL</span>.
            </h1>
            <p class="hero-subtitle">
                Pinky is a personal AI companion framework powered by Claude Code.<br>
                Long-term memory. Multi-platform messaging. Your personality, your rules.
            </p>
            <div class="hero-actions">
                <code class="hero-install">pip install pinky-ai</code>
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
</pre>
        </div>
    </header>

    <section id="features" class="section-l">
        <h2 class="section-l-title">WHAT IT DOES<span class="accent">.</span></h2>
        <div class="feature-grid">
            {#each [
                ['[ MEM ]', 'Long-Term Memory', 'File-based memories with markdown frontmatter. Human-readable, git-trackable, zero infrastructure.'],
                ['[ MSG ]', 'Multi-Platform Chat', 'Telegram, Discord, Slack, iMessage, email. Your AI talks across every platform.'],
                ['[ SOL ]', 'Soul System', 'CLAUDE.md defines personality, values, boundaries. Version-controlled identity.'],
                ['[ CAL ]', 'Google Services', 'Calendar and Gmail integration via MCP. Knows your schedule.'],
                ['[ WAK ]', 'Wake System', 'Cron-based scheduling. Morning check-ins, evening summaries, periodic tasks.'],
                ['[ SQL ]', 'Zero Infrastructure', 'SQLite for storage, markdown for config. One pip install and running.'],
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
│          LLM brain — agent loop,                  │
│     context management, tool execution            │
├──────────────────────────────────────────────────┤
│               CLAUDE.md (Soul)                    │
│      Identity, personality, boundaries,           │
│      user profiles, behavioral rules              │
├─────────────┬──────────────┬─────────────────────┤
│   Memory    │   Outreach   │    Google / Custom   │
│    MCP      │     MCP      │       MCP Servers    │
│   Server    │    Server    │                      │
└─────────────┴──────────────┴─────────────────────┘
</pre>
            <div class="arch-text">
                <p><strong>Claude Code</strong> handles all LLM orchestration.</p>
                <p><strong>CLAUDE.md</strong> is your AI's personality. Edit it like any markdown file.</p>
                <p><strong>MCP Servers</strong> provide capabilities. Memory, messaging, calendar.</p>
            </div>
        </div>
    </section>

    <section id="quickstart" class="section-l">
        <h2 class="section-l-title">QUICK START<span class="accent">.</span></h2>
        <div class="quickstart-steps">
            {#each [
                ['01', 'Install', 'pip install pinky-ai', ''],
                ['02', 'Initialize', 'pinky init --name "Your AI Name"', 'Creates CLAUDE.md, pinky.yaml, and memory directory.'],
                ['03', 'Customize', 'vim CLAUDE.md', 'Give your AI a personality.'],
                ['04', 'Connect', 'pinky connect', 'Writes MCP server config to Claude Code settings.'],
                ['05', 'Go', 'claude', 'Your AI now has memory, personality, and messaging.'],
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
    .landing { background: var(--white); }

    .nav { display: flex; justify-content: space-between; align-items: center; padding: 1.5rem 3rem; border-bottom: var(--border); position: sticky; top: 0; background: var(--white); z-index: 100; }
    .nav-logo { font-family: var(--font-mono); font-size: 1.5rem; font-weight: 700; letter-spacing: 0.05em; }
    .nav-links { display: flex; gap: 2rem; align-items: center; font-family: var(--font-mono); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }
    .nav-links a:hover { color: var(--gray-mid); }
    .nav-cta { background: var(--black); color: var(--yellow) !important; padding: 0.5rem 1.2rem; border: var(--border); font-weight: 700; transition: all 0.15s; }
    .nav-cta:hover { background: var(--yellow); color: var(--black) !important; }

    .hero { display: grid; grid-template-columns: 1fr 1fr; min-height: 80vh; border-bottom: 5px solid var(--black); }
    .hero-content { padding: 5rem 3rem; display: flex; flex-direction: column; justify-content: center; }
    .hero-title { font-family: var(--font-mono); font-size: 3.5rem; font-weight: 700; line-height: 1.1; margin-bottom: 1.5rem; }
    .hero-subtitle { font-size: 1.1rem; color: var(--gray-dark); margin-bottom: 2.5rem; max-width: 500px; line-height: 1.7; }
    .hero-actions { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
    .hero-install { font-family: var(--font-mono); background: var(--black); color: var(--yellow); font-size: 1rem; padding: 0.7rem 1.2rem; border: var(--border); }
    .hero-ascii { background: var(--black); color: var(--yellow); display: flex; align-items: center; justify-content: center; padding: 3rem; border-left: 5px solid var(--black); }
    .hero-ascii pre { font-family: var(--font-mono); font-size: 1rem; }

    .btn-l { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 700; text-transform: uppercase; padding: 0.7rem 1.5rem; border: var(--border); cursor: pointer; transition: all 0.15s; display: inline-block; text-decoration: none; color: var(--black); }
    .btn-l-primary { background: var(--yellow); }
    .btn-l-primary:hover { background: var(--black); color: var(--yellow); }
    .btn-l-secondary { background: var(--white); }
    .btn-l-secondary:hover { background: var(--gray-light); }

    .section-l { padding: 5rem 3rem; border-bottom: var(--border); }
    .section-dark { background: var(--black); color: var(--white); }
    .section-l-title { font-family: var(--font-mono); font-size: 2rem; font-weight: 700; margin-bottom: 3rem; letter-spacing: 0.05em; }

    .feature-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; }
    .feature-card { padding: 2rem; border: var(--border); margin: -1.5px; }
    .feature-card:hover { background: var(--yellow); }
    .feature-icon { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 700; color: var(--gray-mid); margin-bottom: 1rem; }
    .feature-card:hover .feature-icon { color: var(--black); }
    .feature-card h3 { font-family: var(--font-mono); font-size: 1.1rem; margin-bottom: 0.75rem; }
    .feature-card p { font-size: 0.9rem; color: var(--gray-dark); line-height: 1.6; }
    .feature-card:hover p { color: var(--black); }

    .arch-container { display: grid; grid-template-columns: 1fr 1fr; gap: 3rem; align-items: center; }
    .arch-diagram { color: var(--yellow); font-family: var(--font-mono); font-size: 0.9rem; padding: 2rem; border: 2px solid var(--gray-dark); }
    .arch-text p { margin-bottom: 1.5rem; font-size: 1rem; line-height: 1.7; color: var(--gray-mid); }
    .arch-text strong { color: var(--yellow); font-family: var(--font-mono); }

    .quickstart-steps { max-width: 700px; }
    .step { display: flex; gap: 2rem; padding: 2rem 0; border-bottom: 1px solid var(--gray-light); }
    .step:last-child { border-bottom: none; }
    .step-number { font-family: var(--font-mono); font-size: 2rem; font-weight: 700; color: var(--yellow); background: var(--black); width: 4rem; height: 4rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; border: var(--border); }
    .step-content h3 { font-family: var(--font-mono); font-size: 1.1rem; margin-bottom: 0.5rem; text-transform: uppercase; }
    .step-content code { font-family: var(--font-mono); background: var(--black); color: var(--yellow); padding: 0.2em 0.5em; display: inline-block; margin-bottom: 0.5rem; }
    .step-content p { font-size: 0.9rem; color: var(--gray-dark); }

    .footer { padding: 3rem; border-top: 5px solid var(--black); background: var(--black); color: var(--white); }
    .footer-content { display: flex; align-items: center; gap: 2rem; }
    .footer-logo { font-family: var(--font-mono); font-size: 1.2rem; font-weight: 700; }
    .footer a { color: var(--yellow); }
    .footer-small { color: var(--gray-mid); font-size: 0.8rem; margin-left: auto; }

    @media (max-width: 900px) {
        .hero { grid-template-columns: 1fr; }
        .hero-ascii { border-left: none; border-top: 5px solid var(--black); min-height: 300px; }
        .hero-title { font-size: 2.5rem; }
        .feature-grid { grid-template-columns: 1fr; }
        .arch-container { grid-template-columns: 1fr; }
        .nav { padding: 1rem 1.5rem; }
        .nav-links { gap: 1rem; font-size: 0.75rem; }
        .section-l { padding: 3rem 1.5rem; }
        .footer-content { flex-direction: column; align-items: flex-start; }
        .footer-small { margin-left: 0; }
    }
</style>
