# Programming language prototype

This throwaway checker and interpreter explores whether unordered calls, explicit host access, and checked function guarantees work together in a small language. It uses Python 3.10 or later and has no third-party dependencies.

## Run it

From the repository root, run the numeric program, check an intentionally invalid program, and run the numeric function examples.

```sh
python3 -B -m prototype run examples/numeric.proto
python3 -B -m prototype check examples/rejected/divisor.proto
python3 -B -m prototype test examples/numeric.proto
```

The numeric program returns `[-2.3333333333333335]`. The rejected example reports the source location of a divisor that needs a nonzero proof. Other examples under `examples/rejected/` cover ambiguous calls, mutable aliasing, host guarantees, and loop mutation.

To try live I/O, run the following command. After the prompt, type a line and press Enter. The program writes that line to `prototype-output.txt` in the current working directory, replacing the file if it exists, then reads and prints it.

```sh
python3 -B -m prototype run examples/live-io.proto --console --filesystem
```

`--console` passes standard input and output to `main`. `--filesystem` passes a capability with the filesystem access of the Python process. Relative paths resolve from its working directory. `--random` grants access to secure randomness from the operating system. A helper must receive a capability explicitly before it can use it.

| Host function | Result |
| --- | --- |
| `print(console, text)` | `result<unit, HostError>` |
| `write(console, text)` | `result<unit, HostError>` |
| `input(console)` | `result<string, HostError>` |
| `enableKeyInput(console)` | `result<bool, HostError>` |
| `disableKeyInput(console)` | `result<unit, HostError>` |
| `readKeysFor(console, timeout: int32<ms>)` | `result<list<string>, HostError>` |
| `readFileUTF8(filesystem, path)` | `result<string, HostError>` |
| `writeFileUTF8(filesystem, path: path, text: text)` | `result<unit, HostError>` |
| `replaceFileUTF8(filesystem, path: path, text: text)` | `result<unit, HostError>` |
| `random(source: random, bound0: int32, bound1: int32)` | `result<int32, HostError>` |

The host functions run immediately when called. `write` flushes text as supplied, and `print` also appends a newline. `input` reads one line and returns `Err(EndOfInput)` at end of input. `enableKeyInput` switches a supported Unix terminal to character input with echo disabled and returns `false` when that mode is unavailable. Once enabled, `readKeysFor` collects the decoded input characters in order for the given duration. A zero timeout polls immediately. `disableKeyInput` restores line input, and the CLI restores terminal settings on exit. `replaceFileUTF8` writes a temporary file beside the destination and atomically replaces the destination after the write succeeds. Host functions carry no function guarantees.

`random(source, bound0, bound1)` draws a uniform integer from the inclusive range. `pseudoRandom(state: seed, bound0, bound1)` is a pure, deterministic, terminating built-in for repeatable sequences. It returns a `RandomDraw` record with `value` and the next `state`, both `int32`. Pass the returned state into the next call. Each call takes one generator step, even when the bounds are equal. Both functions accept the bounds in either order. `pseudoRandom` maps its 32-bit outputs as evenly as possible across the range and is intended for simulations and games.

## Green threads

Grant `--threads` to a program whose `main` declares a `threads` capability. With exactly one such capability in scope, `thread task(...)` starts a named function and returns a shared `Thread<T>` for task result type `T`. Arguments are evaluated in source order. Ordinary bindings are moved, shared handles and capabilities remain available, and exclusive task parameters require `own`.

`join(thread)` waits for the task and returns its `result<T, Killed>` outcome. Each holder can join independently. Tasks are scheduled in FIFO order at statements and loop iterations. A child can continue after its parent returns. If every task is waiting and none can wake, the interpreter reports a deadlock with the wait reasons.

## Channels

Grant `--channels` to a program whose `main` declares a `channels` capability. To create a channel, give `create_channel` a result type such as `Channel<int32>` through a binding annotation, return type, or `as` annotation.

```text
let queue: Channel<int32> = create_channel(channels, Capacity::Bounded(2));
try_send(queue, 42);
let result = try_receive(queue);
```

