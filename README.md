# Hermes Development Router

실행 소스·테스트·설치 스크립트는 이 `router-development` 브랜치에서 제공합니다. macOS 로컬 실행용 프로젝트입니다.

기존 Hermes Chat/CLI를 유지하면서, 승인된 개발 명세를 독립 Git worktree에서 실행하는 로컬 Python 서비스입니다. Hermes 코어와 기존 Provider 설정을 변경하지 않습니다.

LangGraph는 MiniMax → GLM → DeepSeek 순서로 전환합니다. MiniMax·GLM은 JSON 변경 제안을 만들고, DeepSeek는 진단만 제공합니다. Router가 경로 정책과 위험 승인을 확인한 뒤 파일을 적용하고, 별도 macOS sandbox에서 승인된 모든 검증 명령과 수락 기준을 확인합니다. 모델의 성공 주장이나 종료 코드만으로 작업을 완료하지 않습니다.

## 설치 및 실행

macOS Apple Silicon, Git, Python 3.11 이상, `uv`, 설치된 Hermes CLI가 필요합니다. 이 환경에서는 Python 3.13과 Hermes 0.21.7로 검증했습니다. `.python-version`과 `uv.lock`으로 실행 환경을 고정합니다.

```bash
git clone --branch router-development https://github.com/fastsword0622/Hermes_Router.git hermes_router
cd hermes_router
bash scripts/install.sh
bash scripts/install.sh --install-skill
export HERMES_ROUTER_ALLOWED_REPOS="$(.venv/bin/python -c 'import json,pathlib; print(json.dumps([str(pathlib.Path.cwd())])))"
bash scripts/start.sh
```

기본 주소는 `127.0.0.1:8080`, 기본 실행 모드는 Mock입니다. 실제 호출을 허용하려면 **서버 시작 전** `HERMES_ROUTER_ALLOW_REAL=1`을 설정하고 작업 생성 시 `--mode hermes`를 지정합니다. 호출은 사용 중인 Provider의 사용량을 소비합니다. 기존 `~/.hermes/.env`는 읽기만 하며, 선택된 Provider의 인증 정보만 격리된 Hermes 자식 프로세스에 전달합니다.

`--install-skill`은 `~/.hermes/skills/dev-router`와 `~/.local/bin/dev-router` 진입점을 추가합니다. 기존 사용자 수정본은 보존하고 충돌을 보고합니다. `~/.local/bin`이 PATH에 있어야 합니다. 프로젝트 안에서는 `.venv/bin/dev-router`를 직접 사용할 수도 있습니다.

## Hermes Desktop에서 사용

Router를 실행한 뒤 기본 Hermes 프로필의 새 대화에서 다음과 같이 요청합니다.

> dev-router 스킬을 불러와. 내가 지정한 저장소의 개발 작업을 준비하고, 먼저 요구사항·허용 파일·검증 방법을 정리해 내 승인을 받아.

Desktop에서 로컬 명령 실행 도구를 사용할 수 있어야 합니다. 다른 프로필에서는 해당 프로필에 Skill이 등록됐는지 확인합니다. CLI로 미리 불러올 때는 `hermes chat --skills dev-router`를 사용합니다. 작업 완료 후 Router 터미널에서 Ctrl-C로 정상 종료하며, SQLite 기록은 보존됩니다.

## 작업 흐름

```bash
dev-router create --request-file /absolute/request.txt --repository /absolute/repository --mode hermes
dev-router analyze JOB_ID
dev-router spec JOB_ID --file /absolute/specification.json
```

분석 결과의 질문에 답하고, Objective·기능/비기능 요구사항·허용 파일·금지 변경·수락 기준·검증 명령·위험 수준을 모두 채웁니다. [명세 예시](examples/function-spec.json)를 참고하세요. 허용 파일과 검증 명령을 모델이 임의로 추정해 실행하지 않습니다. 완성된 명세의 버전과 SHA-256 digest를 사용자에게 보여준 뒤 승인받습니다.

```bash
dev-router approve JOB_ID --version VERSION --digest DIGEST --actor human
dev-router start JOB_ID
dev-router status JOB_ID
dev-router report JOB_ID
```

승인된 명세는 SQLite trigger와 해시 검사로 동결됩니다. DB·스키마·의존성·광범위 경로 등은 적용 전에 별도 위험 승인을 요구합니다. 최종 보고서에는 기준 커밋, worktree, 변경 파일·diff, 명령 종료 코드, 요구사항별 판정, 전환 이력이 담깁니다. 실패 worktree도 보존하며 main에 자동 병합하지 않습니다.

