# Lightweight Tool Workflow Router

작성일: 2026-05-26 KST
상태: **SUPERSEDED / CLOSED (2026-05-28)** — 아래 "최종 결론" 참조
대상: Hermes Slack gateway / Cookie alter surface

## 최종 결론 (2026-05-28)

이 문서가 제안한 "LLM 없이 즉답하는 lightweight router/fast-path"는 **만들었다가 제거**했다. 경과:

1. quick-workflow 레지스트리 → `project-status-digest` 스킬을 silent shadow 하는 버그 → 폐기.
2. "스킬이 곧 워크플로" + 게이트웨이 skill fast_path 로 재구현 (team-roster 즉답).
3. 이사님 로이봉봇 참고 + 논의 끝에 **최종 아키텍처 = 단일 정체성 `SOUL.md` + 역할=스킬 (전부 full agent + `skill_view`)** 로 착지. 프로필 기능 미사용(런타임 전환 불가) → `profiles/` 는 `~/.hermes/_archive/` 로 이동.
4. **fast-path(LLM 우회 즉답)는 일관성 + substring 패턴 오발동("팀원"이 "팀원한테 공유"에 매칭) 비용 때문에 5/28 제거.** team-roster 는 평범한 LLM 스킬로 남김. 반복 질문도 전부 full agent + 스킬 경로.

즉 이 문서의 "router-first / fast-path" 전제는 채택되지 않았다. 현행 구조 기록은 메모리 `project_hermes_skill_fast_path` 참조. 아래 본문은 탐색 히스토리로만 보존.

---

## 구현 현황 (2026-05-27)

### 아키텍처 결정 — "워크플로 = 스킬" 통합 (2026-05-27 오후)

초안은 별도 `quick-workflows.yaml` 레지스트리 + `quick_workflow.py` 라우터였다. 첫 라이브 검증에서
**라우터의 `project_status` 가 `project-status-digest` 스킬을 silent shadow** 하는 버그가 드러났다 —
같은 책임(현황 답변)을 두 시스템이 서로 모른 채 갖고 있어서, 앞단 라우터가 뒷단 스킬을 가렸다.

결론: **별도 워크플로 레이어를 두지 않는다. 스킬이 곧 워크플로의 단일 진실이다.** 스킬은 대부분
full agent loop 안에서 LLM 이 실행하지만, **답이 순수 결정적인 스킬**은 frontmatter 에 `fast_path` 를
선언할 수 있고, 그러면 게이트웨이가 그 스킬의 결정적 핸들러를 **LLM 없이** 실행한다 (~1ms, provider
오류 노출 0). `fast_path` 없는 스킬(합성 필요 — project-status-digest 등)은 절대 short-circuit 되지
않고 항상 full agent 로 간다. → 속도 + 자가진화(스킬은 에이전트가 직접 관리) + 프레임워크 정합 동시 달성.

```yaml
# SKILL.md frontmatter
metadata:
  hermes:
    fast_path:
      handler: team_roster        # 게이트웨이에 pre-register 된 결정적 핸들러만 선택 가능 (코드 주입 X)
      max_chars: 60
      patterns: ["우리팀 인원", "앱파트", ...]
```

### 구현물

- ✅ `gateway/skill_fast_path.py` (`SkillFastPathRouter`) — `~/.hermes/skills/**/SKILL.md` 를 스캔해
  `metadata.hermes.fast_path` 선언을 모은다 (파일 mtime 캐시 + 10s walk TTL → 봇 재시작 없이 hot-reload).
  봇이 직접 호출된 경우(DM/@멘션) + 짧은 메시지 + 단일 패턴 매칭에만 발동. no-match/ambiguous/핸들러
  실패 시 full agent fallback. Block Kit payload 마커 이후는 normalize 에서 절단(가시 텍스트로만 매칭).
- ✅ 결정적 핸들러는 게이트웨이 Python 함수(`_HANDLERS`). 현재 `team_roster` (people.yaml `ai_app_part`).
  스킬은 핸들러를 *이름으로 선택*만 하고 코드를 주입할 수 없다. 신뢰 디렉토리(`~/.hermes/skills/`)만 스캔.
