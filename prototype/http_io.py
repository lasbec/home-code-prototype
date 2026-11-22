"""Buffered HTTP providers. Socket I/O runs outside the interpreter scheduler."""

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from select import select
import socket
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit

from .values import Failure, Record, Result

BODY_LIMIT = 64 * 1024
HEADER_LIMIT = 16 * 1024
OPEN_LIMIT = 8
SERVER_LIFETIME = 5
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
RESERVED_HEADERS = {"connection", "content-length", "host", "transfer-encoding", "upgrade",
                    "proxy-connection", "expect"}


def ok(value=None):
    return Result("Ok", value)


def error(message, kind="Other"):
    return Result("Err", Failure(kind, str(message)))


TIMEOUT = error("HTTP deadline expired", "Timeout")


def record(shape, **fields):
    return Record(shape, tuple(fields.items()))


def headers_record(fields):
    return tuple(record("HttpHeader", name=name.lower(), value=value) for name, value in fields)


def checked_headers(headers):
    size = 0
    result = []
    for part in headers:
        name, value = (value for _, value in part.fields)
        if not TOKEN.fullmatch(name) or name.lower() in RESERVED_HEADERS:
            raise ValueError(f"invalid or reserved HTTP header {name!r}")
        if any((ord(character) < 32 and character != "\t") or ord(character) == 127 for character in value):
            raise ValueError("invalid HTTP header value")
        size += len(name) + len(value.encode("latin-1")) + 4
        if size > HEADER_LIMIT:
            raise ValueError("HTTP headers exceed 16 KiB")
        result.append((name, value))
    return result


def checked_body(text):
    body = text.encode("utf-8")
    if len(body) > BODY_LIMIT:
        raise ValueError("HTTP body exceeds 64 KiB")
    return body


def check_target(path):
    if (not path.startswith("/") or path.startswith("//") or "#" in path or "\\" in path
            or any(not 33 <= ord(character) <= 126 for character in path)):
        raise ValueError("HTTP target must be an ASCII path beginning with a single slash")


def parse_origin(origin):
    address = urlsplit(origin)
    port = address.port
    if (address.scheme != "http" or not address.hostname or address.path not in {"", "/"}
            or address.query or address.fragment or address.username is not None
            or any(ord(character) <= 32 or character == "\\" for character in origin)
            or "?" in origin or "#" in origin or port == 0):
        raise ValueError("HTTP client requires a base address such as http://192.168.1.20:8080")
    return address.hostname, port or 80


def stop_socket(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class HttpClient:
    def __init__(self, origin):
        self.host, self.port = parse_origin(origin)
        self.slots = BoundedSemaphore(OPEN_LIMIT)
        self.lock = Lock()
        self.connections = {}
        self.closed = Event()

    def close(self):
        self.closed.set()
        with self.lock:
            for connected_socket in self.connections.values():
                stop_socket(connected_socket)

    def request(self, scheduler, request, timeout):
        method, path, headers, body = (value for _, value in request.fields)
        try:
            if not TOKEN.fullmatch(method) or method.upper() == "CONNECT":
                raise ValueError("invalid or unsupported HTTP method")
            check_target(path)
            headers = checked_headers(headers)
            body = checked_body(body)
        except ValueError as problem:
            return error(problem)
        if timeout == 0:
            return TIMEOUT
        if not self.slots.acquire(blocking=False):
            return error("too many outstanding HTTP client requests")

        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout / 1000)
        complete = Future()
        cancelled = Event()
        deadline = scheduler.clock() + timeout / 1000

        def perform():
            try:
                if cancelled.is_set() or self.closed.is_set():
                    return
                connection.connect()
                with self.lock:
                    if cancelled.is_set() or self.closed.is_set():
                        return
                    self.connections[connection] = connection.sock
                connection.putrequest(method, path, skip_accept_encoding=True)
                for name, value in headers:
                    connection.putheader(name, value)
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("Connection", "close")
                connection.endheaders(body)
                with connection.getresponse() as response:
                    fields = response.getheaders()
                    if sum(len(name) + len(value) + 4 for name, value in fields) > HEADER_LIMIT:
                        raise ValueError("HTTP headers exceed 16 KiB")
                    length = response.length
                    if length is not None and length > BODY_LIMIT:
                        raise ValueError("HTTP body exceeds 64 KiB")
                    payload = response.read(BODY_LIMIT + 1)
                    if len(payload) > BODY_LIMIT:
                        raise ValueError("HTTP body exceeds 64 KiB")
                    if length is not None and len(payload) != length:
                        raise ValueError("HTTP response ended before its declared body length")
                    complete.set_result(ok(record("HttpResponse", status=response.status,
                                                  headers=headers_record(fields),
                                                  body=payload.decode("utf-8"))))
            except TimeoutError:
                complete.set_result(TIMEOUT)
            except (OSError, ValueError, http.client.HTTPException) as problem:
                complete.set_result(error(problem))
            finally:
                connection.close()
                with self.lock:
                    self.connections.pop(connection, None)
                self.slots.release()

        def cancel():
            if not complete.done():
                cancelled.set()
                with self.lock:
                    connected_socket = self.connections.get(connection)
                    if connected_socket is not None:
                        stop_socket(connected_socket)

        Thread(target=perform, daemon=True).start()
        return (yield from scheduler.wait_external("HTTP request", lambda: completed(complete),
                                                   deadline, TIMEOUT, cancel))


def completed(future):
    return future.result() if future.done() else None


@dataclass(eq=False)
class Exchange:
    incoming: Record
    connection: socket.socket
    response: Future = field(default_factory=Future)
    sent: Future = field(default_factory=Future)
    closed: Event = field(default_factory=Event)

    def abort(self):
        self.closed.set()
        stop_socket(self.connection)


class HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, bind):
        host, separator, port = bind.rpartition(":")
        if not separator or not host or not port.isascii() or not port.isdecimal():
            raise ValueError("HTTP server requires a bind address such as 0.0.0.0:8080")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
            self.address_family = socket.AF_INET6
        if int(port) > 65535:
            raise ValueError("HTTP port must be between 0 and 65535")
        self.slots = BoundedSemaphore(OPEN_LIMIT)
        self.lock = Lock()
        self.connections = {}
        self.requests = deque()
        self.sequence = 0
        super().__init__((host, int(port)), Handler)
        self.worker = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.worker.start()

    def close(self):
        self.shutdown()
        with self.lock:
            for connection, (_, exchange) in self.connections.items():
                if exchange is not None:
                    exchange.closed.set()
                stop_socket(connection)
        self.server_close()
        self.worker.join()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.05)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        request.settimeout(SERVER_LIFETIME)
        with self.lock:
            self.connections[request] = (monotonic() + SERVER_LIFETIME, None)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.release(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release(request)

    def release(self, request):
        with self.lock:
            _, exchange = self.connections.pop(request)
            if exchange is not None:
                exchange.closed.set()
                if not exchange.sent.done():
                    exchange.sent.set_result(error("HTTP peer disconnected"))
                if exchange in self.requests:
                    self.requests.remove(exchange)
        self.slots.release()

    def service_actions(self):
        with self.lock:
            now = monotonic()
            for connection, (deadline, exchange) in self.connections.items():
                if deadline <= now:
                    if exchange is not None:
                        exchange.closed.set()
                    stop_socket(connection)

    def submit(self, connection, request):
        with self.lock:
            self.sequence = self.sequence % (2**32 - 1) + 1
            exchange = Exchange(record("HttpIncoming", id=self.sequence, request=request), connection)
            deadline, _ = self.connections[connection]
            self.connections[connection] = (deadline, exchange)
            self.requests.append(exchange)
        return exchange

    def accept(self, scheduler, timeout):
        task = scheduler.current
        for key, (exchange, _) in tuple(task.resources.items()):
            if exchange.closed.is_set():
                del task.resources[key]

        def take():
            with self.lock:
                while self.requests:
                    exchange = self.requests.popleft()
                    if not exchange.closed.is_set():
                        break
                else:
                    return None
            task.resources[(self, exchange.incoming.fields[0][1])] = (exchange, exchange.abort)
            return ok(exchange.incoming)

        deadline = scheduler.clock() + timeout / 1000
        return (yield from scheduler.wait_external("HTTP accept", take, deadline, TIMEOUT))

    def respond(self, scheduler, incoming, response, timeout):
        key = (self, incoming.fields[0][1])
        resources = scheduler.current.resources
        resource = resources.get(key)
        if resource is None or resource[0].incoming is not incoming:
            return error("HTTP request is not open in the current thread")
        exchange, _ = resources.pop(key)
        if exchange.closed.is_set():
            return error("HTTP peer disconnected")
        try:
            status, headers, body = (value for _, value in response.fields)
            headers = checked_headers(headers)
            body = checked_body(body)
            if not 200 <= status <= 599:
                raise ValueError("HTTP final status must be between 200 and 599")
            if status in {204, 205, 304} and body:
                raise ValueError("HTTP status does not allow a body")
        except ValueError as problem:
            exchange.abort()
            return error(problem)
        if timeout == 0:
            exchange.abort()
            return TIMEOUT
        exchange.response.set_result((status, headers, body))

        def cancel():
            if not exchange.sent.done():
                exchange.abort()

        return (yield from scheduler.wait_external("HTTP response", lambda: completed(exchange.sent),
                                                   scheduler.clock() + timeout / 1000, TIMEOUT, cancel))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass

    def log_message(self, *_):
        pass

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.process
        raise AttributeError(name)

    def process(self):
        self.close_connection = True
        try:
            check_target(self.path)
            if self.command.upper() == "CONNECT" or "Transfer-Encoding" in self.headers:
                self.send_error(501)
                return
            fields = list(self.headers.items())
            if sum(len(name) + len(value) + 4 for name, value in fields) > HEADER_LIMIT:
                self.send_error(431)
                return
            lengths = self.headers.get_all("Content-Length", ["0"])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                raise ValueError("invalid Content-Length")
            digits = lengths[0].lstrip("0")
            if len(digits) > 5 or int(digits or "0") > BODY_LIMIT:
                self.send_error(413)
                return
            length = int(digits or "0")
            payload = self.rfile.read(length)
            if len(payload) != length:
                return
            body = payload.decode("utf-8")
        except (OSError, ValueError):
            self.send_error(400)
            return

        request = record("HttpRequest", method=self.command, path=self.path,
                         headers=headers_record(fields), body=body)
        exchange = self.server.submit(self.connection, request)
        while not exchange.closed.is_set():
            if exchange.response.done():
                self.send(exchange)
                return
            exchange.closed.wait(0.002)

    def send(self, exchange):
        status, headers, body = exchange.response.result()
        try:
            readable, _, _ = select([self.connection], [], [], 0)
            if readable and not self.connection.recv(1, socket.MSG_PEEK):
                raise ConnectionError("HTTP peer disconnected")
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            exchange.sent.set_result(ok())
        except OSError as problem:
            exchange.sent.set_result(error(problem))


OPERATIONS = {
    "requestHttp": HttpClient.request,
    "acceptHttp": HttpServer.accept,
    "respondHttp": HttpServer.respond,
}
