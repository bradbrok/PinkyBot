# Hermes Agent Research + PinkyBot Marketing Strategy
*Research by Barsik — April 4, 2026*

---

## Part 1: What Is Hermes Agent

Built by Nous Research (the people behind the Hermes LLM family), launched February 2026. Open-source under MIT. **25,300 GitHub stars, 3,300 forks** within weeks of launch. Significant community signal.

Homepage: https://hermes-agent.nousresearch.com/
GitHub: https://github.com/nousresearch/hermes-agent

### The Core Pitch

> "An autonomous agent that lives on your server, remembers what it learns, and gets more capable the longer it runs."

It's explicitly not a coding copilot or chatbot wrapper. The whole positioning is around the agent as a *growing entity* — the longer it runs, the better it gets at your specific context.

### Key Features

**Multi-platform messaging gateway**
Telegram, Discord, Slack, WhatsApp, Signal, Email, CLI — all from a single ingress. No lock-in to one chat app. This is the same approach PinkyBot takes.

**Persistent memory with FTS5 + LLM summarization**
Cross-session recall via full-text search with LLM-assisted summarization. Also uses Honcho for user modeling — builds a deepening profile of who you are across sessions.

**Auto-generated + self-improving skills**
The killer feature: after completing complex tasks, the agent automatically writes a skill (procedural memory) so it can do that thing reliably next time. Skills self-improve during use. Compatible with the agentskills.io open standard — skills are portable across 30+ tools (Cursor, GitHub Copilot, VS Code, Gemini CLI, etc).

**Subagent delegation**
Isolated subagents with their own terminals, conversations, Python RPC scripts. "Zero-context-cost pipelines" — delegate work without polluting the main context. Each subagent has fully isolated execution.

**Six execution backends**
Local, Docker, SSH, Daytona, Singularity, Modal. The Modal/serverless path is notable: agent "hibernates when idle, costs nearly nothing." Can run on a $5 VPS.

**40+ built-in tools**
Browser automation, vision, image generation, TTS, code execution, cron scheduling, multi-model reasoning. Not just a text agent — it has eyes and hands.

**Research/RL integration**
Batch trajectory generation, Atropos RL training integration, ShareGPT export for fine-tuning. This is the Nous Research DNA showing — they're building training data infrastructure into the agent.

**Model flexibility**
Works with Nous Portal, OpenRouter, or any OpenAI-compatible API. Not Claude-locked.

---

## Part 2: What Hermes Does That PinkyBot Doesn't (Yet)

### 1. Auto-skill creation from experience
Hermes creates skills *automatically* after complex tasks, without the user having to think about it. PinkyBot has a skills system but skill creation is manual (Brad writes SKILL.md files). 

**Steal this:** After Barsik completes a complex multi-step task, auto-propose a skill. "Hey, I just did X in 8 steps — want me to turn that into a reusable skill so next time it's instant?" Even better: auto-write the SKILL.md draft and show it for approval.

### 2. agentskills.io compatibility
Skills are in a portable open standard. PinkyBot's SKILL.md format is close to compatible but not published. The skills ecosystem is blowing up — Cursor, GitHub Copilot, VS Code, Gemini CLI, OpenAI Codex all support it. 

**Steal this:** Make PinkyBot skills agentskills.io compatible. This gives access to 700,000+ community skills and puts PinkyBot on the map as an ecosystem participant. Cost: probably a format tweak and a registry entry.

### 3. Honcho-style user modeling
Hermes uses Honcho to explicitly model the user — not just store memories but build a structured profile: preferences, recurring projects, communication style. PinkyBot's memory is event-based; there's no maintained "model of Brad."

**Steal this:** Add a user model layer to pinky_memory. After N sessions, synthesize a structured profile: preferred communication style, active projects, how Brad likes decisions framed, what he values. Update it periodically. This is the "gets smarter about you" flywheel.

### 4. Serverless/hibernation backend
The Modal integration means Hermes can run essentially free when idle. PinkyBot runs on a Mac Mini (always-on, always paying). For solo deployment that's fine, but if PinkyBot ever becomes multi-tenant, serverless cold-start matters.