- ✅ `~/.hermes/skills/team-roster/SKILL.md` — fast_path(team_roster) + full-agent 본문(특정 인물/타 파트 등).
- ✅ 로깅 `~/.hermes/logs/skill_fast_path.jsonl` — success/fallback, latency, skill/handler. 원문 미저장, no-match 미로깅.
- ✅ `gateway/platforms/slack.py` `_handle_slack_message` 의 `handle_message()` 직전에 삽입.
- 🗑️ 제거: `quick_workflow.py`, `quick-workflows.yaml`, `quick_workflows.jsonl`, `test_quick_workflow.py`.
  `project_status` 는 fast_path 미선언 → project-status-digest 스킬(full agent)이 전담.
- 테스트: `tests/gateway/test_skill_fast_path.py` 10개 통과. slack adapter 기존 210개 회귀 없음.

### 남은 단계

- ⏳ **Phase 3** — 새 fast_path 핸들러/스킬: `today_calendar`(ms365 MCP) 등. 단, 합성 필요한 건 fast_path 없이 스킬로.
- ⏳ **Phase 4** — `skill_fast_path.jsonl` daily/weekly summary cron (어떤 질문이 fast-path 승격 후보인지 집계).

상세 설계 의도는 아래 §11 각 Phase 참조 (단, "별도 router/registry" → "스킬 fast_path" 로 대체됨).

## 1. 문제 정의

현재 Hermes Slack 응답은 짧고 반복적인 질문에도 기본적으로 full agent loop를 탄다.

예:
- "내 진행중인 프로젝트 현황"
- "우리팀 인원"
- "오늘 일정"
- "이 쓰레드 요약해줘"
- "앱파트 전원 + 7층 회의실 빈 시간 찾아줘"

이 질문들은 대부분 "복잡한 추론"보다 "질의 파악 + 정해진 소스 조회 + 짧은 포맷팅"에 가깝다.

그런데 full agent loop는 매번 다음 비용을 낸다.

- 긴 system/persona/memory context 구성
- skill scan 및 tool schema 주입
- 모델이 매번 실행 계획을 새로 판단
- tool call 후 모델 재해석
- provider stream/timeout/retry 상태 노출 위험

결과적으로 cookie.alter 대비 Hermes가 "느리고 에러가 많이 보이는" Slack UX를 만든다.

핵심 문제는 "도구 사용"이 아니다. 빠른 응답도 도구를 써야 한다.
문제는 "매번 full autonomous agent loop로 도구 사용 계획까지 모델에게 맡기는 것"이다.

## 2. 제품 원칙

Hermes alter surface는 agent-first가 아니라 router-first여야 한다.

목표 비율:

- 70%: lightweight tool workflow
- 20%: guided workflow with approval/fallback
- 10%: full agent loop

원칙:

1. 반복 질문은 정해진 workflow로 처리한다.
2. workflow도 필요한 도구를 사용한다.
3. 캐시는 1차 범위에서 제외한다.
4. 불확실하면 확인된 범위만 답하고 full agent로 fallback한다.
5. provider retry/TTFB 같은 내부 상태는 Slack 사용자에게 직접 노출하지 않는다.
6. 외부 write/공유/초대 발송 같은 side effect는 quick workflow에서 바로 실행하지 않고 approval로 넘긴다.

## 3. 개념: Lightweight Tool Workflow

기존 흐름:

```text
Slack message
→ Hermes full agent loop
→ model decides tools
→ tools execute
→ model interprets tool results
→ final answer
```

개선 흐름:

```text
Slack message
→ thread/root/context 최소 조회
→ lightweight intent router
→ deterministic workflow executes tools
→ short formatter
→ final answer
```

즉, 빠른 이유는 캐시가 아니라 "작업 계획을 매번 LLM에게 새로 맡기지 않기 때문"이다.

## 4. 라우팅 레이어

Slack gateway 앞단에 `quick_workflow_router`를 둔다.

입력:
- platform / surface
- channel id
- thread_ts
- sender
- normalized visible text
- fetched thread root/prior replies, 필요한 경우

출력:

```json
{
  "route": "quick_workflow | guided_workflow | full_agent | ignore | react",
  "intent": "project_status",
  "confidence": 0.94,
  "workflow": "project_status.v1",
  "reason": "matched frequent project-status pattern"
}
```

라우팅 기준:

- root/channel mention gating은 기존 Slack policy 유지
- thread context가 필요한 판단은 root + prior replies를 먼저 읽음
- intent가 명확하고 workflow가 존재하면 quick/guided workflow
- 복합 요청, 불명확한 요청, 새 작업 설계는 full agent

## 5. 1차 intent 후보

