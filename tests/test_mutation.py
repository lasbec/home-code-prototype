"""Exercise mutation requirements and statement reachability."""

import re
import unittest

from prototype.checker import Checker
from prototype.runtime import Interpreter
from prototype.syntax import LanguageError, Parser


def compile_program(source):
    return Checker(Parser(source).program()).program()


class Mutation(unittest.TestCase):
    def test_rejects_missing_and_unjustified_mut(self):
        cases = (
            ("fn main() -> unit { let mut value = 1; value; }",
             "mut variable 'value' is never written"),
            ("fn main() -> unit { let value = 1; value = 2; }",
             "'value' needs a mut declaration"),
            ("fn update(value: int32) -> unit { value = 2; }",
             "'value' needs a mut declaration"),
            ("""fn update(value: mut int32) -> unit { value = 2; }
                fn main() -> unit { let value = 1; update(mut value); }""",
             "'value' needs a mut declaration"),
            ("""fn update(first: mut int32, second: mut int32) -> unit { first = first; }
                fn main() -> unit {
                    let mut value = 1;
                    update(first: mut value, second: mut value);
                }""",
             "mut parameter 'second' is never written"),
            ("""fn choose(flag: bool) -> unit {
                    if flag { let mut value = 0; value = 1; }
                    else { let mut value = 0; }
                }""",
             "mut variable 'value' is never written"),
        )
        for source, diagnostic in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(LanguageError, re.escape(diagnostic)):
                    compile_program(source)

    def test_forwarding_uses_the_resolved_parameter_slot(self):
        program = compile_program("""
            fn main() -> int32 {
                let mut value = 0;
                return forward(mut value);
            }
            fn forward(value: mut int32) -> int32 {
                return setValue(enabled: true, target: mut value);
            }
            fn setValue(target: mut int32, enabled: bool) -> int32 {
                if enabled { target = 7; }
                return target;
            }
        """)
        self.assertEqual(Interpreter(program).call("main", ()), 7)

    def test_recursive_forwarding_requires_a_write(self):
        source = """
            fn main() -> int32 {
                let mut value = 0;
                start(ready: false, number: mut value);
                return value;
            }
            fn start(number: mut int32, ready: bool) -> unit {
                if ready { WRITE return unit; }
                finish(mut number);
            }
            fn finish(number: mut int32) -> unit {
                start(number: mut number, ready: true);
            }
        """
        program = compile_program(source.replace("WRITE", "number = 3;"))
        self.assertEqual(Interpreter(program).call("main", ()), 3)
        with self.assertRaisesRegex(LanguageError, "mut variable 'value' is never written"):
            compile_program(source.replace("WRITE", ""))

    def test_rejects_the_first_unreachable_statement(self):
        bodies = (
            "return unit; dead();",
            "loop {} dead();",
            "if false { dead(); }",
            "if true {} else { dead(); }",
            "if true { return unit; } dead();",
            "if flag { return unit; } else { loop {} } dead();",
            "match choice { Yes { return unit; } No { loop {} } } dead();",
        )
        for body in bodies:
            with self.subTest(body=body):
                source = f"enum Choice {{ Yes, No }} fn check(flag: bool, choice: Choice) -> unit {{ {body} }}"
                with self.assertRaisesRegex(LanguageError, "unreachable statement") as error:
                    compile_program(source)
                self.assertEqual(error.exception.token.text, "dead")

    def test_reachable_paths_determine_fallthrough_and_mutation(self):
        program = compile_program("""
            fn main() -> int32 {
                let mut value = 4;
                let items: list<int32> = [];
                for item in items { value = item; return value; }
                if true { return value; }
            }
        """)
        self.assertEqual(Interpreter(program).call("main", ()), 4)
