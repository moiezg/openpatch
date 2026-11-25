from __future__ import annotations

import asyncio
import threading

# BLE transport for the real pump. GATT layout implemented by the firmware:
#   service  6f700001-0b5a-4c5e-9d3e-0e0a7c1d0001
#   command  6f700002-...  write-with-response  (request frame)
#   response 6f700003-...  notify               (response frame)
# Requires `pip install bleak`. Link security: LE Secure Connections pairing with a passkey shown
# on the pump's provisioning step, plus the per-frame HMAC from protocol.py.

SERVICE_UUID = "6f700001-0b5a-4c5e-9d3e-0e0a7c1d0001"
COMMAND_UUID = "6f700002-0b5a-4c5e-9d3e-0e0a7c1d0001"
RESPONSE_UUID = "6f700003-0b5a-4c5e-9d3e-0e0a7c1d0001"


class BleTransport:
    def __init__(self, address: str, timeout: float = 4.0):
        try:
            from bleak import BleakClient
        except ImportError as e:
            raise RuntimeError("BLE transport needs `pip install bleak`") from e
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._client = BleakClient(address)
        self._timeout = timeout
        self._pending: asyncio.Future | None = None
        self._run(self._connect())

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self._timeout + 2)

    async def _connect(self):
        await self._client.connect()
        await self._client.start_notify(RESPONSE_UUID, self._on_notify)

    def _on_notify(self, _handle, data: bytearray):
        if self._pending and not self._pending.done():
            self._pending.set_result(bytes(data))

    async def _exchange(self, frame: bytes) -> bytes:
        self._pending = self._loop.create_future()
        await self._client.write_gatt_char(COMMAND_UUID, frame, response=True)
        return await asyncio.wait_for(self._pending, self._timeout)

    def __call__(self, frame: bytes) -> bytes:
        try:
            return self._run(self._exchange(frame))
        except Exception as e:
            raise TimeoutError(f"BLE exchange failed: {e}") from e
