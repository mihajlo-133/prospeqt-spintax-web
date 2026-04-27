# Anthropic Integration Plan
**Status:** Ready to execute  
**Date:** 2026-04-27  
**Estimated effort:** ~7.5h focused work (1 day, 4 PRs)

This document scopes adding Anthropic (Claude) as a second AI backend
alongside OpenAI. Both providers will work side-by-side, selected
automatically by model name prefix.

---

## 1. Goal

Add Claude (Opus 4.7 + Sonnet 4.6) as selectable models alongside
the existing OpenAI ones. Default to `claude-opus-4-7` for spintax
generation. **No breaking changes** to existing OpenAI flows.

---

## 2. Why two backends

| Reason | Detail |
|---|---|
| **Speed** | Claude Sonnet 4.6 typically 5-30s vs o3 60-170s. Could cut Enavra wall time from ~4 min to ~1 min. |
| **Quality** | Claude is strong at creative writing — well-suited for cold email copy. |
| **No reasoning-token bloat** | o3's reasoning tokens dominate cost. Claude's `thinking` is opt-in via `effort` param. |
| **Optionality** | If one provider has an outage or pricing change, the other is a model-name-prefix away. |
| **Native structured outputs (Anthropic)** | Claude has `tools` with `input_schema` + `tool_choice: {"type": "tool", "name": "..."}` — cleaner than OpenAI's response_format JSON mode. |

---

## 3. Verified facts (already tested with the real keys)

### OpenAI key has access to
- `gpt-5.5` (chat completions API) — already added 2026-04-27
- `gpt-5.5-pro` (Responses API only — bigger refactor needed)
- `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5-pro` family
- All `o1`/`o3`/`o4-mini` legacy
- 86 reasoning + GPT-4/5 family models total

### Anthropic key has access to
- `claude-opus-4-7` (1M input, 128K output, native structured_outputs, adaptive thinking, effort=low/medium/high/max)
- `claude-sonnet-4-6` (same caps as Opus)

Live test confirmed: `claude-opus-4-7` returns text correctly with 27 input + 12 output tokens for a "say OK" prompt.

---

## 4. Architecture

```
app/llm/
  __init__.py            Resolver: model name → backend instance
  base.py                AIBackend abstract class + ToolLoopResult
  openai_backend.py      Existing OpenAI logic, extracted (no behavior change)
  anthropic_backend.py   NEW: Anthropic implementation
```

### Public interface (matches our actual usage)

```python
class AIBackend(ABC):
    @abstractmethod
    async def parse_with_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        model: str,
        effort: str = "medium",
    ) -> dict:
        """Return JSON matching the given schema."""

    @abstractmethod
    async def run_with_tool_loop(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict],
        tool_handlers: dict[str, callable],
        model: str,
        effort: str = "medium",
        max_iterations: int = 10,
    ) -> ToolLoopResult:
        """Drive an iterative tool-calling loop. Stops when the model
        emits final text without a tool call, or when max_iterations hits."""


@dataclass
class ToolLoopResult:
    final_text: str
    total_cost_usd: float
    iterations: int
    api_calls: int
    converged: bool   # False if max_iterations hit before final text
```

### Backend resolver

```python
def get_backend(model: str) -> AIBackend:
    if model.startswith("claude-"):
        return AnthropicBackend()
    return OpenAIBackend()
```

Pick by model name prefix. No env-var toggle needed. Both backends
read their respective API keys from settings at instantiation time.

---

## 5. Phased rollout (de-risked, ~7.5h)

