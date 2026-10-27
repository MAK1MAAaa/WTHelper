"""Exercise the CDP transport against disposable loopback fake servers."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import threading
import time
from typing import Callable
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cdp_client.py"
SPEC = importlib.util.spec_from_file_location("cdp_client", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cdp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cdp)


def read_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = connection.recv(size - len(data))
        if not part:
            raise AssertionError("Client disconnected unexpectedly")
        data.extend(part)
    return bytes(data)


def read_client_frame(connection: socket.socket) -> tuple[int, bytes]:
    first, second = read_exact(connection, 2)
    assert first & 0x80
    assert second & 0x80, "Client frames must be masked"
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(connection, 8))[0]
    mask = read_exact(connection, 4)
    raw = read_exact(connection, length)
    return first & 0x0F, bytes(value ^ mask[index % 4] for index, value in enumerate(raw))


def server_frame(payload: bytes, opcode: int = 1, final: bool = True) -> bytes:
    header = bytes([(0x80 if final else 0) | opcode])
    if len(payload) < 126:
        return header + bytes([len(payload)]) + payload
    if len(payload) <= 65535:
        return header + b"\x7e" + struct.pack("!H", len(payload)) + payload
    return header + b"\x7f" + struct.pack("!Q", len(payload)) + payload


def send_response(connection: socket.socket, request_id: int, result: dict) -> None:
    raw = json.dumps({"id": request_id, "result": result}, ensure_ascii=False).encode("utf-8")
    connection.sendall(server_frame(raw))


class FakeServer:
    def __init__(
        self,
        handler: Callable[[socket.socket], None],
        *,
        valid_handshake: bool = True,
    ) -> None:
        self.handler = handler
        self.valid_handshake = valid_handshake
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(2)
        self.url = f"ws://127.0.0.1:{self.listener.getsockname()[1]}/devtools/page/test-page"
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self) -> None:
        try:
            with self.listener.accept()[0] as connection:
                connection.settimeout(2)
                raw = bytearray()
                while not raw.endswith(b"\r\n\r\n"):
                    raw.extend(read_exact(connection, 1))
                headers = dict(
                    (name.lower(), value.strip())
                    for name, value in (
                        line.split(":", 1)
                        for line in raw.decode("ascii").split("\r\n")[1:]
                        if ":" in line
                    )
                )
                accept = base64.b64encode(
                    hashlib.sha1((headers["sec-websocket-key"] + cdp.WEBSOCKET_GUID).encode("ascii")).digest()
                ).decode("ascii")
                if not self.valid_handshake:
                    accept = "invalid"
                connection.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: WebSocket\r\n"
                        "Connection: keep-alive, Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                self.handler(connection)
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self.listener.close()

    def __enter__(self) -> FakeServer:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(3)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("Fake server did not stop")
        if self.errors:
            raise AssertionError(f"Fake server failed: {self.errors[0]}") from self.errors[0]


class CdpClientTests(unittest.TestCase):
    def test_rejects_nonlocal_or_ambiguous_endpoint_before_connecting(self) -> None:
        invalid = [
            "ws://example.com:9222/devtools/page/a",
            "ws://localhost:9222/devtools/page/a",
            "wss://127.0.0.1:9222/devtools/page/a",
            "ws://127.0.0.1/devtools/page/a",
            "ws://127.0.0.1:9222/devtools/browser/a",
            "ws://user@127.0.0.1:9222/devtools/page/a",
            "ws://127.0.0.1:9222/devtools/page/a?x=1",
            "ws://127.0.0.1:9222/devtools/page/a?",
            "ws://127.0.0.1:9222/devtools/page/a#fragment",
            "ws://127.0.0.1:9222/devtools/page/a#",
            "ws://127.0.0.1:9222/devtools/page/../another",
            "ws://127.0.0.1:9222/devtools/page/a\r\nX: injected",
            "ws://127.0.0.1:99999/devtools/page/a",
        ]
        with patch.object(cdp.socket, "create_connection") as connect:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(cdp.CdpError):
                    cdp.CdpClient(url)
            connect.assert_not_called()

    def test_rejects_invalid_timeout(self) -> None:
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(cdp.CdpError):
                cdp.CdpClient("ws://127.0.0.1:9222/devtools/page/a", timeout=timeout)

    def test_invalid_handshake_is_rejected(self) -> None:
        with FakeServer(lambda connection: None, valid_handshake=False) as server:
            with self.assertRaisesRegex(cdp.CdpError, "握手校验"):
                with cdp.CdpClient(server.url):
                    self.fail("Invalid handshake was accepted")

    def test_connection_failure_is_wrapped(self) -> None:
        with patch.object(cdp.socket, "create_connection", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(cdp.CdpError, "无法连接"):
                cdp.CdpClient("ws://127.0.0.1:9222/devtools/page/a").connect()

    def test_request_masking_events_fragmented_unicode_and_ping(self) -> None:
        def handler(connection: socket.socket) -> None:
            opcode, raw = read_client_frame(connection)
            self.assertEqual(opcode, 1)
            request = json.loads(raw)
            self.assertEqual(request["method"], "Runtime.evaluate")
            self.assertTrue(request["params"]["returnByValue"])
            self.assertTrue(request["params"]["awaitPromise"])
            event = server_frame(b'{"method":"Runtime.consoleAPICalled","params":{}}')
            ping = server_frame(b"alive", opcode=9)
            response = json.dumps(
                {"id": request["id"], "result": {"result": {"type": "string", "value": "宝箱"}}},
                ensure_ascii=False,
            ).encode("utf-8")
            split = response.index("宝".encode("utf-8")) + 1
            connection.sendall(
                event + server_frame(response[:split], final=False)
                + ping + server_frame(response[split:], opcode=0)
            )
            self.assertEqual(read_client_frame(connection), (10, b"alive"))

        with FakeServer(handler) as server, cdp.CdpClient(server.url) as client:
            self.assertEqual(client.evaluate("Promise.resolve('宝箱')", await_promise=True), "宝箱")

    def test_extended_request_and_response_lengths(self) -> None:
        for length in (200, 70000):
            def handler(connection: socket.socket) -> None:
                _, raw = read_client_frame(connection)
                request = json.loads(raw)
                self.assertEqual(request["params"]["text"], "x" * length)
                send_response(connection, request["id"], {"echo": "y" * length})

            with self.subTest(length=length):
                with FakeServer(handler) as server, cdp.CdpClient(server.url) as client:
                    self.assertEqual(client.call("Echo", {"text": "x" * length}), {"echo": "y" * length})

    def test_call_reports_cdp_error_and_disconnects(self) -> None:
        def handler(connection: socket.socket) -> None:
            _, raw = read_client_frame(connection)
            connection.sendall(server_frame(json.dumps({"id": json.loads(raw)["id"], "error": {"code": -1, "message": "rejected"}}).encode()))

        with FakeServer(handler) as server, cdp.CdpClient(server.url) as client:
            with self.assertRaisesRegex(cdp.CdpError, "rejected"):
                client.call("Invalid")
            self.assertIsNone(client._socket)

    def test_evaluate_reports_javascript_exception(self) -> None:
        def handler(connection: socket.socket) -> None:
            _, raw = read_client_frame(connection)
            send_response(connection, json.loads(raw)["id"], {"exceptionDetails": {"text": "Uncaught"}})

        with FakeServer(handler) as server, cdp.CdpClient(server.url) as client:
            with self.assertRaisesRegex(cdp.CdpError, "页面脚本执行失败"):
                client.evaluate("throw Error('test')")

    def test_timeout_is_bounded_even_when_events_keep_arriving(self) -> None:
        def handler(connection: socket.socket) -> None:
            read_client_frame(connection)
            for _ in range(5):
                try:
                    connection.sendall(server_frame(b'{"method":"event"}'))
                except OSError:
                    return
                time.sleep(0.02)

        with FakeServer(handler) as server, cdp.CdpClient(server.url, timeout=0.04) as client:
            started = time.monotonic()
            with self.assertRaises(cdp.CdpError):
                client.call("Wait")
            self.assertLess(time.monotonic() - started, 0.5)

    def test_close_invalid_frames_and_invalid_json_fail_cleanly(self) -> None:
        frames = [
            server_frame(b"\x03\xe8", opcode=8),
            server_frame(b"x", opcode=8),
            server_frame(b"not json"),
            b"\x81\x80\x00\x00\x00\x00",
            server_frame(b"binary", opcode=2),
            server_frame(b"orphan", opcode=0),
        ]
        for frame in frames:
            def handler(connection: socket.socket) -> None:
                read_client_frame(connection)
                connection.sendall(frame)

            with self.subTest(frame=frame):
                with FakeServer(handler) as server, cdp.CdpClient(server.url) as client:
                    with self.assertRaises(cdp.CdpError):
                        client.call("Fail")
                    self.assertIsNone(client._socket)

    def test_oversized_fragmented_response_is_rejected(self) -> None:
        def handler(connection: socket.socket) -> None:
            read_client_frame(connection)
            connection.sendall(server_frame(b"x" * 40, final=False) + server_frame(b"y" * 40, opcode=0))

        with FakeServer(handler) as server, cdp.CdpClient(server.url) as client:
            with patch.object(cdp, "MAX_MESSAGE_SIZE", 64):
                with self.assertRaisesRegex(cdp.CdpError, "大小限制"):
                    client.call("Small")


if __name__ == "__main__":
    unittest.main()
