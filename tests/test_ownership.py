"""Ownership checks through the source-language entry point."""

import unittest

from prototype.checker import Checker
from prototype.runtime import Interpreter
from prototype.syntax import LanguageError, Parser


def compile_program(source):
    return Checker(Parser(source).program()).program()


class Ownership(unittest.TestCase):
    def test_move_reinitialization_on_continuing_paths(self):
        source = """
            fn take(value: own int32) -> unit {}
            fn choose(flag: bool) -> int32 {
                let mut value = 1;
                take(value);
                if flag { value = 2; } else { ELSE }
                return value;
            }
        """
        program = compile_program(source.replace("ELSE", "return 3;"))
        self.assertEqual(Interpreter(program).call("choose", (True,)), 2)
        self.assertEqual(Interpreter(program).call("choose", (False,)), 3)
        with self.assertRaisesRegex(LanguageError, "value.*was moved"):
            compile_program(source.replace("ELSE", ""))

    def test_exclusive_values_can_be_borrowed_and_transferred(self):
        compile_program("""
            record Box { output: console }
            fn use(box: Box) -> unit { print(box.output, "hi"); }
            fn take(box: own Box) -> console { return box.output; }
            fn main(output: own console) -> unit {
                let box = Box(output: output);
                use(box);
                let capability = take(box);
                print(capability, "done");
            }
        """)

    def test_shared_capability_survives_compound_round_trip(self):
        compile_program("""
            enum Box { Some(shared console) }
            fn pack(output: shared console) -> Box { return Box::Some(output); }
            fn main(output: shared console) -> unit {
                let box = pack(output);
                match box { Some(copy) { print(copy, "copy"); } }
                match box { Some(copy) { print(output, "original"); } }
            }
        """)

    def test_exclusive_borrows_cannot_escape(self):
        for source in (
            "fn leak(output: console) -> console { return output; }",
            "fn leak(output: console) -> unit { let leak = [output]; }",
            """enum Box { Some(console) }
               fn leak(box: Box) -> console { match box { Some(output) { return output; } } }""",
            """record Box { output: console }
               fn leak(box: Box) -> console { return box.output; }""",
            """fn take(n: own int32) -> unit {}
               fn update(n: mut int32) -> unit { take(n); n = 1; }""",
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(LanguageError, "cannot move borrowed value"):
                    compile_program(source)

    def test_aliases_and_shared_moves_are_rejected(self):
        cases = (
            ("""fn store(a: mut int32, b: mut int32) -> unit { a = 1; b = 2; }
                fn forward(n: mut int32) -> unit { store(a: mut n, b: mut n); }""", "may alias"),
            ("""fn take(c: own console) -> unit {}
                fn main(c: shared console) -> unit { take(c); }""", "cannot consume a shared value"),
            ("""record Box { output: console }
                fn main(output: own console) -> unit {
                    let box = Box(output: output);
                    let moved = box.output;
                    print(box.output, "duplicate");
                }""", "box.*was moved"),
        )
        for source, diagnostic in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(LanguageError, diagnostic):
                    compile_program(source)
