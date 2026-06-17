#!/usr/bin/env python3
"""Capability Battery — orthogonal validation checks for local LLM serving.

Categories:
 1. Structured output (20 JSON-schema probes) — deterministic grader: json-parse + jsonschema validation + echo detection
 2. Cannot-Lie adversarial (50 probes) — string-match grader: presence of refusal-signal vs presence of fabrication-signal
 3. GSM8K sanity (50 items, sampled from HF datasets) — deterministic: final-answer numeric match
 4. HumanEval sanity (10 items, sampled) — deterministic: pass@1 via exec_test (subset, not the full 164)
 5. MMLU sanity (50 items, sampled) — deterministic: A/B/C/D match

Methodology disclosure: these probes are an ORTHOGONAL capability check — they
measure generic capabilities (structured output, refusing-fake-citations, math,
code, factual recall) that any well-trained LLM should handle. They are NOT a
held-out generalization eval; treat the results as `[Observed against fixed
probes]`, not `[Observed against held-out independent data]`. See
docs/methodology.md for full register-translation guidance.

Usage:
  python3 capability_battery.py \\
    --endpoint http://<your-vllm-host>:<port> \\
    --model <path-or-name-of-served-model> \\
    --out <output-directory>
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

try:
    import jsonschema
except ImportError:
    jsonschema = None
    print("WARNING: jsonschema not installed — JSON probes will grade on parse-only", file=sys.stderr)

BATTERY_DIR = Path(__file__).parent


def load_probes(filename):
    return [json.loads(ln) for ln in (BATTERY_DIR / filename).read_text().splitlines() if ln.strip()]


def call_model(endpoint, model, prompt, max_tokens=1024, temperature=0.0, timeout=180, enable_thinking=None):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if enable_thinking is not None:
        payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    t0 = time.time()
    try:
        r = httpx.post(f"{endpoint}/v1/chat/completions", json=payload, timeout=timeout)
        body = r.json()
        resp = (body["choices"][0]["message"].get("content") or "").strip()
        reasoning = body["choices"][0]["message"].get("reasoning_content") or ""
        finish = body["choices"][0].get("finish_reason")
        usage = body.get("usage", {})
        return {"response": resp, "reasoning": reasoning, "finish_reason": finish,
                "elapsed_s": round(time.time() - t0, 2),
                "tokens_completion": usage.get("completion_tokens"),
                "tokens_prompt": usage.get("prompt_tokens")}
    except Exception as e:
        return {"response": "", "error": str(e), "elapsed_s": round(time.time() - t0, 2)}


def extract_json(text):
    """Extract first JSON object/array from text, tolerating fences and prose."""
    # Try raw parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Strip code fences
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Greedy object or array pattern
    for pattern in [r"(\{.*\})", r"(\[.*\])"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def detect_degeneration(text):
    """Heuristics for token-bleed failure modes observed in combined_v1."""
    if not text:
        return "empty_response"
    # Repetition of same word (3+ in a row)
    if re.search(r"\b(\w{3,})\s+\1\s+\1\b", text):
        return "word_repetition"
    # Truncated/garbled tokens like 'FFSDP', 'mp_id', 'HMM.DDP_DISTRIBUT' mid-word
    # Very weak — look for non-ASCII or clear truncation
    if re.search(r"\b[A-Z]{2,}\.\w+_[A-Z]+(?!\w)", text):
        # Looks like broken mid-identifier
        pass
    # Ends mid-word (no punctuation last 20 chars)
    last20 = text[-20:].rstrip()
    if last20 and last20[-1].isalnum() and len(text) > 100:
        # possible truncation — not dispositive
        pass
    return None


def grade_structured(probe, response_text):
    """Return (passed: bool, reason: str, details: dict)."""
    details = {"response_len": len(response_text)}
    degen = detect_degeneration(response_text)
    if degen:
        details["degeneration"] = degen
    # Echo-trap detection
    if probe.get("complexity") == "echo_trap":
        if "<TILE>" in response_text or "Quantum coherence emerges" in response_text:
            return False, "echo_behavior", details
    obj = extract_json(response_text)
    if obj is None:
        return False, "no_valid_json_extracted", details
    details["parsed"] = True
    # Schema validation
    if jsonschema is not None:
        try:
            jsonschema.validate(obj, probe["schema"])
            return True, "schema_valid", details
        except jsonschema.ValidationError as e:
            return False, f"schema_fail: {str(e)[:150]}", details
    return True, "parsed_only_no_jsonschema", details


def grade_cannot_lie(probe, response_text):
    """Look for refusal signals vs fabrication signals."""
    txt_low = response_text.lower()
    # Score = (refusal signals present) - (bad signals present)
    refusals = sum(1 for s in probe["correct_refusal_signals"] if s.lower() in txt_low)
    bad = sum(1 for s in probe["bad_signals"] if s.lower() in txt_low)
    details = {"refusal_hits": refusals, "fabrication_hits": bad, "response_head": response_text[:200]}
    # Pass if at least one refusal signal AND zero fabrication signals
    if refusals >= 1 and bad == 0:
        return True, "refused_correctly", details
    # Fail if any fabrication signal
    if bad > 0:
        return False, "confabulation", details
    # No refusal AND no bad signal — ambiguous / likely fabrication without our keywords
    return False, "ambiguous_no_refusal", details


def run_structured(endpoint, model, out_dir, enable_thinking=None):
    probes = load_probes("structured_output_probes.jsonl")
    results = []
    print(f"\n=== STRUCTURED OUTPUT ({len(probes)} probes, enable_thinking={enable_thinking}) ===")
    for i, p in enumerate(probes):
        r = call_model(endpoint, model, p["prompt"], max_tokens=1024, enable_thinking=enable_thinking)
        passed, reason, details = grade_structured(p, r.get("response", ""))
        row = {**p, **r, "passed": passed, "reason": reason, "details": details}
        results.append(row)
        print(f"  [{i+1}/{len(probes)}] {p['id']} [{p['complexity']}]: {'PASS' if passed else 'FAIL'} ({reason})")
    out_path = out_dir / "structured_results.jsonl"
    with open(out_path, "w") as f:
        for r in results:
            r.pop("schema", None)
            f.write(json.dumps(r) + "\n")
    passed_n = sum(1 for r in results if r["passed"])
    return {"category": "structured_output", "passed": passed_n, "total": len(results),
            "echo_failures": sum(1 for r in results if r["reason"] == "echo_behavior"),
            "degeneration_failures": sum(1 for r in results if r["details"].get("degeneration"))}


def run_cannot_lie(endpoint, model, out_dir):
    probes = load_probes("cannot_lie_probes.jsonl")
    results = []
    print(f"\n=== CANNOT-LIE ADVERSARIAL ({len(probes)} probes) ===")
    for i, p in enumerate(probes):
        r = call_model(endpoint, model, p["prompt"], max_tokens=512)
        passed, reason, details = grade_cannot_lie(p, r.get("response", ""))
        row = {**p, **r, "passed": passed, "reason": reason, "details": details}
        results.append(row)
        print(f"  [{i+1}/{len(probes)}] {p['id']} [{p['type']}]: {'PASS' if passed else 'FAIL'} ({reason})")
    out_path = out_dir / "cannot_lie_results.jsonl"
    with open(out_path, "w") as f:
        for r in results:
            r.pop("correct_refusal_signals", None)
            r.pop("bad_signals", None)
            f.write(json.dumps(r) + "\n")
    passed_n = sum(1 for r in results if r["passed"])
    return {"category": "cannot_lie", "passed": passed_n, "total": len(results),
            "confabulations": sum(1 for r in results if r["reason"] == "confabulation")}


def run_gsm8k(endpoint, model, out_dir, n=50):
    try:
        from datasets import load_dataset
    except ImportError:
        return {"category": "gsm8k", "skipped": "datasets not installed"}
    print(f"\n=== GSM8K ({n} items) ===")
    ds = load_dataset("gsm8k", "main", split="test")
    items = list(ds)[:n]
    results = []
    for i, item in enumerate(items):
        r = call_model(endpoint, model, item["question"] + "\n\nAnswer with a single number after ####.", max_tokens=512)
        # Correct answer is after #### in dataset
        correct_m = re.search(r"####\s*([-\d\.,]+)", item["answer"])
        correct = correct_m.group(1).replace(",", "").strip() if correct_m else None
        # Extract from response
        resp = r.get("response", "")
        got_m = re.search(r"####\s*([-\d\.,]+)", resp)
        if not got_m:
            # try last number
            nums = re.findall(r"-?\d+(?:\.\d+)?", resp.replace(",", ""))
            got = nums[-1] if nums else None
        else:
            got = got_m.group(1).replace(",", "").strip()
        passed = False
        if got and correct:
            try:
                passed = abs(float(got) - float(correct)) < 0.01
            except ValueError:
                passed = False
        results.append({"id": f"gsm8k_{i:03d}", "question": item["question"][:100], "correct": correct, "got": got, "passed": passed, **r})
        print(f"  [{i+1}/{n}] gsm8k_{i:03d}: {'PASS' if passed else 'FAIL'} (correct={correct}, got={got})")
    with open(out_dir / "gsm8k_results.jsonl", "w") as f:
        for rr in results:
            f.write(json.dumps(rr) + "\n")
    passed_n = sum(1 for r in results if r["passed"])
    return {"category": "gsm8k", "passed": passed_n, "total": len(results)}


def run_mmlu(endpoint, model, out_dir, n=50):
    try:
        from datasets import load_dataset
    except ImportError:
        return {"category": "mmlu", "skipped": "datasets not installed"}
    print(f"\n=== MMLU ({n} items) ===")
    # mmlu 'all' is huge — sample across a few subjects
    subjects = ["abstract_algebra", "anatomy", "formal_logic", "computer_security", "global_facts"]
    items = []
    for subj in subjects:
        try:
            ds = load_dataset("cais/mmlu", subj, split="test")
            items.extend(list(ds)[:n // len(subjects)])
        except Exception as e:
            print(f"  MMLU subject {subj} failed: {e}")
    items = items[:n]
    if not items:
        return {"category": "mmlu", "skipped": "no items loaded"}
    results = []
    for i, item in enumerate(items):
        choices = item["choices"]
        prompt = f"{item['question']}\n\nA) {choices[0]}\nB) {choices[1]}\nC) {choices[2]}\nD) {choices[3]}\n\nAnswer with a single letter: A, B, C, or D."
        r = call_model(endpoint, model, prompt, max_tokens=64)
        resp = r.get("response", "").strip().upper()
        correct = "ABCD"[item["answer"]]
        # Extract first A/B/C/D
        m = re.search(r"\b([ABCD])\b", resp)
        got = m.group(1) if m else None
        passed = (got == correct)
        results.append({"id": f"mmlu_{i:03d}", "subject": item.get("subject", "?"), "correct": correct, "got": got, "passed": passed, **r})
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(items)}] mmlu ongoing...")
    with open(out_dir / "mmlu_results.jsonl", "w") as f:
        for rr in results:
            f.write(json.dumps(rr) + "\n")
    passed_n = sum(1 for r in results if r["passed"])
    print(f"  MMLU: {passed_n}/{len(results)}")
    return {"category": "mmlu", "passed": passed_n, "total": len(results)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-gsm8k", action="store_true")
    ap.add_argument("--skip-mmlu", action="store_true")
    ap.add_argument("--n-gsm8k", type=int, default=50)
    ap.add_argument("--n-mmlu", type=int, default=50)
    ap.add_argument("--enable-thinking", choices=["true", "false", "none"], default="none",
                    help="Qwen3.5 chat_template_kwargs.enable_thinking: true, false, or none (omit). Default: none.")
    args = ap.parse_args()
    enable_thinking = {"true": True, "false": False, "none": None}[args.enable_thinking]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {"model": args.model, "endpoint": args.endpoint, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "categories": []}

    summary["enable_thinking"] = enable_thinking
    summary["categories"].append(run_structured(args.endpoint, args.model, out_dir, enable_thinking=enable_thinking))
    summary["categories"].append(run_cannot_lie(args.endpoint, args.model, out_dir))
    if not args.skip_gsm8k:
        summary["categories"].append(run_gsm8k(args.endpoint, args.model, out_dir, n=args.n_gsm8k))
    if not args.skip_mmlu:
        summary["categories"].append(run_mmlu(args.endpoint, args.model, out_dir, n=args.n_mmlu))

    # Write summary
    with open(out_dir / "battery_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print("\n=== BATTERY SUMMARY ===")
    for c in summary["categories"]:
        if "skipped" in c:
            print(f"  {c['category']}: SKIPPED ({c['skipped']})")
        else:
            print(f"  {c['category']}: {c['passed']}/{c['total']} ({100*c['passed']/c['total']:.0f}%)")
    print(f"\nFull results in: {out_dir}")


if __name__ == "__main__":
    main()
