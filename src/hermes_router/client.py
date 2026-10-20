"""Local REST client used by the Hermes dev-router skill."""
import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .redaction import redact_text


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(base_url, method, path, payload=None, token_path=None):
    url = urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("dev-router connects only to the local HTTP service")
    headers = {"Content-Type": "application/json"}
    if token_path and Path(token_path).exists():
        headers["Authorization"] = "Bearer " + Path(token_path).read_text().strip()
    request = Request(base_url.rstrip("/") + path, data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers, method=method)
    with build_opener(NoRedirect()).open(request, timeout=130) as response:
        return json.load(response)


def parser():
    p = argparse.ArgumentParser(prog="dev-router", description="Hermes Development Router local client")
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--token-file", default=str(Path.home() / "codex/hermes-router/data/admin-token"))
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    group = create.add_mutually_exclusive_group(required=True)
    group.add_argument("--request")
    group.add_argument("--request-file", help="UTF-8 file or '-' for stdin; avoids shell interpolation")
    create.add_argument("--repository", required=True)
    create.add_argument("--mode", choices=["mock", "hermes"], default="mock")
    create.add_argument("--max-attempts", type=int, default=10)
    create.add_argument("--timeout", type=float, default=300)
    for command in ("status", "failures", "history", "cancel", "start", "report", "events"):
        q = sub.add_parser(command)
        q.add_argument("job_id")
    spec = sub.add_parser("spec")
    spec.add_argument("job_id")
    spec.add_argument("--file", required=True)
    analysis = sub.add_parser("analyze")
    analysis.add_argument("job_id")
    analysis.add_argument("--provider", choices=["minimax", "glm"], default="minimax")
    for command in ("approve", "approve-risk"):
        q = sub.add_parser(command)
        q.add_argument("job_id")
        q.add_argument("--digest", required=True)
        q.add_argument("--actor", required=True)
        if command == "approve":
            q.add_argument("--version", type=int, required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("job_id")
    resume.add_argument("--actor", required=True)
    resume.add_argument("--acknowledge-possible-side-effects", action="store_true")
    metrics = sub.add_parser("metrics")
    metrics.add_argument("--mode", choices=["mock", "hermes"], default="hermes")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    method, payload = "GET", None
    path = "/v1/jobs"
    if args.command == "metrics":
        path = "/v1/metrics?mode=" + args.mode
    elif args.command == "create":
        try:
            prompt = args.request if args.request is not None else (sys.stdin.read(100_001) if args.request_file == "-" else Path(args.request_file).read_text())
            if len(prompt) > 100_000:
                raise ValueError("request too large")
        except (OSError, ValueError):
            print(json.dumps({"error": "request file invalid or too large"}))
            return 2
        method, payload = "POST", {"prompt": prompt, "repository": args.repository, "mode": args.mode,
            "max_attempts": args.max_attempts, "timeout_seconds": args.timeout}
    else:
        from uuid import UUID
        try:
            job_id = str(UUID(args.job_id))
        except ValueError:
            print(json.dumps({"error": "job_id must be a UUID"}))
            return 2
        path += "/" + job_id
        if args.command in {"failures", "history", "report"}:
            path += "/report"
        elif args.command in {"events", "start", "cancel", "analyze", "approve", "approve-risk", "resume"}:
            path += "/" + args.command
            if args.command != "events":
                method, payload = "POST", {}
        elif args.command == "spec":
            method = "PUT"
            path += "/specification"
            try:
                payload = json.loads(Path(args.file).read_text())
            except (OSError, ValueError):
                print(json.dumps({"error": "specification file could not be read as JSON"}))
                return 2
        if args.command in {"approve", "approve-risk"}:
            payload = {"digest": args.digest, "actor": args.actor}
            if args.command == "approve":
                payload["version"] = args.version
        elif args.command == "analyze":
            payload = {"provider": args.provider}
        elif args.command == "resume":
            payload = {"actor": args.actor, "acknowledge_possible_side_effects": args.acknowledge_possible_side_effects}
    try:
        result = request_json(args.url, method, path, payload, args.token_file)
        if args.command == "history":
            result = {"switches": result["switches"]}
        elif args.command == "failures":
            result = {"status": result["status"], "failures": [{"number": a["number"], "provider": a["provider"],
                "error": a.get("error"), "test_failures": a.get("test_failures"), "missing_requirements": a.get("missing_requirements"),
                "policy_error": a.get("policy_error")} for a in result["attempts"] if a.get("error") != "none" or not a.get("verified")]}
        print(redact_text(json.dumps(result, ensure_ascii=False, indent=2)))
        return 0
    except HTTPError as exc:
        # Do not dump request headers or server HTML into the Hermes conversation.
        print(json.dumps({"error": "router request rejected", "http_status": exc.code}))
        return 1
    except (OSError, URLError, ValueError):
        print(json.dumps({"error": "local router request failed; check service and token file"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
