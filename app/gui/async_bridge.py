"""Background asyncio loop shared by the windows.

Coroutines are submitted from the Qt thread and run on a dedicated loop thread;
results come back as signals, which Qt delivers on the thread that owns the
receiver.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

from PySide6.QtCore import QObject


class AsyncBridge(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
