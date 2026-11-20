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

`--console` passes standard input and output to `main`. `--filesystem` passes a capability with the filesystem access of the Python process. Relative paths resolve from its working directory. A helper must receive a capability explicitly before it can use it.

| Host function | Result |
| --- | --- |
| `print(console, text)` | `result<unit, HostError>` |
| `input(console)` | `result<string, HostError>` |
| `readFileUTF8(filesystem, path)` | `result<string, HostError>` |
| `writeFileUTF8(filesystem, path: path, text: text)` | `result<unit, HostError>` |

The host functions run immediately when called. `input` reads one line and returns `Err(EndOfInput)` at end of input. Host functions carry no function guarantees.

## Language rules

- **Calls.** Names reserve parameter slots first. The checker matches unnamed arguments by type and reports ambiguity unless `commutative` applies. Argument expressions run in source order. `commutative` is an author promise on `pure deterministic` functions with two equally typed read-only parameters. The checker validates the signature and uses declaration order to resolve ties.
- **Functions.** Define named functions at the top level with `fn`. Data and functions have separate roles. There are no classes, methods, closures, function values, or currying in this prototype.
- **Guarantees.** A caller that declares `pure`, `deterministic`, or `terminates` can only call functions that declare the same guarantee. `pure` allows local mutation and prohibits `mut` parameters and host calls. `terminates` rejects recursive call cycles. A `for item in items` loop runs over the list present at entry and protects the source variable from reassignment or mutable passage, including through possible aliases.
- **Mutation.** A caller declares a writable variable with `let mut` and passes it as `mut variable` to a `mut` parameter. Matching mutable aliases share storage. A `mut int32!0` parameter must preserve the nonzero invariant. Passing a narrowed variable to `mut int32` discards its proof.
- **Numbers.** Use `int32`, `int32!0`, or `f64`, optionally with units such as `int32<meter>`. Equal-unit `int32` addition and subtraction return a `result` with `Overflow` on out-of-range arithmetic. Use `a + b` or `add(a, b)` for commutative addition, and `subtract(left: a, right: b)` for subtraction. Division uses an `int32!0` divisor, produces a floating-point quotient, and requires an explicit result type such as `distance / elapsed as f64<meter/seconds>`. `if elapsed != 0` narrows the variable in that branch. `nonzero(elapsed)` returns the same proof as a `result`.
- **Data and errors.** Use `bool`, `string`, `unit`, homogeneous `list<T>`, and `result<T, E>`. Lists are immutable values. `at(items, index)` uses zero-based indexing and returns `result<T, IndexError>`. A `match` on a `result` requires both `Ok(value)` and `Err(error)` arms. Error values can flow through function calls and returns. The language omits null and exceptions.

See the [glossary](GLOSSARY.md) and [decision records](docs/adr/) for the terms and design choices from the interview.
