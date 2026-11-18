from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from server.mqtt_client import start_mqtt, stop_mqtt
from server.sim_api import router as sim_router
from server.twin_websocket import router as twin_router


def load_sim_runtime():
    """Load explicitly configured isolated simulation; no connection occurs here."""
    config_path = os.environ.get('AGV_SIM_CONFIG')
    if not config_path:
        return None
    token = os.environ.get('AGV_SIM_API_TOKEN', '')
    if not (32 <= len(token) <= 512 and all(33 <= ord(c) <= 126 for c in token)):
        raise ValueError('simulation API token must be 32 to 512 printable ASCII characters')
    path = Path(config_path).resolve()
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError('simulation configuration is too large')
    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                raise ValueError('duplicate simulation configuration field')
            data[key] = value
        return data
    config = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
    required = {'database', 'map_file', 'robots', 'mqtt_port', 'mqtt_client_id'}
    if (not isinstance(config, dict) or not required.issubset(config)
            or set(config) - required - {'field_simulation', 'command_observations'}
            or type(config.get('field_simulation', False)) is not bool
            or type(config.get('command_observations', False)) is not bool
            or (config.get('command_observations', False) and not config.get('field_simulation', False))):
        raise ValueError('invalid simulation configuration fields')
    for key in ('database', 'map_file'):
        if not isinstance(config[key], str) or not config[key].strip() or config[key] == ':memory:':
            raise ValueError('invalid simulation file path')
    db_path = (path.parent / config['database']).resolve()
    map_path = (path.parent / config['map_file']).resolve()
    map_raw = map_path.read_bytes()
    if len(map_raw) > 4 * 1024 * 1024:
        raise ValueError('simulation map is too large')
    map_data = json.loads(map_raw.decode('utf-8'), object_pairs_hook=pairs)
    from server.sim_runtime import SimRuntime
    options = {}
    if config.get('field_simulation', False):
        options['field_simulation'] = True
    if config.get('command_observations', False):
        options['command_observations'] = True
    runtime = SimRuntime(db_path, map_data, config['robots'], port=config['mqtt_port'],
                         client_id=config['mqtt_client_id'], **options)
    return runtime, token


@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_service = start_mqtt()
    app.state.mqtt_service = mqtt_service
    app.state.sim_runtime = None
    app.state.sim_api_token = None
    sim_runtime = None
    try:
        sim_config = load_sim_runtime()
        if sim_config is not None:
            sim_runtime, token = sim_config
            sim_runtime.start()
            app.state.sim_runtime = sim_runtime
            app.state.sim_api_token = token
        yield
    finally:
        app.state.sim_runtime = None
        app.state.sim_api_token = None
        try:
            if sim_runtime is not None:
                sim_runtime.stop()
        finally:
            stop_mqtt(mqtt_service)


app = FastAPI(title="스마트 마트 관제 서버", lifespan=lifespan)
app.include_router(sim_router)
app.include_router(twin_router)


@app.get("/robots")
def robot_observations(request: Request):
    """Return historical MQTT observations, never an authorization to move."""
    try:
        service = request.app.state.mqtt_service
        data = service.snapshot()
        if (not isinstance(data, dict)
                or type(data.get("connected")) is not bool
                or type(data.get("ready")) is not bool
                or not isinstance(data.get("robots"), dict)
                or (data["ready"] and not data["connected"])):
            raise ValueError("invalid observation snapshot")
        result = {key: data[key] for key in ("connected", "ready", "robots")}
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MQTT observations unavailable",
            headers={"Cache-Control": "no-store"},
        ) from None


@app.get("/robots/battery")
def robot_battery_observations(request: Request):
    """Summarize reported battery data without treating it as live evidence."""
    try:
        data = request.app.state.mqtt_service.snapshot()
        if (not isinstance(data, dict)
                or type(data.get("connected")) is not bool
                or type(data.get("ready")) is not bool
                or not isinstance(data.get("robots"), dict)
                or (data["ready"] and not data["connected"])):
            raise ValueError("invalid observation snapshot")
        robots = {}
        for robot_id, observation in data["robots"].items():
            if (not isinstance(robot_id, str) or not isinstance(observation, dict)
                    or type(observation.get("state_received_live")) is not bool
                    or type(observation.get("connection_received_live")) is not bool
                    or observation.get("connection_report") not in (None, "online", "offline")):
                raise ValueError("invalid robot observation")
            state = observation.get("state")
            if state is None:
                status, value = "no_state", None
            elif not isinstance(state, dict):
                raise ValueError("invalid robot state")
            elif "battery" not in state:
                status, value = "not_reported", None
            elif state["battery"] is None:
                status, value = "unknown", None
            elif (type(state["battery"]) is int
                  or (type(state["battery"]) is float and math.isfinite(state["battery"]))):
                status, value = "reported", state["battery"]
            else:
                status, value = "invalid", None
            robots[robot_id] = {"status": status, "value": value,
                                "ratio_range": 0 <= value <= 1 if status == "reported" else None,
                                "state_received_live": observation["state_received_live"],
                                "connection_report": observation["connection_report"],
                                "connection_received_live": observation["connection_received_live"]}
        return JSONResponse(content={"connected": data["connected"], "ready": data["ready"],
                                     "robots": robots}, headers={"Cache-Control": "no-store"})
    except Exception:
        raise HTTPException(status_code=503, detail="MQTT observations unavailable",
                            headers={"Cache-Control": "no-store"}) from None
