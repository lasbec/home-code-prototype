# 09: Join and kill an `EndingThread` with a completion guarantee

## What to build

An `ending` function can start another `ending` task and wait for its result or kill it without introducing a cyclic wait.

**Blocked by** [08](08-kill-threads-and-run-defer.md)

**Status** ready-for-agent

## Acceptance criteria

- [ ] Starting an `ending` task produces `local EndingThread<T>`. It can be bound locally and used by `join_ending`, `kill_ending`, and deferred calls that accept it.
- [ ] `local` cannot escape through ordinary arguments, task arguments, messages, returns, or nested payloads. Explicit conversion to a shared `Thread<T>` is one-way.
- [ ] `join_ending` returns `Result<T, Killed>` and `kill_ending` waits for the target's own cleanup. Both are usable in `ending` functions.
- [ ] `ending` functions can only register `ending` deferred calls. Killing an `EndingThread` requests descendant cancellation without waiting for ordinary descendants to finish cleanup.
