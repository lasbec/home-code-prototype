# 04: Use a nonblocking buffered channel

## What to build

A program can create a typed channel, try to send a value, and receive it without waiting.

**Blocked by** [01](01-transfer-ownership-with-own.md), [02](02-add-uint32.md)

**Status** done

## Acceptance criteria

- [x] The `channels` environment creates `Channel<T>` with `Bounded(Uint32!0)` or `Unbounded`. An existing handle suffices to operate on it.
- [x] Each successful `try_send` adds one message in FIFO order. `try_receive` reports `Empty` when there is no message, and an unbounded channel never reports `Full`.
- [x] `Full` returns the original payload. A failed or successful send consumes the ordinary message binding, while `shared` values remain available.
- [x] The message type can contain shared handles or Host Capabilities, including another channel. Compound payloads retain their ownership and sharing rules.
