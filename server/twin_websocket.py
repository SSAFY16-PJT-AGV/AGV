"""Opt-in local, observation-only prototype for simulated digital twins."""
import asyncio
import ipaddress
import json
import math
import os
import re
import secrets
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from server.order_demo_actions import DemoActionSession

router = APIRouter()
POLL_SECONDS = 0.1
MAX_OBSERVATION_AGE_SECONDS = 1.0
_TOKEN_FORMAT = re.compile(r'[A-Za-z0-9_-]{32,128}\Z', re.ASCII)
_VALIDATION_MAP_ID = 'twin-validation-v1'
_MAX_VALIDATION_COORDINATE_MM = 1000000
_MAX_VALIDATION_YAW_DEGREES = 3600
_SIM_ID = re.compile(r'sim-[A-Za-z0-9_-]{1,60}\Z', re.ASCII)
_TWIN_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z', re.ASCII)


def _invalidate_action_owner(actions, client):
    invalidate = getattr(actions, 'invalidate_owner', None)
    if callable(invalidate):
        invalidate(client)


async def _action_call(fn, *args, on_cancel=None):
    """Keep blocking action work off the ASGI loop without abandoning it.

    A cancelled coroutine waits for its in-flight synchronous action to end;
    the caller then performs the normal disconnect/unknown cleanup. In
    particular a late poll must never mark an unsent action as sent after
    ownership has been handed to a new connection.
    """
    future = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        try:
            await future
        except Exception:
            pass
        raise


async def _attach_action(actions, client, trusted):
    # A cancellation can occur while attach is waiting for the shared lock.
    # If it then succeeds, undo ownership before propagating cancellation.
    future = asyncio.create_task(asyncio.to_thread(actions.attach, client, trusted))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        try:
            if await future:
                _invalidate_action_owner(actions, client)
                await _action_call(actions.detach, client)
        except Exception:
            pass
        raise


def _poll_action(actions, client):
    if isinstance(actions, DemoActionSession):
        # DemoActionSession captures the clock after acquiring its lock.
        return actions.poll(client)
    # Retain compatibility with independent test/adapter implementations.
    return actions.poll(client, time.monotonic())


