# 07: Close channels and drain buffered messages

## What to build

A producer can close a channel and its consumers can drain existing messages before observing `Closed`.

**Blocked by** [06](06-time-out-channel-operations.md)

**Status** ready-for-agent

## Acceptance criteria

- [ ] `ClosableChannel<T>` has separate send and receive operations with the `_closable` suffix in all three modes.
- [ ] Any holder can close a closable channel. Repeated close is harmless. Existing buffered messages are drained in FIFO order.
- [ ] Pending and future sends on a closed channel return `Closed` with their messages. Pending and future receivers observe `Closed` once drained.
- [ ] An open empty channel reports `Empty` to a nonblocking receiver. A blocking closable receive returns `Result<T, Closed>`.
- [ ] Timeout, closure and transfer races produce one outcome per operation.
