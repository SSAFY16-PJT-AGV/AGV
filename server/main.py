from contextlib import asynccontextmanager

from fastapi import FastAPI

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
