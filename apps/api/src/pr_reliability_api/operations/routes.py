"""Private administrator operations dashboard and durable drain control."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..dashboard.routes import _SECURITY_HEADERS, _web_asset
from ..reviewer import request_reviewer
from .store import OperationsStore


def create_operations_router(settings, connection_factory, *, queue="pr-review", sessions=None):
    router = APIRouter()
    store = OperationsStore(connection_factory)

    @router.get("/operations", response_class=HTMLResponse, include_in_schema=False)
    def page():
        return HTMLResponse(
            _web_asset("operations.html").read_text(encoding="utf-8"), headers=_SECURITY_HEADERS
        )

    @router.get("/operations/assets/operations.js", include_in_schema=False)
    def script():
        return Response(
            _web_asset("operations.js").read_text(encoding="utf-8"),
            media_type="text/javascript",
            headers=_SECURITY_HEADERS,
        )

    @router.get("/api/operations/overview")
    def overview(request: Request):
        principal = request_reviewer(request, settings, sessions, admin=True)
        return JSONResponse(
            jsonable_encoder(store.snapshot(settings.owner_id, queue, principal.repository_ids)),
            headers=_SECURITY_HEADERS,
        )

    @router.get("/api/operations/metrics")
    def metrics(request: Request):
        principal = request_reviewer(request, settings, sessions, admin=True)
        snapshot = store.snapshot(settings.owner_id, queue, principal.repository_ids)
        # Names and the sole label are fixed/bounded. Never expose runner, PR, path or run labels.
        names = (
            "queue_observation_unknown",
            "queue_depth",
            "current_wait_seconds",
            "p50_wait_seconds",
            "p95_wait_seconds",
            "active_workers",
            "active_capacity",
            "active_slots",
            "utilization",
            "job_pass_rate",
            "pass_rate_samples",
            "wait_samples",
            "unknown_wait_runs",
        )
        lines = []
        for name in names:
            value = snapshot[name]
            if value is not None:
                lines.extend(
                    (
                        f"# TYPE pr_operations_{name} gauge",
                        f'pr_operations_{name}{{queue="{queue}"}} {float(value)}',
                    )
                )
        return Response("\n".join(lines) + "\n", media_type="text/plain", headers=_SECURITY_HEADERS)

    @router.post("/api/operations/runners/{runner_id}/drain", status_code=202)
    def drain(request: Request, runner_id: Annotated[str, Path(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]):
        request_reviewer(request, settings, sessions, admin=True)
        if not store.request_drain(settings.owner_id, runner_id):
            raise HTTPException(404, "Runner not found")
        return {"drain_requested": True}

    return router