`Capacity::Bounded(n)` takes a positive `uint32!0`. `Capacity::Unbounded` allows any number of queued messages. Channel handles are shared and suffice to send and receive. A send consumes the message binding unless the value is shared. `try_send` returns `result<unit, TrySendError<T>>`, whose `Full(message)` error contains the original message. `try_receive` removes the oldest message or returns an `Empty` error.

`send_blocking` waits for room and returns `result<unit, Never>`. `receive_blocking` waits for a message and returns `T`. A wait parks only the calling thread. When space or a message becomes available, the oldest eligible waiter gets it before later operations.

`send_timeout(channel, message, timeout)` and `receive_timeout(channel, timeout)` take a `uint32<ms>` duration. A zero duration makes one immediate attempt. If the deadline arrives first, the operation returns `Timeout` and leaves the wait queue. A send returns the original message in its `SendTimeout<T>::Timeout` error. Deadlines at the current clock time are processed before another transfer.

Use `create_closable_channel` with a `ClosableChannel<T>` result type when producers need to signal completion. Use the `_closable` suffix on each send and receive operation, for example `receive_blocking_closable` returns `result<T, Closed>`. Any holder can use `close(channel)`, which is idempotent. Buffered messages remain available in FIFO order. Pending and future sends return `Closed(message)`. Receivers see `Closed` once the buffer is drained.

## Mock host environments

A mock is a self-contained `.proto` file that starts with `mock console`, `mock filesystem`, or `mock random`. Export every function for that environment in the host function table and keep other declarations private. Each export replaces the host capability parameter with `state: mut T`, where `T` is the same type across all exports. The remaining parameters, result type, and commutative group match the host signature. The mock can use host-independent built-ins and construct `HostError(kind: HostErrorKind::Other, message: "details")`.

For example, this `seeded.proto` replaces live randomness with the language's seeded generator.

```text
mock random

pure ending fn init() -> int32 { return 42; }

export commutative bound0 bound1
fn random(state: mut int32, bound0: int32, bound1: int32) -> result<int32, HostError> {
    let draw = pseudoRandom(state: state, bound0, bound1);
    state = draw.state;
    return Ok(draw.value);
}
```

Use a bare flag for a live environment and `--environment=PATH` for a mock. A run can mix providers for different environments. This example uses live console and filesystem access with mocked randomness.

```sh
python3 -B -m prototype check seeded.proto --random-state=seed.literal
python3 -B -m prototype run examples/snake.proto --console --filesystem \
    --random=seeded.proto --random-state=seed.literal --random-state-out=next-seed.literal
```

`seed.literal` contains one Home Code literal such as `123`. The runner checks its type before calling `main`. If you omit the fixture, the runner calls a private `pure ending fn init()` returning the state type. A mock with `unit` state can omit both. Each run starts with fresh state, and all calls to that mock share it.

The runner writes the final literal when `main` returns, including when it returns `Err`. Interruptions and interpreter failures before `main` returns leave the output path untouched. You can pass the final state file back as a fixture for another run.

## Play Snake

```sh
python3 -B -m prototype run examples/snake.proto --console --filesystem --random
```

The board is 48 columns wide and 32 rows high. On a supported Unix terminal, Snake advances automatically about every 150 ms and redraws in place. Press `w`, `a`, `s`, or `d` to steer and `q` to quit. With redirected streams or an unsupported terminal, use those commands followed by Enter. The head is `@`, the body is `o`, and food is `*`. Each game draws one seed from the host and uses it to place every apple on a free cell, including the first.

The game saves completed rounds in `snake-scores.txt` in the current working directory and reads the best score on startup. The score file assumes one running game writes it at a time. A malformed or unreadable history disables saving for that run.

Run the game and language checks with `python3 -B -m unittest discover -s tests`.

## Play Runner

```sh
python3 -B -m prototype run examples/runner/main.proto --console --filesystem --random
python3 -B -m prototype test -r examples/runner/main.proto
```

Runner draws an 80-column × 40-row play screen. Use a UTF-8 terminal with at least that much space to see the full frame and its colors.