## 전환과 복구

- 같은 검증 명령의 반복 실패 2회, 필수 산출물 누락, 429/사용량 제한, 복구 가능한 API 오류에 따라 전환합니다. 네트워크·일시적 API 오류는 최대 2회 재시도합니다.
- 금지된 변경은 작업을 중단합니다. DeepSeek 진단 이후에는 사용자 조치를 요청하며 개발 성공으로 처리하지 않습니다.
- 작업의 전체 시도 수와 절대 마감시간을 제한합니다. 취소 시 프로세스 그룹을 정리합니다.
- 프로세스 중단 뒤에는 불확실한 시도를 기록하고 `interrupted`로 복구합니다. 재실행은 사용자의 부작용 확인이 필요하며 기존 예산과 마감시간을 유지합니다.

## API 및 Hermes Skill

`/health` 이외의 모든 HTTP 경로는 로컬 관리자 토큰 인증을 요구합니다. 토큰은 `~/codex/hermes-router/data/admin-token`에 소유자 전용으로 생성됩니다. `dev-router`가 파일을 내부에서 읽으므로 토큰을 명령행이나 대화에 복사하지 않습니다.

| 메서드 | 경로 | 기능 |
| --- | --- | --- |
| POST / GET | `/v1/jobs` | 작업 생성 / 목록 |
| GET | `/v1/jobs/{id}` | 상태·명세 조회 |
| POST | `/v1/jobs/{id}/analyze` | 자연어 명세 초안·질문 |
| PUT | `/v1/jobs/{id}/specification` | 명세 버전 작성 |
| POST | `/v1/jobs/{id}/{approve,start,cancel,approve-risk,resume}` | 승인·실행·취소·복구 |
| GET | `/v1/jobs/{id}/{events,report}` | 감사 이력·결과 |
| GET | `/v1/providers`, `/v1/metrics?mode=hermes` | 등록 정보·관측 지표 |

Phase 1의 `/v1/tasks`는 Mock 중심의 호환 API입니다. 개발 명세·worktree·전환 기능은 `/v1/jobs`를 사용합니다. 설치된 [dev-router Skill](skills/dev-router/SKILL.md)이 시작·상태·실패 원인·전환 이력·취소·승인·보고 명령을 안내합니다.

## 검증 및 운영

2026-10-20 최종 검증: 전체 208개 테스트 통과, 커버리지 85.7%, GLM 5.3 핵심 코드 QA 통과. 실제 MiniMax 개발 수락 검사와 Hermes Skill 등록을 확인했습니다. 상세한 측정 범위와 제한은 아래 보고서에 기록했습니다.

```bash
bash scripts/test.sh
dev-router metrics --mode hermes
```

테스트·커버리지·임시 파일은 기본적으로 `~/codex/hermes-router/work`에 저장됩니다. `HERMES_ROUTER_WORK_DIR`로 변경할 수 있습니다. 실제 Provider 호출은 일반 pytest에서 실행하지 않습니다.

검증 명령은 저장소를 읽기 전용으로 보고 작업별 scratch만 쓸 수 있으며 네트워크를 사용할 수 없습니다. 승인된 외부 런타임·필요 파일이 제한되어 있어, 네트워크 설치나 소스 트리 쓰기가 필요한 테스트는 명령/작업 구성을 조정해야 합니다. macOS `sandbox-exec`가 없는 환경은 지원하지 않습니다. 비밀정보 마스킹은 알려진 키와 패턴에 기반하므로, 작업 저장소 자체에 인증 정보를 넣지 마세요.

[운영·복구](docs/operations.md) · [자동 검증 시나리오](docs/test-scenarios.md) · [Hermes Skill](skills/dev-router/SKILL.md) · [명세 예시](examples/function-spec.json)

## Docker 및 iOS 지원 범위

현재 검증 실행부는 macOS `sandbox-exec`에 의존하므로 Docker에서 그대로 실행할 수 있는 상태는 아닙니다. 컨테이너 적용에는 실행·격리 구조 변경이 필요합니다. iPhone 앱 개발은 Xcode·Simulator와 Router의 빌드 검증 경계를 먼저 확인해야 하며, 현재 iOS 빌드 성공을 검증한 상태는 아닙니다.

내부 환경조사·작업 인계·상세 진단 기록과 개인 경로가 들어간 원본 launchd 구성은 공개 스냅샷에서 제외했습니다. 실제 launchd 구성은 `scripts/generate-launchd.py`로 사용자 환경에서 생성합니다.
