"""Job execution inside persistent, reviewable Git worktrees."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
from .checkpointer import CheckpointManager
from .development import JobConflict
from .models import utcnow
from .routing import build_router, classify
from .specification import WorkSpec
from .workspace import ChangeProposal, Workspace, WorkspaceError, digest_scope, risk_scope, verify_commands
from .execution import process_identity, stop_saved_process
from .providers.hermes import HermesSubprocessProvider


class DevelopmentRunner:
    def __init__(self, store, data_dir, allowed_repositories, provider_factory=None):
        self.store = store
        self.data_dir = data_dir
        self.allowed_repositories = tuple(Path(p) for p in allowed_repositories)
        self.provider_factory = provider_factory
        self.cp = CheckpointManager(data_dir / "routing-checkpoints.sqlite3")
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="development")
        self.lock = threading.RLock()
        self.events = {}
        self.adapters = {}
        self.futures = {}
        self.closed = False

    def recover(self):
        for job in self.store.list():
            if job["status"] not in {"queued", "running"} and not job.get("analysis_in_flight"):
                continue
            cleanup = stop_saved_process(job.get("process"))
            def interrupted(j):
                if pending := j.get("pending_attempt"):
                    if not any(a["number"] == pending for a in j["attempts"]):
                        j["attempts"].append({"number": pending, "provider": j.get("in_flight_provider", "minimax"),
                            "error": "interrupted", "verified": False, "verification": [],
                            "finished_at": utcnow().isoformat(), "result": {"exit_code": None}})
                    j["router_action"] = "attempt"
                if j.get("analysis_in_flight"):
                    j.update(analysis_in_flight=False)
                else:
                    j.update(status="interrupted", reason="process_interrupted")
                j.update(pending_attempt=None, recovery_cleanup=cleanup,
                    report={"error": "process_interrupted", "worktree": j.get("worktree"),
                        "last_attempt": j["attempts"][-1] if j["attempts"] else None,
                        "recovery_cleanup": cleanup, "requires_acknowledgement": True})
            self.store.mutate(job["id"], interrupted, event="process_recovered")

    def resume(self, job_id, actor, acknowledge):
        if not acknowledge:
            raise JobConflict("review possible side effects and explicitly acknowledge before resuming")
        job = self.store.get(job_id)
        cleanup = stop_saved_process(job.get("process"))
        if cleanup in {"identity_unverified", "cleanup_unverified"}:
            raise JobConflict("old process cleanup remains unverified")
        def resume(j):
            if len(j["attempts"]) >= j["max_attempts"]:
                raise JobConflict("attempt budget exhausted; create a new reviewed job")
            if j.get("deadline_at") and utcnow() >= datetime.fromisoformat(j["deadline_at"]):
                raise JobConflict("original deadline expired; create a new reviewed job")
            j.update(status="ready", resume_actor=actor, process=None, recovery_cleanup=cleanup, resume_requested=True)
        return self.store.mutate(job_id, resume, allowed_statuses={"interrupted"}, event="resume_acknowledged")

    def track_process(self, job_id, pid):
        identity = process_identity(pid)
        self.store.mutate(job_id, lambda j: j.update(process=identity))

    def register_adapter(self, job_id, adapter):
        if isinstance(adapter, HermesSubprocessProvider):
            adapter.on_spawn = lambda pid: self.track_process(job_id, pid)
        with self.lock:
            self.adapters[job_id] = adapter

    def analysis(self, job_id, function):
        with self.lock:
            if self.closed or (job_id in self.futures and not self.futures[job_id].done()):
                raise JobConflict("job operation already running or runner closing")
            event = self.events[job_id] = threading.Event()
            def run():
                self.store.mutate(job_id, lambda j: j.update(analysis_in_flight=True), event="analysis_started")
                try:
                    return function(event)
                finally:
                    self.store.mutate(job_id, lambda j: j.update(analysis_in_flight=False, process=None))
                    with self.lock:
                        self.events.pop(job_id, None)
                        self.adapters.pop(job_id, None)
            future = self.futures[job_id] = self.pool.submit(run)
        future.result()
        return self.store.get(job_id)

    def start(self, job_id):
        with self.lock:
            if self.closed:
                raise JobConflict("runner is shutting down")
            job = self.store.get(job_id)
            if job["mode"] == "hermes" and self.provider_factory is None:
                raise JobConflict("Hermes execution must be explicitly configured")
            if not job.get("approved_digest"):
                raise JobConflict("specification approval required")
            if job_id in self.futures and not self.futures[job_id].done():
                raise JobConflict("job already executing")
            def queued(j):
                j.update(status="queued", deadline_at=j.get("deadline_at") or (utcnow()+timedelta(seconds=j["timeout_seconds"])).isoformat())
            self.store.mutate(job_id, queued, allowed_statuses={"ready"}, event="queued")
            event = self.events[job_id] = threading.Event()
            self.futures[job_id] = self.pool.submit(self._run, job_id, event)
        return self.store.get(job_id)

    def cancel(self, job_id):
        def cancel(j):
            if j["status"] not in {"succeeded", "failed", "blocked", "cancelled", "needs_user_action"}:
                j.update(status="cancelled", reason="cancelled")
        job = self.store.mutate(job_id, cancel, event="cancel_requested")
        with self.lock:
            if event := self.events.get(job_id):
                event.set()
            if adapter := self.adapters.get(job_id):
                if callable(cancel_adapter := getattr(adapter, "cancel", None)):
                    cancel_adapter(job_id)
        return job

    def approve_risk(self, job_id, digest, actor):
        def approve(j):
            if j.get("risk_request", {}).get("digest") != digest:
                raise JobConflict("risk approval digest mismatch")
            j.update(risk_approved=digest, risk_actor=actor, status="ready")
        return self.store.mutate(job_id, approve, allowed_statuses={"awaiting_risk_approval"}, event="risk_approved")

    def _run(self, job_id, event):
        try:
            self._execute(job_id, event)
        except Exception as exc:
            def fail(j):
                if j["status"] != "cancelled":
                    j.update(status="blocked", reason=type(exc).__name__, report={"error": str(exc)[:2000]})
            self.store.mutate(job_id, fail, event="execution_error")
        finally:
            with self.lock:
                self.adapters.pop(job_id, None)
                self.events.pop(job_id, None)

    def _execute(self, job_id, event):
        job = self.store.get(job_id)
        if event.is_set() or job["status"] == "cancelled":
            return
        spec = WorkSpec.model_validate(job["specification"]["body"])
        if spec.digest() != job["approved_digest"]:
            raise JobConflict("approved specification changed")
        workspace = Workspace(Path(job["repository"]), self.data_dir / "worktrees", job_id, self.allowed_repositories)
        if job.get("base_commit"):
            workspace.attach(job["base_commit"])
        else:
            workspace.prepare()
            self.store.mutate(job_id, lambda j: j.update(worktree=str(workspace.path), base_commit=workspace.base_commit), event="worktree_created")
        scope = risk_scope(spec, workspace.base_commit)
        scope_digest = digest_scope(scope)
        if scope["required"] and job.get("risk_approved") != scope_digest:
            def risk(j):
                if j["status"] != "cancelled":
                    j.update(status="awaiting_risk_approval", risk_request={**scope, "digest": scope_digest})
            self.store.mutate(job_id, risk, event="risk_approval_required")
            return
        context = {}
        def executor(provider, package, remaining, diagnostic):
            context.clear()
            number = len(self.store.get(job_id)["attempts"])
            if job["mode"] == "mock":
                plan = job["mock_plan"]
                step = plan[min(number, len(plan)-1)] if plan else {}
                result = {"exit_code": step.get("exit_code", 0), "stdout": step.get("stdout", "mock execution"),
                          "stderr": step.get("stderr", ""), "simulation": True}
                proposal = ChangeProposal.model_validate({"edits": step.get("edits", [])})
            else:
                adapter = self.provider_factory(provider, workspace, job_id)
                self.register_adapter(job_id, adapter)
                if event.is_set():
                    return {"exit_code": None, "error_class": "cancelled"}
                prompt = self._prompt(package, workspace)
                if callable(redact := getattr(self.provider_factory, "redact_payload", None)):
                    prompt = redact(prompt)
                result = adapter.run(prompt=prompt, attempt=number+1, timeout_seconds=remaining,
                    max_turns=1, thread_id=job_id).model_dump(mode="json")
                if classify(result) != "none":
                    return result
                if diagnostic:
                    return result
                try:
                    body = result["stdout"].strip()
                    if body.startswith("```"):
                        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
                    proposal = ChangeProposal.model_validate_json(body)
                except ValueError:
                    return {**result, "exit_code": 1, "stderr": "invalid change proposal JSON"}
            if diagnostic:
                if proposal.edits:
                    context["illegal_change"] = True
                return result
            if classify(result) == "none":
                try:
                    workspace.apply(proposal, spec, risk_approved=job.get("risk_approved") == scope_digest)
                except WorkspaceError as exc:
                    context.update(illegal_change=True, policy_error=str(exc))
            return result

        def verifier(provider, result, spec):
            collection = workspace.collect(spec)
            verdict = {"changes": collection, "illegal_change": bool(collection["illegal_files"] or context.get("illegal_change")),
                "policy_error": context.get("policy_error"), "verified": False, "verification": []}
            if verdict["illegal_change"] or provider == "deepseek" or classify(result) != "none":
                return verdict
            remaining = (datetime.fromisoformat(self.store.get(job_id)["deadline_at"]) - utcnow()).total_seconds()
            evidence = verify_commands(workspace.path, self.data_dir / "execution" / job_id, spec, remaining,
                cancelled=event.is_set, on_spawn=lambda pid: self.track_process(job_id, pid))
            passed = {r["command_id"] for r in evidence if r["passed"]}
            missing = set()
            criteria = []
            for criterion in spec.acceptance_criteria:
                absent = [p for p in criterion.required_files if not workspace.destination(p).is_file()]
                if absent:
                    missing.update(criterion.requirement_ids)
                criteria.append({"id": criterion.id, "passed": not absent and set(criterion.verification_command_ids) <= passed,
                                 "requirement_ids": list(criterion.requirement_ids), "missing_files": absent})
            verdict.update(verified=all(c["passed"] for c in criteria) and len(passed) == len(spec.verification_commands), verification=evidence, criteria=criteria,
                missing_requirements=sorted(missing), test_failures=[c.id for c in spec.verification_commands if c.id not in passed],
                cleanup_failed=any(r["cleanup_error"] for r in evidence))
            return verdict

        def persist(state):
            def save(j):
                for key in ("attempts", "switches", "provider_index", "network_retries", "repeated_failures", "pending_attempt", "in_flight_provider"):
                    if key in state:
                        j[key] = state[key]
                if j["status"] != "cancelled":
                    j["status"] = "running"
                j.update(router_action=state.get("action", "attempt"), routing_reason=state.get("reason"))
            self.store.mutate(job_id, save)
        initial = {"job_id": job_id,
            "specification": spec.model_dump(mode="json"), "spec_digest": spec.digest(),
            "deadline_at": self.store.get(job_id)["deadline_at"], "max_attempts": job["max_attempts"],
            "provider_index": job.get("provider_index", 0), "attempts": job["attempts"], "switches": job["switches"],
            "network_retries": job.get("network_retries", 0), "repeated_failures": job.get("repeated_failures", {}), "status": "running"}
        if job.get("resume_requested"):
            action = job.get("router_action", "attempt")
            initial.update(resume_action="switch" if action == "switch" else "reverify" if action in {"decide", "end"} and job["attempts"] else "attempt",
                last=job["attempts"][-1] if job["attempts"] else {}, reason=job.get("routing_reason") or "resumed")
        app = build_router(executor, verifier, cancelled=event.is_set, on_update=persist).compile(checkpointer=self.cp.saver)
        final = app.invoke(initial, {"configurable": {"thread_id": job_id}, "recursion_limit": 200})
        collection = workspace.collect(spec)
        def finish(j):
            if j["status"] != "cancelled":
                j.update(status=final["status"], reason=final.get("reason"))
            j.update(attempts=final.get("attempts", []), switches=final.get("switches", []), pending_attempt=None,
                process=j.get("process") if final.get("reason") == "cleanup_unverified" else None, resume_requested=False,
                report={"status": j["status"], "reason": j.get("reason"), "spec_digest": spec.digest(),
                    "base_commit": workspace.base_commit, "worktree": str(workspace.path), "changes": collection,
                    "verification": final.get("last", {}).get("verification", []), "criteria": final.get("last", {}).get("criteria", []),
                    "simulation": job["mode"] == "mock", "finished_at": utcnow().isoformat()})
        self.store.mutate(job_id, finish, event="execution_finished")

    def _prompt(self, package, workspace):
        files = {}
        from .workspace import allowed
        for path in workspace.git("ls-files", "-z", cwd=workspace.path)["stdout"].split("\x00"):
            if path and allowed(path, package.specification):
                target = workspace.destination(path)
                if target.is_file() and target.stat().st_size <= 40_000 and sum(len(v) for v in files.values()) < 80_000:
                    files[path] = target.read_text(errors="replace")
        return json.dumps({"instruction": "Tools are disabled. Return ONLY JSON {edits:[{path,content}],summary,requirement_notes}. content=null deletes a file. Do not change forbidden paths. Diagnostic mode: return edits:[] and diagnosis in summary. Never claim test success; the router runs approved checks.",
            "handoff": package.model_dump(mode="json"), "files": files}, ensure_ascii=False)

    def close(self):
        with self.lock:
            self.closed = True
            ids = list(self.events)
        for job_id in ids:
            self.cancel(job_id)
        self.pool.shutdown(wait=True, cancel_futures=False)
        self.cp.close()
