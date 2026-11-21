# 01: Transfer ownership with `own` and share capabilities explicitly

## What to build

A program can hand a value to an owning parameter, borrow an exclusive Host Capability for a synchronous call, and explicitly declare a capability shareable in `main`.

**Blocked by** None (can start immediately)

**Status** ready-for-agent

## Acceptance criteria

- [ ] An `own` argument consumes the whole source binding. Reads after a move fail unless a writable binding has been assigned again on every reachable path.
- [ ] An ordinary parameter borrows an exclusive value for the call. Borrowed values cannot escape, and `own` cannot consume a `mut` argument or a `shared` value.
- [ ] Two `mut` arguments cannot designate the same storage, including through forwarding calls. Update the existing mutable-alias decision and its examples with the new rule.
- [ ] `main` can receive an exclusive `own` or a `shared` capability. Calls and compound values preserve sharing rights, and exclusive values cannot be duplicated through assignment, field access, or payload binding.
