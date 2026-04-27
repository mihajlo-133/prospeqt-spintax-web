"""One-shot spike: call client.responses.create with gpt-5.5 + converted lint_spintax tool.
Dumps the shape so the implementer can build adapters from real data, not docs.
"""
import json
import os
import sys
from pathlib import Path

# Load .env
env_path = Path(__file__).resolve().parent.parent / ".env"
for line in env_path.read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import OpenAI

client = OpenAI()

# Converted lint_spintax tool (Responses shape — flat, no "function" wrapper)
TOOL = {
    "type": "function",
    "name": "lint_spintax",
    "description": "Run the deterministic Python linter on your current draft. Returns structured per-block errors and warnings.",
    "parameters": {
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
    "strict": True,
}

# Tiny test prompt — should trigger the model to call lint_spintax then emit a body
SYSTEM = "You are a spintax generator. When the user gives you a body, call lint_spintax once with a candidate {a|b|c} variant of one word, then return the linted body."
USER = "Make spintax for: 'Hi, I noticed your team is hiring.'"

print("=" * 70, flush=True)
print("SPIKE: client.responses.create model=gpt-5.5 + lint_spintax tool", flush=True)
print("=" * 70, flush=True)

try:
    response = client.responses.create(
        model="gpt-5.5",
        input=[{"role": "user", "content": USER}],
        instructions=SYSTEM,
        tools=[TOOL],
        tool_choice="auto",
        reasoning={"effort": "low"},
        max_output_tokens=2000,
    )
except Exception as e:
    print(f"\n!!! INITIAL CALL FAILED: {type(e).__name__}: {e}", flush=True)
    sys.exit(1)

print(f"\nresponse.id        = {response.id}")
print(f"response.object    = {response.object}")
print(f"response.status    = {response.status}")
print(f"response.model     = {response.model}")
print(f"response.usage     = {response.usage.model_dump() if response.usage else None}")
print(f"\nresponse.output (len={len(response.output)}):")
for i, item in enumerate(response.output):
    print(f"  [{i}] type={item.type}")
    print(f"      raw={json.dumps(item.model_dump(), default=str, indent=8)[:600]}")

print(f"\nresponse.output_text = {response.output_text!r}")

# If there's a function_call, do one round of the loop to verify shape
fcs = [item for item in response.output if item.type == "function_call"]
if fcs:
    tc = fcs[0]
    print(f"\n--- second round: submitting tool result ---")
    print(f"call_id = {tc.call_id}")
    print(f"name    = {tc.name}")
    print(f"args    = {tc.arguments[:200]}")
    # Echo output items back to input — but strip status/id which OpenAI rejects on input
    def _echo_output_item(item):
        d = item.model_dump(exclude_none=True)
        # Drop fields that are valid in output but rejected on input
        for k in ("status",):
            d.pop(k, None)
        return d
    input_list = [{"role": "user", "content": USER}]
    input_list += [_echo_output_item(item) for item in response.output]
    input_list.append({
        "type": "function_call_output",
        "call_id": tc.call_id,
        "output": json.dumps({"passed": True, "error_count": 0, "warning_count": 0, "errors": [], "warnings": []}),
    })
    # Print the input list for the implementer to learn from
    print(f"\nINPUT LIST FOR ROUND 2:")
    for i, it in enumerate(input_list):
        print(f"  [{i}] {json.dumps(it, default=str)[:200]}")
    try:
        r2 = client.responses.create(
            model="gpt-5.5",
            input=input_list,
            instructions=SYSTEM,
            tools=[TOOL],
            reasoning={"effort": "low"},
            max_output_tokens=2000,
        )
        print(f"\nr2.status = {r2.status}")
        print(f"r2.usage  = {r2.usage.model_dump() if r2.usage else None}")
        print(f"r2.output_text = {r2.output_text!r}")
        print(f"\nr2.output (len={len(r2.output)}):")
        for i, item in enumerate(r2.output):
            print(f"  [{i}] type={item.type}")
    except Exception as e:
        print(f"!!! SECOND CALL FAILED: {type(e).__name__}: {e}")

print("\n" + "=" * 70)
print("SPIKE COMPLETE")
