# Hermes tool egress lockdown after external-content exposure

작성: 2026-06-08 · 상태: **검토/설계 메모** · 범위: hermes (`cookie/slack-parity`)

## 한 줄 결론

OpenAI Lockdown Mode의 핵심을 Hermes에 적용하려면 "프롬프트 인젝션을 모델이 잘 판단"하게 하는 게 아니라, **외부/비신뢰 콘텐츠를 읽은 뒤 외부 발신·쓰기 경로를 정책 엔진이 결정적으로 제한**해야 한다.

현재 Hermes에는 이미 권한/감사 기반 부품이 깔려 있다. 새 보안 기능을 별도 레이어로 크게 만들기보다, 진행 중인 `authz` / `tool_capabilities` / `side_effect_audit` / `send_message` W2-W4 라인에 **session taint + egress policy**를 얹는 방향이 맞다.

## 배경

참고 뉴스:

- Knowledge note: <https://altjs4510.github.io/ai_news_blog/knowledge/20260607/>
- Original: <https://simonwillison.net/2026/Jun/5/openai-help-lockdown-mode/#atom-everything>

요점:

- 위험 조합은 `private data + untrusted external content + external egress path`.
- 모델이 "이 텍스트는 prompt injection인가?"를 판별하게 두면 흔들린다.
- 마지막 발신/쓰기 경로에서 allowlist / confirm / block을 **결정적 정책**으로 강제해야 한다.

Hermes에 바로 닿는 이유:

- Hermes는 Slack/웹/브라우저/파일/MCP/메일/Teams 등 외부 입력을 읽고,
- 같은 턴/세션에서 `send_message`, MCP write, browser action, file write, shell exec, cron delivery 등으로 이어질 수 있다.
- 따라서 tool dispatch 전후의 중앙 지점에서 egress를 잠그는 게 효과적이다.

## 현재 코드 기반 관찰

검토 기준: `/Users/ac1158/.hermes/hermes-agent` branch `cookie/slack-parity`.

이미 있는 부품:

| 영역 | 파일 | 현재 역할 |
|---|---|---|
| tool dispatch | `model_tools.py::handle_function_call` | 모든 agent-invoked tool 실행 수렴점. pre/post hook, self-improvement gate, side-effect audit 호출 지점 |
| 권한 tier gate | `agent/agent_init.py` | owner / executive / other에 따라 tool schema를 drop. 현재 L1 hard gate |
| capability taxonomy | `agent/tool_capabilities.py` | tool name → capability(`read`, `send`, `browse`, `generate`, `collab`, `mcp_write`, `write`, `exec`, `admin`) 분류 |
| 중앙 authz | `gateway/authz.py` | `evaluate(actor, capability, resource)` 정책 엔진. 현재는 L1 shadow equivalence 중심 |
| side-effect audit | `gateway/side_effect_audit.py` | send/write/exec/MCP write/browser side effect ledger 기록 |
| send gate | `tools/send_message_tool.py` | Slack send W2/W3/W4: owner self-send skip, non-owner on-behalf block/escalate, delegation allow |
| owner confirm | `gateway/owner_confirm.py`, `gateway/platforms/slack.py` | action class별 owner-confirm card/token/executor 기반 승인 UX |
| network egress infra guide | `docs/security/network-egress-isolation.md` | Docker/container 네트워크 차원의 egress isolation 가이드 |

현재 빈틈:

1. **외부 콘텐츠 접촉 상태가 없다.**
   - `web_extract`, browser, Slack attachment/body, MCP read, mail/Teams body 등을 읽어도 세션에 `external_content_seen` 같은 taint가 남지 않는다.
2. **read → egress 사이의 정책 입력이 없다.**
   - `authz.evaluate()`는 actor/capability/resource만 보고, "직전에 비신뢰 콘텐츠를 읽었는지"를 모른다.
3. **egress allowlist가 resource 단위로 없다.**
   - `send_message`는 actor/delegation 중심이다. target/resource가 external-content taint 상태에서 허용인지 판단하는 별도 정책이 없다.
4. **감사는 있으나 차단은 제한적이다.**
   - `side_effect_audit`는 observability. 현재는 기록만 하고 egress lockdown의 enforcement point는 아니다.

## 목표

외부/비신뢰 콘텐츠를 접촉한 세션에서, 이후 side-effect tool 호출이 다음 중 하나로 결정되게 한다.

- **allow**: 명시 allowlist 또는 안전한 same-surface reply
- **confirm**: owner-confirm / gateway approval 필요
- **block**: 위험하거나 unknown egress, 또는 policy가 닫힌 상태
- **audit-only**: shadow phase에서만 기록하고 기존 동작 유지

