"""Git isolation, confined file edits, and read-only macOS verification."""
from __future__ import annotations
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
from typing import Literal
from pydantic import Field, model_validator
from .execution import run_command
from .specification import FrozenModel, WorkSpec


class WorkspaceError(ValueError):
    pass


class Edit(FrozenModel):
    path: str
    content: str | None = Field(default=None, max_length=200_000)


class ChangeProposal(FrozenModel):
    edits: tuple[Edit, ...] = Field(default=(), max_length=50)
    summary: str = Field(default="", max_length=4000)
    requirement_notes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        if sum(len((e.content or "").encode()) for e in self.edits) > 1_000_000:
            raise ValueError("proposal exceeds total byte limit")
        if len({e.path for e in self.edits}) != len(self.edits):
            raise ValueError("duplicate edit path")
        return self


def protected(path: str) -> bool:
    p = PurePosixPath(path)
    return any(x.casefold() in {".git", ".env", ".ssh", ".aws", ".codex", ".agents"} for x in p.parts) or p.suffix.lower() in {".pem", ".key", ".p12"}


def allowed(path: str, spec: WorkSpec) -> bool:
    p = PurePosixPath(path)
    for rule in spec.forbidden_changes:
        patterns = [rule[5:]] if rule.startswith("path:") else []
        if rule.lower() == "do not change dependencies":
            patterns += ["*requirements*.txt", "*pyproject.toml", "*package*.json", "*.lock"]
        if rule.lower() == "do not change tests":
            patterns += ["tests/*", "test_*.py", "*/test_*.py"]
        if any(fnmatch.fnmatchcase(path.casefold(), pattern.casefold()) for pattern in patterns):
            return False
    return (not p.is_absolute() and ".." not in p.parts and not protected(path) and "\\" not in path
        and path not in {"", "."} and any(fnmatch.fnmatchcase(path, pattern) for pattern in spec.allowed_files))


def risky(path: str) -> bool:
    lower = path.lower()
    p = PurePosixPath(lower)
    return (p.suffix in {".db", ".sqlite", ".sqlite3", ".sql", ".lock"}
        or any(token in lower for token in ("migration", "schema", "database", "deploy", ".github/", "launchd"))
        or p.name in {"pyproject.toml", "package.json", "package-lock.json", "requirements.txt", "dockerfile"}
        or path in {"*", "**", "**/*"})


def risk_scope(spec: WorkSpec, base_commit: str) -> dict:
    paths = [p for p in spec.allowed_files if risky(p) or any(c in p for c in "*?[")]
    commands = [c.id for c in spec.verification_commands if Path(c.argv[0]).name in {"sh", "bash", "zsh", "sudo", "env", "osascript", "open", "launchctl"}]
    return {"spec_digest": spec.digest(), "base_commit": base_commit, "paths": paths,
            "commands": commands, "high_risk": spec.risk_level == "high", "required": bool(paths or commands or spec.risk_level == "high")}


