"""FastAPI application for the Phase 1 task API.

Reads stay usable while background work runs: GET/list only touch the task
database, never the graph or the provider, and the worker threads own their own
SQLite connections.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, ExitStack
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .config import Settings, get_settings
from .models import (
    CreateTaskRequest,
    HealthResponse,
    TaskCreated,
    TaskList,
    TaskStatus,
    TaskView,
    ProviderInfo,
)
from .service import ProviderNotAvailable, TaskService
from .store import TaskNotFound
from .development import DevelopmentService, JobConflict, JobNotFound
from .development_api import router as development_router
from .operations import LocalApiBoundary, administrator_token, observed_metrics, rotating_log
from .redaction import redact_text

__all__ = ["create_app", "app", "get_service"]

logger = logging.getLogger("hermes_router.api")


def get_service(request: Request) -> TaskService:
    """FastAPI dependency resolving the process-wide service instance."""
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - lifespan guarantees this
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="service not ready"
        )
    return service


def create_app(
    settings: Settings | None = None,
    *,
    service: TaskService | None = None,
    recover: bool = True,
) -> FastAPI:
    """Build the ASGI application.

    ``service`` may be injected directly (tests, and the explicit opt-in path for
    a real Hermes adapter).  Otherwise a default mock-only service is built.
    """
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved.ensure_dirs()
        app.state.admin_token = administrator_token(resolved)
        with ExitStack() as stack:
            handler = rotating_log(resolved)
            def close_log():
                logging.getLogger("hermes_router").removeHandler(handler)
                handler.close()
            stack.callback(close_log)
            svc = service or TaskService(resolved)
            app.state.service = svc
            stack.callback(svc.close)
            factory = None
            if resolved.allow_real_providers:
                from .hermes_runtime import HermesFactory
                factory = HermesFactory(resolved.data_dir, executable=resolved.hermes_executable, credentials_path=resolved.hermes_credentials)
            app.state.development = DevelopmentService(resolved.data_dir, resolved.allowed_repositories, factory)
            stack.callback(app.state.development.close)
            if recover:
                svc.recover_orphaned_tasks()
            yield

    app = FastAPI(
        title="Hermes Development Router",
        version=resolved.version,
        summary="Approved development jobs with isolated worktrees and verified provider escalation.",
        lifespan=lifespan,
    )

    # -- error handling -----------------------------------------------------
    app.add_middleware(LocalApiBoundary, state=app.state, max_bytes=resolved.max_request_bytes)

    @app.exception_handler(RequestValidationError)
    async def safe_validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": [{"loc": e["loc"], "type": e["type"],
            "msg": redact_text(e["msg"])} for e in exc.errors()]})

    @app.get("/v1/metrics", tags=["operations"])
    def metrics(request: Request, mode: str | None = None):
        if mode not in {None, "mock", "hermes"}:
            raise HTTPException(400, "mode must be mock or hermes")
        return observed_metrics(request.app.state.development.store, mode)

    @app.exception_handler(JobNotFound)
    async def job_not_found(_: Request, exc: JobNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(JobConflict)
    async def job_conflict(_: Request, exc: JobConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    app.include_router(development_router)

    def provider_details(svc):
        details = svc.describe_providers()
        factory = getattr(getattr(app.state, "development", None), "provider_factory", None)
        configured = factory.available_providers() if factory else set()
        for provider in details:
            if provider["is_real"]:
                provider["available"] = provider["name"] in configured
        return details

    @app.exception_handler(TaskNotFound)
    async def _task_not_found(_: Request, exc: TaskNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)}
        )

    @app.exception_handler(ProviderNotAvailable)
    async def _provider_unavailable(_: Request, exc: ProviderNotAvailable) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)}
        )

    # -- routes -------------------------------------------------------------

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    def health(svc: Annotated[TaskService, Depends(get_service)]) -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=resolved.version,
            default_provider=resolved.default_provider,
            providers=[ProviderInfo(**info) for info in provider_details(svc)],
            task_db=str(svc.store.path),
            checkpoint_db=str(svc.checkpointer.path),
            store_open=not svc.store.is_closed,
            checkpoints_open=not svc.checkpointer.is_closed,
            active_dispatches=svc.active_dispatches,
            allow_real_providers=resolved.allow_real_providers,
            details={
                "recovered_task_ids": svc.recovered_task_ids,
                "allow_real_providers": resolved.allow_real_providers,
            },
        )

    @app.get("/v1/providers", tags=["meta"])
    def list_providers(
        svc: Annotated[TaskService, Depends(get_service)],
    ) -> dict[str, Any]:
        return {"default": resolved.default_provider, "providers": provider_details(svc),
                "availability_check": "configured credentials; not a network probe"}

    @app.post(
        "/v1/tasks",
        response_model=TaskCreated,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["tasks"],
    )
    def create_task(
        payload: CreateTaskRequest,
        response: Response,
        svc: Annotated[TaskService, Depends(get_service)],
    ) -> TaskCreated:
        task = svc.create_task(payload)
        response.headers["Location"] = f"/v1/tasks/{task.id}"
        return TaskCreated(
            id=task.id,
            status=task.status,
            provider=task.provider,
            created_at=task.created_at,
            location=f"/v1/tasks/{task.id}",
        )

    @app.get("/v1/tasks", response_model=TaskList, tags=["tasks"])
    def list_tasks(
        svc: Annotated[TaskService, Depends(get_service)],
        task_status: Annotated[
            TaskStatus | None, Query(alias="status")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> TaskList:
        return svc.list_tasks(status=task_status, limit=limit, offset=offset)

    @app.get("/v1/tasks/{task_id}", response_model=TaskView, tags=["tasks"])
    def get_task(
        svc: Annotated[TaskService, Depends(get_service)],
        task_id: Annotated[uuid.UUID, Path()],
    ) -> TaskView:
        return TaskView.from_task(svc.get_task(task_id))

    @app.get("/v1/tasks/{task_id}/attempts", tags=["tasks"])
    def get_attempts(
        svc: Annotated[TaskService, Depends(get_service)],
        task_id: Annotated[uuid.UUID, Path()],
    ) -> dict[str, Any]:
        task = svc.get_task(task_id)
        return {
            "task_id": str(task.id),
            "attempts_used": task.attempts_used,
            "max_attempts": task.max_attempts,
            "attempts": [a.model_dump(mode="json") for a in svc.store.get_attempts(task.id)],
        }

    @app.post("/v1/tasks/{task_id}/cancel", response_model=TaskView, tags=["tasks"])
    def cancel_task(
        svc: Annotated[TaskService, Depends(get_service)],
        task_id: Annotated[uuid.UUID, Path()],
    ) -> TaskView:
        return TaskView.from_task(svc.cancel_task(task_id))

    return app


#: Module-level app for ``uvicorn hermes_router.api:app``.  Its lifespan builds a
#: default mock-only service.
app = create_app()