| # | Phase | Time | Outcome |
|---|---|---|---|
| 1 | **Backend abstraction (no Claude yet)** | 2h | Extract existing OpenAI code into `OpenAIBackend`. Refactor `parser.py` and `spintax_runner.py` to delegate. **No behavior change.** All 275 tests still pass. |
| 2 | **Add AnthropicBackend** | 2.5h | Wrap `anthropic.AsyncAnthropic`. Implement `parse_with_schema` (uses native `tools` + `tool_choice` for structured outputs) and `run_with_tool_loop` (uses Claude's `tool_use` / `tool_result` content blocks). Add `claude-opus-4-7` and `claude-sonnet-4-6` to `MODEL_PRICES`. |
| 3 | **Expose in UI + API** | 0.5h | Add Claude options to model picker. Update `BatchRequest.model` validator. Update CLI help text. |
| 4 | **Live A/B test on Enavra** | 1h | Run same .md through `o3` vs `claude-opus-4-7`. Compare quality, speed, cost. Document findings in a session breadcrumb. |
| 5 | **Switch defaults (if A/B passes)** | 0.5h | Change `OPENAI_MODEL` env var on Render to `claude-opus-4-7`. UI default flips to Claude. Or: keep o3 default, let teammates pick Claude per-request. |
| 6 | **Tests + docs** | 1h | Mock both backends in tests. Update `BATCH_API_SPEC.md` and `TEAM_QUICKSTART.md`. |
| | **Total** | **~7.5h** | |

Ship as 4 PRs across one focused day.

---

## 6. Concrete file changes

| File | Change | Type | LOC |
|---|---|---|---|
| `requirements.txt` | + `anthropic>=0.40.0` | edit | +1 |
| `.env` | + `ANTHROPIC_API_KEY=sk-ant-...` | edit | +1 |
| Render env vars | + `ANTHROPIC_API_KEY` (via PUT API) | runtime | — |
| `app/config.py` | Add `anthropic_api_key` field, expand `MODEL_PRICES` with Claude entries | edit | ~25 |
| `app/llm/__init__.py` | Resolver `get_backend(model)` | new | ~30 |
| `app/llm/base.py` | `AIBackend` abstract + `ToolLoopResult` dataclass | new | ~80 |
| `app/llm/openai_backend.py` | Extracted OpenAI logic | new | ~220 |
| `app/llm/anthropic_backend.py` | Anthropic implementation | new | ~220 |
| `app/parser.py` | Refactor `_parse_single_chunk` to call `backend.parse_with_schema` | edit | ~30 |
| `app/spintax_runner.py` | Refactor tool-loop to call `backend.run_with_tool_loop` | edit | ~80 |
| `app/api_models.py` | `SpintaxRequest.model` validator allows `claude-*` and `gpt-5*` | edit | ~5 |
| `templates/index.html` | Picker: add `claude-opus-4-7`, `claude-sonnet-4-6` | edit | ~5 |
| `static/main.js` | Timer hint table for Claude models | edit | ~10 |
| `tests/test_*.py` | Parametrize by backend, mock both | edit | ~80 |
| `BATCH_API_SPEC.md` | Update model section | edit | ~10 |
| `TEAM_QUICKSTART.md` | Update model picker section | edit | ~10 |

**Total: ~1000 lines touched** (~600 new in `app/llm/`, ~400 modified elsewhere).

---

## 7. Anthropic-specific things to handle

### 7.1 Native structured outputs
Claude has `tools` with `input_schema` + `tool_choice: {"type": "tool", "name": "..."}` for guaranteed JSON. Use this for the parser instead of prompting for JSON.

```python
response = await client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=8192,
    system=system_prompt,
    messages=[{"role": "user", "content": user_msg}],
    tools=[{
        "name": "extract_segments",
        "description": "Extract segments from markdown",
        "input_schema": PARSER_SCHEMA["schema"],
    }],
    tool_choice={"type": "tool", "name": "extract_segments"},
)
parsed = response.content[0].input  # the JSON, guaranteed schema-valid
```

### 7.2 Tool loop has different semantics
Claude returns `content` array with mixed `text` and `tool_use` blocks. Assemble the response message with the full content, then send `tool_result` blocks back as a user message.

```python
# After Claude calls a tool:
assistant_msg = {"role": "assistant", "content": response.content}
tool_results = [
    {"type": "tool_result", "tool_use_id": block.id, "content": result_str}
    for block in response.content if block.type == "tool_use"
]
user_msg = {"role": "user", "content": tool_results}
messages.append(assistant_msg)
messages.append(user_msg)
# Loop again until stop_reason != "tool_use"
```

### 7.3 Effort parameter
Claude supports `low`/`medium`/`high`/`max` (matches OpenAI's `low`/`medium`/`high`). Map directly. `max` is Claude-only.

### 7.4 System prompt placement
Claude takes `system` as a top-level param (string), not as a message with role="system". Trivial change.

### 7.5 Token usage shape
- OpenAI: `usage.prompt_tokens`, `usage.completion_tokens`
- Anthropic: `usage.input_tokens`, `usage.output_tokens`

Cost calculation function needs a branch — or normalize via the backend abstraction.

### 7.6 Thinking
Opus 4.7 supports `thinking: {"type": "adaptive"}` — model decides when to think. Recommended for spintax engine. NOT recommended for parser (waste of tokens for simple extraction).

### 7.7 No reasoning_tokens leak
Claude's `thinking_tokens` are billed but counted separately. Cost calc needs to include them when thinking is enabled.

---

## 8. Pricing for cost estimates

```python
MODEL_PRICES = {
    # OpenAI (existing)
    "gpt-5.5":           {"input": 3.00,  "output": 15.00},
    "o3":                {"input": 2.00,  "output": 8.00},
    "o4-mini":           {"input": 1.10,  "output": 4.40},
    # ... etc

    # Anthropic (NEW)
    "claude-opus-4-7":   {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.00},
}
```

For an Enavra-size batch (5 Email 1 bodies, ~3-5k tokens out each):
- **claude-opus-4-7**: ~$2.50 per batch (more expensive than o3 at $1.20)
- **claude-sonnet-4-6**: ~$0.80 per batch (cheaper than o3)
- Quality probably matches: Opus = best, Sonnet ≈ o3, o3 = baseline

---

## 9. A/B test methodology (Phase 4)

Run the **same** Enavra .md through both backends. Capture:

| Metric | How |
|---|---|
| Wall time | Already in `_summary.md` |
| Total cost | Already in `_summary.md` |
| Lint errors / warnings count | Already in batch state per-body |
| QA errors / warnings count | Already in batch state per-body |
| Manual quality check | grep output for clichés ("leverage", "utilize", "I hope this finds you well"), generic phrases, em-dashes that slipped through |

Decide based on data, not vibes. Document findings in a session breadcrumb so we can refer back later.

---

## 10. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Claude quality drops vs o3 on real copy | A/B test before changing defaults. Keep OpenAI as fallback option in picker. |
| Tool loop infinite-loops differently | Use the same `max_tool_calls=10` budget. Add Claude-specific stop_reason check (`tool_use` vs `end_turn`). |
| Pricing changes after launch | Pull from `MODEL_PRICES` constants; users can update without redeploy by editing the file and pushing. |
| Anthropic API outage | Backend resolver handles `anthropic.APIError` gracefully → falls back to error response, doesn't crash. Could add per-request retry with provider fallback if we want belt-and-suspenders. |
| Test mocks become double-burden | Use a thin shared mock factory in `tests/conftest.py` that handles both backend types. |
| `gpt-5.5-pro` requires Responses API | Out of scope for this plan. Add later as Phase 7 if needed — separate Responses API code path. |

---

## 11. Decision points needed before starting

1. **Default model after Phase 5** — flip to `claude-opus-4-7`, OR keep `o3` and let teammates choose? My recommendation: keep `o3` as default until A/B proves Claude is at least as good, then flip.

2. **Render GitHub App setup** — needs to happen first, otherwise we keep doing the public-flip dance for every deploy. (See main README for steps.)

3. **Should `gpt-5.5-pro` be added too?** — Requires Responses API refactor. ~3-4h additional. Defer until after Anthropic ships.

4. **Display names in picker** — examples:
   - `Opus 4.7 · best`
   - `Sonnet 4.6 · fast`
   - `gpt-5.5 · openai`
   - `o3 · legacy`

---

## 12. What I'd do tomorrow morning if greenlit

1. (1.5h) Phase 1 — backend abstraction. No behavior change. PR#1.
2. (2.5h) Phase 2 — Anthropic backend. PR#2.
3. (1h) Phase 3 — expose in UI + API. PR#3.
4. (1h) Phase 4 — live A/B on Enavra. Document in a new session breadcrumb.
5. (1h) Phase 5+6 — flip default if A/B passes, update tests + docs. PR#4.

Ship as 4 commits / PRs across one focused day.

---

## 13. Out of scope (parking lot)

- gpt-5.5-pro (Responses API refactor)
- Per-teammate API keys (currently single shared bearer token)
- Slack notification on batch done
- Browser-close survival via SQLite
- Provider fallback on outage (auto-retry with the other backend)
- Streaming responses (we currently buffer the whole completion)

These are all valuable but they're not blockers for the Anthropic swap.
