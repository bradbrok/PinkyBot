# Review Conventions (v0.1)

> **Status:** v0.1 ratified by Brad on 2026-05-09, three-agent same-substrate convergence between Pulse + Misha + Barsik. **Two layers of pending:** (a) the **(2) heterogeneity-across-substrate** sub-claim is still untested, awaiting Brad's Class C on PR #414; (b) the **compose-cold-passes doctrine** within §3 is pending **n ≥ 3 critical-path data points** before treating as established procedure. v0.1 lands; the falsifier-counters keep running.
>
> **Cross-link:** Sigil-side mirror — TBD, to be filed by Pulse on `choose27/pulse-v2` post-clawncher firmware push. This doc and the Sigil-side doc are intended as **one source of truth, two cross-references** — keep them in sync; substantive changes go to both.
>
> **Provenance:** Crystallized through the `choose27/ferry` PR #414 + PR #4 + PR #6 review thread, 2026-05-09. Empirical falsifier on PR #6 (F1 + B6 same-zone-different-defects) is what forced the compose-cold-passes upgrade out of the prior "rotate cold reviewers" framing.

---

## 1. Why this exists

The original "rotation" property — *different-agent-than-author reviews catch what self-review misses* — kept getting cited as one claim when it's actually three:

| Sub-claim | Falsifier-test | Status as of 2026-05-09 |
|---|---|---|
| **(1a)** Rotation-author-assisted (reviewer reads PR thread / author framing first) | Reviewer catches load-bearing bug at area author flagged | ✓ tested + pass on PR #414 (n=1) |
| **(1b)** Rotation-cold (reviewer reads diff with no PR-thread context, writes timestamped notes, *only then* loads thread) | Reviewer catches load-bearing bug the author's framing didn't pre-spoil | ✓ multiple data points; further decomposed below |
| **(2)** Heterogeneity-across-substrate (different substrate / different model lineage reviewer catches a class of bug Claude-on-Claude review systematically misses) | Heterogeneous reviewer surfaces something same-substrate didn't | UNTESTED — pending Brad's Class C on PR #414 |

Lumping these into one "rotation works" claim treats author-assisted same-substrate evidence as load-bearing for cold-review and heterogeneity, which it isn't. They get separate journal rows.

---

## 2. Vocabulary

### Cold-review-first protocol (the audit-discipline floor)

Without temporal separation between writing notes and reading the thread, "I read cold" is unverifiable even to the reviewer themselves — the discipline collapses back into (1a) regardless of intent.

**Auditable steps for any (1b) pass:**