def _robot_mapping():
    """Return explicit MQTT-to-Unreal IDs, or reject an invalid configuration."""
    raw = os.getenv('AGV_TWIN_ROBOT_MAP')
    if raw is None:
        robot_id = os.getenv('AGV_TWIN_ROBOT_ID', '')
        return {robot_id: robot_id} if robot_id.startswith('sim-') else None
    if len(raw) > 4096:
        return None

    def unique_pairs(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError('duplicate robot ID')
            values[key] = value
        return values

    try:
        mapping = json.loads(raw, object_pairs_hook=unique_pairs)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(mapping, dict) or len(mapping) != 2:
        return None
    if any(not isinstance(key, str) or _SIM_ID.fullmatch(key) is None
           or not isinstance(value, str) or _TWIN_ID.fullmatch(value) is None
           for key, value in mapping.items()):
        return None
    if len(set(mapping.values())) != 2:
        return None
    return mapping


def _allowed_origin(headers):
    origins = headers.getlist('origin')
    if not origins:
        return True  # Preserve the existing local non-browser client policy.
    configured = os.getenv('AGV_TWIN_UNREAL_TOKEN', '')
    supplied = headers.getlist('x-agv-twin-token')
    return (origins == ['http://127.0.0.1'] and len(supplied) == 1
            and _TOKEN_FORMAT.fullmatch(configured) is not None
            and _TOKEN_FORMAT.fullmatch(supplied[0]) is not None
            and secrets.compare_digest(configured, supplied[0]))


def _message(robot_id, observation):
    if not isinstance(observation, dict) or not observation.get('state_received_live'):
        return None
    state = observation.get('state')
    if not isinstance(state, dict):
        return None
    data = {'agv_id': robot_id}
    for source, destination in (('last_node', 'current_node'), ('status', 'status')):
        value = state.get(source)
        if isinstance(value, str) and value:
            data[destination] = value
    battery = state.get('battery')
    if type(battery) in (int, float) and abs(battery) <= 1e308 and math.isfinite(battery):
        data['battery'] = battery
    if set(data) != {'agv_id', 'current_node', 'status', 'battery'}:
        return None
    pose_map_id = os.getenv('AGV_TWIN_POSE_MAP_ID', '')
    if pose_map_id:
        # This mapping is only calibrated for the isolated validation map.
        if pose_map_id != _VALIDATION_MAP_ID or state.get('map_id') != pose_map_id:
            return None
        pose = state.get('pose')
        if not isinstance(pose, dict):
            return None
        values = [pose.get('x'), pose.get('y'), pose.get('th')]
        limits = [_MAX_VALIDATION_COORDINATE_MM, _MAX_VALIDATION_COORDINATE_MM,
                  _MAX_VALIDATION_YAW_DEGREES]
        if any(type(value) not in (int, float) or abs(value) > limit
               or not math.isfinite(value) for value, limit in zip(values, limits)):
            return None
        data.update(x=values[0] / 1000, y=values[1] / 1000, yaw=values[2])
    return {'type': 'robot.state', 'data': data}


@router.websocket('/ws/digital-twin')
async def digital_twin(websocket: WebSocket):
    mapping = _robot_mapping()
    try:
        local_client = ipaddress.ip_address(websocket.client.host).is_loopback
    except (AttributeError, ValueError):
        local_client = False
    if (os.getenv('AGV_TWIN_WEBSOCKET') != '1' or mapping is None
            or not local_client or not _allowed_origin(websocket.headers)):
        await websocket.close(code=1008)
        return
    service = getattr(websocket.app.state, 'mqtt_service', None)
    if service is None:
        await websocket.close(code=1011)
        return
    actions = getattr(websocket.app.state, 'demo_actions', None)
    claimed = False
    try:
        await websocket.accept()
        if actions is not None:
            trusted = (websocket.headers.getlist('origin') == ['http://127.0.0.1']
                       and _allowed_origin(websocket.headers))
            claimed = await _attach_action(actions, websocket, trusted)
        revisions = {robot_id: service.twin_observation(robot_id)['revision']
                     for robot_id in mapping}
        while True:
            try:
                event = await asyncio.wait_for(websocket.receive(), timeout=POLL_SECONDS)
                if event['type'] == 'websocket.disconnect':
                    return
                text = event.get('text')
                if claimed and isinstance(text, str) and len(text) <= 4096:
                    try:
                        await _action_call(actions.receive, websocket, json.loads(text),
                                           on_cancel=lambda: _invalidate_action_owner(actions, websocket))
                    except (ValueError, TypeError, RecursionError):
                        pass
            except asyncio.TimeoutError:
                pass
            for robot_id, twin_id in mapping.items():
                snapshot = service.twin_observation(robot_id)
                current_revision = snapshot['revision']
                if current_revision == revisions[robot_id]:
                    continue
                revisions[robot_id] = current_revision
                received_at = snapshot['received_at']
                if (not snapshot['connected'] or not snapshot['ready'] or received_at is None
                        or not 0 <= time.monotonic() - received_at <= MAX_OBSERVATION_AGE_SECONDS):
                    continue
                payload = _message(robot_id, snapshot['observation'])
                if payload is not None:
                    payload['data']['agv_id'] = twin_id
                    await websocket.send_json(payload)
            if claimed:
                action = await _action_call(_poll_action, actions, websocket,
                                            on_cancel=lambda: _invalidate_action_owner(actions, websocket))
                if action is not None:
                    await websocket.send_json(action)
    except WebSocketDisconnect:
        return
    finally:
        if claimed:
            _invalidate_action_owner(actions, websocket)
            await _action_call(actions.detach, websocket)
