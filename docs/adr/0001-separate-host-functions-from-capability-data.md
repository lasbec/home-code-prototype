# Separate host functions from capability data

The host supplies opaque capability values and named functions separately. A helper that prints must receive the console value explicitly and pass it to the host's `print` function. Keeping operations separate from data preserves the language's function-only model and makes access to external resources visible at the call site.

The prototype provides built-in console and filesystem environments. The console provides `print` and `input`. The filesystem provides `readFileUTF8` and `writeFileUTF8`. Each operation has a fixed signature.

These operations use the active console and filesystem during execution. The live operations declare none of `pure`, `deterministic`, or `ending`, since they perform external effects, depend on host state, and can block.
