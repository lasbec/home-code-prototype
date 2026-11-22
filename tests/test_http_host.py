"""Verify live HTTP limits, deadline handling, and ownership."""

from contextlib import ExitStack
import http.client
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from textwrap import dedent
from threading import Event, Thread
import unittest

from prototype.channels.buffer import Channels
from prototype.http_io import BODY_LIMIT, OPEN_LIMIT, HttpClient, HttpServer
from prototype.modules import ModuleLoader
from prototype.runtime import Interpreter
from prototype.threads.scheduler import Threads


class HttpHost(unittest.TestCase):
    def server(self):
        provider = HttpServer("127.0.0.1:0")
        self.addCleanup(provider.close)
        return provider, provider.server_address[1]

    def program(self, text):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "main.proto"
        source.write_text(dedent(text).lstrip(), encoding="utf-8")
        loaded = ModuleLoader().load(source)
        interpreter = Interpreter(loaded.functions)
        return lambda *args: interpreter.call((loaded.entry, "main"), args)

    def test_accept_timeout_does_not_block_other_green_threads(self):
        provider, _ = self.server()
        run = self.program("""
            fn wake(workers: shared threads, ready: Channel<bool>) -> unit {
                sleep(workers, duration: 5);
                try_send(ready, true);
            }

            fn main(server: http_server, workers: shared threads, queues: channels) -> bool {
                let ready: Channel<bool> = create_channel(queues, Capacity::Bounded(1));
                let child = thread wake(workers, ready);
                match acceptHttp(server, timeout: 60) {
                    Ok(incoming) { return false; }
                    Err(problem) {
                        match try_receive(ready) {
                            Ok(ran) { return hostErrorKind(problem) == HostErrorKind::Timeout; }
                            Err(empty) { return false; }
                        }
                    }
                }
            }
        """)
        self.assertTrue(run(provider, Threads(), Channels()))

    def test_thread_completion_closes_an_unanswered_request(self):
        provider, port = self.server()
        run = self.program("""
            fn main(server: http_server) -> bool {
                match acceptHttp(server, timeout: 1000) {
                    Ok(incoming) { return incoming.request.path == "/unanswered"; }
                    Err(problem) { return false; }
                }
            }
        """)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
            peer.sendall(b"GET /unanswered HTTP/1.0\r\n\r\n")
            self.assertTrue(run(provider))
            self.assertEqual(peer.recv(1), b"")

    def test_client_deadline_and_oversized_response(self):
        provider, port = self.server()
        client = HttpClient(f"http://127.0.0.1:{port}")
        self.addCleanup(client.close)
        run = self.program("""
            fn main(client: http_client) -> result<HttpResponse, HostError> {
                let request = HttpRequest(method: "GET", path: "/", headers: [], body: "");
                return requestHttp(client, request, timeout: 80);
            }
        """)
        outcome = run(client)
        self.assertEqual(outcome.tag, "Err")
        self.assertEqual(outcome.value.code, "Timeout")

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(2)
            oversized = HttpClient(f"http://127.0.0.1:{listener.getsockname()[1]}")
            self.addCleanup(oversized.close)
            done = Event()

            def send_headers():
                with listener.accept()[0] as peer:
                    peer.settimeout(2)
                    peer.recv(4096)
                    peer.sendall(f"HTTP/1.0 200 OK\r\nContent-Length: {BODY_LIMIT + 1}\r\n\r\n".encode())
                    done.wait(2)

            worker = Thread(target=send_headers, daemon=True)
            worker.start()
            try:
                outcome = run(oversized)
                self.assertEqual(outcome.tag, "Err")
                self.assertIn("64 KiB", outcome.value.message)
            finally:
                done.set()
                worker.join(timeout=3)

    def test_request_body_and_concurrent_connection_limits(self):
        _, port = self.server()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.putrequest("POST", "/")
            connection.putheader("Content-Length", str(BODY_LIMIT + 1))
            connection.endheaders()
            with connection.getresponse() as response:
                self.assertEqual(response.status, 413)
        finally:
            connection.close()

        with ExitStack() as stack:
            for _ in range(OPEN_LIMIT):
                peer = stack.enter_context(socket.create_connection(("127.0.0.1", port), timeout=2))
                peer.sendall(b"GET / HTTP/1.0\r\n")
            extra = stack.enter_context(socket.create_connection(("127.0.0.1", port), timeout=2))
            self.assertIn(b"503", extra.recv(1024))