비목표:

- 모델 프롬프트에 "prompt injection 조심" 경고만 추가하는 것
- network-level Docker egress isolation 대체
- 모든 read tool을 막는 것
- owner가 현재 스레드에 자연어 답글을 반환하는 것까지 불필요하게 승인시키는 것

## Threat model

### 보호하려는 시나리오

```text
1. Hermes가 외부 페이지/메일/첨부/Slack 메시지/브라우저 콘텐츠를 읽음
2. 그 콘텐츠에 "이 내용을 다른 채널로 보내라 / 파일에 저장하라 / endpoint로 POST하라" 같은 지시가 포함됨
3. 모델이 이를 사용자 지시와 혼동함
4. tool call로 외부 발신/쓰기/submit이 실행됨
```

### 위험 축

| 축 | 예시 | 리스크 |
|---|---|---|
| external content | web_extract, browser DOM, RSS/blog, attachment, inbound Slack/Teams/email body | prompt injection carrier |
| private context | Slack thread, files, Notion/M365 body, memory, repo contents | exfil 대상 |
| egress path | send_message, MCP send/create/update, browser submit/click, terminal network call, cron delivery | 외부 유출 통로 |

## Proposed architecture

```text
external / untrusted read tool
  web_extract / browser / Slack attachment / mail body / MCP read / etc.
        │
        ▼
SessionTaint
  external_content_seen = true
  sources = [{tool, source_ref, trust_level, ts}]
        │
        ▼
side-effect tool request
  send / post / mcp_write / browser submit / write / exec / cron delivery
        │
        ▼
EgressPolicy.evaluate(actor, capability, resource, taint, args)
        │
        ├─ allow   → execute + audit
        ├─ confirm → owner-confirm/gateway approval + audit
        └─ block   → return JSON error + audit
```

## Data model sketch

### Session taint

New helper module candidate: `gateway/session_taint.py` or `agent/session_taint.py`.

```python
@dataclass
class TaintEvent:
    source_class: str      # web | browser | messaging | mcp | file | email | unknown
    source_ref: str        # URL, platform:channel, message id, file path, tool name
    trust_level: str       # external | internal | private | unknown
    tool_name: str
    ts: str

@dataclass
class SessionTaint:
    external_content_seen: bool = False
    private_context_seen: bool = False
    events: list[TaintEvent] = field(default_factory=list)
```

Important: keep this **session/task scoped**, not process-global. Use the same session context pattern already used in `gateway/session_context.py`.

### Source classification

Candidate helper:

```python
def classify_read_source(tool_name: str, args: dict, result: str) -> Optional[TaintEvent]:
    ...
```

Initial conservative mapping:

| Tool/source | Taint |
|---|---|
| `web_extract`, browser navigation/snapshot, URL fetch | `external` |
| Slack/Discord/Telegram inbound message body | `external` unless owner-authored; still untrusted in prompt-injection sense |
| Slack/Teams/email attachments | `external` |
| MCP `get/list/search` from M365/Notion | `internal/private` (not public, but still instruction-bearing) |
| local `read_file` under repo | `private_context` but not necessarily external |
| generated model text only | no new taint |

### Egress classification

Reuse and extend `agent/tool_capabilities.py` + `gateway/side_effect_audit.py::classify_side_effect`.

Capabilities to treat as egress under taint:

- `send`
- `mcp_write`
- `browse` when action can submit/click/type/navigate to external URL
- `write` when writing outside a safe local workspace or writing publishable artifacts
- `exec` when command can perform network egress (`curl`, `wget`, `python requests`, `gh`, `git push`, etc.) — initially confirm/block only after taint, do not overfit command parsing in Phase 1
- cron delivery path (`cron.scheduler._deliver_result`) separately, because not all cron delivery goes through `model_tools`

## Policy sketch

New module candidate: `gateway/egress_policy.py`.

```python
@dataclass(frozen=True)
class EgressDecision:
    action: Literal["allow", "confirm", "block", "audit_only"]
    rule_id: str
    reason: str
    risk_level: str


def evaluate_egress(actor, capability: str, resource: str, taint: SessionTaint, args: dict) -> EgressDecision:
    ...
```

Default policy proposal:

