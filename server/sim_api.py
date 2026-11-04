"""Authenticated HTTP adapter for the isolated simulation runtime.

This is an unapproved proposal, not physical robot authorization. Neither the
Bearer token nor a loopback address proves robot identity or physical state.
"""
import hmac

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from server.coordinator import CoordinatorError
from server.order_publisher import OrderPublishError, OrderPublishingDisabled
from server.progress_publisher import ProgressPublishingError


class _NoStoreRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request):
            try:
                response = await original(request)
            except HTTPException as error:
                response = JSONResponse(status_code=error.status_code,
                                        content={"detail": error.detail},
                                        headers=error.headers)
            except RequestValidationError:
                # Validation exceptions can include sensitive submitted data.
                response = JSONResponse(status_code=422, content={"detail": "invalid simulation request"})
            response.headers["Cache-Control"] = "no-store"
            return response
        return handle


router = APIRouter(prefix="/simulation", route_class=_NoStoreRoute)


def _runtime(request: Request, _credentials=Security(HTTPBearer(auto_error=False))):
    peer = request.client.host if request.client is not None else None
    if peer not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="local simulation access required")
    token = getattr(request.app.state, "sim_api_token", None)
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=503, detail="simulation unavailable")
    auth = request.headers.getlist("authorization")
    try:
        ok = (len(auth) == 1 and auth[0].startswith("Bearer ")
              and hmac.compare_digest(auth[0][7:].encode("utf-8"), token.encode("utf-8")))
    except (UnicodeError, TypeError):
        ok = False
    if not ok:
        raise HTTPException(status_code=401, detail="authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    runtime = getattr(request.app.state, "sim_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="simulation unavailable")
    return runtime


def _invoke(runtime, method, *args):
    try:
        return getattr(runtime, method)(*args)
    except (ValueError, CoordinatorError, OrderPublishError, OrderPublishingDisabled,
            ProgressPublishingError):
        raise HTTPException(status_code=409, detail="simulation request rejected") from None
    except Exception:
        raise HTTPException(status_code=503, detail="simulation unavailable") from None


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Candidate(_Model):
    robot_id: StrictStr
    node: StrictStr
    available: StrictBool


class _Prepare(_Model):
    pickup_node: StrictStr
    candidates: list[_Candidate]
    action: dict


class _Session(_Model):
    session: StrictStr


class _RecoveryComplete(_Model):
    session: StrictStr
    challenge: StrictStr
    robot_id: StrictStr
    order_id: StrictStr
    seq: StrictInt = Field(ge=1)
    map_id: StrictStr
    stopped_at: StrictStr
    order_digest: StrictStr
    stopped: StrictBool


@router.get("/status")
def status(runtime=Depends(_runtime)):
    return _invoke(runtime, "status")


@router.post("/tasks/{task_id}")
def create_task(task_id: str, runtime=Depends(_runtime)):
    return _invoke(runtime, "create_task", task_id)


@router.get("/tasks/{task_id}")
def task_status(task_id: str, runtime=Depends(_runtime)):
    return _invoke(runtime, "task_status", task_id)


@router.post("/tasks/{task_id}/prepare")
def prepare(task_id: str, body: _Prepare, runtime=Depends(_runtime)):
    return _invoke(runtime, "prepare", task_id, body.pickup_node,
                   [candidate.model_dump() for candidate in body.candidates], body.action)


@router.post("/tasks/{task_id}/publish")
def publish(task_id: str, runtime=Depends(_runtime)):
    return _invoke(runtime, "publish", task_id)


@router.post("/recovery/{task_id}/begin")
def begin(task_id: str, body: _Session, runtime=Depends(_runtime)):
    return _invoke(runtime, "begin", task_id, body.session)


@router.post("/recovery/{task_id}/complete")
def complete(task_id: str, body: _RecoveryComplete, runtime=Depends(_runtime)):
    return _invoke(runtime, "complete", task_id, body.session, body.challenge,
                   body.robot_id, body.order_id, body.seq, body.map_id,
                   body.stopped_at, body.order_digest, body.stopped)


@router.post("/recovery/{task_id}/reset-unallocated")
def reset_unallocated(task_id: str, body: _Session, runtime=Depends(_runtime)):
    return _invoke(runtime, "reset_unallocated", task_id, body.session)


@router.get("/events/pending")
def pending_events(runtime=Depends(_runtime)):
    return _invoke(runtime, "pending_events")


@router.post("/events/{robot_id}/{event_id}/retry")
def retry(robot_id: str, event_id: str, runtime=Depends(_runtime)):
    return _invoke(runtime, "retry", robot_id, event_id)


@router.post("/orders/{task_id}/{seq}/submit")
def submit(task_id: str, body: _Session, seq: int = Path(ge=2), runtime=Depends(_runtime)):
    return _invoke(runtime, "submit", task_id, seq, body.session)
