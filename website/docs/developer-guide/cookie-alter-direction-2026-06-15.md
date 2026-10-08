# 쿠키 알터 방향성 — 이사님 AX 전략의 내재화

> **출처:** AX 전략실 보고 2026.06.15 (박봉섭 이사님). 이 문서는 그 5개 기둥을 *쿠키 개인 알터(Hermes 인스턴스)* 에 어떻게 반영할지의 설계 방향이다.
> **용도:** Claude Code 에서 이어서 구현. 각 섹션 끝 `▶ 작업` 이 착수 단위.
> **대상 시스템:** 이 Hermes (`/Users/ac1158/.hermes`, git basis `hermes-agent` @ `cookie/slack-parity`). cookie-jarvis 는 legacy.
> **작성:** 2026-06-15 / 쿠키 알터

---

## 0. 한 줄 결론

이사님 방향성(Sovereign AI · Harness · Dreaming · LLM Wiki · Agent 아키텍처)은 **이미 Hermes 가 가진 1차 기능(스킬 자동생성·영속 메모리·서브에이전트·cron)의 상위 개념**이다. 쿠키 알터는 이걸 *제품 데모*가 아니라 **쿠키 1인의 업무 운영체계로 먼저 완성**해서, 전사 확대(프로세스→디지털본부→전사)의 **레퍼런스 구현(reference implementation)** 이 되는 걸 목표로 한다.

핵심 통찰: **알터 자체가 이사님 아키텍처의 살아있는 증명**이어야 한다. 보고서로 설명하지 말고, 알터가 그렇게 돌아가는 걸 보여준다.

---

## 1. 매핑 — 이사님 5기둥 ↔ 쿠키 알터

