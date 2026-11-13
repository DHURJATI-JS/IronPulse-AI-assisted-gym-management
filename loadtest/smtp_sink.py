"""Local SMTP sink for the load-test server: accepts every mail on 127.0.0.1:2525, waits 100-300 ms like a real relay,
then DISCARDS it - the app's real delivery path (outbox worker -> aiosmtplib -> SMTP) runs, and no mail ever leaves
this machine. Started by ``loadtest/run_server.sh start --smtp sink`` (the server then gets SMTP_HOST=127.0.0.1,
SMTP_PORT=2525, SMTP_STARTTLS=false, SMTP_SSL=false). Standard library only.

    .venv-loadtest/bin/python loadtest/smtp_sink.py [--port 2525] [--delay-ms 100..300]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import signal
import sys
import time

COUNTS = {"connections": 0, "messages": 0, "bytes": 0}


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, delay: tuple[float, float]) -> None:
    COUNTS["connections"] += 1

    async def say(line: str) -> None:
        writer.write((line + "\r\n").encode())
        await writer.drain()

    try:
        await say("220 ironpulse-loadtest-sink ESMTP ready (mail is discarded)")
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=60)
            if not raw:
                break
            cmd = raw.decode("utf-8", "replace").strip()
            verb = cmd.split(" ", 1)[0].upper()
            if verb == "EHLO":
                writer.write(b"250-ironpulse-loadtest-sink\r\n250-8BITMIME\r\n250-SIZE 52428800\r\n250 SMTPUTF8\r\n")
                await writer.drain()
            elif verb == "HELO":
                await say("250 ironpulse-loadtest-sink")
            elif verb in ("MAIL", "RCPT", "RSET", "NOOP"):
                await say("250 OK")
            elif verb == "DATA":
                await say("354 End data with <CR><LF>.<CR><LF>")
                size = 0
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=60)
                    if not line or line in (b".\r\n", b".\n"):
                        break
                    size += len(line)
                await asyncio.sleep(random.uniform(*delay))  # a relay's accept time
                COUNTS["messages"] += 1
                COUNTS["bytes"] += size
                await say("250 OK: queued as discarded")
            elif verb == "QUIT":
                await say("221 Bye")
                break
            else:
                await say("502 Command not implemented")
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()


async def main_async(port: int, delay: tuple[float, float]) -> None:
    server = await asyncio.start_server(lambda r, w: handle(r, w, delay), "127.0.0.1", port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    print(f"[smtp-sink] listening on 127.0.0.1:{port} (mail is discarded)", flush=True)
    t0 = time.time()
    async with server:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                print(f"[smtp-sink] {time.time() - t0:7.0f}s {COUNTS}", flush=True)
    print(f"[smtp-sink] stopped: {COUNTS}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=2525)
    ap.add_argument("--delay-ms", default="100..300")
    args = ap.parse_args()
    lo, _, hi = args.delay_ms.partition("..")
    delay = (float(lo) / 1000.0, float(hi or lo) / 1000.0)
    asyncio.run(main_async(args.port, delay))
    return 0


if __name__ == "__main__":
    sys.exit(main())
