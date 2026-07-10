"""Targeted WebSocket connection registry.

A single shared `manager` instance is imported by both the `/ws/{display_id}` socket
handler and the remote-command push path — re-instantiating `ConnectionManager` in a
second module would silently split the registry, so this is the one true home.
"""

import logging
from typing import Dict, List

from fastapi import WebSocket

logger = logging.getLogger("artwork-display-api")


class ConnectionManager:
    """Manages targeted WebSocket connections grouped by display_id."""
    def __init__(self):
        # Maps display_id -> list of active WebSocket connections
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, display_id: str):
        await websocket.accept()
        if display_id not in self.active_connections:
            self.active_connections[display_id] = []
        self.active_connections[display_id].append(websocket)
        logger.info(f"New connection to display '{display_id}'. Total for ID: {len(self.active_connections[display_id])}")

    def disconnect(self, websocket: WebSocket, display_id: str):
        if display_id in self.active_connections:
            if websocket in self.active_connections[display_id]:
                self.active_connections[display_id].remove(websocket)
                if not self.active_connections[display_id]:
                    del self.active_connections[display_id]
            logger.info(f"Disconnected from display '{display_id}'.")

    async def send_personal_message(self, message: dict, display_id: str):
        """Sends a JSON message only to sockets registered under a specific display_id."""
        if display_id in self.active_connections:
            for connection in self.active_connections[display_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass

    async def broadcast(self, message: dict):
        """Sends a JSON message to absolutely all connected clients."""
        for display_id in self.active_connections:
            for connection in self.active_connections[display_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass


manager = ConnectionManager()
