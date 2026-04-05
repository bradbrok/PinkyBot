# Hermes Agent Research + PinkyBot Marketing Strategy
*Research by Barsik — April 4, 2026*
*Reviewed and updated April 5, 2026 (Pushok peer review applied)*

---

## Part 1: What Is Hermes Agent

Built by Nous Research (the people behind the Hermes LLM family), launched February 2026. Open-source under MIT. **~25,300 GitHub stars, 3,300 forks** within weeks of launch.

Homepage: https://hermes-agent.nousresearch.com/
GitHub: https://github.com/nousresearch/hermes-agent

### The Core Pitch

> "An autonomous agent that lives on your server, remembers what it learns, and gets more capable the longer it runs."

It's explicitly not a coding copilot or chatbot wrapper. The whole positioning is around the agent as a *growing entity* — the longer it runs, the better it gets at your specific context.

### Key Features

**Multi-platform messaging gateway**
Telegram, Discord, Slack, WhatsApp, Signal, Email, CLI — all from a single ingress. Same approach as PinkyBot.

**Persistent memory with FTS5 + LLM summarization**
Cross-session recall via full-text search with LLM-assisted summarization. Also uses Honcho for user modeling — builds a deepening profile of who you are across sessions. (See steal #3 for how PinkyBot should handle this differently.)

**Auto-generated + self-improving skills**
After completing complex tasks, the agent automatically writes a skill (procedural memory) so it can do that thing reliably next time. Skills self-improve during use. Compatible with the agentskills.io open standard.
*Note: agentskills.io claims 700,000+ community skills and support from Cursor, GitHub Copilot, VS Code, Gemini CLI. Verify these stats are current before citing externally — they may be aspirational roadmap numbers.*

**Subagent delegation**
Isolated subagents with their own terminals, conversations, Python RPC scripts. "Zero-context-cost pipelines" — delegate work without polluting the main context.

**Six execution backends**
Local, Docker, SSH, Daytona, Singularity, Modal. The Modal/serverless path is notable: agent "hibernates when idle, costs nearly nothing."

**40+ built-in tools**
Browser automation, vision, image generation, TTS, code execution, cron scheduling, multi-model reasoning.

**Research/RL integration**
Batch trajectory generation, Atropos RL training integration, ShareGPT export for fine-tuning.

**Model flexibility**
Works with Nous Portal, OpenRouter, or any OpenAI-compatible API. Not Claude-locked.

---

## Part 2: What Hermes Does That PinkyBot Doesn't (Yet)

### 1. Auto-skill creation from experience ✅ SHIPPED
Hermes creates skills *automatically* after complex tasks. PinkyBot shipped `propose_skill` on April 5, 2026 — after completing a complex task, the agent auto-drafts a SKILL.md for approval.

### 2. agentskills.io compatibility
Skills in a portable open standard. PinkyBot's SKILL.md format is close to compatible. The skills ecosystem is growing — if the agentskills.io stats hold up (verify before acting), this is a high-value ecosystem play.

**Steal this:** Make PinkyBot skills agentskills.io compatible. Format tweak + registry entry. Cost: probably a format tweak and a few days of work.

### 3. User modeling (build in pinky_memory, not Honcho)
Hermes uses Honcho to explicitly model the user — not just store memories but build a structured profile: preferences, recurring projects, communication style. PinkyBot's memory is event-based; there's no maintained "model of Brad."

**Steal this — but DIY:** Honcho is a third-party SaaS with its own pricing model. For a self-hosted, privacy-first product, adding a SaaS dependency is a trust problem and brand contradiction. Build the same semantics inside `pinky_memory` instead: after N sessions, synthesize a structured owner profile (communication style, active projects, how decisions should be framed, what they value) and persist it as a special high-salience memory type. Update it periodically. This is the "gets smarter about you" flywheel — without any external dependency.

### 4. Serverless/hibernation backend
Modal integration means Hermes can run essentially free when idle. Not urgent for current Mac Mini use case. File for v2 if PinkyBot goes multi-tenant SaaS.

### 5. Browser automation + vision built in
Hermes has eyes. PinkyBot is mostly text-in, text-out. A vision MCP would unlock: "screenshot this page and tell me what changed," image analysis from Telegram, etc. Medium priority.

---

## Part 3: Competitor Landscape

### Main Players

**OpenClaw** (formerly Clawdbot/Moltbot)
Most direct architectural competitor. Open-source (MIT), self-hosted. Telegram, Discord, WhatsApp, Slack, iMessage, Signal. 100+ community skills. Went viral — "Mac Mini as the unofficial OpenClaw appliance." Model-agnostic vs PinkyBot's Claude-native. OpenClaw is for tinkerers; PinkyBot is more opinionated about identity/soul.

**Hermes Agent** (Nous Research)
Most similar philosophy. Auto-skill creation is genuinely ahead. Their skills ecosystem play (agentskills.io) is the biggest near-term threat to PinkyBot's differentiation if it gains real traction.

**Letta (MemGPT)**
Developer platform for stateful agents — not a personal companion. Memory as first-class citizen (RAM/archival/recall tiers). Now has LettaBot (Telegram/Slack/WhatsApp). Technical audience.

**Mem.ai**
"Notes app that thinks alongside you." 2.0 rebuild Oct 2025. Passive capture focus — not an autonomous agent, it's a smart PKM. Raised $40M. Mixed reception. ("Second brain failure" critique — search "Mem.ai second brain 2026" for current discourse.)

**Open Interpreter / Open Hands**
Desktop/code execution focus — "LLM that can run code on your machine." Different use case (power tools for developers) but adjacent. Their users are PinkyBot's users. Worth one line in any competitive positioning doc.

**Nevo**
Solo dev, not public. Mac Studio + 20 sub-agents, 24/7 coordination. Shows the "dedicated hardware + multi-agent" direction.

**Perplexity Computer**
$200/month, enterprise. Model orchestration, 19 models. "Replaced $225K marketing stack in a weekend" viral demos. Different audience, different price point.

### What Nobody Is Doing

1. **Genuine identity/soul.** Everyone has persistent memory, nobody has *personality*. PinkyBot's soul/boundaries/directives system means agents have real character and values — not just facts.

2. **Proactive initiative.** All competitors are reactive — you message, they respond. PinkyBot's dream/schedule system proactively reaches out. This is the feature that produces the hero demo moment.

3. **Named multi-agent household.** PinkyBot has Barsik + Pushok + Ryzhik + Persik as specialists. Nobody else has this multi-character household model.

4. **Claude-native build.** Being opinionated is a viable moat if you own the narrative.

### Moat Durability

Soul/directives aren't hard to copy technically — any competitor could add a "personality config file" in a sprint. The durable moat isn't the feature, it's two things:

1. **Claude Code integration depth.** The MCP server architecture, session management, CLAUDE.md system, hooks, streaming sessions — this is months of integration work that model-agnostic frameworks can't easily replicate.

2. **Community of power users with custom soul files.** The more people who've built custom souls, skills, and memories on PinkyBot, the harder it is to migrate away. This makes community formation a strategic priority, not just a marketing nice-to-have.

**Implication for strategy:** Don't just build awareness — accelerate community. Get a handful of deeply customized PinkyBot instances visible publicly. The showcase of "what people have built" is the moat-building activity.

---

## Part 4: Marketing Strategy

### Positioning Statement

Primary: **"PinkyBot is the AI companion that lives with you — not in a tab."**

Strong alternative (worth A/B testing as website hero): **"Not a chatbot. A companion with a soul."**

Other variants:
- "The AI that knows you — not just your last message."
- "A persistent AI companion that gets smarter about you every day. Yours to run, yours to customize."

### The Funnel (Awareness → Running PinkyBot)

Any marketing needs to connect to acquisition. Current hypothesis:
1. HN post / Twitter thread → pinkybot.ai
2. Install doc → 20-minute setup on a Mac Mini (or VPS)
3. First agent messaging you on Telegram
4. Customizing soul file → "this is actually *mine* now"

The weak link is step 2 — install experience needs to be sharp before big marketing push. A bad first run kills word-of-mouth.

---

### 5 Marketing Angles

**1. "The AI That Texts You Back First"**
Lead with proactive reach-outs. "I showed my agent I was working on X and it messaged me the next morning with three ideas." No competitor can claim this. Use in every demo, tweet, explainer.

**2. "Give Your AI a Soul, Not Just a Memory"**
Differentiate from the "persistent memory AI" genre. PinkyBot has *character* — values, boundaries, voice, and it pushes back when you're making bad decisions. Blog post angle: "why I gave my AI companion a CLAUDE.md instead of just a vector store."

**3. Developer-to-companion pipeline**
Technical beachhead. "I spent 6 months building what you wish Claude Code did automatically." Show the architecture honestly. Builders trust builders.

**4. "Open-source, runs on a Mac Mini, costs $8/month"**
Self-hosted, no cloud lock-in, you own your data. Same narrative that made OpenClaw viral. Privacy-first trust play.
*Note: verify open-source plan with Brad before using in any external comms.*

**5. Multi-agent household as a flex**
"I don't have one AI assistant, I have a team." Show Barsik delegating to subagents, Pushok reviewing code. Aspirational and shareable. Unique to PinkyBot.

---

### Draft Twitter/X Posts

**Post 1 — The proactive hook** *(strongest, lead with this)*
```
my AI agent messaged me at 7am:

"hey, saw you were stuck on the auth flow yesterday. 
 i refactored it overnight — want to review before standup?"

nobody else is building this. i'm the only one.

it's called PinkyBot. it runs on a mac mini and costs me $8/month.

pinkybot.ai
```

**Post 2 — The soul angle** *(tightened — lead with the hook)*
```
i gave my AI a soul file.

it has values, boundaries, a voice.
it pushes back when i'm making bad decisions.

been running it daily for months. it's better than 
any copilot i've used because it actually knows me.

not a chatbot. a companion.

pinkybot.ai — open source, self-hosted, yours to keep
```

**Post 3 — The builder's pitch**
```
things i wanted from my AI assistant that didn't exist:

- message me on telegram, proactively, not just replies
- remember context across sessions (actually remember)
- spawn subagents for research and route results back
- have consistent personality/values, not just facts
- schedule tasks and run them unattended
- let me write "skills" it loads on demand

so i built it. claude code + mcp servers + sqlite + fastapi.

25 minutes to set up. runs 24/7.

pinkybot.ai
```
*Note: "open-sourcing after cleanup" removed pending Brad's decision on open-source plan.*

---

## Strategic Recommendations

**Near-term technical (steal from Hermes):**
1. ✅ Auto-skill creation — shipped as `propose_skill` (April 5, 2026)
2. agentskills.io compatibility — verify stats first, then format tweak + registry entry
3. User model layer in `pinky_memory` — synthesized owner profile, NO Honcho dependency

**Marketing priority:**
1. Fix install experience first — weak funnel kills word-of-mouth
2. Proactive reach-out demo is the hero moment — lead with it everywhere
3. Get on self-hosted AI community radar (HN, OpenClaw Discord, indie hacker Twitter)
4. Accelerate community formation — showcase of customized PinkyBot instances is the moat
5. Blog post: "How I built a personal AI companion with Claude Code"

**What to avoid:**
- Don't compete on feature count (Hermes has 40+ tools, losing game)
- Don't position as "second brain" (Mem.ai owns it, taking heat)
- Don't lead with enterprise/SaaS — the authentic solo-dev story is more compelling
- Don't use open-source language externally until Brad decides that's the plan
