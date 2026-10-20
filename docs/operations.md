# 운영 및 장애 복구

초기 실행은 로컬 포그라운드 프로세스입니다. launchd 구성은 향후 운영용 산출물이며 자동 설치·부트스트랩하지 않습니다.

## 설치와 기동

```bash
bash scripts/install.sh --install-skill
export HERMES_ROUTER_ALLOWED_REPOS='["/absolute/repository"]'
export HERMES_ROUTER_ALLOW_REAL=1
bash scripts/start.sh
```

실제 호출을 원하지 않으면 `HERMES_ROUTER_ALLOW_REAL`을 설정하지 않습니다. 서버 기본 주소는 `127.0.0.1:8080`입니다. `--port 8090` 등 실행 옵션을 start.sh에 전달할 수 있습니다. 단일 프로세스만 하나의 data 디렉터리를 소유할 수 있습니다. 여러 uvicorn worker를 사용하지 마세요. 종료는 Ctrl-C이며 실행 중 자식 프로세스를 정리하고 SQLite 연결을 닫습니다.

| 환경 변수 | 기본값 / 의미 |
| --- | --- |
| `HERMES_ROUTER_DATA_DIR` | `~/codex/hermes-router/data` |
| `HERMES_ROUTER_LOG_DIR` | data 디렉터리의 `logs` |
| `HERMES_ROUTER_ALLOWED_REPOS` | 빈 JSON 배열; 정확한 저장소 루트의 allowlist |
| `HERMES_ROUTER_ALLOW_REAL` | false; 실제 Hermes 호출 허용 |
| `HERMES_ROUTER_HERMES` | PATH의 `hermes`; 필요하면 절대 경로 지정 |
| `HERMES_ROUTER_CREDENTIALS` | `~/.hermes/.env`; 읽기 전용 |

등록된 호출별 ID는 MiniMax `minimax-cn / MiniMax-M3`, GLM `zai / glm-5.3`, DeepSeek `deepseek / deepseek-v4-pro`입니다. 다른 Hermes 설치본에서는 지원 여부를 먼저 확인합니다. Router는 작업별 격리 HOME에서 모델 도구를 끄고 변경 제안만 받습니다. 기존 Hermes의 config.yaml, .env, auth.json과 일반 대화 설정을 덮어쓰지 않습니다. 새 격리 홈의 첫 실행에는 Hermes 자체 런타임 의존성 준비가 포함될 수 있으며 이 시간도 전체 작업 timeout에서 소비합니다. 최초 런타임 준비 지연은 Provider API 오류와 구분해 확인합니다.

## 인증과 로그

최초 시작 시 data/admin-token을 0600으로 생성합니다. dev-router의 기본 토큰 경로도 동일합니다. 경로를 바꿨다면 `dev-router --token-file /absolute/data/admin-token status JOB_ID`처럼 지정합니다. 토큰을 stdout, 명령행 값, 프롬프트에 복사하지 마세요. `/health`만 인증 없이 상태를 보여줍니다.

앱 로그 `router.log`는 기본 1 MB마다 회전하며 5개 백업을 유지합니다. 파일과 회전본은 소유자 전용이고 알려진 키·토큰 및 인증 패턴을 저장 전에 마스킹합니다. 작업별 Provider 결과·검증 stdout/stderr·시간·handoff는 development.sqlite3에 보존됩니다. 이는 실패 분석용 영구 기록이므로 디스크 사용량을 점검하고 종료 작업을 검토한 뒤 보관하세요. 작업의 결과를 삭제하는 자동 보존 정책은 없습니다. data/hermes-homes의 Hermes 런타임·캐시도 작업별로 생성될 수 있으므로 종료 작업의 코드·증거와 함께 디스크 사용량을 점검하세요.

```bash
dev-router status JOB_ID
dev-router failures JOB_ID
dev-router history JOB_ID
dev-router events JOB_ID
dev-router report JOB_ID
dev-router metrics --mode hermes
```

Mock 지표는 `--mode mock`으로 구분합니다. Provider 개발 성공은 독립 검증 통과, DeepSeek 성공은 진단 반환을 뜻합니다. 테스트 통과율의 단위는 승인된 검증 명령입니다. 총 토큰·비용은 확인된 CLI가 제공하지 않아 null이며 0으로 해석하지 않습니다.

## 승인과 실패 대응