### 5.1 `project_status`

사용자 질문 예:
- "내 진행중인 프로젝트 현황"
- "내 프로젝트 뭐 있지?"
- "프로젝트 현황 정리해줘"

workflow:
1. project registry 조회
   - `~/.my-alter/projects/*.json`
   - 필요 시 `~/.hermes/context/projects.yaml` 추가
2. project별 상태/source/agent_endpoint 확인
3. agent_endpoint가 있으면 짧은 질의로 보강
4. 5~8줄 요약 답변
5. source가 없으면 full agent fallback

### 5.2 `team_members`

사용자 질문 예:
- "우리팀 인원"
- "앱파트 전원"
- "app 파트 누구야?"

workflow:
1. `~/.hermes/context/people.yaml` 조회
2. team/part alias resolve
3. 이름, 역할, 소속만 짧게 출력
4. 모호하면 확인 질문 또는 full agent fallback

### 5.3 `today_calendar`

사용자 질문 예:
- "오늘 일정"
- "오늘 회의 뭐 있어?"

workflow:
1. ms365 MCP calendar events 조회
2. 시간순 정렬
3. 회의/개인 일정 구분 가능한 범위에서 요약
4. 미팅 초대 자체는 TODO로 분류하지 않음

### 5.4 `slack_thread_summary`

사용자 질문 예:
- "이 쓰레드 요약해줘"
- "이거 무슨 내용이야?"
- "방금 쓰레드 결론 뭐야?"

workflow:
1. Slack replies fetch
2. root + prior replies 구성
3. 짧은 summarizer 또는 규칙 기반 요약
4. 결정/요청/다음 액션 분리
5. thread fetch 실패 시 명시적으로 실패 이유 답변

### 5.5 `meeting_availability_prepare`

사용자 질문 예:
- "앱파트 전원, 7층 회의실 빈 시간 찾아줘"
- "이 사람들 가능한 시간 봐줘"

workflow:
1. people.yaml에서 참석자 resolve
2. room list / 7층 회의실 resolve
3. ms365 MCP free/busy + room availability 조회
4. 후보 시간 2~3개 제안
5. 인비 발송은 approval 필요
6. 참석자/회의실 모호하면 fallback reason 기록 후 질문

## 6. Workflow Registry 예시

```yaml
quick_workflows:
  project_status:
    version: v1
    patterns:
      - "진행중인 프로젝트"
      - "내 프로젝트"
      - "프로젝트 현황"
    route: quick_workflow
    sources:
      - project_registry
    tools:
      - project_registry.read
    fallback: full_agent

  team_members:
    version: v1
    patterns:
      - "우리팀 인원"
      - "앱파트 인원"
      - "app 파트 전원"
    route: quick_workflow
    sources:
      - people_yaml
    tools:
      - people_yaml.read
    fallback: ask_or_full_agent

  meeting_availability_prepare:
    version: v1
    patterns:
      - "빈 시간"
      - "회의실"
      - "인비 보낼 준비"
    route: guided_workflow
    sources:
      - people_yaml
      - ms365_mcp
    tools:
      - people_yaml.read
      - ms365.find_meeting_times
      - ms365.get_room_availability
    side_effects:
      - calendar_event_create_requires_approval
    fallback: full_agent
```

## 7. Slack 상태 메시지 정책

사용자에게 노출할 것:

- "확인 중이야."
- "조금 걸려. 먼저 확인된 범위만 말하면..."
- "참석자 확인이 애매해서 여기서 멈췄어. 후보는 A/B야."

사용자에게 노출하지 않을 것:

- `No first byte from provider`
- `Retrying in 2.4s`
- `APIConnectionError`
- provider/base_url/model 내부 오류 문자열

내부 오류는 gateway/error log와 workflow log에만 기록한다.

권장 UX:

```text
0~8초: 무응답 허용
8초 초과: 1회 lightweight ack
30초 초과: partial/fallback message
provider retry: log only
```

## 8. Workflow Logging

목적:
- 어떤 반복 질문이 많은지 확인
- 어떤 workflow가 full agent fallback을 많이 타는지 확인
- 정식 workflow 승격 후보를 찾기
- Slack alter UX 개선 효과를 수치화하기

로그 위치 후보:
- `~/.hermes/logs/quick_workflows.jsonl`
- 또는 Hermes audit DB/table로 통합

