# Bounded scheduler delivery

`SCHEDULER_BUSY_DELIVER_AFTER_S` controls how long a scheduled wake waits for an
idle transport. Set it in registry settings or the daemon environment; the
registry setting takes precedence. The default is 600 seconds. Invalid,
non-finite and non-positive values fall back to the default.

For each fire, the effective delay is the minimum of that setting, half its
replay-age limit, and half the receipt ceiling. The absolute deadline follows
the original fire across release and API routing. Before it, scheduler delivery
waits for idle. After it, Claude and Codex tmux use the ordinary mid-turn paste
path, retaining the context lock, REPL lock, cancellation fence and unresolved
paste veto. A busy hook or active tool alone cannot prolong that wait.

Each scheduler paste writes `pasted_at` and its owning session generation to the
exact-fire ledger, together with the exact pasted prompt, before handing text to tmux. This marker is not acceptance
and does not increment delivery attempts. A crash after the marker may lose a
wake, but its replay cannot duplicate work. If that session is lost before
acceptance while its receipt is still active, the row records `PASTED_UNCONFIRMED_SESSION_LOST` and follows the
existing owner-alert route. Restart context lists its schedule name and fire
time with a warning to check before redoing the work.

A past-deadline paste has a fresh acceptance window from the paste handoff.
Only existing transcript or queue-dequeue evidence grants acceptance. Released
rows also receive a fresh first receipt window. Neither change resets the
replay staleness clock or counts context rendering as delivery.

Drain parking applies each row's own age limit. An attempt-cap event parks only
the oldest fire of the current drain episode. Restart context also lists owed
recurring wakes without consuming or receipting them.

Inflight checks use the stored pasted text, so later stale-drop notices cannot
change its identity. A legacy marked row without that text conservatively
fences its schedule until the owning session changes or its original receipt
ceiling expires. At the ceiling it is abandoned with
`PASTED_PROMPT_UNKNOWN_RECEIPT_CEILING` and one owner alert; the marked fire is
never replayed. Pre-marker rows retain the existing reconstructed-prompt probe.
