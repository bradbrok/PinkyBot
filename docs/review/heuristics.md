# Shared Review Heuristics

> A lens for **authoring** and **reviewing** PRs in the agent pool.
> Goal: compress review rounds by surfacing classes of bugs at write-time, not just review-time.
>
> Origin: PR8b (TmuxSession response capture, #496, merged as `7817bf2`), crystallized across 4 review rounds with Pushok / Murzik / Barsik / Brad.

---

## Why this exists

Each review round is a lesson. Without a shared frame, the lesson dies with the PR — the next author hits the same class of bug, and the reviewer re-derives the same finding. With a shared frame, the lens transfers:

- **Reviewer** applies it → finds bugs faster, with named categories.
- **Author** internalizes it → writes code that's already passed the review-time check.
- **Pool** compounds it → review rounds drop proportionally to adoption.

Empirical evidence (PR8b): 4-round arc compressed each iteration because the author was learning to apply the lens at write-time, not just absorbing review notes. Round 3's findings were small and within-framework; round 4 surfaced new flavors of an existing case that revealed the case's dimensionality — both productive outcomes of the lens.

---

## The meta-rule: every case has a mechanical detector

This framework is a **sieve of detectors, not a taxonomy of bug shapes**.

Each case in Part 3 earns its place by having a procedure the author can run **without judgment**. "Grep all callers of any function whose docstring documents composition" is mechanical. "Is there an invariant here?" is not — it lands wherever the reader's charity puts it.

The discipline:

- **Author** runs the sieve at write-time. The detectors are short — minutes, not hours.
- **Reviewer's job is to find what the sieve missed.** Every reviewer-found bug is either (a) an instance of an existing case the author skipped, or (b) a candidate for a new detector.
- **(b) is how the framework grows.** New cases come from real bugs + named detectors, not from speculation.

If you find yourself wanting to add a case without a detector, file it in Appendix B as a candidate with the open question: *what's the mechanical detector?* It earns case-hood when one exists.

**The Tagged Observation Pattern.** When you notice a meta-mechanic on only n=1 evidence, neither promote it to a principle nor lose it. File it as an `Open observation (n=1)` sidebar in the case study where it emerged. The sidebar must (a) name the specific mechanism, not an umbrella, and (b) **define the falsification test — what would distinguish n=2 from coincidence**. Without the test, the sidebar just sits there; future-you won't connect a second instance because you never said what would count.

This is the procedural inverse of premature taxonomy: keep the breadcrumb visible, keep the discipline honest.

> _Evidence quality: promoted on three within-doc sidebars across different mechanisms (failure-mode visibility, topology shift, case multiplicity), not on cross-doc-domain n=2. Procedural patterns are more domain-agnostic than bug-shape cases — a deliberate softening, called out so a future reader can revisit if the assumption proves wrong._

**Size ceiling:** 5 sections + 2 appendices. If the doc wants to grow past that, fork into sibling docs (e.g. `heuristics-state-machines.md`, `heuristics-concurrency.md`). One sieve per doc, otherwise the sieve dulls.

---

## Part 1 — Start with the matrix-delta

If a PR ships with a regression matrix, test matrix, or behavior table, **that is the first artifact a reviewer reads**. Not the code. The matrix.

Two checks:

1. **What changed in observable behavior?** The delta. If you can't name it in one sentence, the matrix isn't doing its job — push back.
2. **What's silent?** Rows that didn't change. If a PR claims to fix X but the X-row is unchanged from before, ask what's really being fixed.

**Author tip:** if you're about to open a PR without a matrix and the change touches behavior, draft one first. The act of writing it surfaces gaps in your own model.

---

## Part 2 — Find the concurrency

Before reading the diff, map the concurrent execution paths the changed code participates in:

- Threads, coroutines, processes, subprocesses, network peers.
- Shared state between them: files, sockets, DB rows, in-memory queues, env vars, locks.
- **Every junction is a question.** Walk each one through Part 3.

If you can't draw the concurrency graph in your head, the PR is too big or the code is too tangled — say so.

**Author tip:** if the diff touches `await`, `Thread`, `Process`, `subprocess`, `asyncio`, `queue`, or any shared file/socket, you owe the reviewer (and yourself) the concurrency map in the PR description.

---

## Part 3 — The Five Cases

For every junction Part 2 surfaces, walk these five:

### 1. Canonical race
Two paths read/write the same state without coordination. Classic. Look for: shared variables touched from multiple coroutines/threads, file/socket I/O where ordering matters, "the other side will have written by then" assumptions.

### 2. Failure-propagation
One path fails. Does the other path **notice**, or does the failure get **masked**? Patterns:
- Exception swallowed in a `try/except` that the parallel path depends on.
- A task that crashed silently — its consumer is still waiting on a queue/future that will never resolve.
- A subprocess that died; the parent thinks it's still running.

### 3. Post-completion straggler
Work continues **after** the completion signal. Where do stragglers land?
- A coroutine completes the user-facing turn but a background task is still writing to shared state.
- A `done` flag is set, but in-flight callbacks fire after.
- Tear-down begins while a slow task is still mid-flight.

### 4. Rejection-window
Work submitted **just before** a rejection condition triggers, but processed **after**. Boundary timing.
- A request arrives during shutdown.
- A message enqueued microseconds before the consumer flips to "closed."
- A reconnect attempt scheduled before the parent decided to stop reconnecting.

### 5. Documented-composition drift
When a docstring or comment documents **how pieces compose** ("X always comes up with Y", "must be called after Z"), the doc is a **contract**. Grep all callers. Verify they uphold it.

This is the case that PR8b round 2 surfaced: `_start_tailer`'s docstring documented "Called from `_spawn_tmux_repl` after the REPL boots" — but `_spawn_tmux_repl` had three callers and only one of them started the tailer. The fix was to enforce the invariant at the spawn point, not the call sites.

**Author tip:** if you write a composition contract in a docstring, the next thing you do is `grep` your own callers. If you don't, the reviewer will, and the round-trip costs more.

---

## Part 4 — Fail-on-broken discipline

Every regression test that lands with a fix must be **verified to fail on the broken commit** before merge.

Why: a test that passes on the fix but *also* passes on the broken commit is a false-green — it didn't catch the bug. It might catch a future regression by luck, or it might not. You don't know.

Procedure:
1. `git checkout <broken-commit>`
2. Apply the new test (cherry-pick, copy, whatever).
3. Run it. **It must fail.**
4. `git checkout <fix-commit>`. Run it. It must pass.

If step 3 doesn't fail, the test is testing something else. Rewrite or delete.

Cost: ~2 minutes of git acrobatics. Value: every regression test in the suite is provably real.

---

## Part 5 — Write-time vs review-time

This frame is **twice as valuable at write-time as at review-time**.

- At review-time: reviewer names a category, author fixes one bug.
- At write-time: author writes code that doesn't trip any of the five cases, and there's no round-trip at all.

The compression effect compounds across the pool. Every agent authoring with this lens cuts review rounds proportionally. The doc is the lens.

Practical write-time checklist (1 minute):
- [ ] Is there a matrix delta I can point at? If not, draft one.
- [ ] Did I touch concurrency? If yes, draw the graph.
- [ ] Walked the five cases at each junction?
- [ ] If I wrote a composition contract in a docstring, did I grep all callers?
- [ ] Are my regression tests proven to fail on the broken commit?

If all five are yes before you open the PR, the review is short.

---

## Appendix A — PR8b case studies

Concrete instances drawn from the PR8b arc (PR #496, merged as squash commit `7817bf2` on beta after 4 review rounds). Line references use `path@7817bf2:line` — stable against the merged commit since the original round-by-round commits (`a9a0eef`, `584a23a`) are orphaned by the squash. Future doc updates should preserve the SHA refs rather than chasing HEAD, so the case studies stay anchored to the commits they describe.

### Round 1 — Case 1 (canonical race): meta-clobber on pumped-dispatch

**Bug.** `TmuxSession._inflight_meta` is a single dict slot that stores per-turn metadata while a turn is in flight. The original worker design pulled the next prompt off the queue and dispatched it immediately after `send_keys`, **without** awaiting turn completion. Under back-to-back dispatches, the second turn's `_deliver_turn` would overwrite `_inflight_meta` before the first turn's response was matched against it. Silent meta corruption on every concurrent turn.

**Detector that caught it.** Matrix-delta + canonical-race walk: shared per-turn state (`_inflight_meta`) read/written by two paths (`_deliver_turn` writer, `_handle_turn_complete` reader) without a coordination primitive.

**Fix.** Added `_turn_done: asyncio.Event` as a gate. Cleared at dispatch time inside `_deliver_turn` (`tmux_session.py@7817bf2:1180–1204`), set at turn completion in `_handle_turn_complete` (`@7817bf2:986`). The worker awaits `_turn_done.wait()` between dispatches (`@7817bf2:1110`).

**Regression tests.** `tests/test_tmux_session.py@7817bf2:1017` (`test_deliver_turn_clears_turn_done_before_send_keys`) and `:1044` (`test_deliver_turn_send_keys_failure_re_arms_turn_done`). Both verified to fail pre-fix.

### Round 2 — Case 5 (documented-composition drift): force_restart skips tailer

**Bug.** `_start_tailer` was called exactly once — from `connect()` after `_spawn_tmux_repl` returned. Its docstring documented "Called from `_spawn_tmux_repl` after the REPL boots" — but at the time the docstring was written, only `connect()` actually called the spawn function. Two other callers existed: `force_restart()` and `attempt_reconnect()`. Both called `_spawn_tmux_repl` without starting a new tailer. Result: after `force_restart`, the REPL was alive but no tailer was tailing the transcript → `turn_done` never fired → death loop on the next turn.

This bug was **pre-existing** and silent before PR8b. The round-1 `turn_done` gate changed the failure mode from silent → loud (worker timeouts where there used to be silent misses), which surfaced it.

**Detector that caught it.** Documented-composition drift walk: `_start_tailer`'s docstring documents a composition contract. Grep all callers of `_spawn_tmux_repl` → three sites → one upholds the contract, two skip the step.

**Fix.** Moved `_start_tailer()` invocation **inside** `_spawn_tmux_repl` (`tmux_session.py@7817bf2:745`) so the composition is enforced at the spawn point, not at the call sites. One move beats three duplicates. Removed the docstring's caller claim — the invariant is now structural, not documentary.

**Regression test.** `tests/test_tmux_session.py@7817bf2:1249` (`test_force_restart_resumes_tailer`). Verified to fail on the round-1 fix commit (turn_done gate present, spawn-time tailer-start move absent), pass on the round-2 fix commit. Both intermediate commits are orphaned by the squash merge; the discipline was applied against the pre-merge round-by-round history.

> **Open observation (n=1): fixing one case sometimes surfaces another by changing failure-mode loudness.**
> The round-1 `turn_done` gate (Case 1 fix) changed the failure mode of the pre-existing tailer-skip bug from silent (missed turn matches) to loud (worker timeouts). That loudness is what made Case 5 detectable in round 2. The bug had existed for as long as `force_restart` had existed; nothing surfaced it before.
>
> **Falsification test (n=2):** an instance must (a) involve a pre-existing latent bug, (b) become detectable specifically because a *separate* fix changed *its* observable behavior (timeout where there was silence, exception where there was a wrong-but-valid value, etc.), and (c) be confirmable as pre-existing via git archaeology. A coincidental ordering of two new bugs doesn't count.
>
> This is **observed, not promoted** — we have one instance. If you hit a matching mechanic, file in Appendix B with the falsification check applied.

### Round 3 — Matrix-delta-first paid off

By round 3, the matrix delta between rounds was small enough to read in one sitting. The remaining findings were a medium-severity teardown-ordering nit and one test-symmetry gap (round-2 added a regression test for the recovery path but not the symmetric tear-down path). Both fixed in a single force-push. No new categories; the sieve held.

### Round 4 — Case 5 again, two new flavors (drift in the fix itself)

Murzik delivered the round-3 cross-model pass (relayed via Brad) with two more in-scope findings — both *consequences* of round 3's Case 5 fix, in different directions.

#### 4a — Symmetric-teardown gap (rollback)

**Bug.** Round 3's fix moved `_start_tailer()` **into** `_spawn_tmux_repl` (composition-as-unit). The spawn body's exception handler killed the tmux session on failure but didn't tear down a partially-started tailer: if `_start_tailer` raised after `self._tailer = TmuxTranscriptTailer(...)` was assigned but before/during the `start()` await, the caller transitioned DEAD with a live orphan tailer instance.

**Detector that caught it.** Symmetric-teardown walk: if setup composes A+B as a unit, the rollback path must un-compose A+B as a unit. Round 3 had the up-direction; the down-direction was untested. Mechanical version: grep every `await self._start_X` for a paired teardown in the surrounding exception handler.

**Fix.** Rollback now calls `_stop_tailer()` and nulls `self._tailer` **before** `kill_session()` and the re-raise (`tmux_session.py@7817bf2:744–757`). Order matters: stop the tailer first so its `_read_and_dispatch` loop is cancelled gracefully before tmux goes away.

**Regression test.** `tests/test_tmux_session.py@7817bf2:1409` (`test_spawn_tmux_repl_rollback_clears_partial_tailer`). Monkey-patches `TmuxTranscriptTailer.start` to raise; asserts `ss._tailer is None` post-rollback. Verified to fail on the round-2 fix commit (rollback only killed tmux), pass on the round-4 fix commit.

#### 4b — Retained-state-across-lifecycle gap (same-path resume)

**Bug.** Round 2's fix drained the tailer's in-progress turn buffer when `set_transcript_path(new_path)` was called with a path **different** from the current one. This covered cold-restart-with-new-session-id cases. But `claude --continue` after `force_restart` resumes the **same** JSONL path — the path-equality guard skipped the drain, and partial assistant text from the killed session survived into the next session's first turn. First complete turn's callback would fire with `old_partial + new_text`.

**Detector that caught it.** Retained-state walk: round-3's spawn-as-unit fix changed the lifecycle topology — tailer instances now restart in lockstep with REPL spawns. But the *buffer state inside the tailer* is retained across that lifecycle by design (instance is reused for stats continuity). Round-2's drain was correct in its original topology (path-swap = session-swap) but the new topology has lifecycle restarts where path doesn't change. Mechanical version: for every piece of state retained across a lifecycle boundary, verify a draining/reset point exists on that boundary, not just on adjacent ones.

**Fix.** Move the buffer drain into `_stop_tailer` (`tmux_session.py@7817bf2:1045–1077`). Lifecycle-boundary drain becomes the primary defense; path-swap drain becomes belt-and-suspenders. Defense-in-depth here is honest because the two drains cover different scope categories (path-changed vs. lifecycle-restart).

**Regression test.** `tests/test_tmux_session.py@7817bf2:1344` (`test_stop_tailer_drains_buffer_for_same_path_resume`). Feeds partial turn → `_stop_tailer` → `_start_tailer` (same path) → complete turn → asserts the callback's text is the new turn only, no `partial from X` prefix. Verified to fail on the round-2 fix commit, pass on the round-4 fix commit.

> **Open observation (n=1): fixing one case can change the system's topology, leaving prior fixes with incomplete coverage in the new topology.**
> Round 3's Case 5 fix (compose tailer-start into spawn) changed two things implicitly:
> (i) Spawn now has a richer rollback surface — the symmetric teardown contract widened.
> (ii) Tailer lifecycle is now coupled to spawn lifecycle, so state retained inside the tailer (the buffer) now spans a different scope than before.
>
> Round 2's drain at `set_transcript_path` was correct in the round-2 topology. Round 3 didn't break it — but the new topology made a parallel drain at `_stop_tailer` necessary, and that wasn't audited.
>
> **Falsification test (n=2):** an instance must (a) identify a prior fix that was provably correct in its original topology, (b) show that a *later* fix changed the topology — coupled lifecycles, moved a composition point, widened a contract — and (c) show the prior fix becoming incomplete in the new topology *without itself being broken*. Two unrelated fixes that both turn out to be incomplete don't count.
>
> The Round-2 sidebar named a related-but-distinct mechanism (silent → loud); this one is topology-shift. Both fall under the loose umbrella "fixes have second-order effects," which is too broad to be useful as a case. **Observed, not promoted** — file in Appendix B with the falsification check applied, never under the umbrella.

**Meta-observation across all four rounds.** Findings distributed across cases as:
- Round 1: Case 1 (canonical race)
- Round 2: Case 5a (caller-grep flavor), surfaced by silent → loud failure-mode shift from round 1
- Round 3: Case 2 (failure-propagation, test-symmetry gap)
- Round 4: Case 5b + 5c (symmetric-teardown flavor + retained-state-across-lifecycle flavor), both consequences of round 3's Case 5 fix changing the system's topology

When a single PR exercises three of five cases and surfaces three flavors of Case 5, the framework is doing its job — and Case 5 is signaling that it has more dimensions than just caller-grep (see Appendix B).

> **Open observation (n=1): multiple flavors of one case in a single PR may signal an incomplete detector, unusually case-prone code, or both.**
> PR8b surfaced Case 5 across three compositional axes — **WHO** composes (caller-grep, 5a), **HOW** the composition unwinds on failure (symmetric-teardown, 5b), and **WHAT** state composition preserves (retained-state, 5c). The most likely explanation is "both": the detector is provably incomplete (the Appendix B candidates point at the missing axes) AND TmuxSession is unusually compositional — state machine + lifecycle + subprocess + file watcher + worker loop + turn buffer all composing in one class.
>
> **Falsification test (n=2):** the next instance of 2+ flavors of one case in a single PR should be classified by composition-density of the code under review:
> - **High-composition PR hits Case 5 in 2+ flavors** → reinforces "code surfaces this case densely" (code-quality signal). Says nothing new about the detector.
> - **Low-composition PR hits Case 5 in 2+ flavors** → framework-signal dominates regardless. The detector needs splitting (5a/5b/5c become first-class cases).
> - **Any PR hits 2+ flavors of a *different* case** → both signals generalize; the procedural lesson is "cases can have flavor-dimensions" rather than "Case 5 specifically is wide."
>
> Observed, not promoted. If you see a matching arc, file in Appendix B with the density classification.

---

## Appendix B — Case candidates (need detectors before earning case-hood)

Per the meta-rule, a case earns its place when there's a mechanical detector. These are real patterns we've seen, but the detectors aren't sharp yet. Filed here to keep the lesson alive without diluting Part 3.

### Candidate: type-erased-but-runtime-enforced invariant
**Pattern.** A function signature claims one type (often `Any`, `dict`, `str`, or a too-loose protocol), but a stricter runtime contract is enforced inside. Callers pass things that satisfy the signature but not the contract; the failure is a runtime assertion or worse.

**Candidate detector.** The typechecker — but only if the signature is tightened. As long as the signature lies, the typechecker is mute. So the real detector is two-step: (1) grep functions whose body asserts/coerces beyond what the signature claims; (2) tighten the signature; (3) let the typechecker find the callers. That's a refactor, not a check. Earns case-hood when there's a one-step grep that surfaces the lie.

### Candidate: architectural-rule-without-enforcement-point
**Pattern.** A rule lives in a README, an ADR, a CLAUDE.md, or tribal knowledge. It says "always do X" or "never call Y from Z." There's no test, no lint rule, no type constraint. The rule decays as the codebase grows and no one re-reads the doc.

**Candidate detector.** None known. This is the open question: can a doc-stated rule be made mechanically checkable? Possibilities: a linter that parses a structured "rules" file, a test that asserts properties of the dependency graph, an LLM-as-judge pass on PRs against the rules. Each has costs. Until one earns its keep, this stays a candidate.

### Candidate: lifecycle-symmetry of Case-5 fixes (n=2 of Case 5 in one PR)
**Pattern.** Case 5 ("documented-composition drift") has a clean detector for the **forward** direction: grep callers of any function whose docstring claims composition. But a Case 5 *fix* — moving the composed call into a setup function — can leave two adjacent gaps:

1. **Symmetric teardown.** If setup composes A+B, the rollback path inside setup must un-compose A+B. If teardown lives elsewhere (a separate cleanup function), it must also un-compose A+B, not just A or just B.
2. **Retained state across the lifecycle the fix coupled.** If the fix made A and B lifecycle-coupled, any state retained inside A or B now spans a different scope than before. Drains, resets, or invariant-restorations at the old scope boundary may no longer cover the new one.

Both surfaced in PR8b round 4 (Appendix A 4a + 4b). Same root mechanism: a Case 5 fix changes the topology, and the detector "grep callers" doesn't cover the new surface area.

**Candidate detectors.** Two separate ones, neither sharp yet:
- For (1) symmetric teardown: grep every `await self._start_X` / `self._X = SomeThing(...)` for a paired teardown in the surrounding exception handler **and** in any explicit cleanup function. The grep is mechanical but the "paired" check needs judgment unless we accept false positives.
- For (2) retained state: when a fix couples two lifecycles, enumerate state inside each and check whether each piece has a draining/reset point on the new shared boundary. This requires understanding what counts as "retained state" — harder to mechanize.

Earns case-hood when either detector tightens to a one-step grep that an author can run without judgment. Until then: when reviewing or authoring a Case 5 fix, walk both gaps manually.

### Open process questions

- **Concurrency-map threshold.** Does the Part 2 concurrency-map requirement scale to PRs that touch `asyncio` lightly? Working threshold: "touches `await` outside trivial sequential code" (i.e. anything that introduces a new concurrent path, not just awaits one).

- **Adoption path.** How does a new agent in the pool internalize this? Three options on the table:
  - **(a) Advisory.** Doc exists; reviewers reference it when relevant.
  - **(b) PR template checklist.** Part 5's author checklist becomes a section in the PR description template.
  - **(c) Output-producing skill.** A SKILL.md that, on invocation, produces a numbered report — for each of the five cases, the reviewer must state explicitly whether the case applies and what the detector found ("N/A" requires a why). The review is gated on the report existing.

  **(a) and (b) are functionally identical**: both depend on attention discipline, which decays to zero in the long run. A checkbox you don't have to run is the same as a doc you don't have to read. **(c) is the only non-decaying shape** — but only if the skill is *output-producing*, not a reference-doc loaded into context. Same shape as `code-review-coordinator`: the skill produces output in a defined form, so it's structurally unavoidable.

  Recommendation pending owner decision: **(c), gated on the skill being designed as a mechanical checklist that produces output**. The commitment cost is real (skill authoring + every reviewer pays the report-writing tax), so this is the owner's call. Framed honestly: anything less than (c) is (a) wearing a costume.

---

_Authored by Pushok with input from Barsik. Tracked in `docs/review/` per the carve-out in PR #499._
