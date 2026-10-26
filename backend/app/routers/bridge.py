"""WebSocket live bridge (BRIDGE-1).

- `/ws/bridge/{case_id}`: chat events for one case (`{"type": "chat_message", ...}`).
- `/ws/cases`: every case create/update (`{"type": "case_upserted", ...}`), filtered to the
  cases the caller may see.

Both check `auth.authorize_websocket` (the `token` query parameter); on refusal they accept and
then close with 1008, so the browser sees the 1008 (a close before accept() shows up as 1006). Events arrive through `app.services.events_service`, which delivers them
in-process (mock mode) or from this task's SNS-subscribed SQS queue (Floci / AWS). Clients may
send `ping` (or `{"type": "ping"}`) and get `{"type": "pong"}`; other client input is ignored,
so a browser can't inject chat messages that were never saved.
"""
import json
from typing import Dict, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import auth
from app.services.dynamo_service import dynamo_service

router = APIRouter(tags=["WebSocket Live Chat Bridge"])


class ConnectionManager:
    def __init__(self):
        # case_id -> sockets on /ws/bridge/{case_id}
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # sockets on /ws/cases
        self.case_list_connections: List[WebSocket] = []

    async def connect(self, case_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(case_id, []).append(websocket)

    def disconnect(self, case_id: str, websocket: WebSocket):
        conns = self.active_connections.get(case_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self.active_connections.pop(case_id, None)

    async def connect_case_list(self, websocket: WebSocket):
        await websocket.accept()
        self.case_list_connections.append(websocket)

    def disconnect_case_list(self, websocket: WebSocket):
        if websocket in self.case_list_connections:
            self.case_list_connections.remove(websocket)

    async def _send(self, websocket: WebSocket, message: dict) -> bool:
        try:
            await websocket.send_text(json.dumps(message))
            return True
        except Exception:
            return False

    async def broadcast_to_case(self, case_id: str, message: dict):
        for connection in list(self.active_connections.get(case_id, [])):
            if not await self._send(connection, message):
                self.disconnect(case_id, connection)

    async def broadcast_case_update(self, message: dict):
        case_data = message.get("case")
        for connection in list(self.case_list_connections):
            if case_data is not None and not _visible(connection, case_data):
                continue
            if not await self._send(connection, message):
                self.disconnect_case_list(connection)

    async def deliver_event(self, event: dict):
        """Entry point for events_service: routes an event to the right sockets."""
        kind = event.get("type")
        if kind == "chat_message":
            await self.broadcast_to_case(event.get("case_id"), event)
        elif kind == "case_upserted":
            await self.broadcast_case_update(event)


manager = ConnectionManager()


def _visible(websocket: WebSocket, case_data: dict) -> bool:
    """Doctors only get events for their own cases (auth.visible_cases reads state.principal)."""
    try:
        from app.models.schemas import EmergencyCase
        return bool(auth.visible_cases(websocket, [EmergencyCase(**case_data)]))
    except Exception:
        return False


async def _refuse(websocket: WebSocket, reason: str) -> None:
    """Accept, then close with 1008. Closing before accept() rejects the HTTP upgrade, which a
    browser only reports as 1006 (handshake failed), so the client could not tell a refusal
    from a network error and would reconnect forever."""
    try:
        await websocket.accept()
        await websocket.close(code=1008, reason=reason)
    except Exception:
        pass


async def _authorize(websocket: WebSocket):
    principal = await auth.authorize_websocket(websocket)
    if principal is None:
        await _refuse(websocket, "unauthorized")
        return None
    websocket.state.principal = principal
    return principal


async def _keepalive(websocket: WebSocket):
    """Reads client frames until disconnect; answers pings."""
    while True:
        data = await websocket.receive_text()
        text = data.strip()
        is_ping = text.lower() == "ping"
        if not is_ping:
            try:
                is_ping = json.loads(text).get("type") == "ping"
            except Exception:
                is_ping = False
        if is_ping:
            await websocket.send_text(json.dumps({"type": "pong"}))


@router.websocket("/ws/bridge/{case_id}")
async def websocket_case_bridge(websocket: WebSocket, case_id: str):
    """Live chat stream of one emergency case for the doctor's portal."""
    principal = await _authorize(websocket)
    if principal is None:
        return
    case = dynamo_service.get_case(case_id)
    if case is None and not getattr(principal, "is_admin", False):
        await _refuse(websocket, "case not found")
        return
    if case is not None:
        try:
            auth.assert_case_visible(websocket, case)
        except Exception:
            await _refuse(websocket, "case not visible")
            return
    await manager.connect(case_id, websocket)
    try:
        await _keepalive(websocket)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        manager.disconnect(case_id, websocket)


@router.websocket("/ws/cases")
async def websocket_case_list(websocket: WebSocket):
    """Portal-wide stream of created/updated cases (replaces case-list polling)."""
    if await _authorize(websocket) is None:
        return
    await manager.connect_case_list(websocket)
    try:
        await _keepalive(websocket)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        manager.disconnect_case_list(websocket)