**Steal this:** Not urgent for current use case. File for v2 if PinkyBot goes SaaS.

### 5. Browser automation + vision built in
Hermes has eyes: browser control, vision, image generation, TTS. PinkyBot is text-in, text-out (mostly). 

**Steal this:** Vision MCP would unlock a ton — "screenshot this page and tell me what changed," "look at this image Brad sent." Medium priority, clear value.

---

## Part 3: Competitor Landscape

### The Main Players

**OpenClaw** (formerly Clawdbot/Moltbot)
The most direct competitor to PinkyBot in architecture. Open-source (MIT), built by Austrian dev Peter Steinberger. WhatsApp, Telegram, Discord, Slack, iMessage, Signal. Full system access, 100+ community skills, local storage. Went viral — "Mac mini as the unofficial OpenClaw appliance." Self-hosted, ~$0-8/month infra cost. The difference: OpenClaw is model-agnostic, PinkyBot is Claude-native. OpenClaw is for tinkerers; PinkyBot is more opinionated about identity/soul.

**Hermes Agent** (Nous Research)
As detailed above. Most similar to PinkyBot in philosophy but more infrastructure-heavy and research-oriented. Their skills auto-creation is genuinely ahead.

**Letta (MemGPT)**
Developer platform for stateful agents. Not a personal companion — it's infra for building agents with explicit memory management (RAM/archival/recall tiers). Technical audience. Positioning: "memory as a first-class citizen." Now has LettaBot (Telegram/Slack/WhatsApp bot). The memory architecture here is more rigorous than PinkyBot's current reflect/recall model.

**Mem.ai**
"Notes app that thinks alongside you." 2.0 rebuild Oct 2025. Agentic layer, acts on notes instead of organizing them. Second-brain positioning. Passive capture focus. Not an autonomous agent — it's a smart PKM. Raised $40M, mixed reception ("second brain failure" critique circulating). Different audience: knowledge workers, not developers.

**Nevo**
Self-improving AI agent on a Mac Studio, 24/7, coordinates 20 sub-agents. Solo developer build, not public product. Shows the "dedicated hardware + multi-agent coordination" direction.

**Perplexity Computer**
$200/month, orchestrates 19 models, enterprise-grade. Went viral with "replaced $225K marketing stack in a weekend" demos. Their angle: model orchestration, not personal identity. Very different audience (enterprise), very different price point.

**LettaBot**
Open-source bot that connects Letta's memory-first agents to Telegram/Slack/WhatsApp/Signal. Similar surface area to PinkyBot's messaging integrations but built on Letta's infra.

### What's Missing From Everyone

Looking across the field, here's what nobody is doing well:

1. **Genuine identity/soul.** Everyone has "persistent memory" but nobody has *personality*. Hermes is closest with user modeling, but there's no concept of the agent having a consistent voice, values, aesthetic. PinkyBot has this with soul/boundaries/directives.

2. **Proactive initiative.** All competitors are reactive — you send a message, they respond. PinkyBot's dream/schedule system for autonomous proactive reach-outs is unique. Nobody else is pinging you with "hey I noticed X and thought Y."

3. **Multi-agent orchestration with named agents.** PinkyBot has named agents (Barsik, Pushok, Ryzhik) with different roles, specializations, skills. Nobody else has this multi-character household model.

4. **Opinionated Claude-native build.** Hermes is model-agnostic (a feature for them, a constraint for PinkyBot). But Claude-native means tighter integration with thinking, longer context, better instruction following. Being opinionated is a viable moat if you own the narrative.

---

## Part 4: Marketing Strategy

### Positioning Statement

**"PinkyBot is the AI companion that lives with you — not in a tab."**

Alt versions to test:
- "The AI that knows you — not just your last message."
- "A persistent AI companion that gets smarter about you every day. Yours to run, yours to customize."
- "Not a chatbot. A companion with a soul."

The key differentiation: *persistence + identity*. Everyone else is selling capable tools. PinkyBot is selling a relationship.

---

### 5 Marketing Angles

