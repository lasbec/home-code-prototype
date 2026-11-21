# Concurrency model

## Ownership

- An `own` parameter consumes the caller's binding. Reading a moved `mut` requires reassignment on every path.
- Ordinary immutable values can have multiple bindings. A move consumes the whole binding, including its payload.
- Ordinary parameters borrow exclusive values for the call. A `mut` borrow cannot be moved or alias another `mut`.
- In `main`, `own` grants exclusive ownership of a Host Capability and `shared` allows it to be shared.
- Passing a `shared` handle or capability leaves existing bindings valid. Both channel types and `Thread<T>` are shared.
- `local` stays within one thread. Arguments, messages and returns cannot carry it, even inside other values.

## Threads

- `thread run_task(args)` needs one visible `threads` capability. The caller evaluates the arguments first.
- A start moves ordinary argument bindings and shares `shared` arguments. Exclusive parameters must declare `own`.
- Tasks return shareable values. `thread` is available in `ending` functions and forbidden in `pure` or `deterministic`.
- Start returns `Thread<T>`, or `local EndingThread<T>` for an `ending` task. The latter can be explicitly shared as
  a `Thread<T>`. The conversion is one-way.
- `join` returns `Result<T, Killed>`. All joiners get the same outcome after cleanup. A self-join can block.
- `join_ending` and `kill_ending` accept local handles created by the caller. Both are `ending`.
- Ordinary `join` and `kill` are unavailable inside `ending` functions.
- Any holder can call `kill`. It waits for the target's cleanup. Self-kill unwinds the caller and reports `Killed`.
- A normal return can leave children running. Aborting a thread requests cancellation of all its descendants.
- `kill_ending` waits for the target's own cleanup. Ordinary descendants may still be cleaning up when it returns.
- `defer` captures arguments at registration and runs calls once, last in first out, on block exit or cancellation.
- In an `ending` function, deferred calls must be `ending`. Cancellation lets cleanup already underway finish.
- Return and cancellation race to set one outcome. The first to commit determines what all joiners receive.

## Channels

- The `channels` environment creates `Channel<T>` and `ClosableChannel<T>`. A handle suffices to use a channel.
- Capacity is `Bounded(Uint32!0)` or `Unbounded`. Messages and waiting senders and receivers are served FIFO.
- Both types support nonblocking, blocking and timed send and receive. The closable API uses `_closable` names.
- A send moves its message binding unless it is `shared`. Success returns `unit` in a `Result`.
- `Full`, `Timeout` and `Closed` send errors carry the original message.
- Nonblocking receive distinguishes `Empty` from `Closed`. Blocking receive on an ordinary channel returns `T`.
- Blocking receive on a closable channel returns `Result<T, Closed>`.
- Any holder can close a `ClosableChannel<T>`. Repeat closes have no effect. Receivers drain buffered messages first.
- Closing wakes pending senders with `Closed`. Receivers get `Closed` once the buffer is empty.
- A transfer commits once. Cancellation before commit leaves the message on its original side. A cancelled sender
  discards any unsent message it still holds. A committed transfer stays committed.
- Timeouts use `Uint32<ms>`. Zero makes one immediate attempt. `Uint32` has checked arithmetic and explicit,
  fallible conversion from `Int32`.

## Scheduling

- Ready threads run in a reproducible order with fixed scheduling points. Blocking waits park only the caller.
- Cancellation wakes channel waiters and interrupts running tasks at scheduling points, including loop back edges.
- Report a deadlock when no thread can run and no pending timer or external event can wake one.
- Each host environment defines how it handles concurrent calls. Live I/O and time are external inputs.
