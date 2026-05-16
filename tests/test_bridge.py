import asyncio
import json

import pytest
import websockets

from td_mcp.bridge import TDBridge
from td_mcp.protocol import TDError


async def _echo_server(host: str = "127.0.0.1", port: int = 0):
    """Mock TD WebServer DAT: echoes get_status, errors on 'boom'."""

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            mid = msg["id"]
            action = msg["action"]
            if action == "get_status":
                await ws.send(
                    json.dumps({"id": mid, "ok": True, "result": {"fps": 60, "frame": 1}})
                )
            elif action == "boom":
                await ws.send(
                    json.dumps({"id": mid, "ok": False, "error": {"message": "kaboom"}})
                )
            else:
                await ws.send(
                    json.dumps({"id": mid, "ok": False, "error": {"message": "unknown"}})
                )

    server = await websockets.serve(handler, host, port)
    return server


async def test_connect_send_disconnect():
    bridge = TDBridge()
    server = await _echo_server()
    port = server.sockets[0].getsockname()[1]
    try:
        await bridge.connect(f"ws://127.0.0.1:{port}")
        assert bridge.connected
        result = await bridge.send("get_status")
        assert result == {"fps": 60, "frame": 1}
    finally:
        await bridge.disconnect()
        server.close()
        await server.wait_closed()

    assert not bridge.connected


async def test_error_propagation():
    bridge = TDBridge()
    server = await _echo_server()
    port = server.sockets[0].getsockname()[1]
    try:
        await bridge.connect(f"ws://127.0.0.1:{port}")
        with pytest.raises(TDError, match="kaboom"):
            await bridge.send("boom")
    finally:
        await bridge.disconnect()
        server.close()
        await server.wait_closed()


async def test_not_connected_rejects():
    bridge = TDBridge()
    with pytest.raises(TDError, match="Not connected"):
        await bridge.send("get_status")


async def test_timeout():
    bridge = TDBridge()

    async def handler(ws):
        async for _ in ws:
            await asyncio.sleep(2)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await bridge.connect(f"ws://127.0.0.1:{port}", timeout=0.3)
        with pytest.raises(TDError, match="timed out"):
            await bridge.send("get_status")
    finally:
        await bridge.disconnect()
        server.close()
        await server.wait_closed()