Choose **Play**, enter a name, and press Space to jump over barriers (`▓`) and gaps while collecting coins (`◈`). The course advances automatically and speeds up as the distance milestones double, up to six tiles per second. Press `q` to return to the menu early. With redirected input or an unsupported terminal, Enter advances one tile and Space followed by Enter jumps.

Completed runs are saved in `runner-scores.txt` in the working directory. The leaderboard keeps each player's best run, ranked by coins and then by distance. Names are case-sensitive. Use one running game per score file.

## Modules

Put imports before declarations. An import resolves a relative `.proto` path from the importing file and uses the filename stem as its namespace. For example, `import "./scores.proto"` makes exports available as `scores.loadScores(...)` or `scores.Score`. Stems must be valid identifiers. Circular imports and duplicate namespaces are errors.

Mark public functions, records, and enums with `export`. Place function examples above the `export` line. Exported declarations can use public types in their signatures and fields. Each file imports the namespaces it uses directly. Variables are local to function bodies.

`check`, `run`, and `test` check all reachable files. `run` invokes `main` from the selected file. `test` executes examples attached to functions in that file, and `test -r` includes examples from every reachable file once.

## Save and export a program

```sh
python3 -B -m prototype save examples/runner/main.proto runner.sqlite
python3 -B -m prototype export runner.sqlite runner-export
python3 -B -m prototype test -r runner-export/main.proto
```

`save` checks the entry file and all transitive imports, then writes one SQLite snapshot. `export` regenerates the module tree from the database, checks the generated program, and prints the path to its entry file. Generated source uses consistent formatting and keeps comments and function examples. The database file and export directory must be new paths with existing parent directories.

Inspect the snapshot with any SQLite client. `modules` identifies the entry file, `imports` records the graph, and `declarations` stores parsed declarations and bodies as JSON alongside effective guarantees. `semantic_facts` holds inferred types and declaration references. `calls` and `call_arguments` identify callees and record bound parameters in evaluation order. Token ordinals link these facts to source locations, and `comments` records placement between tokens. The schema version is stored in `PRAGMA user_version`. Treat the database as a snapshot for querying and export.

## Nano syntax highlighting

Add this line to your Nano configuration (`~/.nanorc` or `~/.config/nano/nanorc`), using the absolute path to your checkout.

```nanorc
include "/path/to/home-code-prototype/editors/nano/home-code.nanorc"
```

Nano automatically selects this syntax for `.proto` files under a directory named `home-code-prototype`. To select it for a file in a renamed checkout or elsewhere, run `nano -Y home-code path/to/file.proto`.

## Function examples

Place any number of `example` blocks directly above a user-defined function that explicitly declares both `deterministic` and `ending`. Include both keywords on pure functions with examples as well. Each block can have a quoted description.

```text
example "sets the duration to three seconds" {
    input elapsed: 5;
    expect unit;
    after elapsed: 3;
}
deterministic ending fn setDuration(elapsed: mut int32!0<seconds>) -> unit {
    elapsed = 3;
}
```

Provide one named `input` per parameter, one `expect` for the return value, and one named `after` per `mut` parameter, even when that example leaves the value unchanged. Each mutable input gets independent storage. Entries can appear in any order.

Values can use numbers, strings, booleans, `unit`, lists, named record fields, enum cases, `Ok`, and `Err`, with literal values throughout. Built-in errors use `Overflow()`, `Zero()`, `IndexError("message")`, or `ParseIntError("message")` inside examples. Host capabilities are opaque. Examples in mock files can also construct `HostError` values. Values are compared structurally, including error messages, with exact numeric equality for `f64`.

`check` and `run` validate examples. `test` runs examples in source order, reports every mismatch in a case, and exits nonzero when any case fails. It can run files without `main`.

## Language rules

