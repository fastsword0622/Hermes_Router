from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .specification import WorkSpec
from .workspace import Edit

router = APIRouter(prefix="/v1/jobs", tags=["development"])


class MockStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[Edit] = Field(default_factory=list, max_length=50)
    exit_code: int = Field(default=0, ge=-255, le=255)
    stdout: str = Field(default="mock execution", max_length=8192)
    stderr: str = Field(default="", max_length=8192)


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=100_000)
    repository: str = Field(min_length=1)
    mode: Literal["mock", "hermes"] = "mock"
    max_attempts: int = Field(default=10, ge=1, le=30)
    timeout_seconds: float = Field(default=300, gt=0, le=3600)
    mock_plan: list[MockStep] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def mock_only(self):
        if self.mode != "mock" and self.mock_plan:
            raise ValueError("mock plan is allowed only in mock mode")
        return self


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=200)


def store(request: Request):
    return request.app.state.development.store


@router.post("", status_code=201)
def create(payload: JobRequest, request: Request):
    return store(request).create(payload.prompt, payload.repository, payload.mode, max_attempts=payload.max_attempts,
        timeout_seconds=payload.timeout_seconds, mock_plan=[s.model_dump(mode="json") for s in payload.mock_plan])


@router.get("")
def listing(request: Request):
    return {"items": store(request).list()}


@router.get("/{job_id}")
def get(job_id: UUID, request: Request):
    return store(request).get(str(job_id))


@router.put("/{job_id}/specification")
def set_spec(job_id: UUID, payload: WorkSpec, request: Request):
    return store(request).set_spec(str(job_id), payload)


@router.post("/{job_id}/approve")
def approve(job_id: UUID, payload: ApprovalRequest, request: Request):
    return store(request).approve(str(job_id), payload.version, payload.digest, payload.actor)


@router.get("/{job_id}/events")
def events(job_id: UUID, request: Request):
    return {"items": store(request).events(str(job_id))}


class AnalysisRequest(BaseModel):
    provider: Literal["minimax", "glm"] = "minimax"


@router.post("/{job_id}/analyze")
def analyze(job_id: UUID, payload: AnalysisRequest, request: Request):
    return request.app.state.development.analyze(str(job_id), payload.provider)


@router.post("/{job_id}/start", status_code=202)
def start(job_id: UUID, request: Request):
    return request.app.state.development.runner.start(str(job_id))


@router.post("/{job_id}/cancel")
def cancel(job_id: UUID, request: Request):
    return request.app.state.development.runner.cancel(str(job_id))


class RiskApproval(BaseModel):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=200)


@router.post("/{job_id}/approve-risk")
def approve_risk(job_id: UUID, payload: RiskApproval, request: Request):
    return request.app.state.development.runner.approve_risk(str(job_id), payload.digest, payload.actor)


class ResumeRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    acknowledge_possible_side_effects: bool = False


@router.post("/{job_id}/resume")
def resume(job_id: UUID, payload: ResumeRequest, request: Request):
    return request.app.state.development.runner.resume(str(job_id), payload.actor, payload.acknowledge_possible_side_effects)


@router.get("/{job_id}/report")
def report(job_id: UUID, request: Request):
    job = store(request).get(str(job_id))
    return {"status": job["status"], "report": job["report"], "attempts": job["attempts"], "switches": job["switches"]}