| # | 이사님 기둥 (PPT) | Hermes 현재 자산 | 알터 목표 상태 | 갭 |
|---|---|---|---|---|
| 1 | **Sovereign AI / 모델주권** | provider/model 설정 가능, Local LLM 연동 여지 | 데이터 등급별 라우팅 (민감=local/sandboxed, 일반=frontier) | 등급 분류·라우팅 규칙 미정립 |
| 2 | **Harness (Agent 오케스트레이션)** | `delegate_task` 서브에이전트, 역할=스킬 라우팅 | 리더 알터가 역할 알터(PM/researcher/writer/architect…)를 지휘하는 명시적 harness | role 인터페이스·위임 규약 암묵적 |
| 3 | **Dreaming (자율학습 루프)** | 스킬 자동생성, 메모리 add/replace | 주기적 반성·정리·최적화 cron (자는 동안 똑똑해짐) | reflection 루프 부재 ★ |
| 4 | **LLM Wiki (지식 저장소)** | 영속 메모리 + 스킬 + context/*.yaml | 알터 경험·결정이 누적되는 single source Wiki | 산재(memory/skills/yaml/세션) → 통합 SoT 없음 ★ |
| 5 | **Agent 아키텍처 (Data/Agent/Human/Business)** | 단일 알터 + Slack surface | 4축이 명시적으로 분리·연결된 구조 | Data·Business 맥락 레이어 약함 |

★ = 가장 약한 두 곳. 우선순위 상위.

---

## 2. 타깃 아키텍처 (쿠키 알터 내부)

```
                    ┌─────────────────────────────────────┐
   L0 정체성/정책   │ SOUL.md + persona/* (이사님 권한룰)   │  거버넌스 / 변동 느림
                    └───────────────┬─────────────────────┘
                                    │
                    ┌───────────────▼─────────────────────┐
   L1 Harness 규약  │ 역할 알터 인터페이스 + 위임 규약       │  "한 번 정하면 모두 따름"
                    │  (role I/O 계약 · 컨텍스트 전달 표준)  │
                    └───────────────┬─────────────────────┘
          ┌──────────┬──────────────┼────────────┬─────────┐
   L2 역할  │ PM     │ researcher   │ writer     │architect│ …  자율 수행 / L1 준수
          └────┬─────┴──────┬───────┴─────┬──────┴────┬────┘
               └────────────┼─────────────┴───────────┘
                    ┌────────▼─────────────────────────────┐
   L3 지식(Wiki)    │ LLM Wiki = 경험·결정·산출의 SoT       │  single source
                    │  memory + skills + context + 결정로그 │
                    │  ＋ Dreaming cron (반성·정리·최적화)   │
                    └────────┬─────────────────────────────┘
                             │ 누적 지식이 L0/L1 을 갱신 (환류)
                             └──────────► L0·L1 로 피드백
```

**관리의 본질 = L3 → L0/L1 환류 루프를 닫는 것.** 이게 닫혀야 "자율학습"이고, 안 닫히면 그냥 기능 모음.

---

## 3. 기둥별 상세 — 현재/목표/작업

### 3.1 Sovereign AI — 데이터 등급별 모델 라우팅
- **현재:** 단일 provider(anthropic/opus)로 전부 처리. 회사 민감본문도 frontier API 로 감.
- **목표:** 데이터 클래스 → 모델 경로 매핑. 민감(인사·원가·미공개 전략)=local/sandboxed, 일반=frontier.
- **설계 원칙:** 봇 이름이 아니라 *데이터 클래스 + 실제 model/provider 경로*로 판정 (기존 data-policy 원칙 유지).
- `▶ 작업`
  1. `persona/data-policy.md` 에 데이터 클래스 3등급 정의(공개/내부/민감) + 각 등급 허용 경로 표.
  2. 민감 등급용 local LLM 경로 PoC (Gemma/Qwen via ollama) — 분류·요약 등 저위험 태스크부터.
  3. 라우팅 판정을 스킬/체크 함수로 — 호출 전 등급 태깅.

### 3.2 Harness — 역할 알터 오케스트레이션 규약
- **현재:** 역할 = 스킬 라우팅(SOUL §3). `delegate_task` 로 위임 가능하나 role I/O 계약이 암묵적.
- **목표:** 리더 알터가 역할 알터에게 일을 넘길 때의 **명시적 인터페이스** — 입력(목표·컨텍스트·제약), 출력(산출물·근거·다음액션), 컨텍스트 전달 표준.
- **설계 원칙:** Context Window 한계 대응 = 역할별 제한 role 만 수행, 메인 컨텍스트 오염 방지(이미 SOUL 에 있는 원칙을 규약화).
- `▶ 작업`
  1. `docs/harness-role-contract.md` — 역할 알터 I/O 계약 스펙(목표/컨텍스트/제약 → 산출/근거/다음액션).
  2. 각 역할 스킬 frontmatter 에 `io_contract` 섹션 추가(입력 기대·출력 형식).
  3. 위임 시 컨텍스트 패킹 표준(무엇을 넘기고 무엇을 안 넘기나) 명문화.

### 3.3 Dreaming — 자율학습 루프 ★최우선
- **현재:** 스킬 자동생성·메모리 갱신은 *수동/즉시*. 주기적 반성 루프 없음.
- **목표:** 자는 동안(야간 cron) 그날 세션을 반성 → 스킬 갱신/신규 → 메모리 정리 → 비효율 패턴 최적화.
- **설계 원칙:** "AI가 잠자는 동안 스스로 똑똑해진다"(PPT 5p)를 알터에 직접 구현. 세션 DB(session_search) + 스킬 + 메모리를 입력으로.
- `▶ 작업`
  1. `dreaming` cron(매일 새벽) — 전일 세션 리뷰 → ① 반복된 수동작업=스킬화 후보 ② 틀린/낡은 스킬=패치 ③ 메모리 정리 후보 도출.
  2. 산출을 `dreaming/` 일자별 리포트로 저장 + 쿠키 DM 요약(승인형: 적용 전 확인).
  3. 반복 적용으로 자동 적용 신뢰도 쌓이면 저위험 갱신은 auto-apply.

### 3.4 LLM Wiki — 지식 SoT ★최우선
- **현재:** 지식이 4곳에 산재 — `memory`(개인노트) / `skills`(절차) / `context/*.yaml`(사람·프로젝트) / 세션DB. single source 없음.
- **목표:** 알터의 경험·결정·산출이 누적되는 **하나의 Wiki**. 카파시 LLM Wiki 개념을 1인 알터 스케일로.
- **설계 원칙:** 메모리=지금 사실, 스킬=절차, Wiki=경험·결정 누적. 셋 경계 유지하되 Wiki 가 결정로그·프로젝트 지식의 SoT.
- `▶ 작업`
  1. Wiki 구조 정의 — 노드 타입(결정/프로젝트/사람/개념/사례) + 링크. `context/wiki/` 또는 Notion DB.
  2. 기존 산재 지식의 Wiki 매핑(중복 제거, SoT 지정 — 사람=people.yaml 유지, 결정=Wiki 신규).
  3. 결정로그 자동 적재 — 의사결정·중요 산출 시 Wiki 노드 1건 append(쿠키 worklog 스킬과 연계).
  4. Dreaming(3.3) output 을 Wiki 에 환류 → L3→L0 루프 연결.

### 3.5 Agent 아키텍처 — 4축 명시화
- **현재:** Slack surface + 도구 호출 위주. Data·Business 맥락 레이어 약함.
- **목표:** Data(내부/외부/지식그래프) · Agent(역할 harness) · Human(쿠키 in-loop) · Business(맥락·정책) 4축이 명시적으로 연결.
- `▶ 작업`
  1. Data 레이어 — 알터가 닿는 소스 인벤토리(Notion·MS365·DCSAI·분석계) + 접근 경로 표준화(SOUL §2 도구 라우팅 확장).
  2. Business 레이어 — `context/projects.yaml` + 정책을 알터 판단의 명시 입력으로.
  3. Human-in-loop — 승인 게이트 vs 통보 vs 자율 경계 명문화(이미 data-policy/operating 에 있음 → 4축 관점으로 재정리).

---

## 4. 우선순위 & 단계

| 단계 | 무엇 | 왜 먼저 |
|---|---|---|
| **P0** | 3.4 Wiki 구조 정의 + 3.3 Dreaming cron 골격 | ★두 약한 고리. 환류 루프의 양 끝 |
| P1 | 3.2 Harness role contract 명문화 | 역할 알터 품질 일관성 |
| P2 | 3.1 데이터 등급 라우팅 + local LLM PoC | Sovereign 정합, 민감데이터 안전 |
| P3 | 3.5 4축 재정리 (대부분 기존 자산 정리) | 통합 시야 |

**P0 가 닫히면** = 알터가 "쓸수록 똑똑해지고 지식이 한곳에 쌓이는" 상태 → 이사님 방향성의 핵심을 1인 스케일로 증명.

---

## 5. Claude Code 인수 메모

- 이 문서는 **방향**이다. 각 `▶ 작업`을 이슈/태스크로 쪼개 착수.
- 기존 자산 재사용 우선(SOUL §3 역할스킬, memory, cron, session_search, context/*.yaml). 새 코어 도구 만들지 말 것 — Hermes 철학(narrow waist)상 스킬/cron/플러그인으로.
- Hermes 변경은 `cookie/slack-parity` 브랜치. cookie-jarvis 손대지 말 것.
- P0 부터: ① Wiki 노드 스키마 초안 → ② Dreaming cron 프로토타입(읽기전용 리포트만, 적용은 승인형) → ③ 둘 연결.
- 검증 원칙: "개발 완료"는 실제 런타임 동작 확인 후. core/parser-only 는 partial 로 보고.

---

## 6. 미결 — 쿠키 결정 필요
- Wiki 저장소: `context/wiki/` (로컬 md, git 버전관리) vs Notion DB(공유·확대 용이) — **어느 쪽?**
- Dreaming 자동적용 범위: 처음부터 승인형 고정 vs 신뢰 쌓이면 저위험 auto-apply 허용?
- Local LLM: 이번 분기 PoC 우선순위에 넣을지 (인프라·시간 비용).