`awaiting_spec_approval`은 명세 본문·버전·digest를 사용자에게 보여준 뒤 승인합니다. `awaiting_risk_approval`이면 risk_request의 변경 범위와 기준 커밋을 검토한 뒤 해당 digest로 승인합니다. API 인증은 로컬 관리자 권한이며 사람의 신원을 별도로 인증하는 기능은 아닙니다. Hermes Skill은 사용자의 명시적 승인 전 approve 명령을 실행하지 않습니다.

금지 파일 변경은 `blocked`, 예산 소진·DeepSeek 진단은 `needs_user_action`으로 보고합니다. 실패한 worktree와 diff는 그대로 남습니다. 원본 저장소가 dirty이면 시작을 거부합니다. 자동 merge 명령은 없습니다. 검토 후 필요한 커밋을 별도로 만드는 것은 운영자의 작업입니다.

429는 대체 Provider로 전환합니다. 네트워크/일시적 API 장애는 같은 Provider에서 최대 2회 재시도한 뒤 전환합니다. cleanup_unverified이면 기존 프로세스 정리가 확인되지 않았으므로 새 실행 전 프로세스와 저장된 PID·시작시간을 확인합니다. 상한을 늘리려고 DB를 편집하지 말고 새로 검토한 작업을 만드세요.

## 프로세스 중단과 복구

시작 시 기존 queued/running 작업을 `interrupted`로 복구하고, 저장된 프로세스의 PID·시작시간·그룹이 일치할 때만 정리합니다. 불확실한 Provider 시도도 예산에서 소모하며 자동 호출하지 않습니다.

1. `report`, `events`, 보존된 worktree diff와 실행 부작용을 확인합니다.
2. 사용자 확인 후 `dev-router resume JOB_ID --actor human --acknowledge-possible-side-effects`를 실행합니다.
3. `dev-router start JOB_ID`로 다시 시작합니다. 원래 마감시간과 시도 예산을 유지하며, 저장된 완료 시도는 재검증합니다.

이미 마감이 지났거나 예산이 소진되면 새 명세와 승인으로 새 작업을 만듭니다. SIGKILL 이후 실제 재시작·프로세스 정리·예산 유지 동작은 자동 통합 테스트에 포함되어 있습니다.

## 백업과 복원

서비스를 정상 종료한 뒤 SQLite backup API를 사용합니다. 실행 중이면 소유 잠금으로 거부합니다. 기존 백업을 덮어쓰지 않습니다.

```bash
.venv/bin/python scripts/backup.py --data-dir ~/codex/hermes-router/data \
  --output ~/codex/hermes-router/backups/2026-10-20
```

대상은 tasks.sqlite3, checkpoints.sqlite3, development.sqlite3, routing-checkpoints.sqlite3입니다. 원본 WAL/SHM 파일을 실행 중 복사하거나 삭제하지 마세요. 백업에는 인증키와 관리자 토큰을 넣지 않습니다.

복원은 서버를 정지하고 현재 data 디렉터리를 별도 보관한 뒤 수행합니다. 새 data 디렉터리를 0700으로 만들고 백업 DB를 0600으로 복사한 뒤 `PRAGMA integrity_check`를 확인합니다. 작업 payload의 절대 worktree 경로와 원본 Git 저장소를 함께 보존해야 재개할 수 있습니다. DB 백업만으로 코드 파일이 복구되지는 않습니다. worktrees·execution 증거·원본 저장소도 같은 시점으로 보관하세요. 토큰은 기존 0600 파일을 별도 안전 보관하거나 새 토큰을 생성하고 클라이언트를 갱신합니다. 서비스 시작 후 interrupted 작업은 위 복구 절차에 따릅니다.

## launchd 구성

```bash
.venv/bin/python scripts/generate-launchd.py --output deploy/com.hermes.router.plist \
  --data-dir ~/codex/hermes-router/data --repository /absolute/repository
plutil -lint deploy/com.hermes.router.plist
```

기본 실제 Provider 비활성, 루프백 주소, 비정상 종료만 KeepAlive, 재시작 간격 30초입니다. 설치 경로·허용 저장소·Provider 허용 여부를 검토하고 향후 서비스 전환 시 운영자가 설치합니다. 이 프로젝트 구축 과정에서는 launchctl을 실행하지 않습니다. plist의 stdout/stderr 파일은 launchd가 직접 기록하므로 향후 서비스 운영 시 별도 보관/회전 정책이 필요합니다. 앱 router.log의 회전과 별개입니다.