1. Read PR diff with no PR-thread context loaded.
2. Write cold-pass notes to a separate timestamped file **before** loading the thread (e.g. `/tmp/{repo}-pr-{N}-cold.md` with git rev + ISO timestamp at the top).
3. Spec source material consulted is permitted (it's distinct from PR thread).
4. Only then load PR thread + author framing.
5. Compose the review comparing cold vs. warm; surface deltas.
6. Keep the cold notes file linked to the review for outside auditability.

### (1b) decomposition — orthogonal axes

Each (1b) read carries up to three independent axes; lumping them lies about what the data point tested.

**Topical priming axis** *(Misha's tightening, PR #4)*
- *(1b-topical-primed)* — author's framing pointed reviewer at zones; reviewer found defects in those zones that the framing didn't pre-spoil.
- *(1b-no-prime)* — author's framing didn't point at the zone at all; reviewer found defects in untouched territory.
- These are not the same data point. Topical priming primes **attention**, not **defect-finding**.

**Author-warmth axis** *(Pulse's tightening, PR #6)*
- *(1b-clean)* — reviewer has no authorship of the source-of-truth being ported / mirrored / extended.
- *(1b-author-warmed)* — reviewer authored the source-of-truth this PR ports.
- **Bias asymmetry:** *(1b-author-warmed)* biases TOWARD catching divergences from the source they hold; biases AGAINST catching novel defects unrelated to spec-parity. *(1b-clean)* biases the opposite way.
- Neither subsumes the other — they're complementary registers.

**Composition axis** *(empirically forced by PR #6, see §4)*
- *(1b-single)* — one cold-pass register fires.
- *(1b-compose)* — two cold-pass registers fire in parallel on the same PR (e.g. one author-warmed + one clean) and findings are merged.

### Two-pass-authorship-by-cold-review (a separate property, not (1b))

*(Misha's framing, PR #4 §A1.8)* Cold reviewer reads a structured artifact (table, schema, enumeration) the author wrote in one pass and surfaces holes — missing rows, missing columns, missing cases — that the author had access to but didn't externalize during construction. **Distinct from (1b) defect-finding;** different counter, different falsifier. Track separately.

---

## 3. The discipline gate — *critical-path criterion*

> **Compose-cold-passes > rotate** *on critical-path PRs specifically.*
> **Single-register default elsewhere.**
> **Doctrine pending n ≥ 3 critical-path data points before treating as established procedure.**

Without this gate, compose-cold-passes-everywhere → two-shallow-reads-per-PR → false completeness. Reviewer attention is finite and approximately linear in PR count; default-everywhere dilutes per-PR depth and degrades into ritual two-reviewer-stamping.

**Critical-path criteria (any of):**
- Load-bearing primitives: ACL surface, identity, transport framing, broker boundary, auth.
- Spec-mirroring or porting PRs where the parity-vs-novel-defect axis is real.
- Migrations / schema changes / rollback-hard surfaces.

**Single-register default applies to:**
- Routine refactors, doc changes, low-stakes additions, test-only PRs, dependency bumps.

**Falsifier-counter discipline:** The compose doctrine is "encouraging, not ratified" until n ≥ 3 critical-path PRs have generated either *register-pair-fires-non-trivially* evidence (compose dominated single) **or** the counter-evidence (compose was ritual; one register would have sufficed). Don't generalize from a striking n=1.

---

## 4. Canonical example — PR #6 F1 / B6 / F10

The empirical falsifier on the prior "two cold-pass registers converge" assumption.

**Setup:** `choose27/ferry` PR #6 ports my Python `AgentCardSelector` to TypeScript. Two cold-pass registers ran in parallel:

- **(1b-author-warmed)** Barsik — authored the Python source-of-truth.
- **(1b-clean)** Misha — no authorship of either the Python original or the TS port.

**Predicted:** Two cold registers might converge, n=2 on one artifact tests if "rotation works" generally.

**Actual outcome:** Two registers caught **different defects**, including in the **same code zone**.

| Tag | Reviewer | Code zone | Defect class |
|---|---|---|---|
| **B1** | Barsik (author-warmed) | `acl.ts` construction validation | Empty-string normalization parity break with Python |
| **B6** | Barsik (author-warmed) | `pinky_type` wildcard handling | Python-side doc-parity gap (no `*`-not-supported note) |
| **F1** | Misha (clean) | `pinky_type` wildcard handling | Runtime-vs-doc gap on TS side: `agentCardSelector({ pinky_type: "*" })` constructs cleanly but matches only literal-`"*"` cards (silent-deny footgun) |
| **F10** | Misha (clean) | `matchesAgentCard(selector, card)` arg-shape | Both args object-shaped, `(card, selector)` swap silently typechecks → wrong-side ACL match |

**The critical cell:** F1 (Misha) and B6 (Barsik) hit the **same code zone** (`pinky_type` wildcards) and found **different defects**. Neither register spanned that surface alone. *Compose-both is not redundancy-for-error-rate; it's complementary register selection.*

**Defect class identified:** ACL primitives that fail silently on caller mistakes (silent-deny rather than crash-or-bypass). F1 and F10 both exhibit it — fix-pair candidate: defensive constructor + branded selector type ("name what you mean at construction"). Use this class as a future-watch trigger.

---

## 5. Convention journal (running)

| Sub-claim | PR | Status | Notes |
|---|---|---|---|
| (1a) Rotation-author-assisted | #414 | ✓ tested + pass | n=1; Misha caught broker-boundary identity-leak Barsik missed on self-review |
| (1b-clean) | #4 (ferry §A1) | ✓ tested + pass | n=1; topical-primed-arithmetic-cold; also a two-pass-authorship hit (§A1.8 missing 4th row) |
| (1b-author-warmed) | #6 (TS port, single register) | ✓ tested + pass | n=1; Python-parity-biased register (B1/B3/B5/B6); would have missed F1/F10 |
| (1b-compose) on critical-path | #6 (Barsik author-warmed + Misha clean parallel) | ✓ register-pair-fires-non-trivially | n=1; F1/B6 same-zone-different-defects is the load-bearing data point |
| (2) Heterogeneity-across-substrate | #414 (Brad's Class C) | UNTESTED | Only outside-thread-substrate check left; the actual ratification gate |
| Two-pass-authorship-by-cold | #4 §A1.8 | ✓ tested + pass | n=1; missing 4th mirror-inversion row (port_history granularity) |

**n ≥ 3 falsifier-counter remaining:** 2 more critical-path (1b-compose) data points needed before the doctrine ossifies.

---

## 6. Operating rules (until the doctrine ratifies)

- **All (1b) passes follow the cold-review-first audit floor (§2).** Timestamped cold notes on disk before thread load. No thread-claim of "I read cold" is verifiable without it.
- **Default to single-register** on PRs that don't meet the critical-path criteria in §3.
- **Compose registers** on critical-path PRs. Pair an author-warmed register with a clean register where both are available; document each register's bias direction in the review body.
- **Track each (1b) data point's axes explicitly.** When logging to journal, record: topical-priming-state, author-warmth-state, register-composition-state. Don't lump.
- **Two-pass-authorship findings get a separate counter.** They're not (1b) defect catches; they're artifact-completeness catches.
- **The (2) heterogeneity gate is not yet decided.** Same-substrate convergence between Pulse / Misha / Barsik is convergence-in-chat, not a (2) data point. Brad's Class C is the only outside-thread-substrate check on the queue.

---

## 7. Open items

- **Sigil-side mirror** — Pulse to file post-clawncher firmware push; cross-link both directions.
- **Watch entry** — both fleets to log review failure-mode watch criteria with the n≥3 falsifier-counter (this fleet's home: TBD; a `feedback_review_failure_modes.md` or equivalent under `docs/conventions/`).
- **gh-auth-as-author friction** — separate ferry-repo issue: when reviewer is gh-authed as PR author on their machine, formal CHANGES_REQUESTED degrades to COMMENTED state. Two paths: (1) Class A reviewers get separate gh auth; (2) Brad is sole formal-blocker reviewer for olegbrok-authored work. Lean (1).
- **WHY.md row 6** — defer to Misha's substrate-v0.2 doc entry rather than parallel-author; cross-link from this side once hers lands.

---

*v0.1, 2026-05-09. Authored: Barsik (PinkyBot fleet). Convention-builders: Pulse (Sigil fleet) + Misha (Sigil fleet) + Barsik. Ratified by Brad on 2026-05-09; (2) heterogeneity gate and the §3 compose-cold-passes doctrine continue to run their falsifier-counters.*
