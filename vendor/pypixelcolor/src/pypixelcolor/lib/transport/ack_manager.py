import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class AckPolicy:
    ack_per_window: bool = True
    ack_final: bool = True

class AckManager:
    def __init__(self):
        self.window_event = asyncio.Event()
        self.all_event = asyncio.Event()

    def reset(self):
        self.window_event.clear()
        self.all_event.clear()

    def make_notify_handler(self):
        def handler(_, data: bytes):
            if not data:
                return
            try:
                logger.debug(f"Notify frame: {data.hex()}")
            except Exception:
                pass
            # Protocol ACK frame: exactly 5 bytes, opcode 0x05, status in data[4].
            #
            # SECURITY (review F-8): only accept the strict, fixed-length frame.
            # The previous fallback also accepted *any* length >= 5 frame starting
            # with 0x05, so stray/oversized notifications were misread as ACKs and
            # could advance the sender into a desynchronised transfer. We drop the
            # permissive branch; non-conforming frames are ignored.
            if len(data) != 5 or data[0] != 0x05:
                return
            status = data[4]
            if status in (0, 1):
                self.window_event.set()
            elif status == 3:
                self.window_event.set()
                self.all_event.set()
        return handler