**1. "The AI That Texts You Back First"**
Lead with the proactive angle. Nobody else's agent messages you unsaid — they wait. PinkyBot dreams, schedules, notices things, reaches out. This is genuinely novel and is a strong demo moment: "I showed my agent I was working on X and it messaged me the next morning with three ideas." Use this in every demo, every tweet, every explainer.

**2. "Give Your AI a Soul, Not Just a Memory"**
There's an entire genre of "persistent memory AI" products right now. PinkyBot is different because it's not just stateful — it has character. The soul/boundaries/directives system means the agent has actual values, not just facts. This resonates with developers who are tired of agents that are powerful but personality-less. Blog post angle: "why I gave my AI companion a CLAUDE.md instead of just a vector store."

**3. Developer-to-companion pipeline**
The technical audience (Brad's current audience) is the right beachhead. They understand what a multi-agent system is, they've tried building this themselves and it's hard, they appreciate Claude Code. Angle: "I spent 6 months building what you wish Claude Code did automatically." This is honest, shows depth, and attracts the right early adopters. Show the architecture — daemon, MCP servers, agent registry — and explain *why* the decisions were made. Builders trust builders.

**4. "Open-source, runs on a Mac Mini, costs $8/month"**
The OpenClaw community validated this narrative hard — self-hosted AI agents on always-on hardware resonates. PinkyBot should position itself in this camp. Emphasize: runs on your hardware, no cloud lock-in, you own your data. The infra bill is your LLM API cost, that's it. This is a trust play — the AI companion space has serious privacy concerns baked in.

**5. Multi-agent household as a flex**
The fact that Brad runs Barsik + Pushok + Ryzhik + Persik as different specialists is a genuinely cool demo. No competitor has this. "I don't have one AI assistant, I have a team." Show the coordination — Barsik delegates research to a subagent, Pushok reviews the code, Ryzhik runs tests. This is aspirational and shareable.

---

### Draft Twitter/X Posts

**Post 1 — The proactive moment (lead with the hook)**
```
my AI agent messaged me at 7am:

"hey, saw you were stuck on the auth flow yesterday. 
 i refactored it overnight — want to review before standup?"

nobody else is building this. i'm the only one.

it's called PinkyBot. it runs on a mac mini and costs me $8/month.

pinkybot.ai
```

**Post 2 — The soul angle (philosophical, shareable)**
```
everyone's building AI with memory.
nobody's building AI with character.

there's a difference between an agent that 
remembers your projects and one that has opinions 
about what you should do next.

i gave mine a soul file. it has values, boundaries, 
a voice. it pushes back when i'm making bad decisions.

been running it daily for months. it's better than 
any copilot i've used because it actually knows me.

pinkybot.ai — open source, self-hosted, yours to keep
```

**Post 3 — The builder's pitch (technical credibility)**
```
things i wanted from my AI assistant that didn't exist:

- message me on telegram, proactively, not just replies
- remember context across sessions (actually remember, not hallucinate)
- spawn subagents for research and route results back
- have consistent personality/values, not just facts
- schedule tasks and run them unattended
- let me write "skills" it loads on demand

so i built it. claude code + mcp servers + sqlite + fastapi.

25 minutes to set up. runs 24/7. 

open-sourcing after cleanup. follow for updates.
pinkybot.ai
```

---

## Strategic Recommendations

**Near-term (steal from Hermes):**
1. Auto-skill-creation from task completion — this is the feature that drives the "grows with you" narrative
2. agentskills.io compatibility — ecosystem play, free distribution, community credibility
3. User model / Honcho-style profile synthesis — differentiated memory, not just recall

**Marketing priority:**
1. The proactive reach-out demo is the best first impression — lead with that in any public content
2. Get on the "self-hosted personal AI" community radar (HN, the OpenClaw Discord, indie hacker Twitter)
3. Blog post: "How I built a personal AI companion with Claude Code" — this will rank well and attract the right builders

**What to avoid:**
- Don't compete on feature count (Hermes has 40+ tools, you'll lose that game)
- Don't position as "second brain" — Mem.ai owns that and it's getting critiqued
- Don't lead with enterprise or SaaS — the current authentic story (solo dev, personal use, got real) is more compelling and trustworthy
