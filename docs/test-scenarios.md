# 필수 10개 자동 검증 시나리오

실 Provider 장애를 유발하거나 실패 코드를 병합하지 않고, Mock 전송/변경 제안과 실제 Git worktree·sandbox 검증을 조합한다.
시뮬레이션 여부는 모든 작업 보고서에 명시된다. 실제 API 호출 가능성 검사는 환경 조사, 개발 성공 smoke는 별도로 구분한다.

| 번호 | 요구 시나리오 | 통합 검증 증거 |
| --- | --- | --- |
| 1 | MiniMax 성공 → 완료 | `test_workspace.py::test_worktree_success_diff_and_original_untouched` |
| 2 | 요구사항 누락 → GLM | `test_scenarios.py::test_missing_required_file_switches_immediately` |
| 3 | MiniMax 동일 테스트 2회 실패 → GLM | `test_workspace.py::test_repeated_real_test_failure_switches_to_glm` |
| 4 | GLM도 실패 → DeepSeek 진단 | `test_scenarios.py::test_end_to_end_scenarios[glm_failure_diagnosis-...]` |
| 5 | HTTP 429 → 대체 Provider | `test_scenarios.py::test_end_to_end_scenarios[rate_limit-...]` |
| 6 | 네트워크 장애 → 제한적 재시도 | `test_scenarios.py::test_end_to_end_scenarios[network-...]` |
| 7 | DB/스키마 변경 → 사전 사용자 승인 | `test_workspace.py::test_schema_change_requires_approval_before_write` |
| 8 | 프로세스 강제 종료 → 상태 복구 | `test_recovery.py::test_actual_sigkill_recovers_without_automatic_replay_and_resumes` |
| 9 | 허용되지 않은 변경 → 중단 | `test_workspace.py::test_forbidden_package_is_rejected_before_any_write` |
| 10 | 전체 최대 시도 초과 → 사용자 보고 | `test_scenarios.py::test_end_to_end_scenarios[attempt_limit-...]` |

추가 경계 검사에는 dirty 저장소 거부, symlink 및 대소문자 Git 경로 우회 거부,
전체 변경 패키지 검증 후 첫 쓰기, 외부 쓰기·네트워크 sandbox 차단, 명세 DB 불변,
취소 경쟁 조건, 자식 전용 비밀정보 마스킹, 동일 데이터 디렉터리 중복 프로세스 거부가 포함된다.

복구 시 살아 있는 기록 프로세스의 PID·시작 시각·프로세스 그룹을 대조해 같은 실행만 정리한다.
검증하지 못한 정리는 새 Provider로 넘기지 않고 중단한다. 미완료 시도도 전체 시도 예산에 포함한다.
재개는 명시적 부작용 검토 동의와 actor를 요구하며 deadline·최대 시도 수를 초기화하지 않는다.
완료된 시도의 검증을 다시 확인하는 재개 경로와 새 Provider 전환은 체크포인트의 결정을 보존한다.