def digest_scope(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Workspace:
    def __init__(self, repository: Path, root: Path, job_id: str, allowed_repositories: tuple[Path, ...]):
        self.repository = repository.resolve()
        if self.repository not in [p.resolve() for p in allowed_repositories]:
            raise WorkspaceError("repository is not registered in HERMES_ROUTER_ALLOWED_REPOS")
        self.path = root / job_id
        self.base_commit = ""

    def git(self, *args, cwd=None, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = run_command(["git", "-c", "core.hooksPath=/dev/null", *args], cwd or self.repository, env=env)
        if check and (result["exit_code"] != 0 or result["truncated"] or result["cleanup_error"] or result["timed_out"]):
            raise WorkspaceError("Git command failed: " + result["stderr"][:1000])
        return result

    def prepare(self):
        top = self.git("rev-parse", "--show-toplevel")["stdout"].strip()
        if Path(top).resolve() != self.repository:
            raise WorkspaceError("repository must be its Git top-level directory")
        if self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")["stdout"]:
            raise WorkspaceError("repository is dirty; preserve or commit existing changes first")
        self.base_commit = self.git("rev-parse", "--verify", "HEAD")["stdout"].strip()
        if self.path.exists():
            raise WorkspaceError("worktree already exists; use explicit recovery")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", str(self.path), self.base_commit)
        return self

    def attach(self, base_commit: str):
        self.base_commit = base_commit
        if not self.path.is_dir() or self.git("rev-parse", "HEAD", cwd=self.path)["stdout"].strip() != base_commit:
            raise WorkspaceError("saved worktree identity changed")
        top = self.git("rev-parse", "--show-toplevel", cwd=self.path)["stdout"].strip()
        if Path(top).resolve() != self.path.resolve():
            raise WorkspaceError("worktree identity mismatch")
        return self

    def destination(self, relative: str) -> Path:
        target = self.path / relative
        if not target.resolve().is_relative_to(self.path.resolve()):
            raise WorkspaceError("path escapes worktree")
        current = target
        while current != self.path:
            if current.is_symlink():
                raise WorkspaceError("symlink edits are forbidden")
            current = current.parent
        return target

    def apply(self, proposal: ChangeProposal, spec: WorkSpec, risk_approved=False):
        targets = []
        for edit in proposal.edits:
            if not allowed(edit.path, spec):
                raise WorkspaceError("forbidden edit: " + edit.path)
            if risky(edit.path) and not risk_approved:
                raise WorkspaceError("risk approval required before file modification")
            targets.append((edit, self.destination(edit.path)))
        # Validate the entire package before the first write.
        for edit, target in targets:
            if edit.content is None:
                if target.exists():
                    if not target.is_file():
                        raise WorkspaceError("only regular files can be deleted")
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(edit.content)

    def changes(self) -> list[str]:
        tracked = self.git("diff", "--no-renames", "--name-only", "-z", "HEAD", cwd=self.path)["stdout"]
        # Git ignore rules must not hide model edits from policy checks or reports.
        untracked = self.git("ls-files", "--others", "-z", cwd=self.path)["stdout"]
        return sorted(set(p for p in (tracked + untracked).split("\x00") if p))

    def collect(self, spec: WorkSpec) -> dict:
        paths = self.changes()
        illegal = [p for p in paths if not allowed(p, spec)]
        for p in paths:
            try:
                self.destination(p)
            except WorkspaceError:
                illegal.append(p)
        tracked_diff = self.git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", cwd=self.path)
        untracked_diff = []
        for p in paths:
            target = self.path / p
            if target.is_file() and not target.is_symlink() and not self.git("ls-files", "--error-unmatch", "--", p, cwd=self.path, check=False)["exit_code"] == 0:
                result = self.git("diff", "--no-index", "--", "/dev/null", p, cwd=self.path, check=False)
                if result["truncated"]:
                    illegal.append(p)
                untracked_diff.append(result["stdout"])
        return {"files": paths, "diff": tracked_diff["stdout"] + "".join(untracked_diff), "illegal_files": sorted(set(illegal))}


def sandbox_profile(worktree: Path, scratch: Path, executable: Path) -> str:
    read_roots = [worktree, Path(sys.prefix), Path(sys.base_prefix),
        Path("/System/Library"), Path("/usr"), Path("/bin"), Path("/sbin"),
        Path("/Library/Frameworks"), Path("/Library/Apple"),
        Path("/private/etc"), Path("/private/var/db/dyld"), scratch]
    sub = lambda p: "(subpath " + json.dumps(str(p.resolve())) + ")"
    # CPython locates virtualenvs by probing ancestor directories and pyvenv.cfg.
    # Permit those exact entries, without granting access to their other files.
    ancestor_entries = set()
    for base in (worktree, scratch, Path(sys.executable), executable):
        for parent in base.parents:
            ancestor_entries.add(str(parent))
            ancestor_entries.add(str(parent / "pyvenv.cfg"))
    exact = " ".join("(literal " + json.dumps(p) + ")" for p in sorted(ancestor_entries))
    return ("(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow file-map-executable)"
        "(allow file-read-metadata)(allow file-read* " + " ".join(sub(p) for p in read_roots) + ")"
        "(allow file-read* " + exact + " (literal " + json.dumps(str(executable)) + "))"
        "(allow file-read* (literal \"/dev/null\") (literal \"/dev/urandom\") (literal \"/dev/random\"))"
        "(allow file-write* " + sub(scratch) + " (literal \"/dev/null\"))")


def verify_commands(worktree: Path, scratch: Path, spec: WorkSpec, remaining: float, *, cancelled=lambda: False, on_spawn=None) -> list[dict]:
    import time
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        raise WorkspaceError("verified macOS sandbox-exec is required; unsafe verification is disabled")
    scratch.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    evidence = []
    for command in spec.verification_commands:
        budget = min(command.timeout_seconds, remaining - (time.monotonic() - started))
        if budget <= 0 or cancelled():
            break
        argv = list(command.argv)
        if argv[0] in {"python", "python3", "pytest"}:
            argv = [sys.executable, *( ["-m", "pytest"] if argv[0] == "pytest" else []), *argv[1:]]
        executable = ((worktree / argv[0]).resolve() if "/" in argv[0] and not Path(argv[0]).is_absolute()
                      else Path(shutil.which(argv[0]) or argv[0]).resolve())
        profile = sandbox_profile(worktree, scratch, executable)
        env = {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(scratch), "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(worktree), "PYTEST_ADDOPTS": "-o cache_dir=" + str(scratch / "pytest-cache")}
        result = run_command(["/usr/bin/sandbox-exec", "-p", profile, *argv], worktree, budget,
            env=env, cancelled=cancelled, on_spawn=on_spawn)
        result.update(command_id=command.id, argv=list(command.argv), sandboxed=True)
        result["passed"] = result["exit_code"] == 0 and not any(result[k] for k in
            ("timed_out", "cancelled", "truncated", "cleanup_error"))
        evidence.append(result)
    return evidence
