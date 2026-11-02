# Agent continuity: validation of the unreleased 1.2.0 prototype

Measured locally on Windows / Python 3.12, 2026-11-02. No real personal memory stores were opened
or copied by the tests, and no answer-model API was called. Test homes and databases are temporary.
The implementation under review started at `9fc0888`; this report records follow-up fixes.

## Reproduced and fixed

| Case | Before | After |
|---|---|---|
| Pausing with an explicitly empty next action | Old value passed validation, then was overwritten by an empty value | Refused without changing the saved packet |
| Claiming assigned work with an empty objective | The inherited goal could disappear | Invalid input; whitespace/private-only goals are refused too |
| Forty newer completed tasks after an older active task | The active task fell outside the first page and startup packet | An indexed unfinished-first queue preserves visibility; first-prototype rows are upgraded |
| Code changes after its review completed | A delivery task could still consume the old review | Dependency revision freshness is checked through the whole chain |

`tests/test_work_stress.py` includes these reproductions, upgrade preservation, query-plan checks,
600 seeded action transitions, eight independent processes competing for one task, and recovery
after a test writer is killed before commit. Exactly one competing claim succeeds; rejected
updates leave the packet unchanged. Killing an uncommitted writer preserves the prior checkpoint
and passes SQLite's integrity check. This is process-crash recovery, not a physical power-loss test.

`tests/test_work_stdio.py` starts two separate MCP server processes against one temporary store.
It checks assigned implementation/review dependencies, transferred Unicode text and file/check
references, privacy omission, concurrent claims, handover, and state after a third server starts.
These are real stdio transports with scripted clients, not calls to Claude/GPT models or their UIs.

## Broader checks

- The standard product suite ran **229 tests, all passed, none skipped**, after the continuity
  fixes. Run: `python -m unittest discover -s tests -p "test_*.py" -v`.
- The research suite ran **76 tests: 75 passed, one skipped**, because its LongMemEval source and
  frozen research-run prerequisite were not both available at the expected paths. No claim of a
  new benchmark or blind evaluation follows from this run.
- Vector-cache, sharing and warmup self-checks passed on disposable stores.
- Ruff and scoped whitespace checks passed.
- The full run exposed shutdown resource warnings: three ledger handles left open in a test,
  cached Postgres readers left open at shutdown/reset, and an unreaped dummy test process. Their
  cleanup was corrected. Four additional connection-lifecycle tests cover setup failure,
  reconnect, reuse and idempotent cleanup. The affected shared-brain/team/recovery paths are
  validated separately with ResourceWarning enabled: **36 tests passed, zero ResourceWarnings**
  (including those four new tests). This is an affected-path rerun after the 229-test full run.
- The final 1.2.0 wheel built successfully; its changed Python modules were checked byte-for-byte
  against the working source. It was not installed or published.

On Windows, launch the multiprocessing suite through the normal unittest CLI (or a proper file
with a main guard). An initial ad hoc stdin-based runner could not be imported by spawned child
processes. The normal CLI run above replaced that invalid attempt. A new upgrade test also needed
an explicit SQLite connection close; a SQLite transaction context alone does not close it.

## Still outside this validation

Actual installed AI-app handover, automatic quota detection and waking another app, a visual
timeline review, cross-PC task ownership, macOS/Linux execution, and a long-running soak remain
separate gates. Existing memory sync does not distribute `brain_work` tasks. Passing these tests
does not turn app/session labels into authentication or enforce filesystem edit locks.

No installation, service restart, public push, tag or release was performed.
