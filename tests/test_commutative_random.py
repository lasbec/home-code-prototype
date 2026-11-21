"""Check commutative proofs and their use by the native random functions."""

from io import StringIO
import unittest
from unittest.mock import patch

from prototype.checker import Checker
from prototype.runtime import Console, Failure, Interpreter, RandomSource, Result
from prototype.syntax import LanguageError, Parser


def compile_program(source):
    return Interpreter(Checker(Parser(source).program()).program())


class CommutativeGroups(unittest.TestCase):
    def test_nested_calls_and_effects_preserve_source_order(self):
        interpreter = compile_program("""
            commutative a b
            fn lower(a: int32, b: int32, terminal: console) -> int32 {
                let number = min(a, b);
                print(terminal, formatInt32(number));
                return number;
            }

            fn log(terminal: console, label: string, value: int32) -> int32 {
                print(terminal, label);
                return value;
            }

            fn main(terminal: console) -> int32 {
                return lower(terminal, b: log(terminal, "first", 2), a: log(terminal, "second", 9));
            }
        """)
        terminal = Console(StringIO(), StringIO())
        self.assertEqual(interpreter.call("main", (terminal,)), 2)
        self.assertEqual(terminal.stdout.getvalue(), "first\nsecond\n2\n")

    def test_complete_group_may_have_more_than_two_parameters(self):
        interpreter = compile_program("""
            commutative x y z fn ignore(x: int32, y: int32, z: int32) -> unit {}
            commutative a b c fn forward(a: int32, b: int32, c: int32) -> unit {
                ignore(c, a, b);
            }
            fn main() -> unit { return forward(1, 2, 3); }
        """)
        self.assertIsNone(interpreter.call("main", ()))

    def test_incomplete_and_order_sensitive_uses_fail_even_with_named_arguments(self):
        prefix = """
            commutative x y fn take(x: int32, y: int32, extra: int32) -> int32 { return min(x, y); }
        """
        for expression in ("a", "max(a, a)", "max(a, 0) + b", "take(x: a, y: 0, extra: b)"):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(LanguageError, "commutative parameters.*passed together"):
                    compile_program(prefix + f"commutative fn bad(a: int32, b: int32) -> int32 {{ {expression}; return 0; }}")

        with self.assertRaisesRegex(LanguageError, "commutative parameters.*passed together"):
            compile_program("""
                commutative fn bad(a: list<int32>, b: list<int32>) -> unit { for item in a {} }
            """)

    def test_ambiguity_outside_group_requires_names(self):
        with self.assertRaisesRegex(LanguageError, "ambiguous call to 'pseudoRandom'.*state"):
            compile_program("fn main() -> RandomDraw { return pseudoRandom(1, 2, 3); }")

    def test_signature_requires_distinct_read_only_parameters_of_one_type(self):
        for modifier, parameters in (
            ("commutative a a", "a: int32, b: int32"),
            ("commutative a missing", "a: int32, b: int32"),
            ("commutative a b", "a: int32, b: mut int32"),
            ("commutative a b", "a: int32, b: int32<meter>"),
            ("commutative", "a: int32, b: int32, c: int32"),
        ):
            with self.subTest(modifier=modifier, parameters=parameters):
                with self.assertRaisesRegex(LanguageError, "commutative"):
                    compile_program(f"{modifier} fn invalid({parameters}) -> unit {{}}")

    def test_integer_operators_prove_symmetry_and_preserve_units(self):
        interpreter = compile_program("""
            commutative fn same(a: int32, b: int32) -> bool { return a == b; }
            commutative fn product(a: int32<meter>, b: int32<meter>) -> result<int32<meter*meter>, Overflow> {
                return min(a, b) * max(a, b);
            }
            fn cancel(distance: int32<meter>, inverse: int32<1/meter>) -> result<int32, Overflow> {
                return distance * inverse;
            }
            commutative fn multiply(a: int32, b: int32) -> result<int32, Overflow> { return a * b; }
            fn main() -> result<int32<meter*meter>, Overflow> { return product(2, 3); }
        """)
        self.assertTrue(interpreter.call("same", (5, 5)))
        self.assertEqual(interpreter.call("main", ()), Result("Ok", 6))
        self.assertEqual(interpreter.call("cancel", (2, 3)), Result("Ok", 6))
        self.assertEqual(interpreter.call("multiply", (-(2**31), -1)), Result("Err", Failure("Overflow")))


class RandomFunctions(unittest.TestCase):
    def test_pseudo_random_replays_and_steps_for_every_valid_range(self):
        interpreter = compile_program("""
            pure deterministic terminates
            commutative a b fn draw(a: int32, b: int32, seed: int32) -> RandomDraw {
                return pseudoRandom(state: seed, a, b);
            }
            fn main() -> RandomDraw { return pseudoRandom(123, bound0: 5, bound1: 5); }
        """)
        seed = 123
        for _ in range(8):
            draw = interpreter.call("draw", (7, -2, seed))
            self.assertEqual(draw, interpreter.call("draw", (-2, 7, seed)))
            value, state = (field for _, field in draw.fields)
            self.assertIn(value, range(-2, 8))
            self.assertNotEqual(seed, state)
            full = dict(interpreter.call("draw", (-(2**31), 2**31 - 1, seed)).fields)
            self.assertEqual(full["state"], state)
            self.assertIn(full["value"], range(-(2**31), 2**31))
            seed = state
        self.assertEqual(dict(interpreter.call("main", ()).fields),
                         {"value": 5, "state": dict(interpreter.call("draw", (7, -2, 123)).fields)["state"]})

    def test_host_random_permits_commutative_wrappers_and_both_endpoints(self):
        interpreter = compile_program("""
            commutative a b fn draw(source: random, a: int32, b: int32) -> result<int32, HostError> {
                return random(source, b, a);
            }
            fn main(source: random) -> result<int32, HostError> {
                return draw(2147483647, source, -2147483648);
            }
        """)
        with patch("prototype.runtime.secrets.randbelow", side_effect=(0, 2**32 - 1)) as draw:
            self.assertEqual(interpreter.call("main", (RandomSource(),)), Result("Ok", -(2**31)))
            self.assertEqual(interpreter.call("main", (RandomSource(),)), Result("Ok", 2**31 - 1))
        self.assertEqual([call.args for call in draw.call_args_list], [(2**32,), (2**32,)])
