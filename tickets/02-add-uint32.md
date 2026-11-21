# 02: Use `Uint32` for unsigned values and durations

## What to build

A program can compute a nonnegative duration with units and use a positive unsigned value as a capacity.

**Blocked by** None (can start immediately)

**Status** done

## Acceptance criteria

- [x] `Uint32` accepts values from zero to `2^32 - 1`, supports existing unit rules, and rejects negative and overflowing literals.
- [x] `Uint32!0` excludes zero and supports nonzero proofs, including for variables.
- [x] Unsigned arithmetic preserves the applicable unit and range rules and reports overflows and underflows.
- [x] Conversion from `Int32` is explicit and fails for negative inputs.
