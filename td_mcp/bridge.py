import asyncio
import json
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from td_mcp.protocol import BridgeRequest, BridgeResponse, TDError


class TDBridge:
    """Async WebSocket client to a TouchDesigner WebServer DAT.

    The protocol matches the existing td-control-mcp .tox companion so the
    bridge stays drop-in compatible during the Node→Python transition.
    """

    def __init__(self) -> None:
        self._ws: ClientConnection | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._token: str | None = None
        self._timeout: float = 5.0
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self, url: str, token: str | None = None, timeout: float = 5.0) -> None:
        async with self._lock:
            await self._disconnect_locked()
            self._token = token or None
            self._timeout = timeout
            # ping_interval=None disables client-side keepalive pings: TD's
            # WebServer DAT does not respond to WS-level ping frames, so the
            # default 20s ping/pong cycle would silently close the connection
            # whenever the agent paused for more than 20s between calls.
            self._ws = await websockets.connect(
                url, open_timeout=timeout, ping_interval=None
            )
            self._reader_task = asyncio.create_task(self._reader_loop(self._ws))

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(TDError("Disconnected from TouchDesigner"))
        self._pending.clear()

    async def _reader_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                    msg = BridgeResponse.model_validate(payload)
                except Exception:
                    continue
                if msg.id is None:
                    continue
                fut = self._pending.pop(msg.id, None)
                if fut is None or fut.done():
                    continue
                if msg.ok:
                    fut.set_result(msg.result or {})
                else:
                    err = msg.error or None
                    fut.set_exception(
                        TDError(
                            err.message if err else "Unknown TD error",
                            err.traceback if err else None,
                        )
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            # Connection died — drain pending
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(TDError("Bridge reader crashed"))
            self._pending.clear()

    async def send(self, action: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise TDError("Not connected — call td_connect first")

        msg_id = self._next_id
        self._next_id += 1
        req = BridgeRequest(id=msg_id, action=action, data=data or {}, token=self._token)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut

        try:
            await self._ws.send(req.model_dump_json(exclude_none=True))
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(msg_id, None)
            raise TDError(f'Action "{action}" timed out after {self._timeout}s') from e


bridge = TDBridge()