- **Calls.** Names reserve parameter slots first. The checker matches unnamed arguments by type and reports ambiguity unless the possible bindings differ only by swaps within a `commutative` group. Argument expressions run in source order. Use `commutative a b` to name a group of same-type, read-only parameters. Bare `commutative` names both parameters of a two-parameter function. The checker verifies that every use of a group member passes all members together, exactly once, into a commutative parameter group.
- **Functions.** Define named functions at the top level with `fn`. Data and functions have separate roles. There are no classes, methods, closures, function values, or currying in this prototype.
- **Guarantees.** A callee must provide every guarantee its caller requires. `pure` implies `deterministic` for calls, allows local mutation, and prohibits `mut` parameters and host calls. Writing both `pure` and `deterministic` is valid. `ending` is independent of both and rejects recursive call cycles. A `for item in items` loop runs over the list present at entry and protects the source variable from reassignment or mutable passage, including in calls made by the loop.
- **Mutation.** A local variable or parameter requires `mut` exactly when its binding can be written through a reachable assignment or a chain of `mut` arguments leading to one. Assigning the same value counts as a write. A forwarding cycle needs a reachable assignment to justify `mut`. Two mutable arguments must refer to distinct bindings, including through forwarding calls. A `mut int32!0` parameter must preserve the nonzero invariant. Passing a narrowed variable to `mut int32` discards its proof.
- **Ownership.** An `own` parameter moves its argument at the call site. A moved binding can be read again after reassignment on every continuing path. Ordinary parameters borrow exclusive values for the duration of a call. An `own console` parameter in `main` can transfer the capability, while `shared console` can be copied and passed through arguments, fields, and payloads typed `shared console`. The same rules apply to the other host capabilities.
- **Reachability.** The checker rejects statements after a `return`, an unconditional `loop`, or a branch statement whose reachable paths all terminate or loop. It rejects statements in an `if` branch ruled out by literal `true` or `false`. It assumes calls can return and a `for` loop can run zero or more times.
- **Numbers.** Use `int32`, `uint32` (also spelled `Uint32`), or `f64`, optionally with units such as `int32<meter>`. Finite decimal literals such as `-0.5` and `1e-6` have type `f64` and take their units from context. Integer addition, subtraction, and multiplication return a `result` with `Overflow` on out-of-range arithmetic. Addition and subtraction require matching units. Use `a + b` or `add(a, b)` for addition, `subtract(left: a, right: b)` for subtraction, and `a * b` for multiplication. Multiplication derives the product units and treats bare integer literals as scalars. `min(a, b)` and `max(a, b)` return an integer with the operands' matching units. For integers, `+`, `*`, `==`, `!=`, `min`, and `max` are commutative. Division uses an integer divisor with the `!0` refinement, produces a floating-point quotient, and requires an explicit result type such as `distance / elapsed as f64<meter/seconds>`. `if elapsed != 0` narrows the variable in that branch. `nonzero(elapsed)` returns the same proof as a `result`. `uint32` ranges from 0 to 4,294,967,295 and supports `uint32!0`. Unsigned subtraction below zero returns `Err(Overflow)`. Use `toUint32(value)` to convert from `int32`, preserving units. It returns `Err(Overflow)` for negative inputs.
- **Data and errors.** Use `bool`, `string`, `unit`, homogeneous `list<T>`, and `result<T, E>`. Records have named fields and enums have named cases with an optional typed payload. A `match` requires an arm for each result or enum case. Error values can flow through function calls and returns. The language omits null and exceptions.
- **Iteration and collections.** Use `loop { ... }` for repeated work that exits via `return`. The checker forbids this construct in an `ending` function. Lists are immutable. `prepend(item, items)` and `dropLast(items)` return new lists. `dropLast([])` returns an empty list. `at(items, index)` uses zero-based indexing and returns `result<T, IndexError>`.
- **Text and host errors.** Use `concat(left: ..., right: ...)` to join strings, `splitLines` to split on LF or CRLF, and `formatInt32` and `parseInt32` to convert between strings and integers. Parsing returns a `result<int32, ParseIntError>`. `hostErrorKind` distinguishes `EndOfInput`, `FileNotFound`, and `Other`, and `describeHostError` formats the diagnostic.

See the [glossary](GLOSSARY.md) and [decision records](docs/adr/) for the terms and design choices from the interview.
