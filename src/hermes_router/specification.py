"""Versioned, evidence-oriented development specifications."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .redaction import redact_text


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Requirement(FrozenModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=4000)


class VerificationCommand(FrozenModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    argv: tuple[str, ...] = Field(min_length=1, max_length=100)
    timeout_seconds: float = Field(default=60, gt=0, le=600)

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value):
        if any("\x00" in x for x in value) or not value[0].strip():
            raise ValueError("command contains invalid argument")
        return value


class AcceptanceCriterion(FrozenModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=4000)
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    verification_command_ids: tuple[str, ...] = Field(min_length=1)
    required_files: tuple[str, ...] = ()


class WorkSpec(FrozenModel):
    objective: str = Field(min_length=1, max_length=8000)
    functional_requirements: tuple[Requirement, ...] = Field(min_length=1)
    non_functional_requirements: tuple[Requirement, ...] = Field(min_length=1)
    allowed_files: tuple[str, ...] = Field(min_length=1)
    forbidden_changes: tuple[str, ...] = Field(min_length=1)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    verification_commands: tuple[VerificationCommand, ...] = Field(min_length=1)
    risk_level: Literal["low", "medium", "high"]

    @field_validator("allowed_files")
    @classmethod
    def safe_paths(cls, value):
        for pattern in value:
            p = PurePosixPath(pattern)
            if not pattern.strip() or p.is_absolute() or ".." in p.parts or any(x.casefold() == ".git" for x in p.parts) or "\\" in pattern or "\x00" in pattern:
                raise ValueError("allowed files must be relative paths outside Git metadata")
        return value

    @model_validator(mode="after")
    def complete_evidence(self):
        requirements = [r.id for r in (*self.functional_requirements, *self.non_functional_requirements)]
        commands = [c.id for c in self.verification_commands]
        criteria = [c.id for c in self.acceptance_criteria]
        if any(len(x) != len(set(x)) for x in (requirements, commands, criteria)):
            raise ValueError("requirement, command and criterion IDs must be unique")
        covered = set()
        for criterion in self.acceptance_criteria:
            if not set(criterion.requirement_ids) <= set(requirements):
                raise ValueError("criterion references an unknown requirement")
            if not set(criterion.verification_command_ids) <= set(commands):
                raise ValueError("criterion references an unknown command")
            covered.update(criterion.requirement_ids)
        if covered != set(requirements):
            raise ValueError("each requirement needs an acceptance criterion and verification command")
        for criterion in self.acceptance_criteria:
            for path in criterion.required_files:
                self.safe_paths((path,))
        for rule in self.forbidden_changes:
            if rule.startswith("check:"):
                if rule[6:] not in commands:
                    raise ValueError("forbidden change check references an unknown command")
            elif not (rule.startswith("path:") and rule[5:]) and rule.lower() not in {
                "do not change dependencies", "do not change tests"}:
                raise ValueError("ambiguous forbidden change: use path:pattern or check:command_id")
        data = self.model_dump_json()
        if redact_text(data) != data:
            raise ValueError("specification contains a credential; use environment variable references")
        return self

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


QUESTIONS = {
    "functional_requirements": "어떤 기능과 동작을 구현해야 합니까?",
    "non_functional_requirements": "성능·호환성·품질 조건은 무엇입니까?",
    "allowed_files": "수정을 허용할 저장소 상대 경로 또는 패턴은 무엇입니까?",
    "forbidden_changes": "금지할 변경은 무엇입니까?",
    "acceptance_criteria": "각 요구사항의 수락 기준과 검증 명령 연결은 무엇입니까?",
    "verification_commands": "실행을 승인할 검증 명령의 argv와 시간 제한은 무엇입니까?",
    "risk_level": "위험 수준은 low, medium, high 중 무엇입니까?",
}


def analyze_request(prompt: str) -> dict:
    """Conservative local analysis; unknowns become explicit questions, never guesses."""
    prompt = redact_text(prompt.strip())
    try:
        candidate = json.loads(prompt)
    except (ValueError, TypeError):
        candidate = {"objective": prompt}
    if not isinstance(candidate, dict):
        candidate = {"objective": prompt}
    candidate.setdefault("objective", prompt)
    missing = [k for k in QUESTIONS if not candidate.get(k)]
    return {"draft": candidate, "questions": [{"field": k, "question": QUESTIONS[k]} for k in missing]}
