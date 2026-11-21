# 03: Start and join a green thread

## What to build

A program can start a named task, continue running, and join it to retrieve its result.

**Blocked by** [01](01-transfer-ownership-with-own.md)

**Status** done

## Acceptance criteria

- [x] `thread` requires exactly one visible `threads` capability. The parent evaluates the task arguments in source order using the existing argument binding rules.
- [x] Ordinary argument bindings are consumed, exclusive task parameters require `own`, and `shared` arguments remain available to the parent.
- [x] `Thread<T>` is shared. Several joiners can retrieve the same shareable `Result<T, Killed>` outcome, and exclusive task results are rejected.
- [x] Ready tasks run in a reproducible order. Fixed scheduling points allow another task to run even when one task loops without I/O.
- [x] A child can outlive normal parent return. `thread` is allowed in an `ending` function, while `join` is excluded. `pure` and `deterministic` functions cannot start tasks.
- [x] A self-join parks the caller. If no task can run and no pending event can wake one, the runtime reports a deadlock and its wait reasons.
