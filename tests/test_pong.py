"""Exercise Pong over loopback and the HTTP mock contracts."""

from contextlib import contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
from select import select
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
from time import monotonic, sleep
import unittest

try:
    import pty
    import termios
except ImportError:
    pty = None


ROOT = Path(__file__).resolve().parents[1]
PONG = ROOT / "examples" / "pong"
RUN = [sys.executable, "-B", "-m", "prototype", "run"]


class Pong(unittest.TestCase):
    @contextmanager
    def server(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = [*RUN, str(PONG / "server.proto"), "--http-server", "--http-bind",
                   f"127.0.0.1:{port}", "--console", "--threads", "--channels"]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT) as server:
            try:
                ready, _, _ = select([server.stdout], [], [], 5)
                self.assertTrue(ready, "Pong server did not announce readiness")
                self.assertIn(b"Pong server ready", server.stdout.readline())
                yield port
            finally:
                server.terminate()
                _, stderr = server.communicate(timeout=5)
                self.assertFalse(stderr, stderr.decode())

    def post(self, port, path, player, step=0):
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("POST", path, body=json.dumps({"id": player, "step": step}),
                               headers={"Content-Type": "application/json"})
            with connection.getresponse() as response:
                return response.status, json.load(response)
        finally:
            connection.close()

    def test_match_moves_scores_expires_and_accepts_new_players(self):
        with self.server() as port:
            self.assertEqual(self.post(port, "/join", "100")[1]["phase"], "waiting")
            self.assertEqual(self.post(port, "/join", "100")[1]["seat"], 1)
            self.assertEqual(self.post(port, "/join", "200")[1]["seat"], 2)
            self.assertEqual(self.post(port, "/join", "300")[0], 409)
            left = self.post(port, "/input", "100", -1)[1]
            right = self.post(port, "/input", "200", 1)[1]
            self.assertEqual((right["left"], right["right"]), (8, 10))
            self.assertEqual((left["x"], left["y"]), (right["x"], right["y"]))
            self.assertEqual(right["phase"], "playing")

            deadline = monotonic() + 8
            while right["leftScore"] + right["rightScore"] == 0:
                self.assertLess(monotonic(), deadline, "the ball never scored")
                self.post(port, "/input", "100")
                right = self.post(port, "/input", "200", 1)[1]
                sleep(0.08)

            deadline = monotonic() + 7
            while right["phase"] == "playing":
                self.assertLess(monotonic(), deadline, "the absent player did not time out")
                right = self.post(port, "/input", "200")[1]
                sleep(0.08)
            self.assertEqual(right["phase"], "aborted")
            self.assertEqual(self.post(port, "/join", "300")[1]["phase"], "waiting")
            self.assertEqual(self.post(port, "/join", "400")[1]["phase"], "playing")
            self.assertEqual(self.post(port, "/input", "200")[1]["phase"], "aborted")

    def test_bot_rejoins_after_the_match_ends(self):
        with self.server() as port:
            self.assertEqual(self.post(port, "/join", "100")[1]["seat"], 1)
            command = [*RUN, str(PONG / "bot.proto"), "--http-client", "--http-origin",
                       f"http://127.0.0.1:{port}", "--console", "--threads", "--random"]
            with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as bot:
                try:
                    deadline = monotonic() + 4
                    while True:
                        _, frame = self.post(port, "/input", "100")
                        if frame["phase"] == "playing" and frame["right"] != 9:
                            break
                        self.assertLess(monotonic(), deadline, "the bot did not move its paddle")
                        sleep(0.06)
                    self.assertLess(frame["right"], 9, "the bot should anticipate the bounce")
                    self.assertLess(frame["x"], 70, "the bot should move well before the ball arrives")
                    output = self.read_until(bot.stdout.fileno(), "Waiting for an opponent as Cyan", timeout=7)
                    self.assertIn("Playing as Magenta", output)
                    self.assertIn("Match abandoned", output)
                    next_match = self.post(port, "/join", "200")[1]
                    self.assertEqual((next_match["seat"], next_match["phase"]), (2, "playing"))
                    self.assertEqual((next_match["leftScore"], next_match["rightScore"]), (0, 0))
                finally:
                    if bot.poll() is None:
                        bot.kill()
                        bot.communicate()

    @contextmanager
    def terminal(self, port):
        master, slave = pty.openpty()
        command = [*RUN, str(PONG / "client.proto"), "--http-client", "--http-origin",
                   f"http://127.0.0.1:{port}", "--console", "--threads", "--channels", "--random"]
        env = dict(os.environ, TERM="xterm-256color", PYTHONIOENCODING="utf-8")
        try:
            original = termios.tcgetattr(slave)
            with subprocess.Popen(command, cwd=ROOT, env=env, stdin=slave, stdout=slave,
                                  stderr=subprocess.PIPE) as client:
                try:
                    yield master, client
                    self.assertEqual(client.wait(timeout=3), 0, client.stderr.read().decode())
                    self.assertEqual(termios.tcgetattr(slave), original)
                finally:
                    if client.poll() is None:
                        client.kill()
        finally:
            os.close(master)
            os.close(slave)

    def read_until(self, master, text, *, timeout=5):
        marker = text.encode("utf-8")
        output = bytearray()
        deadline = monotonic() + timeout
        while marker not in output:
            readable, _, _ = select([master], [], [], max(0, deadline - monotonic()))
            self.assertTrue(readable, f"terminal omitted {text!r}: {output[-500:]!r}")
            chunk = os.read(master, 8192)
            self.assertTrue(chunk, f"output ended before {text!r}: {output[-500:]!r}")
            output.extend(chunk)
        return output.decode("utf-8", errors="replace")

    def quit_terminal(self, master, client):
        os.write(master, b"q")
        deadline = monotonic() + 3
        while client.poll() is None:
            self.assertLess(monotonic(), deadline, "terminal client did not quit")
            if select([master], [], [], 0.05)[0]:
                os.read(master, 8192)

    @unittest.skipIf(pty is None, "requires Unix pseudo-terminals")
    def test_terminal_clients_draw_the_court_and_replay(self):
        with self.server() as port, self.terminal(port) as (left, first):
            waiting = self.read_until(left, "Waiting for player 2").split("Waiting for player 2", 1)[0]
            plain = re.sub(r"\x1b\[[?0-9;]*[A-Za-z]", "", waiting)
            rows = [line for line in plain.splitlines() if line.startswith(("┌", "│", "└"))]
            self.assertEqual(len(rows), 22)
            self.assertTrue(all(len(row) == 98 for row in rows))
            self.assertEqual(plain.count("██"), 10)
            self.assertIn("●", plain)
            self.assertIn("\x1b[1;96m██", waiting)
            self.assertIn("\x1b[1;95m██", waiting)
            with self.terminal(port) as (right, second):
                self.read_until(right, "Get ready")
                self.read_until(left, "Get ready")
                os.write(left, b"w")
                os.write(right, b"s")
                self.read_until(left, "First to five")
                self.quit_terminal(right, second)
            results = self.read_until(left, "Match abandoned", timeout=7)
            self.assertIn("Play again", results)
            os.write(left, b"r")
            self.read_until(left, "Waiting for player 2")
            replay = self.post(port, "/join", "200")[1]
            self.assertEqual((replay["seat"], replay["phase"]), (2, "playing"))
            self.assertEqual((replay["leftScore"], replay["rightScore"]), (0, 0))
            self.read_until(left, "Get ready")
            self.quit_terminal(left, first)

    def mock_run(self, name, app, provider):
        flag = name.replace("_", "-")
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "app.proto"
            mock = directory / "mock.proto"
            state = directory / "out.literal"
            source.write_text(dedent(app).lstrip(), encoding="utf-8")
            mock.write_text(dedent(provider).lstrip(), encoding="utf-8")
            result = subprocess.run([*RUN, str(source), f"--{flag}={mock}", f"--{flag}-state-out={state}"],
                                    cwd=ROOT, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout, state.read_text(encoding="utf-8")

    def test_client_mock_can_check_the_request_and_return_a_response(self):
        answer, state = self.mock_run("http_client", """
            fn main(client: http_client) -> result<HttpResponse, HostError> {
                let request = HttpRequest(method: "POST", path: "/join", headers: [], body: "hello");
                return requestHttp(client, request, timeout: 100);
            }
        """, """
            mock http_client
            pure ending fn init() -> string { return ""; }
            export fn requestHttp(state: mut string, request: HttpRequest, timeout: uint32<ms>)
                -> result<HttpResponse, HostError> {
                state = request.path;
                return Ok(HttpResponse(status: 201, headers: [], body: request.body));
            }
        """)
        self.assertIn('status: 201', answer)
        self.assertIn('body: hello', answer)
        self.assertEqual(state, '"/join"\n')

    def test_server_mock_can_feed_a_request_and_capture_a_response(self):
        answer, state = self.mock_run("http_server", """
            fn main(server: http_server) -> result<unit, HostError> {
                match acceptHttp(server, timeout: 100) {
                    Ok(incoming) {
                        let response = HttpResponse(status: 200, headers: [], body: incoming.request.body);
                        return respondHttp(server, incoming, response, timeout: 100);
                    }
                    Err(error) { return Err(error); }
                }
            }
        """, """
            mock http_server
            pure ending fn init() -> string { return "ping"; }
            export fn acceptHttp(state: mut string, timeout: uint32<ms>) -> result<HttpIncoming, HostError> {
                let request = HttpRequest(method: "POST", path: "/", headers: [], body: state);
                return Ok(HttpIncoming(id: 1, request: request));
            }
            export fn respondHttp(state: mut string, incoming: own HttpIncoming, response: HttpResponse,
                                  timeout: uint32<ms>) -> result<unit, HostError> {
                state = response.body;
                return Ok(unit);
            }
        """)
        self.assertEqual(answer.strip(), 'Ok(unit)')
        self.assertEqual(state, '"ping"\n')
