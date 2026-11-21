# 06: Time out channel sends and receives

## What to build

A task can bound its wait for a message or for space in a buffer while other tasks run.

**Blocked by** [05](05-wait-on-channel-operations.md)

**Status** ready-for-agent

## Acceptance criteria

- [ ] Both timeout operations accept `Uint32<ms>`. Zero attempts the operation once without parking.
- [ ] A pending deadline can wake a task and keeps the scheduler from reporting deadlock while progress is still possible.
- [ ] A send that times out returns the original message. A timed-out operation is removed from the wait queue and cannot transfer a message later.
- [ ] At the deadline, transfer and timeout resolve to one outcome. A controlled clock exercises repeatable boundary cases.
