---
name: dev-router
description: Start, inspect, approve, cancel, and report isolated development jobs through the local Hermes Development Router.
---

# Hermes Development Router

Use the installed `dev-router` CLI to call the local Router. Keep normal Hermes chat and its Provider configuration unchanged.
The Router must be running on `http://127.0.0.1:8080`. Its repository allowlist and real-provider option are operator settings.
Never run `hermes model`, `hermes config set`, modify Hermes core, or expose credential/token files.

## Start a development job

1. Save the request verbatim to a UTF-8 task file under the current user’s `~/codex/hermes-router/requests/` directory (resolve `~` to an absolute path) using a file-writing tool.
   Run `dev-router create --request-file /absolute/request.txt --repository /absolute/repository --mode hermes`.
   Never interpolate arbitrary request text into a shell command.
   Use `--mode mock` when the user asks for a simulation. Never represent a mock run as real development.
2. Run `dev-router analyze JOB_ID` if real inference is enabled, otherwise use the returned structured draft and questions.
3. Ask the user about every missing field. Prepare a JSON specification with objective, functional_requirements,
   non_functional_requirements, allowed_files, forbidden_changes, acceptance_criteria, verification_commands, risk_level.
   Requirements and commands need unique IDs; each acceptance criterion references requirement IDs and command IDs.
   Use `path:PATTERN` or `check:COMMAND_ID` for precise forbidden changes. Allowed files are repository-relative.
4. Save the reviewed specification to a task file and run `dev-router spec JOB_ID --file /absolute/spec.json`.
5. Show the specification version, digest, allowed changes, and verification commands to the user.
   Ask for explicit approval. **Never approve your own draft or infer approval from a request to start development.**
6. Only after the user's explicit approval, run `dev-router approve JOB_ID --version VERSION --digest DIGEST --actor human`.
7. Run `dev-router start JOB_ID`. If the state is awaiting_risk_approval, show the complete risk scope and digest,
   ask the user for explicit approval, then use `dev-router approve-risk JOB_ID --digest DIGEST --actor human` and start again.

## Commands

| User intent | Command |
| --- | --- |
| Progress | `dev-router status JOB_ID` |
| Failure causes | `dev-router failures JOB_ID` |
| Provider switches | `dev-router history JOB_ID` |
| Audit events | `dev-router events JOB_ID` |
| Cancel | `dev-router cancel JOB_ID` |
| Approve specification | `dev-router approve JOB_ID --version VERSION --digest DIGEST --actor human` |
| Report | `dev-router report JOB_ID` |
| Observed Provider metrics | `dev-router metrics --mode hermes` (use `--mode mock` for simulations) |

The CLI reads its local token file internally; do not print, pass, or copy that token into prompts or reports.
Report exact tests, failures, changed files, worktree, and whether execution was simulated.
DeepSeek is diagnostic only. Never claim development success from model output or CLI exit code alone.
Do not merge worktrees or failed code into main. Keep failed worktrees and verification evidence for review.
For an interrupted job, report the preserved state and require the Router's explicit recovery acknowledgement.
