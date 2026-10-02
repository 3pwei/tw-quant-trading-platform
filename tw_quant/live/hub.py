from __future__ import annotations

import asyncio


class BroadcastHub:
    def __init__(self, queue_size: int = 256):
        self.queue_size = queue_size
        self.clients: set[asyncio.Queue] = set()
        self.total_connections = 0
        self.total_disconnections = 0
        self.dropped_messages = 0

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self.clients.add(queue)
        self.total_connections += 1
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.clients:
            self.clients.discard(queue)
            self.total_disconnections += 1

    def publish(self, message: dict[str, object]) -> None:
        for queue in tuple(self.clients):
            if queue.full():
                try:
                    queue.get_nowait()
                    self.dropped_messages += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    def stats(self) -> dict[str, int]:
        return {
            "active_connections": len(self.clients),
            "total_connections": self.total_connections,
            "total_disconnections": self.total_disconnections,
            "dropped_messages": self.dropped_messages,
        }
