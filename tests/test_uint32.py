"""Unsigned literals, arithmetic, and refinements."""

import unittest

from prototype.checker import Checker
from prototype.runtime import Interpreter, display
from prototype.syntax import LanguageError, Parser


def compile_program(source):
    return Checker(Parser(source).program()).program()


class Uint32(unittest.TestCase):
    def test_literal_range_and_spelling(self):
        for value in ("0", "4294967295"):
            program = compile_program(f"fn main() -> Uint32 {{ return {value}; }}")
            self.assertEqual(Interpreter(program).call("main", ()), int(value))
        for value in ("-1", "4294967296"):
            with self.assertRaisesRegex(LanguageError, "outside the uint32 range"):
                compile_program(f"fn main() -> uint32 {{ return {value}; }}")
        with self.assertRaisesRegex(LanguageError, "expected uint32!0"):
            compile_program("fn main() -> uint32!0 { return 0; }")

    def test_checked_arithmetic_and_conversion(self):
        program = compile_program("""
            fn addTime(n: uint32<ms>) -> result<uint32<ms>, Overflow> { return n + 1; }
            fn scale(n: uint32<ms>) -> result<uint32<ms>, Overflow> { return 2 * n; }
            fn underflow(n: uint32) -> result<uint32, Overflow> {
                return subtract(left: 0, right: n);
            }
            fn convert(n: int32<ms>) -> result<uint32<ms>, Overflow> { return toUint32(n); }
        """)
        interpreter = Interpreter(program)
        for name, argument, outcome in (
            ("addTime", 4294967295, "Err(Overflow)"),
            ("addTime", 9, "Ok(10)"),
            ("scale", 2147483648, "Err(Overflow)"),
            ("scale", 11, "Ok(22)"),
            ("underflow", 1, "Err(Overflow)"),
            ("convert", -1, "Err(Overflow)"),
            ("convert", 1, "Ok(1)"),
        ):
            with self.subTest(name=name, argument=argument):
                self.assertEqual(display(interpreter.call(name, (argument,))), outcome)

    def test_nonzero_proofs_for_variables_and_units(self):
        program = compile_program("""
            fn quotient(n: uint32<meters>, time: uint32<ms>) -> f64<meters/ms> {
                if time != 0 { return n / time as f64<meters/ms>; }
                return 0.0;
            }
            fn proof(n: uint32) -> result<uint32!0, Zero> { return nonzero(n); }
        """)
        interpreter = Interpreter(program)
        self.assertEqual(interpreter.call("quotient", (9, 2)), 4.5)
        self.assertEqual(display(interpreter.call("proof", (0,))), "Err(Zero)")
        with self.assertRaisesRegex(LanguageError, "proven nonzero divisor"):
            compile_program("fn divide(n: uint32) -> f64 { return n / n as f64; }")
        with self.assertRaisesRegex(LanguageError, "expected uint32<ms>"):
            compile_program("""
                fn addTime(n: uint32<ms>, m: uint32<s>) -> result<uint32<ms>, Overflow> {
                    return left(n, m);
                }
                fn left(n: uint32<ms>, m: uint32<s>) -> result<uint32<ms>, Overflow> {
                    return add(left: n, right: m);
                }
            """)
