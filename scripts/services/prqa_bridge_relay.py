#!/usr/bin/env python3
"""Byte relay: TCP :8790 -> the host bridge's Unix socket, or (PRQA_RELAY_TARGET=host:port) -> a TCP target.

Runs in the `prqa-n8n-bridge-relay` container (n8n -> bridge), and per local OpenCode run as the disposable API's
host relay (127.0.0.1-published port -> the API alias on the internal sandbox network; the API itself keeps no egress).
It does not parse, log or store requests; authentication stays end to end between n8n and scripts/services/prqa_n8n_bridge.py
(bearer token). Exists because the host firewall drops container -> host TCP.
"""
import asyncio
import os

SOCK = os.environ.get("PRQA_BRIDGE_SOCKET", "/run/prqa-bridge/bridge.sock")
PORT = int(os.environ.get("PRQA_BRIDGE_RELAY_PORT", "8790"))
TARGET = os.environ.get("PRQA_RELAY_TARGET", "")                     # host:port; empty = the Unix socket


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        if TARGET:
            host, _, port = TARGET.rpartition(":")
            up_r, up_w = await asyncio.open_connection(host, int(port))
        else:
            up_r, up_w = await asyncio.open_unix_connection(SOCK)
    except OSError:
        body = b'{"error": "relay target not reachable"}'
        client_w.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                       % (len(body), body))
        await client_w.drain()
        client_w.close()
        return
    await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w))


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print(f"relay :{PORT} -> {TARGET or SOCK}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
