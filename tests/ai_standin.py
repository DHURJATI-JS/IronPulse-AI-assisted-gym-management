"""Stand-in OpenAI-compatible servers for the failover tests (tests/test_ai_failover.py) - a llama.cpp gateway or an
Ollama container that is up, down, slow, erroring, malformed or half-working, on demand.

Plain asyncio streams (no web framework), so a test can break the protocol exactly where a real server breaks it: a
connection refused, a first token that never comes, HTTP 500/503, a body that is not JSON, a broken server-sent event, a
response without choices, a chunked stream that is cut before or after its first words (``transport.abort()``: no
terminating chunk, like a crashed process). Every request is recorded (``requests``) with the in-flight high-water mark
(``max_inflight``), so a test can tell which provider saw which prompt and how many at once.

    server = StandIn("gateway", mode="ok"); await server.start(); ...; server.mode = "error503"; ...; await server.stop()

``python -m tests.ai_standin --port 12301 --mode slow --delay 30`` runs one by hand (the failover matrix).

Modes: ok, slow (first words after ``delay``), slow_headers (response headers after ``delay``), error500, error503,
bad_json, broken_sse, empty_choices, die_before_text, die_mid_stream, length (finish_reason 'length'), down
(``stop()``: connection refused). A switched-off host (no RST, no answer: the connect itself hangs) is ``BLACKHOLE_URL``
(TEST-NET-1, RFC 5737 - never a real host); ``blackhole_hangs()`` tells whether this network drops it silently.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import time
from typing import Any

MODES = ("ok", "slow", "slow_headers", "error500", "error503", "bad_json", "broken_sse", "empty_choices",
         "die_before_text", "die_mid_stream", "length")


class StandIn:
    def __init__(self, name: str, *, mode: str = "ok", port: int = 0, delay: float = 30.0,
                 words: list[str] | None = None, chunk_delay: float = 0.0, model: str = "gemma") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.name = name
        self.mode = mode
        self.port = port
        self.delay = delay
        self.chunk_delay = chunk_delay
        self.model = model
        self.words = words or [f"{name}", " answers", " this", " question", "."]
        self.requests: list[dict[str, Any]] = []
        self.inflight = 0
        self.max_inflight = 0
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> StandIn:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        """Close the port (from now on: connection refused) and cut every open connection."""
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                writer.transport.abort()
            for task in list(self._tasks):  # a handler still sleeping (slow modes) ends now
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None

    async def restart(self, mode: str = "ok") -> None:
        """Come back on the same port (a container restarted)."""
        await self.stop()
        self.mode = mode
        await self.start()

    # ------------------------------------------------------------------ HTTP
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
            body = await reader.readexactly(int(headers.get("content-length") or 0))
            if method == "GET" and path.rstrip("/").endswith("/models"):
                await self._json(writer, 200, {"object": "list", "data": [{"id": self.model, "object": "model"}]})
                return
            if method != "POST" or not path.rstrip("/").endswith("/chat/completions"):
                await self._json(writer, 404, {"error": {"message": "not found"}})
                return
            payload = json.loads(body or b"{}")
            self.requests.append({"at": time.monotonic(), "stream": bool(payload.get("stream")), "body": payload})
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                await self._chat(writer, payload)
            finally:
                self.inflight -= 1
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            if not writer.transport.is_closing():
                writer.close()

    async def _chat(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        mode = self.mode
        if mode == "slow_headers":
            await asyncio.sleep(self.delay)
        if mode in ("error500", "error503"):
            await self._json(writer, int(mode[-3:]), {"error": {"message": "the backend is unavailable", "type": "server_error"}})
            return
        if mode == "bad_json":
            await self._raw(writer, 200, b"{this is not json", "application/json")
            return
        if not payload.get("stream"):
            await self._complete(writer, mode)
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                     b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
        await writer.drain()
        if mode == "broken_sse":
            await self._chunk(writer, b"data: {\"choices\": [{\"delta\": {\"content\": \"oops\"\n\n")
            await self._chunk(writer, b"")
            return
        if mode == "empty_choices":
            for _ in range(3):
                await self._event(writer, {"choices": []})
            await self._chunk(writer, b"data: [DONE]\n\n")
            await self._chunk(writer, b"")
            return
        await self._event(writer, self._delta({"role": "assistant"}))  # role first, no text (llama.cpp does this)
        if mode == "die_before_text":
            writer.transport.abort()
            return
        if mode == "slow":
            await asyncio.sleep(self.delay)
        for i, word in enumerate(self.words):
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            await self._event(writer, self._delta({"content": word}))
            if mode == "die_mid_stream" and i == 1:
                writer.transport.abort()
                return
        await self._event(writer, self._delta({}, finish="length" if mode == "length" else "stop"))
        await self._chunk(writer, b"data: [DONE]\n\n")
        await self._chunk(writer, b"")

    async def _complete(self, writer: asyncio.StreamWriter, mode: str) -> None:
        if mode == "slow":
            await asyncio.sleep(self.delay)
        if mode == "empty_choices":
            await self._json(writer, 200, {"id": "x", "object": "chat.completion", "choices": []})
            return
        text = "".join(self.words)
        body = json.dumps({"id": "chatcmpl-standin", "object": "chat.completion", "model": self.model,
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                        "finish_reason": "length" if mode == "length" else "stop"}]}).encode()
        if mode in ("die_before_text", "die_mid_stream", "broken_sse"):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body[: len(body) // 3])
            await writer.drain()
            writer.transport.abort()
            return
        await self._raw(writer, 200, body, "application/json")

    def _delta(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {"id": "chatcmpl-standin", "object": "chat.completion.chunk", "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    async def _event(self, writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        await self._chunk(writer, b"data: " + json.dumps(data).encode() + b"\n\n")

    @staticmethod
    async def _chunk(writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        await writer.drain()

    async def _json(self, writer: asyncio.StreamWriter, status: int, data: dict[str, Any]) -> None:
        await self._raw(writer, status, json.dumps(data).encode(), "application/json")

    @staticmethod
    async def _raw(writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str) -> None:
        reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "X")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                     "Connection: close\r\n\r\n".encode() + body)
        await writer.drain()


BLACKHOLE_URL = "http://192.0.2.1:9/v1"


def blackhole_hangs(wait: float = 0.5) -> bool:
    """Does a TCP connect to BLACKHOLE_URL hang here (rather than fail at once with 'no route')?"""
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=wait).close()
    except TimeoutError:
        return True
    except OSError:
        return False
    return False


def free_port(preferred: int) -> int:
    """``preferred`` when nothing listens there, else any free port (parallel runs never collide)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


async def _serve(args: argparse.Namespace) -> None:
    server = await StandIn(args.name, mode=args.mode, port=args.port, delay=args.delay).start()
    print(f"stand-in '{args.name}' ({args.mode}) on {server.base_url} - Ctrl+C to stop", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="A stand-in OpenAI-compatible server (failover tests)")
    ap.add_argument("--name", default="gateway")
    ap.add_argument("--port", type=int, default=12301)
    ap.add_argument("--mode", choices=MODES, default="ok")
    ap.add_argument("--delay", type=float, default=30.0)
    args = ap.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
