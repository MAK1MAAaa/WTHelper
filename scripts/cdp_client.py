"""Small, bounded CDP client restricted to a local WebView2 page endpoint."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import socket
import struct
import time
from typing import Any
from urllib.parse import urlsplit


MAX_MESSAGE_SIZE = 4 * 1024 * 1024
MAX_HEADER_SIZE = 16 * 1024
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CdpError(Exception):
    """A local debugging connection or CDP operation failed."""


class CdpClient:
    """Use one synchronous CDP request at a time over a loopback WebSocket."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise CdpError("调试连接地址格式无效。") from exc
        if (
            parsed.scheme != "ws"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or "?" in url
            or "#" in url
            or re.fullmatch(r"/devtools/page/[A-Za-z0-9._-]+", parsed.path) is None
        ):
            raise CdpError("仅允许连接本机 127.0.0.1 的 WebView2 页面调试地址。")
        if not math.isfinite(timeout) or timeout <= 0:
            raise CdpError("连接超时必须为正数。")
        self.url = url
        self.timeout = timeout
        self._port = port
        self._path = parsed.path
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._next_id = 1

    def __enter__(self) -> CdpClient:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CdpError("本机调试连接超时。")
        return remaining

    def connect(self) -> None:
        if self._socket is not None:
            return
        deadline = time.monotonic() + self.timeout
        try:
            self._socket = socket.create_connection(
                ("127.0.0.1", self._port), timeout=self._remaining(deadline)
            )
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {self._path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self._port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
            self._send_bytes(request, deadline)
            while b"\r\n\r\n" not in self._buffer:
                if len(self._buffer) >= MAX_HEADER_SIZE:
                    raise CdpError("调试连接握手响应过大。")
                self._socket.settimeout(self._remaining(deadline))
                part = self._socket.recv(min(4096, MAX_HEADER_SIZE - len(self._buffer)))
                if not part:
                    raise CdpError("调试连接在握手期间关闭。")
                self._buffer.extend(part)
            header, trailing = bytes(self._buffer).split(b"\r\n\r\n", 1)
            self._buffer = bytearray(trailing)
            lines = header.decode("iso-8859-1").split("\r\n")
            status = lines[0].split()
            if len(status) < 2 or status[:2] != ["HTTP/1.1", "101"]:
                raise CdpError("WebView2 拒绝了调试 WebSocket 连接。")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" not in line:
                    raise CdpError("调试握手响应头格式无效。")
                name, value = line.split(":", 1)
                name = name.strip().lower()
                separator = "," if name in headers else ""
                headers[name] = headers.get(name, "") + separator + value.strip()
            expected = base64.b64encode(
                hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
            ).decode("ascii")
            if (
                headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in {
                    item.strip().lower()
                    for item in headers.get("connection", "").split(",")
                }
                or headers.get("sec-websocket-accept") != expected
                or headers.get("sec-websocket-extensions")
                or headers.get("sec-websocket-protocol")
            ):
                raise CdpError("调试 WebSocket 握手校验失败。")
        except (OSError, CdpError) as exc:
            self.close()
            if isinstance(exc, CdpError):
                raise
            raise CdpError("无法连接本机 WebView2 调试端口，或连接已超时。") from exc

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._send_frame(8, struct.pack("!H", 1000), time.monotonic() + 0.1)
            except (OSError, CdpError):
                pass
        self._terminate()

    def _terminate(self) -> None:
        connection, self._socket = self._socket, None
        self._buffer.clear()
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _send_bytes(self, raw: bytes, deadline: float) -> None:
        if self._socket is None:
            raise CdpError("调试连接未打开。")
        self._socket.settimeout(self._remaining(deadline))
        self._socket.sendall(raw)

    def _send_frame(self, opcode: int, payload: bytes, deadline: float) -> None:
        size = len(payload)
        if size > MAX_MESSAGE_SIZE:
            raise CdpError("调试请求超过大小限制。")
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size <= 65535:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._send_bytes(header + mask + masked, deadline)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        if self._socket is None:
            raise CdpError("调试连接未打开。")
        while len(self._buffer) < size:
            self._socket.settimeout(self._remaining(deadline))
            part = self._socket.recv(min(65536, size - len(self._buffer)))
            if not part:
                raise CdpError("WebView2 调试连接已关闭。")
            self._buffer.extend(part)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def _read_frame(self, deadline: float) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2, deadline)
        final, opcode, length = bool(first & 0x80), first & 0x0F, second & 0x7F
        if first & 0x70 or second & 0x80:
            raise CdpError("调试服务器返回无效的 WebSocket 帧。")
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline))[0]
            if length < 126:
                raise CdpError("调试帧的长度编码无效。")
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
            if length <= 65535 or length & (1 << 63):
                raise CdpError("调试帧的长度编码无效。")
        if length > MAX_MESSAGE_SIZE or (opcode >= 8 and (not final or length > 125)):
            raise CdpError("调试帧超过大小限制或控制帧无效。")
        return final, opcode, self._read_exact(length, deadline)

    def _read_message(self, deadline: float) -> str:
        message = bytearray()
        fragmented = False
        while True:
            final, opcode, payload = self._read_frame(deadline)
            if opcode == 8:
                if len(payload) == 1:
                    raise CdpError("调试关闭帧格式无效。")
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                    if not 1000 <= code < 5000 or code in {1004, 1005, 1006, 1015}:
                        raise CdpError("调试关闭状态码无效。")
                    try:
                        payload[2:].decode("utf-8")
                    except UnicodeError as exc:
                        raise CdpError("调试关闭原因不是有效 UTF-8 文本。") from exc
                try:
                    self._send_frame(8, payload, deadline)
                finally:
                    self._terminate()
                raise CdpError("WebView2 主动关闭了调试连接。")
            if opcode == 9:
                self._send_frame(10, payload, deadline)
                continue
            if opcode == 10:
                continue
            if opcode == 1 and not fragmented:
                fragmented = not final
            elif opcode != 0 or not fragmented:
                raise CdpError("调试连接返回了非文本或无效分片消息。")
            message.extend(payload)
            if len(message) > MAX_MESSAGE_SIZE:
                raise CdpError("调试响应超过大小限制。")
            if final:
                try:
                    return message.decode("utf-8")
                except UnicodeError as exc:
                    raise CdpError("调试响应不是有效 UTF-8 文本。") from exc

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.connect()
        request_id = self._next_id
        self._next_id += 1
        deadline = time.monotonic() + self.timeout
        try:
            payload = json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self._send_frame(1, payload, deadline)
            while True:
                response = json.loads(self._read_message(deadline))
                if not isinstance(response, dict):
                    raise CdpError("调试响应不是 JSON 对象。")
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"]
                    detail = (
                        str(error.get("message", "未知错误"))[:200]
                        if isinstance(error, dict)
                        else "未知错误"
                    )
                    raise CdpError(f"CDP 请求失败：{detail}")
                result = response.get("result", {})
                if not isinstance(result, dict):
                    raise CdpError("CDP 返回值格式无效。")
                return result
        except (OSError, ValueError, CdpError) as exc:
            self.close()
            if isinstance(exc, CdpError):
                raise
            raise CdpError("本机调试通信失败、超时或返回了无效 JSON。") from exc

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        if "exceptionDetails" in result:
            raise CdpError("页面脚本执行失败。")
        remote_object = result.get("result")
        if not isinstance(remote_object, dict):
            raise CdpError("页面脚本返回值格式无效。")
        if "unserializableValue" in remote_object:
            raise CdpError("页面脚本返回了不能序列化的值。")
        if remote_object.get("type") == "undefined":
            return None
        return remote_object.get("value")
