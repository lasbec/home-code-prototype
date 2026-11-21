"""Exercise the live console host and Snake through a Unix pseudo-terminal."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
from select import select
import signal
import subprocess
import sys
import tempfile
from time import monotonic
import unittest

try:
    import pty
    import termios
except ImportError:
    pty = None


ROOT = Path(__file__).resolve().parents[1]
SNAKE = ROOT / "examples" / "snake.proto"


@unittest.skipIf(pty is None, "requires a Unix pseudo-terminal")
class TerminalSnake(unittest.TestCase):
    @contextmanager
    def run_terminal(self, source, directory):
        master, slave = pty.openpty()
        try:
            original = termios.tcgetattr(slave)
            command = [sys.executable, "-B", "-m", "prototype", "run", str(source),
                       "--console", "--filesystem", "--random"]
            environment = dict(os.environ, TERM="xterm", PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
            with subprocess.Popen(command, stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                                  cwd=directory, env=environment) as process:
                try:
                    yield process, master, slave, original
                finally:
                    if process.poll() is None:
                        process.kill()
        finally:
            os.close(master)
            os.close(slave)

    def read_until(self, master, marker, count=1):
        output = bytearray()
        deadline = monotonic() + 5
        while output.count(marker) < count:
            ready, _, _ = select([master], [], [], max(0, deadline - monotonic()))
            if not ready:
                self.fail(f"expected {marker!r} in terminal output: {output!r}")
            output.extend(os.read(master, 4096))
        return bytes(output)

    def test_idle_ticks_redraw_and_save_a_completed_round(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.run_terminal(SNAKE, directory) as (process, master, slave, original):
                output = self.read_until(master, b"\x1b[J", count=2)
                os.write(master, b"w")
                output += self.read_until(master, b"Score saved.")
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(process.stderr.read(), b"")
                self.assertEqual(termios.tcgetattr(slave), original)

            self.assertEqual(output.count(b"\x1b[2J"), 1)
            self.assertGreaterEqual(output.count(b"\x1b[H"), 2)
            self.assertIn(b"Score: 0\r\n\x1b[J", output)
            first, second, *_ = output.split(b"\x1b[H")[1:]
            first_head_row = next(line for line in first.splitlines() if b"@" in line)
            second_head_row = next(line for line in second.splitlines() if b"@" in line)
            self.assertEqual(first_head_row.index(b"@"), 3)
            self.assertEqual(second_head_row.index(b"@"), 4)
            score = re.search(rb"You hit a wall\. Final score: (\d+)\r\n", output)
            self.assertIsNotNone(score)
            self.assertEqual(Path(directory, "snake-scores.txt").read_bytes(), score[1] + b"\n")

    def test_quit_in_a_key_batch_restores_terminal_and_leaves_no_score(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.run_terminal(SNAKE, directory) as (process, master, slave, original):
                self.read_until(master, b"Score: 0")
                mode = termios.tcgetattr(slave)[3]
                self.assertFalse(mode & (termios.ECHO | termios.ICANON))
                os.write(master, b"wqsd")
                output = self.read_until(master, b"Round abandoned.")
                self.assertNotIn(b"wqsd", output)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(termios.tcgetattr(slave), original)
            self.assertFalse(Path(directory, "snake-scores.txt").exists())

    def test_interrupt_and_termination_restore_terminal(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum), tempfile.TemporaryDirectory() as directory:
                with self.run_terminal(SNAKE, directory) as (process, master, slave, original):
                    self.read_until(master, b"Score: 0")
                    process.send_signal(signum)
                    self.assertEqual(process.wait(timeout=5), 128 + signum)
                    self.assertEqual(termios.tcgetattr(slave), original)

    def test_timed_read_returns_ordered_characters_and_write_adds_no_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "key-window.proto")
            source.write_text("""
                fn main(terminal: console) -> result<list<string>, HostError> {
                    match enableKeyInput(terminal) {
                        Ok(enabled) {
                            match write(terminal, "Ready") {
                                Ok(written) { return readKeysFor(terminal, 800 as int32<ms>); }
                                Err(error) { return Err(error); }
                            }
                        }
                        Err(error) { return Err(error); }
                    }
                }
            """, encoding="utf-8")
            with self.run_terminal(source, directory) as (process, master, slave, original):
                output = self.read_until(master, b"Ready")
                os.write(master, "wés".encode("utf-8"))
                output += self.read_until(master, "Ok([w, é, s])".encode("utf-8"))
                self.assertIn("ReadyOk([w, é, s])".encode("utf-8"), output)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(termios.tcgetattr(slave), original)
