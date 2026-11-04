from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from server.mqtt_client import start_mqtt, stop_mqtt


@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_service = start_mqtt()
    app.state.mqtt_service = mqtt_service
    try:
        yield
    finally:
        stop_mqtt(mqtt_service)


app = FastAPI(title="스마트 마트 관제 서버", lifespan=lifespan)


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
