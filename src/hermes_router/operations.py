"""Local authentication, bounded requests, safe logs, and observed metrics."""
from collections import Counter
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import stat
from starlette.responses import JSONResponse
from .redaction import redact_text, register_secrets


def administrator_token(settings):
    path = settings.data_dir / "admin-token"
    if path.is_symlink():
        raise ValueError("administrator token cannot be a symlink")
    if path.exists():
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("administrator token must be owner-only (chmod 600)")
        token = path.read_text().strip()
        if settings.api_token and not secrets.compare_digest(token, settings.api_token):
            raise ValueError("configured administrator token differs from persisted token")
    else:
        token = settings.api_token or secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    if len(token) < 16 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("administrator token must have at least 16 non-whitespace ASCII characters")
    register_secrets([token])
    return token


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_text(super().format(record))


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        from .permissions import private_file
        private_file(Path(self.baseFilename))
        return super()._open()


def rotating_log(settings):
    settings.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = PrivateRotatingHandler(settings.log_dir / "router.log", maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count, encoding="utf-8")
    Path(handler.baseFilename).chmod(0o600)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("hermes_router")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("router ready; local API authentication enabled")
    return handler


class LocalApiBoundary:
    def __init__(self, app, *, state, max_bytes):
        self.app, self.state, self.max_bytes = app, state, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        if scope["path"] != "/health":
            expected = getattr(self.state, "admin_token", None)
            supplied = headers.get(b"authorization", b"")
            if not expected or not supplied.startswith(b"Bearer ") or not secrets.compare_digest(supplied[7:], expected.encode()):
                return await JSONResponse({"detail": "local administrator authorization required"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        try:
            if int(headers.get(b"content-length", b"0")) > self.max_bytes:
                return await JSONResponse({"detail": "request too large"}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"detail": "invalid content length"}, status_code=400)(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                return await JSONResponse({"detail": "request too large"}, status_code=413)(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        return await self.app(scope, replay, send)


def observed_metrics(store, mode=None):
    jobs = [j for j in store.list() if mode is None or j["mode"] == mode]
    providers = {p: {"attempts": 0, "verified_successes": 0, "diagnoses": 0, "seconds": 0.0} for p in ("minimax", "glm", "deepseek")}
    tests, passed = 0, 0
    for job in jobs:
        for attempt in job["attempts"]:
            p = providers[attempt["provider"]]
            p["attempts"] += 1
            p["seconds"] += attempt.get("duration_seconds", 0)
            p["verified_successes"] += int(attempt.get("verified") is True and attempt.get("error") == "none")
            p["diagnoses"] += int(attempt["provider"] == "deepseek" and attempt.get("error") == "none")
            for result in attempt.get("verification", []):
                tests += 1
                passed += int(result.get("passed") is True)
    for name, p in providers.items():
        successes = p["diagnoses"] if name == "deepseek" else p["verified_successes"]
        p["success_rate"] = successes / p["attempts"] if p["attempts"] else None
        p["success_definition"] = "diagnostic response returned" if name == "deepseek" else "acceptance verified"
    return {"mode": mode or "all", "jobs": len(jobs), "statuses": dict(Counter(j["status"] for j in jobs)),
        "providers": providers, "verification": {"commands": tests, "passed": passed, "pass_rate": passed / tests if tests else None},
        "tokens": None, "cost": None, "cost_note": "not reported by the verified text CLI; unknown is not zero"}
