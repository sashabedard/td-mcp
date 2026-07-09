import asyncio
import json
import os
from collections import deque
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
        # Rolling roundtrip latencies (ms). TD's WebServer DAT shares the
        # main cook thread: sustained slow responses mean the graph is
        # starving the bridge — the wedge is coming.
        self._latencies: deque[float] = deque(maxlen=10)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def cook_pressure(self) -> dict[str, Any] | None:
        """Non-None when the MEDIAN of recent roundtrips exceeds the
        threshold (TD_MCP_COOK_PRESSURE_MS, default 750). Median, not mean:
        one legitimate 180s catalog refresh must not trip it — only
        sustained slowness does. Needs ≥3 samples."""
        if len(self._latencies) < 3:
            return None
        ordered = sorted(self._latencies)
        median = ordered[len(ordered) // 2]
        threshold = float(os.environ.get("TD_MCP_COOK_PRESSURE_MS", "750"))
        if median <= threshold:
            return None
        return {
            "median_ms": round(median, 1),
            "threshold_ms": threshold,
            "samples": len(ordered),
        }

    async def connect(self, url: str, token: str | None = None, timeout: float = 5.0) -> None:
        async with self._lock:
            await self._disconnect_locked()
            self._token = token or None
            self._timeout = timeout
            # ping_interval=None disables client-side keepalive pings: TD's
            # WebServer DAT does not respond to WS-level ping frames, so the
            # default 20s ping/pong cycle would silently close the connection
            # whenever the agent paused for more than 20s between calls.
            # max_size lifts the websockets default of 1 MiB — a single
            # td_snapshot of a 720x1280 16-bit-float TOP exceeds it, which
            # killed the reader loop mid-session. 256 MiB bounds memory while
            # covering 4K float frames (localhost trust model).
            self._ws = await websockets.connect(
                url, open_timeout=timeout, ping_interval=None,
                max_size=256 * 1024 * 1024,
            )
            self._latencies.clear()
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
        error: Exception | None = None
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
        except Exception as e:
            # Keep the cause: a bare "reader crashed" hides actionable
            # failures (payload too big, connection reset) behind an
            # identical symptom.
            error = e
        finally:
            # Reaching here on ANY path — clean close (TD quit), crash, or
            # cancellation from disconnect() — means this connection is done.
            # Mark the bridge disconnected and fail in-flight calls now:
            # letting them wait out their timeout reports "timed out" for
            # what is really a dead connection. The identity guard keeps a
            # stale reader from clobbering a newer connection's state.
            if self._ws is ws:
                self._ws = None
                self._reader_task = None
            msg_text = (
                f"Bridge reader crashed: {error!r}"
                if error
                else "Connection to TouchDesigner closed"
            )
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(TDError(msg_text))
            self._pending.clear()

    async def send(
        self,
        action: str,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """`timeout` overrides the connection default for this one call —
        needed by known-long actions (full-catalog introspection) without
        loosening the 5s guard that catches a wedged cook thread early."""
        if self._ws is None:
            raise TDError("Not connected — call td_connect first")

        msg_id = self._next_id
        self._next_id += 1
        req = BridgeRequest(id=msg_id, action=action, data=data or {}, token=self._token)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut

        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            await self._ws.send(req.model_dump_json(exclude_none=True))
        except Exception as e:
            # Transport failure must honor the TDError contract (_call turns
            # it into {ok: false}) and must not leak the pending future.
            self._pending.pop(msg_id, None)
            raise TDError(
                f'Failed to send "{action}" — connection to TouchDesigner lost: {e!r}'
            ) from e
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            result = await asyncio.wait_for(fut, timeout=effective_timeout)
            self._latencies.append((loop.time() - t0) * 1000.0)
            return result
        except asyncio.TimeoutError as e:
            self._pending.pop(msg_id, None)
            # A timeout is the strongest pressure signal there is.
            self._latencies.append(effective_timeout * 1000.0)
            raise TDError(f'Action "{action}" timed out after {effective_timeout}s') from e


bridge = TDBridge()