원문 전체를 중복 저장하지 않는다. 필요하면 Slack thread_ts/session_id로 역참조한다.

성공 로그 예:

```json
{
  "ts": "2026-05-26T15:00:00+09:00",
  "surface": "slack",
  "channel": "C0ANUN2AQER",
  "thread_ts": "1779758867.252549",
  "user_key": "owner",
  "intent": "project_status",
  "workflow": "project_status.v1",
  "route": "quick_workflow",
  "status": "success",
  "latency_ms": 820,
  "tools": ["project_registry.read"],
  "fallback": null,
  "confidence": 0.94
}
```

Fallback 로그 예:

```json
{
  "ts": "2026-05-26T15:01:00+09:00",
  "surface": "slack",
  "channel": "C0ANUN2AQER",
  "thread_ts": "1779758867.252549",
  "user_key": "owner",
  "intent": "meeting_availability_prepare",
  "workflow": "meeting_availability_prepare.v1",
  "route": "guided_workflow",
  "status": "fallback",
  "fallback": "full_agent",
  "reason": "attendee_resolution_ambiguous",
  "latency_ms": 1430,
  "tools": ["people_yaml.read"]
}
```

## 9. 주기적 정리

### Daily summary

집계 항목:
- intent별 요청 수
- workflow별 success/fallback/error 수
- fallback reason top N
- p50/p90 latency
- 새 intent 후보

예시 출력:

```text
어제 quick workflow 42건
success 31, fallback 9, error 2
가장 많이 쓴 intent: team_members 12건, project_status 8건
가장 많은 fallback: meeting_availability_prepare / attendee_resolution_ambiguous 4건
신규 후보: "최근 내가 하던 거" 5건
```

### Weekly review

집계 항목:
- full agent loop로 갈 필요 없었던 요청 비율
- 새 workflow 후보
- 수정/제거할 workflow
- 품질 신호: 즉시 정정, 재질문, approval 전환률

## 10. 품질 신호

정량 지표:
- workflow success rate
- fallback rate
- p50/p90 latency
- full agent 대비 절감 시간
- workflow별 오류율

정성/행동 지표:
- 답변 직후 "아니야", "틀렸어", "그게 아니라" 패턴
- 같은 질문 반복 여부
- full agent fallback 이후 사용자가 만족했는지
- approval까지 자연스럽게 이어졌는지

## 11. 구현 순서

### Phase 1: 로깅 없는 최소 router

- gateway 앞단에 router hook 추가
- `team_members`, `project_status` 2개 intent만 처리
- 실패 시 full agent fallback
- Slack에 provider retry/TTFB 노출 금지

### Phase 2: workflow logging

- quick workflow jsonl 로그 추가
- success/fallback/error schema 고정
- latency 측정
- 원문 저장 금지, thread/session 참조만 저장

### Phase 3: intent 확장

- `today_calendar`
- `slack_thread_summary`
- `meeting_availability_prepare`

### Phase 4: daily/weekly summary

- cron 또는 gateway scheduler에서 summary 생성
- Cookie에게 짧은 운영 리포트 전달
- 반복 fallback을 새 workflow 후보로 제안

### Phase 5: workflow registry config화

- hardcoded pattern에서 YAML/JSON registry로 이동
- workflow versioning
- enable/disable 플래그

## 12. 비범위

1차 범위에서 제외:
- 답변 캐시
- 복잡한 semantic router 학습
- 자동 workflow 생성/배포
- 외부 write 자동 실행
- full agent 제거

캐시는 나중에 추가한다. 지금 우선순위는 캐시보다 routing/workflow/logging이다.

## 13. 기대 효과

사용자 경험:
- 자주 묻는 질문은 1~3초 안에 근거 있는 답변
- 내부 provider 오류 메시지 노출 감소
- cookie.alter의 빠른 반사신경 회복
- Hermes의 도구/확장성 유지

운영 효과:
- 반복 질문을 실제 사용량 기준으로 workflow화
- full agent loop 비용과 latency 감소
- 실패/fallback 패턴 기반으로 개선 우선순위 결정
- Slack alter surface가 점점 Cookie 업무에 최적화됨

## 14. 한 줄 요약

Hermes Slack alter는 매번 full agent loop를 돌리는 범용 챗봇이 아니라,
반복 업무를 lightweight tool workflow로 먼저 처리하고,
불확실하거나 복잡한 요청만 full agent로 넘기는 router-first agent surface가 되어야 한다.
