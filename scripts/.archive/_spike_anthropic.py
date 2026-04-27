"""One-shot spike: call client.messages.create with claude-opus-4-7 + tool + adaptive thinking.
Dumps the shape so the implementer can build the adapter from real data.
"""
import json
import os
import sys
from pathlib import Path

# Load .env
env_path = Path(__file__).resolve().parent.parent.parent / ".env"
for line in env_path.read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# If ANTHROPIC_API_KEY is not in .env, fall back to the one user provided earlier
if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = "REDACTED"  # key removed; load from .env instead

try:
    from anthropic import Anthropic
except ImportError:
    print("anthropic package not installed; install with: pip install anthropic")
    sys.exit(1)

client = Anthropic()

TOOL = {
    "name": "lint_spintax",
    "description": "Run the deterministic Python linter on your current draft. Returns structured per-block errors and warnings.",
    "input_schema": {
        "type": "object",
        "properties": {
            "spintax_body": {
                "type": "string",
                "description": "The full spintax email body to check.",
            }
        },
        "required": ["spintax_body"],
        "additionalProperties": False,
    },
}

SYSTEM = "You are a spintax generator. When the user gives you a body, call lint_spintax once with a candidate {a|b|c} variant of one word, then return the linted body."
USER = "Make spintax for: 'Hi, I noticed your team is hiring.'"

print("=" * 70, flush=True)
print("SPIKE: client.messages.create model=claude-opus-4-7 + tool + adaptive thinking", flush=True)
print("=" * 70, flush=True)
print(f"anthropic SDK version: {__import__('anthropic').__version__}", flush=True)

# Round 1
try:
    r1 = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content": USER}],
        tools=[TOOL],
        tool_choice={"type": "auto"},
        thinking={"type": "adaptive"},
        # Per researcher: effort lives in output_config, not thinking
        # Try both placements to see what SDK accepts
    )
except Exception as e:
    print(f"\n!!! ROUND 1 FAILED (no output_config): {type(e).__name__}: {e}", flush=True)
    # Retry with output_config
    try:
        r1 = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            system=SYSTEM,
            messages=[{"role": "user", "content": USER}],
            tools=[TOOL],
            tool_choice={"type": "auto"},
            thinking={"type": "adaptive"},
            extra_body={"output_config": {"effort": "low"}},
        )
        print("  (recovered with extra_body output_config)", flush=True)
    except Exception as e2:
        print(f"!!! ROUND 1 FAILED (with output_config): {type(e2).__name__}: {e2}", flush=True)
        sys.exit(1)

print(f"\nr1.id          = {r1.id}")
print(f"r1.model       = {r1.model}")
print(f"r1.stop_reason = {r1.stop_reason}")
print(f"r1.usage       = {r1.usage.model_dump() if hasattr(r1.usage, 'model_dump') else r1.usage}")
print(f"\nr1.content (len={len(r1.content)}):")
for i, block in enumerate(r1.content):
    print(f"  [{i}] type={block.type}")
    bd = block.model_dump() if hasattr(block, "model_dump") else dict(block)
    print(f"      {json.dumps(bd, default=str, indent=8)[:600]}")

# Round 2: submit tool result
tool_uses = [b for b in r1.content if b.type == "tool_use"]
if not tool_uses:
    print("\nNo tool_use block in round 1; can't test tool result loop")
    sys.exit(0)

tu = tool_uses[0]
print(f"\n--- ROUND 2: submitting tool_result for {tu.id} ---")

# Echo assistant content unmodified, then user with tool_result
messages = [
    {"role": "user", "content": USER},
    {"role": "assistant", "content": r1.content},
    {"role": "user", "content": [
        {
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": json.dumps({"passed": True, "error_count": 0, "warning_count": 0, "errors": [], "warnings": []}),
        }
    ]},
]

try:
    r2 = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
        system=SYSTEM,
        messages=messages,
        tools=[TOOL],
        tool_choice={"type": "auto"},
        thinking={"type": "adaptive"},
    )
except Exception as e:
    print(f"!!! ROUND 2 FAILED: {type(e).__name__}: {e}", flush=True)
    sys.exit(1)

print(f"\nr2.stop_reason = {r2.stop_reason}")
print(f"r2.usage       = {r2.usage.model_dump() if hasattr(r2.usage, 'model_dump') else r2.usage}")
print(f"\nr2.content (len={len(r2.content)}):")
for i, block in enumerate(r2.content):
    print(f"  [{i}] type={block.type}")
    if block.type == "text":
        print(f"      text={block.text!r}")
    elif block.type == "thinking":
        sig_len = len(getattr(block, "signature", "") or "")
        thinking_len = len(getattr(block, "thinking", "") or "")
        print(f"      thinking_len={thinking_len} signature_len={sig_len}")

# Compute cost (rough)
PRICE_OPUS = {"input": 5.00, "output": 25.00}
total_input = (r1.usage.input_tokens + r2.usage.input_tokens)
total_output = (r1.usage.output_tokens + r2.usage.output_tokens)
total_cost = (total_input/1e6) * PRICE_OPUS["input"] + (total_output/1e6) * PRICE_OPUS["output"]
print(f"\n--- COST (uncached, no thinking-token separation) ---")
print(f"input_tokens total:  {total_input}")
print(f"output_tokens total: {total_output}")
print(f"estimated cost:      ${total_cost:.4f}")

# Cache fields, if present
print(f"\nr1.usage.cache_creation_input_tokens = {getattr(r1.usage, 'cache_creation_input_tokens', 'MISSING')}")
print(f"r1.usage.cache_read_input_tokens     = {getattr(r1.usage, 'cache_read_input_tokens', 'MISSING')}")

print("\n" + "=" * 70)
print("SPIKE COMPLETE")
