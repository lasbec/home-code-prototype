# Allow mutable aliases with matching refinement contracts

Mutable parameters refer to caller storage, so sequential writes through one alias are visible through the others. The prototype allows mutable arguments to alias when their refinement contracts match, and rejects possible aliases between refined and unrefined parameters. A write through a possible alias invalidates a flow-derived proof when the write could violate it. This keeps sequential aliasing available without letting an unrefined write break another parameter's declared `!0` invariant.
