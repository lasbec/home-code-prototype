# Require nonzero evidence for division

Division requires a divisor with a nonzero refinement, such as `int32!0<seconds>`. Programs can establish that type through `if n != 0` or use a checked conversion that returns a `result`. This keeps division by zero out of accepted programs while allowing programs to handle unvalidated input as data.

Division produces a floating-point quotient with an explicit result type. A mutable call preserves a nonzero refinement when its parameter also requires the refinement. A call through an unrefined mutable parameter discards the proof.
