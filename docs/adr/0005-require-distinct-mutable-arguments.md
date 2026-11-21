# Require distinct mutable arguments

Mutable parameters refer to caller storage. Each call must pass a distinct binding for each `mut` parameter, even when the parameters have the same refinement contract. A function can rely on its mutable parameters having distinct storage when it forwards them.

This replaces the original decision to permit aliases with matching refinement contracts. Distinct storage gives mutable borrows a single writer and keeps assignment and nonzero proofs tied to one binding. See [the rejected forwarding example](../../examples/rejected/alias.proto).
