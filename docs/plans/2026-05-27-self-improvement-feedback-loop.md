# 자가발전 피드백 루프 (owner-confirmed)

작성: 2026-05-27 · 상태: 설계(쿠키 컨펌 대기) · 범위: hermes (`cookie/slack-parity`)

## 목표

non-owner(팀원/이사님)가 **봇 자신에 대한 피드백/개선요청**을 주면, 봇이 ① 진행여부 1차 판단 → ② 자가발전 계획 수립 → ③ **누가 줬는지(우선순위 포함)** 와 함께 쿠키에게 컨펌 요청 → ④ 쿠키 승인 시 실행. read-only 게이트는 안전 바닥으로 유지하고, "막다른 거절" 대신 "제안 파이프라인"을 얹는다.

## 핵심 단순화 (쿠키 피드백 2026-05-27)

- **편집 surface용 코드 추가 X.** 자가발전 범위 = "쿠키가 평소 hermes에게 시킬 수 있는 것"과 동일. 실행은 **승인된 계획을 owner 컨텍스트에서 hermes가 그냥 수행** (SOUL/프롬프트/스킬/config… owner가 할 수 있는 일 무엇이든). 전용 편집기 불필요.
- **게스트쪽을 거스르지 않는다.** 피드백 주는 게스트한테는 *거절*이 아니라 **"피드백 감사합니다, 이렇게 개선 제안하겠습니다" 긍정 마무리**. 컨펌·실행 게이팅은 **백그라운드(쿠키쪽)**에서 일어나고 게스트에겐 안 보임. (write/action 요청의 read-only 하드거절과는 별개 — 그건 기존대로 거절+멘션.)
- 따라서 새 코드 = ①게스트 접수응답 + ②계획 + ③쿠키 컨펌 + "승인 시 owner 재디스패치".

## 흐름 (게스트-facing vs owner-facing 분리)

```
게스트(non-owner) 피드백
 │
 ├─[게스트쪽, 즉시] ───────────────────────────────
 │   ① 1차 판단 [read-only]: 봇 개선 피드백인가?
 │   ② 게스트에게 긍정 응답: "피드백 감사합니다 — [이렇게 개선]하겠습니다 :pray:"
 │      (거절 아님. 여기서 게스트 대화는 마무리. junk면 가볍게 감사만, 제안 안 만듦)
 │
 └─[쿠키쪽, 백그라운드] ──────────────────────────
     ③ 컨펌 카드 → 쿠키 DM(또는 전용 채널):
        피드백 원문 + 제공자(이름·직함·우선순위) + 제안 계획 + [승인][거절]
     ④ 쿠키 승인 → 쿠키 컨텍스트(쿠키 스레드)에서 owner 권한 실행 (+audit)
        거절/무시 → 큐에 남김 (우선순위 낮으면 미뤄짐)
```

게스트는 "접수+개선예정"만 보고 끝 — 게이팅·실행은 전부 쿠키쪽 백그라운드. ①② 는 도구 없이 텍스트라 read-only 게이트와 무충돌. 실제 변경(④)은 쿠키 승인 후 owner 권한으로만.

## 제공자 → 우선순위 (people.json 기반)

`~/.claude/knowledge/world/people.json` (= `~/.my-alter/people.json`, 76명) 의 `slackId → {role, team, title, name}` 로 자동 도출. (라이브 표시이름 매칭 X → 위조 불가, 큐레이트된 로스터)

| 조건 | tier | 카드 표기 | 처리 |
|---|---|---|---|
| `role == executive` | **high / urgent** | "이사 박봉섭" 등 | 상단 강조 + 즉시 쿠키 DM |
| `team == PRCS` (비-executive) | normal | "PRCS 팀원 OOO" | 큐 적재, 일반 알림 (미뤄도 됨) |
| 로스터 등록·기타 | low | "OOO" | 큐 적재 |
| 미등록 slackId | low / 외부 | "외부 사용자" | 큐 적재 |

- 이사님 박봉섭 = `U0AM13JAWM8` (role=executive). 별도 ID 하드코딩 불필요 — people.json이 소스.
- hermes는 `~/.claude/knowledge/world/people.json` 을 읽기로 참조 (cross-read, read-only).

## 재사용 부품 (재구현 X)

- `gateway/owner_confirm.py` — `OwnerConfirmStore`(propose/confirm/reject/execute) + Block Kit 승인 버튼 + 토큰. 현재 "Slack 전송"용 → **자가발전 제안 action_class 추가**. ④의 executor = "승인된 계획을 owner로 실행".
- `agent/curator.py` — 자가발전/큐레이션 기존 로직.
- non-owner read-only 게이트 ([[project_hermes_access_control]]) — ①② 안전 보장.
- `people.json` 로스터 ([[reference_fnf_people_roster]]).

## 단계적 구현

- **Phase 1**: ①②③ — 피드백 감지 → 1차 판단 → 계획 → 쿠키 컨펌 카드(제공자·우선순위 포함). ④ 승인 시 실행까지 owner_confirm executor로 연결.
- **Phase 2**: 우선순위 큐 뷰/정렬, 미뤄둔 피드백 리마인드, audit/롤백 강화.

## 결정 (2026-05-27 확정)

1. **컨펌 카드 목적지** = 쿠키 DM (`U0AMDSG0Q49`). (전용 채널은 추후)
2. **피드백 감지** = 모델 자동 인지 + 명시(`피드백:`) 둘 다.
3. 편집범위·이사님ID는 해소됨 (owner 실행 / people.json).

## 구현 계획

핵심 문제: 게스트한테 "개선하겠다" 약속만 하고 실제 제안이 쿠키한테 안 가면 **공약(空約)**. 그래서 Phase 1은 "게스트 접수응답 + 쿠키 라우팅"을 **함께** 묶어야 함(①②③). ④ 실행은 Phase 2.

**메커니즘 후보 (구현 시 택1):**
- (a) **propose 도구 신설** — `propose_self_improvement(feedback, plan)` 를 non-owner 허용목록에 추가(외부 쓰기 아님, 큐+owner DM만). 모델이 피드백 감지 시 호출 → 게스트 ack 반환 + 쿠키 DM 카드. 깔끔하나 tool registry 손봄.
- (b) **응답 sentinel 후처리** — NO_REPLY 패턴처럼 모델이 `PROPOSE_IMPROVEMENT: {…}` 출력 → 게이트웨이가 가로채 게스트엔 ack로 치환 + 쿠키 DM. base.py 후처리 추가(병렬 세션과 영역 겹침 주의).

**Phase 1 작업 목록:**
- non-owner 자각 프롬프트에 "피드백이면 거절 말고 접수+개선계획, propose 경로로" 추가
- propose 경로(도구 or sentinel) → `OwnerConfirmStore` 에 제안 저장 + 쿠키 DM Block Kit 카드(원문/제공자·우선순위/계획/[승인][거절])
- people.json 우선순위 lookup 유틸 (role=executive→high, team=PRCS→normal, else low)
- read-only 게이트: propose 경로만 non-owner 예외 허용

**Phase 2:** 승인 시 owner 컨텍스트 재디스패치 실행 + audit + 미뤄둔 큐 리마인드/정렬.