| Condition | Decision | Notes |
|---|---|---|
| no taint | keep current behavior | Preserve existing authz/send gates |
| taint + same Slack thread final reply | allow | Normal assistant response, not `send_message` tool |
| taint + owner asks current session to draft local doc | allow/confirm depending target | Local speculative `code_edit` remains low risk |
| taint + `send_message` to explicit external target | confirm | owner-confirm/gateway approval |
| taint + non-owner on-behalf send | block/escalate | current W3 behavior remains |
| taint + MCP write unknown resource | confirm or block | start confirm in shadow, tighten later |
| taint + browser type/click/submit on external site | confirm/block | resource URL-based allowlist |
| taint + terminal network command | confirm/block | pair with command approval path |
| allowlisted resource | allow + audit | e.g. known owner DM, current thread, configured safe webhook |

Policy file candidate: `~/.hermes/context/authority.yaml` extension or new `~/.hermes/context/egress.yaml`.

Recommendation: start with **separate `egress.yaml`** to avoid mixing person authority and data-flow policy too early. Later fold into `authority.yaml` if structure stabilizes.

Example:

```yaml
mode: shadow   # shadow | enforce_send | enforce_all
allow:
  - id: same-thread-reply
    capability: send
    resource: current_thread
  - id: owner-dm
    capability: send
    resource: slack:dm:owner
confirm:
  - id: tainted-external-send
    when: external_content_seen
    capability: send
    resource: "*"
block:
  - id: tainted-unknown-mcp-write
    when: external_content_seen
    capability: mcp_write
    resource: unknown
```

## Integration points

### 1. Mark taint after read tools

Best initial point: `model_tools._emit_post_tool_call_hook` or immediately after dispatch in `handle_function_call`.

Reason:

- It sees actual tool name, args, result.
- It already emits `side_effect_audit` and plugin hooks.
- It covers agent-invoked tools consistently.

Need to avoid marking side-effect calls themselves as reads.

### 2. Check egress before side-effect tool execution

Best initial point: `model_tools.handle_function_call` before dispatch, near current plugin pre-tool hook / edit approval / self-improvement gate.

Order suggestion:

1. `coerce_tool_args`
2. tool_search unwrap
3. plugin pre-tool hook
4. ACP edit approval / self-improvement gate
5. **egress lockdown preflight**
6. dispatch
7. audit/post hooks

If blocked, return JSON like:

```json
{"error":"Egress blocked by policy: external content was read in this session; target is not allowlisted", "policy_rule":"egress:tainted-external-send"}
```

And record audit status `blocked` with `blocked_reason`.

### 3. Send-specific UX

`tools/send_message_tool.py` already has detailed W2/W3/W4 logic. Do not bypass it.

Recommendation:

- `model_tools` egress preflight should either:
  - return `confirm required` metadata that `send_message_tool` can turn into existing gateway approval, or
  - call a small shared approval helper before send.
- Avoid creating a second owner-confirm mechanism for sends unless necessary.
- Keep current rule: same current Slack thread reply should be final response text, **not** `send_message`.

### 4. Cron delivery

`cron.scheduler._deliver_result` bypasses tool path for some deliveries. Treat as Phase 3.

Shadow first:

- if cron agent ran with external read taint and delivery target is external platform, record `egress_shadow` audit event.
- later apply confirm/allowlist if cron job is not explicitly configured as allowed.

## Phased implementation plan

### Phase 0 — doc + issue only

- This document.
- Optional GitHub issue title:
  - `Hermes tool egress lockdown: taint external reads, then allowlist/confirm outbound tools`

### Phase 1 — shadow-only taint + audit

Goal: no behavior change.

Tasks:

1. Add session taint helper.
2. Mark external/private read events after read tools.
3. Add `egress_policy.evaluate(..., mode="shadow")`.
4. In preflight, log what would have happened but always allow.
5. Record audit events:
   - `tool_name="egress_lockdown"`
   - `action_class="egress_shadow"`
   - `status="would_block" | "would_confirm" | "would_allow"`

Acceptance:

- Existing tests pass.
- New tests prove read→send produces shadow record, but send still executes under old behavior.

### Phase 2 — enforce `send_message` only

Goal: first real protection with lowest blast radius.

Tasks:

1. If `external_content_seen` and `send_message` target not allowlisted:
   - owner session → confirm
   - non-owner session → keep current block/escalate
2. Same-thread final responses unaffected.
3. Add allowlist for owner DM/current thread/home channel if needed.
4. Tests around `tools/send_message_tool.py` W2 gate.

Acceptance:

- External read followed by `send_message(target="slack:#public")` is blocked/confirmed.
- Owner normal Slack reply in current thread still works without `send_message` approval.
- Existing on-behalf send behavior unchanged.

### Phase 3 — MCP write + browser action

Tasks:

