# 05: Wait for channel sends and receives

## What to build

Two green threads can exchange a message through a bounded channel while other runnable threads continue.

**Blocked by** [03](03-start-and-join-green-threads.md), [04](04-use-nonblocking-channels.md)

**Status** done

## Acceptance criteria

- [x] On a full buffer, `send_blocking` parks the sender. On an empty buffer, `receive_blocking` parks the receiver. A completed ordinary receive returns `T`.
- [x] A send or receive wakes the oldest eligible waiter. A later nonblocking operation cannot overtake an eligible waiter.
- [x] Shared handles can be used both as task arguments and as messages between tasks. FIFO buffer and waiter behavior is reproducible.
- [x] A program with only blocked channel operations reports deadlock when no event can make progress.
