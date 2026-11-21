# Programming language prototype

This throwaway checker and interpreter explores whether unordered calls, explicit host access, and checked function guarantees work together in a small language. It uses Python 3.10 or later and has no third-party dependencies.

## Run it

From the repository root, run the numeric example and check an intentionally invalid program.

```sh
python3 -B -m prototype run examples/numeric.proto
python3 -B -m prototype check examples/rejected/divisor.proto
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
| `readKeysFor(console, timeout: int32<ms>)` | `result<list<string>, HostError>` |
| `readFileUTF8(filesystem, path)` | `result<string, HostError>` |
| `writeFileUTF8(filesystem, path: path, text: text)` | `result<unit, HostError>` |
| `random(source: random, bound0: int32, bound1: int32)` | `result<int32, HostError>` |

The host functions run immediately when called. `write` flushes text as supplied, and `print` also appends a newline. `input` reads one line and returns `Err(EndOfInput)` at end of input. `enableKeyInput` switches a supported Unix terminal to character input with echo disabled and returns `false` when that mode is unavailable. Once enabled, `readKeysFor` collects the decoded input characters in order for the given duration. A zero timeout polls immediately. The runner restores the terminal when the program exits. Host functions carry no function guarantees.

`random(source, bound0, bound1)` draws a uniform integer from the inclusive range. `pseudoRandom(state: seed, bound0, bound1)` is a pure, deterministic, terminating built-in for repeatable sequences. It returns a `RandomDraw` record with `value` and the next `state`, both `int32`. Pass the returned state into the next call. Each call takes one generator step, even when the bounds are equal. Both functions accept the bounds in either order. `pseudoRandom` maps its 32-bit outputs as evenly as possible across the range and is intended for simulations and games.

## Play Snake

```sh
python3 -B -m prototype run examples/snake.proto --console --filesystem --random
```

The board is 48 columns wide and 32 rows high. On a supported Unix terminal, Snake advances automatically about every 150 ms and redraws in place. Press `w`, `a`, `s`, or `d` to steer and `q` to quit. With redirected streams or an unsupported terminal, use those commands followed by Enter. The head is `@`, the body is `o`, and food is `*`. Each game draws one seed from the host and uses it to place every apple on a free cell, including the first.

The game saves completed rounds in `snake-scores.txt` in the current working directory and reads the best score on startup. The score file assumes one running game writes it at a time. A malformed or unreadable history disables saving for that run.

Run the game and language checks with `python3 -B -m unittest discover -s tests`.

## Language rules

- **Calls.** Names reserve parameter slots first. The checker matches unnamed arguments by type and reports ambiguity unless the possible bindings differ only by swaps within a `commutative` group. Argument expressions run in source order. Use `commutative a b` to name a group of same-type, read-only parameters. Bare `commutative` names both parameters of a two-parameter function. The checker verifies that every use of a group member passes all members together, exactly once, into a commutative parameter group.
- **Functions.** Define named functions at the top level with `fn`. Data and functions have separate roles. There are no classes, methods, closures, function values, or currying in this prototype.
- **Guarantees.** A caller that declares `pure`, `deterministic`, or `terminates` can only call functions that declare the same guarantee. `pure` allows local mutation and prohibits `mut` parameters and host calls. `terminates` rejects recursive call cycles. A `for item in items` loop runs over the list present at entry and protects the source variable from reassignment or mutable passage, including through possible aliases.
- **Mutation.** A caller declares a writable variable with `let mut` and passes it as `mut variable` to a `mut` parameter. Matching mutable aliases share storage. A `mut int32!0` parameter must preserve the nonzero invariant. Passing a narrowed variable to `mut int32` discards its proof.
- **Numbers.** Use `int32`, `int32!0`, or `f64`, optionally with units such as `int32<meter>`. Integer addition, subtraction, and multiplication return a `result` with `Overflow` on out-of-range arithmetic. Addition and subtraction require matching units. Use `a + b` or `add(a, b)` for addition, `subtract(left: a, right: b)` for subtraction, and `a * b` for multiplication. Multiplication derives the product units and treats bare integer literals as scalars. `min(a, b)` and `max(a, b)` return an integer with the operands' matching units. For integers, `+`, `*`, `==`, `!=`, `min`, and `max` are commutative. Division uses an `int32!0` divisor, produces a floating-point quotient, and requires an explicit result type such as `distance / elapsed as f64<meter/seconds>`. `if elapsed != 0` narrows the variable in that branch. `nonzero(elapsed)` returns the same proof as a `result`.
- **Data and errors.** Use `bool`, `string`, `unit`, homogeneous `list<T>`, and `result<T, E>`. Records have named fields and enums have named cases with an optional typed payload. A `match` requires an arm for each result or enum case. Error values can flow through function calls and returns. The language omits null and exceptions.
- **Iteration and collections.** Use `loop { ... }` for repeated work that exits via `return`. The checker forbids this construct in a `terminates` function. Lists are immutable. `prepend(item, items)` and `dropLast(items)` return new lists. `dropLast([])` returns an empty list. `at(items, index)` uses zero-based indexing and returns `result<T, IndexError>`.
- **Text and host errors.** Use `concat(left: ..., right: ...)` to join strings, `splitLines` to split on LF or CRLF, and `formatInt32` and `parseInt32` to convert between strings and integers. Parsing returns a `result<int32, ParseIntError>`. `hostErrorKind` distinguishes `EndOfInput`, `FileNotFound`, and `Other`, and `describeHostError` formats the diagnostic.

See the [glossary](GLOSSARY.md) and [decision records](docs/adr/) for the terms and design choices from the interview.
