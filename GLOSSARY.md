# Programming Language

## Language

**Function guarantee**

A property declared on a function and checked by the compiler. `ending` guarantees completion, `deterministic` guarantees repeatable results and observable effects for the same inputs and input state, and `pure` implies determinism and rules out host effects and caller-visible mutation.

**Commutative function**

A function marked `commutative` that permits otherwise ambiguous unnamed arguments. Swapping its interchangeable arguments preserves its behavior.

**Host environment**

A built-in set of host functions and the opaque capability type those functions accept. The prototype provides console and filesystem environments.

**Host capability**

An opaque data value supplied by the host that grants access to an external resource when passed to a host function.

**Host function**

A named operation supplied by the host, such as `print`, that can receive a host capability and operate on the associated resource.

**Mutable parameter**

A parameter marked `mut` that receives an argument variable also marked `mut` at the call site.

**Nonzero refinement**

A restriction on a numeric type that excludes zero, written `!0` in a type such as `int32!0<seconds>`.

**Unit-bearing number**

A number whose type includes a unit dimension, such as `meter` in `int32<meter>`.

## Concurrency

**Ownership parameter (`own`)**

A parameter that takes ownership of its argument. The caller loses access through the argument binding.

**Shared value (`shared`)**

A handle or Host Capability that multiple threads may keep and use. Passing it leaves existing bindings valid.

**Local value (`local`)**

A value confined to the thread that created it. The restriction also applies through a containing value.

**Thread start (`thread`)**

An expression that starts a named function in a green thread and returns a handle to that thread.

**Deferred call (`defer`)**

A call registered in a block to run when that block exits, including on cancellation.