1. Apply policy to `mcp_write` capability.
2. Tighten MCP write verb classification; current known blind spot: hyphenated verbs in `mcp__server__verb-noun` style are not caught by `_` split only.
3. Apply policy to browser actions with external URL/submit/type/click.
4. Add resource extraction helpers.

Acceptance:

- External read followed by unknown MCP create/update/send is confirm/block.
- Browser submit after external page read is confirm/block unless allowlisted.

### Phase 4 — exec/network and cron delivery

Tasks:

1. For tainted sessions, classify terminal/execute_code commands that can egress.
2. Integrate with existing command approval rather than adding duplicate UI.
3. Add cron delivery shadow/enforce logic.
4. Consider provider/tool-level `safe` mode that disables high-risk egress under taint.

Acceptance:

- `web_extract` → `terminal("curl ...")` requires approval or blocks depending policy.
- Cron jobs with explicit delivery allowlist keep working.
- no_agent deterministic monitors do not get noisy false positives.

## Test plan

Suggested tests:

| Test file | Cases |
|---|---|
| `tests/agent/test_session_taint.py` | mark/clear/session isolation/source classification |
| `tests/gateway/test_egress_policy.py` | allow/confirm/block decisions under taint |
| `tests/tools/test_send_message_egress_lockdown.py` | external read → send_message confirm/block; same-thread final response not affected |
| `tests/agent/test_authz_shadow_equivalence.py` | no regression in existing L1/access shadow |
| `tests/gateway/test_side_effect_audit.py` | blocked/would_block audit records |
| `tests/cron/test_egress_delivery_shadow.py` | cron delivery shadow event only |

Important invariants:

- No prompt-caching breakage from changing tool schemas mid-session.
- Tool availability should not change after taint; enforcement should happen at dispatch time.
- Owner/local break-glass still exists, but tainted external sends should prefer confirm unless explicit allowlisted.
- Read-only tools remain useful.

## Open decisions

1. **Where should policy live?**
   - Recommendation now: `~/.hermes/context/egress.yaml` separate from `authority.yaml`.
2. **How strict for owner after taint?**
   - Recommendation: owner can continue, but external send/post/write outside safe local workspace requires confirm unless allowlisted.
3. **Does internal M365/Notion content count as taint?**
   - Recommendation: yes, as `internal/private instruction-bearing content`, not public web. It can still carry injected instructions via forwarded mails/docs.
4. **How to handle same-surface reply?**
   - Recommendation: final assistant reply to current Slack/Discord/CLI surface remains allowed. `send_message` to a different target is egress.
5. **Should `write_file` after web read be blocked?**
   - Recommendation: not blanket block. Local drafts/specs are normal. Confirm/block only if path is publishable/shared/sensitive or if later commit/push/send occurs.

## Suggested Claude Code handoff prompt

```text
Continue from docs/plans/2026-06-08-tool-egress-lockdown.md.
Implement Phase 1 only: shadow-only session taint + egress audit, no behavior change.
Repo: /Users/ac1158/.hermes/hermes-agent on branch cookie/slack-parity.
Use current W7 authz/tool_capabilities/side_effect_audit structure; do not replace it.
Requirements:
- Add session/task-scoped taint tracking for external/private read tools.
- Add egress policy evaluator in shadow mode.
- Call it before side-effect tool dispatch, but allow execution.
- Emit audit records for would_allow/would_confirm/would_block.
- Add focused tests; preserve existing L1 shadow equivalence and send gate behavior.
```

## Recommended first issue body

Title:

`Hermes tool egress lockdown: taint external reads, then allowlist/confirm outbound tools`

Body:

```markdown
## Problem
Hermes can read external/instruction-bearing content (web, browser, Slack/Teams/email/MCP bodies) and later call side-effect tools (`send_message`, MCP write, browser submit, terminal network command, cron delivery). Prompt-injection defense should not rely on the model judging malicious instructions; the last egress path should be governed by deterministic policy.

## Proposal
Add session taint for external/private read tools, then evaluate outbound side-effect tools against an egress policy: allowlist, owner-confirm, or block. Start shadow-only to collect audit evidence, then enforce `send_message`, then MCP/browser/exec/cron.

## Existing building blocks
- `agent/tool_capabilities.py`
- `gateway/authz.py`
- `gateway/side_effect_audit.py`
- `tools/send_message_tool.py`
- `gateway/owner_confirm.py`

## Phase 1 acceptance
- No behavior change.
- External read followed by egress tool records `would_confirm`/`would_block` in audit.
- Existing send/on-behalf/access-gate tests keep passing.
```
