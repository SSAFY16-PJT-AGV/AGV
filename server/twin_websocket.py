"""Opt-in local, observation-only prototype for a single digital twin robot."""
import asyncio
import ipaddress
import math
import os
import re
import secrets
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
POLL_SECONDS = 0.1
MAX_OBSERVATION_AGE_SECONDS = 1.0
_TOKEN_FORMAT = re.compile(r'[A-Za-z0-9_-]{32,128}\Z', re.ASCII)
_VALIDATION_MAP_ID = 'twin-validation-v1'
_MAX_VALIDATION_COORDINATE_MM = 1000000
_MAX_VALIDATION_YAW_DEGREES = 3600


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
    robot_id = os.getenv('AGV_TWIN_ROBOT_ID', '')
    try:
        local_client = ipaddress.ip_address(websocket.client.host).is_loopback
    except (AttributeError, ValueError):
        local_client = False
    if (os.getenv('AGV_TWIN_WEBSOCKET') != '1' or not robot_id.startswith('sim-')
            or not local_client or not _allowed_origin(websocket.headers)):
        await websocket.close(code=1008)
        return
    service = getattr(websocket.app.state, 'mqtt_service', None)
    if service is None:
        await websocket.close(code=1011)
        return
    try:
        revision = service.twin_observation(robot_id)['revision']
        await websocket.accept()
        while True:
            try:
                event = await asyncio.wait_for(websocket.receive(), timeout=POLL_SECONDS)
                if event['type'] == 'websocket.disconnect':
                    return
            except asyncio.TimeoutError:
                pass
            snapshot = service.twin_observation(robot_id)
            current_revision = snapshot['revision']
            if current_revision == revision:
                continue
            revision = current_revision
            received_at = snapshot['received_at']
            if (not snapshot['connected'] or not snapshot['ready'] or received_at is None
                    or not 0 <= time.monotonic() - received_at <= MAX_OBSERVATION_AGE_SECONDS):
                continue
            payload = _message(robot_id, snapshot['observation'])
            if payload is not None:
                await websocket.send_json(payload)
    except WebSocketDisconnect:
        return
