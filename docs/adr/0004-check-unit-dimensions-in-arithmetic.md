# Check unit dimensions in arithmetic

Numeric types can carry unit dimensions. Addition, subtraction, and comparison require matching dimensions. Division derives its quotient dimension and checks that dimension against the explicitly declared `f64` result type. Literals can take their dimension from context. These rules catch incompatible arithmetic at compile time and preserve units in computed values.
