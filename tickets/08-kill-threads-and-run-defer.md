# 08: Kill green threads and run deferred calls

## What to build

Any holder can kill a thread. A task can register cleanup that runs on block exit and cancellation.

**Blocked by** [05](05-wait-on-channel-operations.md)

**Status** done

## Acceptance criteria

- [x] `kill` interrupts running tasks at scheduler checkpoints and wakes tasks blocked on a join or channel. It waits for the target's own cleanup.
- [x] A self-kill stops ordinary execution, runs cleanup once, and publishes `Killed`. An already terminated task can be killed again without changing its result.
- [x] `defer` captures arguments when registered and runs the calls once in reverse order on normal exit, early return, or cancellation. Cancellation lets cleanup already underway finish.
- [x] Cancellation propagates to descendants. An uncommitted transfer is cancelled without duplicate delivery. A transfer that committed stays committed.
- [x] All joiners see the same outcome after cleanup, whether return or cancellation commits first. Ordinary `kill` is unavailable in an `ending` function.
