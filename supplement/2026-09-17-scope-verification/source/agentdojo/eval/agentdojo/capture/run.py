#!/usr/bin/env python3
"""Hardened AgentDojo capture runner.

Runs the chosen model over AgentDojo suites via OpenRouter and OVER-RECORDS everything:
AgentDojo's full per-task traces (conversation + tool calls + args + tool *results* + the
benchmark's utility/security labels) PLUS a fingerprint AgentDojo drops — served provider,
finish_reason, token usage (raw_responses.jsonl) — and a manifest (model, decoding params,
versions, cost). Resumption is accepted only after a complete artifact preflight. Budgeted capture
runs one task at a time so its hard cumulative cost ceiling is enforceable.

The runner is deliberately lossless: it records benchmark traces and raw provider responses
without interpreting them.

Usage:
  cp .env.example .env   # put OPENROUTER_API_KEY there  (or export it)
  python -m eval.agentdojo.capture --smoke --provider TAG --endpoint-revision REVISION --max-cost-usd LIMIT
                                                # 2 user tasks/suite; serial budgeted smoke
  python -m eval.agentdojo.capture --provider TAG --endpoint-revision REVISION --max-cost-usd LIMIT
                                                # reference run: glm-4.7-flash, temp 0, all suites
  python -m eval.agentdojo.capture --temperature 0.7 --rollouts 5 --provider TAG --endpoint-revision REVISION --max-cost-usd LIMIT
                                                # primary distribution; every rollout preflighted

Output: corpus/<run_id>/ -> agentdojo/ (per-task traces), raw_responses.jsonl, manifest.json, results.json
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path

from .integrity import (
    expected_trace_cells,
    billed_cost_decimal,
    ensure_capture_lock,
    provider_preferences,
    preflight_capture_artifacts,
    raw_billed_cost_total,
    summarize_raw_responses,
    validate_raw_response_summary,
    validate_trace_corpus,
)

try:
    from tqdm import tqdm
except ImportError:  # graceful fallback if tqdm is missing
    def tqdm(it, **k):
        return it
    tqdm.write = lambda s: print(s, flush=True)

CAPTURE_DIR = Path(__file__).resolve().parent
EVAL_ROOT = CAPTURE_DIR.parents[1]

# ---- env (key from eval/.env or environment) ----
envf = EVAL_ROOT / ".env"
if envf.exists():
    for ln in envf.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
KEY = os.environ.get("OPENROUTER_API_KEY")
if not KEY:
    sys.exit("ERROR: set OPENROUTER_API_KEY (in eval/.env or the environment)")
os.environ["OPENAI_COMPATIBLE_BASE_URL"] = "https://openrouter.ai/api/v1"
os.environ["OPENAI_COMPATIBLE_API_KEY"] = KEY

try:
    import agentdojo
    from agentdojo.models import ModelsEnum
    from agentdojo.task_suite.load_suites import get_suite
    from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
    from agentdojo.attacks.attack_registry import load_attack
    from agentdojo.benchmark import run_task_with_injection_tasks, run_task_without_injection_tasks
    from agentdojo.logging import OutputLogger, LOGGER_STACK
    import agentdojo.agent_pipeline.llms.openai_llm as _oai
except ImportError as e:
    sys.exit(f"ERROR: agentdojo not installed ({e}).\n  python -m venv .venv && ./.venv/bin/pip install -r requirements.txt")

SUITES = ["banking", "workspace", "slack", "travel"]
ATTACK = "important_instructions"
CALL_CONTEXT = threading.local()


class BudgetExceeded(RuntimeError):
    """Raised after capture has reached its configured spend ceiling."""


_SENSITIVE_ERROR_KEYS = {
    "api_key", "authorization", "headers", "input", "messages", "prompt", "request",
    "x_api_key",
}


def _safe_error_body(value, depth=0):
    """Keep structured provider diagnostics without retaining request content or credentials."""
    if depth > 8:
        return None
    if isinstance(value, str):
        if KEY:
            value = value.replace(KEY, "[REDACTED]")
        return re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [_safe_error_body(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _safe_error_body(item, depth + 1)
            for key, item in value.items()
            if str(key).strip().lower().replace("-", "_") not in _SENSITIVE_ERROR_KEYS
        }
    return None


_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "output_tokens", "total_tokens", "cost")
_ERROR_METADATA_KEYS = ("provider_name", "model", "model_id", "model_name", "retry_after")
_RESPONSE_METADATA_KEYS = (
    "id", "model", "provider", "created", "object", "service_tier",
    "system_fingerprint", "moderation",
)


def _as_mapping(value):
    return value if isinstance(value, dict) else {}


def _safe_scalar(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return _safe_error_body(value)
    return "[INVALID]"


def _error_payload(payload):
    payload = _as_mapping(payload)
    nested = payload.get("error")
    return nested if isinstance(nested, dict) else payload


def _safe_error_projection(payload):
    """Allowlist provider diagnostics; error bodies can otherwise echo request data."""
    error = _error_payload(payload)
    projected = {}
    if "code" in error:
        projected["code"] = _safe_scalar(error["code"])
    metadata = error.get("metadata")
    if isinstance(metadata, dict):
        safe_metadata = {
            key: _safe_scalar(metadata[key])
            for key in _ERROR_METADATA_KEYS
            if key in metadata
        }
        if safe_metadata:
            projected["metadata"] = safe_metadata
    return projected


def _safe_usage(payload):
    usage = _as_mapping(payload).get("usage")
    if not isinstance(usage, dict):
        return None
    return {key: _safe_scalar(usage[key]) for key in _USAGE_KEYS if key in usage}


def _finish_reasons(payload):
    choices = _as_mapping(payload).get("choices")
    if not isinstance(choices, list):
        return []
    return [_safe_scalar(choice.get("finish_reason")) for choice in choices if isinstance(choice, dict)]


def _output_status(payload):
    choices = _as_mapping(payload).get("choices")
    if choices is None or choices == []:
        return "empty"
    if not isinstance(choices, list):
        return "unknown"
    output_keys = ("content", "text", "reasoning", "reasoning_content", "refusal", "tool_calls", "function_call")
    for choice in choices:
        if not isinstance(choice, dict):
            return "unknown"
        for container in (choice, choice.get("message"), choice.get("delta")):
            if not isinstance(container, dict):
                continue
            if any(container.get(key) not in (None, "", [], {}) for key in output_keys):
                return "present"
    return "empty"


def _safe_response_metadata(payload, usage=None):
    payload = _as_mapping(payload)
    metadata = {}
    for key in _RESPONSE_METADATA_KEYS:
        if key not in payload:
            continue
        metadata[key] = (
            _safe_error_body(payload[key]) if key == "moderation" else _safe_scalar(payload[key])
        )
    error = _error_payload(payload)
    error_metadata = error.get("metadata") if isinstance(error, dict) else None
    provider_evidence = [
        value for value in (
            payload.get("provider"),
            error_metadata.get("provider_name") if isinstance(error_metadata, dict) else None,
        )
        if isinstance(value, str) and value.strip()
    ]
    model_values = [payload.get("model")]
    if isinstance(error_metadata, dict):
        model_values.extend(error_metadata.get(key) for key in ("model", "model_id", "model_name"))
    model_evidence = [value for value in model_values if isinstance(value, str) and value.strip()]
    route_conflicts = {}
    for key, evidence in (("model", model_evidence), ("provider", provider_evidence)):
        distinct = list(dict.fromkeys(evidence))
        if len(distinct) > 1:
            route_conflicts[key] = distinct
    if route_conflicts:
        metadata["route_conflicts"] = route_conflicts
    if "provider" not in metadata and isinstance(error_metadata, dict):
        provider_name = error_metadata.get("provider_name")
        if isinstance(provider_name, str) and provider_name.strip():
            metadata["provider"] = provider_name
    if "model" not in metadata and isinstance(error_metadata, dict):
        for key in ("model", "model_id", "model_name"):
            value = error_metadata.get(key)
            if isinstance(value, str) and value.strip():
                metadata["model"] = value
                break
    safe_usage = usage if isinstance(usage, dict) else _safe_usage(payload)
    if safe_usage is not None:
        metadata["usage"] = {key: _safe_scalar(safe_usage[key]) for key in _USAGE_KEYS if key in safe_usage}
    metadata["finish_reasons"] = _finish_reasons(payload)
    projected_error = _safe_error_projection(payload)
    if projected_error:
        metadata["error"] = projected_error
    return metadata


def _zero_completion_error(payload, usage):
    if not isinstance(usage, dict):
        return False
    counters = [usage[key] for key in ("completion_tokens", "output_tokens") if key in usage]
    if not counters or any(not isinstance(value, int) or isinstance(value, bool) or value != 0 for value in counters):
        return False
    if _output_status(payload) != "empty":
        return False
    return all(reason in (None, "", "error") for reason in _finish_reasons(payload))


def usage_usd():
    try:
        out = subprocess.run(
            ["curl", "-s", "https://openrouter.ai/api/v1/credits", "-H", f"Authorization: Bearer {KEY}"],
            capture_output=True, text=True, timeout=20).stdout
        return json.loads(out)["data"]["total_usage"]
    except Exception:
        return None


def git_sha(path):
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or "?"
    except Exception:
        return "?"


def adojo_version():
    try:
        import importlib.metadata as _m
        return _m.version("agentdojo")
    except Exception:
        return getattr(agentdojo, "__version__", "?")


def install_recorder(raw_path: Path, provider: str, temperature: float, max_cost_usd: float):
    """Monkeypatch AgentDojo's LLM call to (1) FORCE the run temperature — benchmark hardcodes the
    OpenAILLM default of 0.0, so temp>0 rollouts would otherwise silently run at 0; (2) pin the
    OpenRouter provider (no silent fallback across backends/quant — the dominant temp=0 variance
    source); (3) over-record the raw provider response (served model, provider, finish_reason,
    usage) that AgentDojo drops. THREAD-SAFE append (concurrent workers share one file handle)."""
    import openai
    from tenacity import retry, wait_random_exponential, stop_after_attempt, retry_if_not_exception_type
    fh = open(raw_path, "a")
    lock = threading.Lock()
    try:
        billed_cost_usd = raw_billed_cost_total(raw_path)
    except ValueError as error:
        fh.close()
        raise RuntimeError(f"cannot resume with invalid raw response log: {error}") from error
    max_cost = Decimal(str(max_cost_usd))
    attempt_id = sum(1 for line in raw_path.read_text().splitlines() if line.strip()) if raw_path.exists() else 0
    capture_closed_reason = None
    NG = _oai.NOT_GIVEN

    # more attempts than the AgentDojo default — concurrency makes provider 429s likely; back off and retry.
    @retry(wait=wait_random_exponential(multiplier=1, max=60), stop=stop_after_attempt(6), reraise=True,
           retry=retry_if_not_exception_type((openai.BadRequestError, openai.UnprocessableEntityError, BudgetExceeded)))
    def patched(client, model, messages, tools, reasoning_effort, _ignored_temperature=0.0):
        nonlocal attempt_id, billed_cost_usd, capture_closed_reason
        kw = dict(model=model, messages=messages, tools=tools or NG,
                  tool_choice="auto" if tools else NG,
                  temperature=temperature,  # forced run temperature (see docstring)
                  reasoning_effort=reasoning_effort or NG)
        kw["extra_body"] = provider_preferences(provider)
        with lock:
            if capture_closed_reason is not None:
                raise BudgetExceeded(capture_closed_reason)
            if billed_cost_usd >= max_cost:
                raise BudgetExceeded(
                    f"configured cost ceiling already reached: ${billed_cost_usd} >= ${max_cost}"
                )
            attempt_id += 1
            this_attempt = attempt_id
        trace_cell = getattr(CALL_CONTEXT, "trace_cell", None)
        try:
            resp = client.chat.completions.create(**kw)
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            error_payload = _as_mapping(getattr(error, "body", None))
            usage = _safe_usage(error_payload)
            cost = usage.get("cost") if isinstance(usage, dict) else None
            try:
                parsed_cost = billed_cost_decimal(cost)
            except ValueError:
                parsed_cost = None
            is_provider_error = (
                isinstance(error, openai.APIStatusError)
                and isinstance(status_code, int)
                and status_code >= 400
            )
            if parsed_cost is not None:
                billing_status = "reported"
            elif cost is None and is_provider_error and _zero_completion_error(error_payload, usage):
                billing_status = "not_reported"
            else:
                billing_status = "unknown"
            response_metadata = _safe_response_metadata(error_payload, usage)
            route_conflict = bool(response_metadata.get("route_conflicts"))
            response_model = response_metadata.get("model")
            response_provider = response_metadata.get("provider")
            finish_reasons = response_metadata.get("finish_reasons", [])
            with lock:
                if route_conflict:
                    capture_closed_reason = "conflicting response route identity; recorder is closed"
                elif billing_status == "unknown":
                    capture_closed_reason = "capture billing outcome is unknown; recorder is closed"
                fh.write(json.dumps({
                    "attempt_id": this_attempt,
                    "attempt_status": "error",
                    "billing_status": billing_status,
                    "response_id": response_metadata.get("id"),
                    "requested_model": model,
                    "requested_provider_tag": provider,
                    "response_model": response_model,
                    "provider": response_provider,
                    "finish_reason": finish_reasons[0] if finish_reasons else None,
                    "usage": usage,
                    "billed_cost_usd": cost,
                    "trace_cell": trace_cell,
                    "raw_response": None,
                    "response_metadata": response_metadata,
                    "output_status": _output_status(error_payload),
                    "error": {
                        "type": type(error).__name__,
                        "status_code": status_code if isinstance(status_code, int) else None,
                        "body": _safe_error_projection(error_payload),
                    },
                }) + "\n")
                fh.flush()
                if parsed_cost is not None:
                    billed_cost_usd += parsed_cost
                    if billed_cost_usd > max_cost:
                        raise BudgetExceeded(
                            f"configured cost ceiling exceeded: ${billed_cost_usd} > ${max_cost}"
                        )
            if route_conflict or billing_status == "unknown":
                raise BudgetExceeded(capture_closed_reason) from error
            raise
        choices = getattr(resp, "choices", None)
        ch = choices[0] if choices else None
        dump_response = getattr(resp, "model_dump", None)
        raw_response = None
        if callable(dump_response):
            try:
                raw_response = dump_response(mode="json")
            except TypeError:
                raw_response = dump_response()
        raw_error = raw_response.get("error") if isinstance(raw_response, dict) else None
        attempt_status = "error" if raw_error is not None or not choices else "response"
        reported_usage = resp.usage.model_dump() if getattr(resp, "usage", None) else None
        usage = reported_usage if isinstance(reported_usage, dict) else _safe_usage(raw_response)
        if attempt_status == "error" and isinstance(usage, dict):
            usage = _safe_usage({"usage": usage})
        cost = usage.get("cost") if isinstance(usage, dict) else None
        try:
            parsed_cost = billed_cost_decimal(cost)
        except ValueError:
            parsed_cost = None
        if parsed_cost is not None:
            billing_status = "reported"
        elif raw_error is not None and cost is None and _zero_completion_error(raw_response, usage):
            billing_status = "not_reported"
        else:
            billing_status = "unknown"
        response_metadata = _safe_response_metadata(raw_response, usage) if attempt_status == "error" else None
        route_conflict = bool(response_metadata and response_metadata.get("route_conflicts"))
        error = None
        if raw_error is not None:
            code = raw_error.get("code") if isinstance(raw_error, dict) else None
            error = {
                "type": "ProviderResponseError",
                "status_code": code if isinstance(code, int) else None,
                "body": _safe_error_projection(raw_error),
            }
        elif not choices:
            error = {"type": "EmptyChoices", "status_code": None, "body": None}
        with lock:
            line = json.dumps({
                "attempt_id": this_attempt,
                "attempt_status": attempt_status,
                "billing_status": billing_status,
                "response_id": getattr(resp, "id", None),
                "requested_model": model,
                "requested_provider_tag": provider,
                "response_model": (
                    response_metadata.get("model") if response_metadata is not None
                    else getattr(resp, "model", model)
                ),
                "provider": (
                    response_metadata.get("provider") if response_metadata is not None
                    else getattr(resp, "provider", None)
                ),
                "finish_reason": getattr(ch, "finish_reason", None),
                "usage": usage,
                "billed_cost_usd": cost,
                "trace_cell": trace_cell,
                "raw_response": None if attempt_status == "error" else raw_response,
                "response_metadata": response_metadata,
                "output_status": _output_status(raw_response) if attempt_status == "error" else None,
                "error": error,
            })
            fh.write(line + "\n")
            fh.flush()
            if parsed_cost is not None:
                billed_cost_usd += parsed_cost
            if billed_cost_usd > max_cost:
                    raise BudgetExceeded(
                        f"configured cost ceiling exceeded: ${billed_cost_usd} > ${max_cost}"
                    )
            if route_conflict:
                capture_closed_reason = "conflicting response route identity; recorder is closed"
                raise BudgetExceeded(capture_closed_reason)
            if billing_status == "unknown":
                capture_closed_reason = "capture billing outcome is unknown; recorder is closed"
                raise BudgetExceeded(capture_closed_reason)
        if raw_error is not None:
            raise RuntimeError("provider returned an error response (transient)")
        if not choices:
            # Record the billable response before asking tenacity to retry it.
            raise RuntimeError(f"empty choices from provider={getattr(resp, 'provider', None)} (transient)")
        return resp

    _oai.chat_completion_request = patched


def run_rollout(model_id, temperature, suites, bench_version, smoke, run_dir, provider, workers, max_cost_usd):
    logdir = run_dir / "agentdojo"
    logdir.mkdir(parents=True, exist_ok=True)
    install_recorder(run_dir / "raw_responses.jsonl", provider, temperature, max_cost_usd)
    pipeline = AgentPipeline.from_config(PipelineConfig(
        llm=ModelsEnum.OPENAI_COMPATIBLE, model_id=model_id,
        defense=None, system_message_name=None, system_message=None))

    def seeded(trace_cell, thunk):
        # Each worker thread gets its OWN logger stack. AgentDojo's LOGGER_STACK is a ContextVar with
        # a SHARED MUTABLE default ([]), mutated by append/pop and never .set() — so without this,
        # concurrent tasks share one stack and their per-task JSON traces cross-contaminate. Seeding
        # with an OutputLogger(logdir) also makes TraceLogger.delegate.logdir resolve to OUR run dir.
        LOGGER_STACK.set([OutputLogger(str(logdir), live=None)])
        CALL_CONTEXT.trace_cell = trace_cell
        try:
            return thunk()
        finally:
            CALL_CONTEXT.trace_cell = None

    def run_pool(work_items, desc):
        out, n = [], len(work_items)
        if n == 0:
            return out
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(seeded, cell, thunk) for cell, thunk in work_items]
            for f in tqdm(as_completed(futs), total=n, desc=f"  {desc}", unit="task", leave=True):
                try:
                    out.append(f.result())
                except Exception as e:  # one failed task must not kill the run — it re-runs on resume
                    tqdm.write(f"      [WARN] {desc}: task failed ({type(e).__name__}: {str(e)[:100]}) — retries on resume")
        return out

    results = {}
    for s in suites:
        suite = get_suite(bench_version, s)
        uids = list(suite.user_tasks.keys())
        if smoke:
            uids = uids[:2]  # 2 user tasks => exercises the thread pool while staying cheap
        utasks = [suite.get_user_task_by_id(i) for i in uids]
        attacker = load_attack(ATTACK, suite, pipeline)
        print(f"  [suite {s}] {'smoke' if smoke else 'full'} | user_tasks={len(utasks)} injections={len(suite.injection_tasks)} workers={workers}", flush=True)

        # (1) ATTACK pass. Full runs also do the injection-task baselines (each injection task run as
        #     a benign user task) — AgentDojo's standard protocol; skipped in smoke to stay fast.
        if not smoke:
            base_thunks = [(
                {"suite_name": s, "user_task_id": i, "attack_type": "none", "injection_task_id": "none"},
                lambda it=suite.get_injection_task_by_id(i):
                run_task_without_injection_tasks(suite, pipeline, it, logdir, False, bench_version),
            ) for i in suite.injection_tasks]
            run_pool(base_thunks, f"{s} baselines")
        atk = run_pool([(
            {"suite_name": s, "user_task_id": ut.ID, "attack_type": ATTACK, "injection_task_id": injection_id},
            lambda ut=ut, injection_id=injection_id:
            run_task_with_injection_tasks(suite, pipeline, ut, attacker, logdir, False, [injection_id], bench_version),
        ) for ut in utasks for injection_id in suite.injection_tasks], f"{s} attack")
        util, sec = {}, {}
        for u, se in atk:
            util.update(u)
            sec.update(se)

        # (2) BENIGN pass (attack=None). REQUIRED for the false-positive edge AND to derive the legit
        #     recipient set; the attack pass never produces true-benign user-task runs.
        ben = run_pool([(
            {"suite_name": s, "user_task_id": ut.ID, "attack_type": "none", "injection_task_id": "none"},
            lambda ut=ut:
            (ut.ID, run_task_without_injection_tasks(suite, pipeline, ut, logdir, False, bench_version)[0]),
        ) for ut in utasks], f"{s} benign")
        butil = dict(ben)

        results[s] = {
            "n_attack": len(sec),
            "asr": round(sum(sec.values()) / len(sec), 4) if sec else None,
            "utility_under_attack": round(sum(util.values()) / len(util), 4) if util else None,
            "n_benign": len(butil),
            "benign_utility": round(sum(butil.values()) / len(butil), 4) if butil else None,
        }
        print(f"  [suite {s}] -> attacks={len(sec)} ASR={results[s]['asr']} | benign={len(butil)} util={results[s]['benign_utility']}", flush=True)
    return results


def expected_cells_for_run(suites, bench_version, smoke):
    definitions = []
    baseline_cells = []
    for suite_name in suites:
        suite = get_suite(bench_version, suite_name)
        user_task_ids = list(suite.user_tasks.keys())
        if smoke:
            user_task_ids = user_task_ids[:2]
        definitions.append({
            "suite_name": suite_name,
            "user_task_ids": user_task_ids,
            "injection_task_ids": list(suite.injection_tasks),
        })
        if not smoke:
            baseline_cells.extend(
                {
                    "suite_name": suite_name,
                    "user_task_id": injection_task_id,
                    "attack_type": "none",
                    "injection_task_id": "none",
                }
                for injection_task_id in suite.injection_tasks
            )
    return [*expected_trace_cells(definitions), *baseline_cells]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="z-ai/glm-4.7-flash")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--rollouts", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1, help="must remain 1 for budgeted capture")
    ap.add_argument("--suites", default=",".join(SUITES))
    ap.add_argument("--benchmark-version", default="v1.2.2", help="AgentDojo suite version (v1.2.2 ships the workspace mass-action tasks AAP-5 reads)")
    ap.add_argument("--smoke", action="store_true", help="2 user tasks/suite end-to-end")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--provider", required=True, help="exact OpenRouter endpoint tag; fallbacks are disabled")
    ap.add_argument("--endpoint-revision", required=True, help="exact backend revision from the frozen OpenRouter endpoint catalog")
    ap.add_argument("--max-cost-usd", type=float, required=True, help="fail the capture if raw billed cost exceeds this ceiling")
    args = ap.parse_args()

    suites = [s.strip() for s in args.suites.split(",") if s.strip()]
    if args.max_cost_usd <= 0:
        ap.error("--max-cost-usd must be positive")
    if args.workers != 1:
        ap.error("budgeted capture requires --workers 1 so the configured ceiling is enforceable")
    short = args.model.split("/")[-1]
    base = args.run_id or f"{short}_t{args.temperature}{'_smoke' if args.smoke else ''}"
    corpus = EVAL_ROOT / "corpus"
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    cost0 = usage_usd()

    summary = []
    for k in range(args.rollouts):
        run_id = base if args.rollouts == 1 else f"{base}_r{k}"
        run_dir = corpus / run_id
        expected_cells = expected_cells_for_run(suites, args.benchmark_version, args.smoke)
        capture_config = {
            "schema_version": 2,
            "run_id": run_id,
            "model_id": args.model,
            "temperature": args.temperature,
            "provider": args.provider,
            "response_model": args.model,
            "endpoint_revision": args.endpoint_revision,
            "max_cost_usd": args.max_cost_usd,
            "workers": args.workers,
            "suites": suites,
            "benchmark_version": args.benchmark_version,
            "agentdojo_version": adojo_version(),
            "smoke": args.smoke,
            "attack": ATTACK,
            "expected_trace_cells": expected_cells,
        }
        lock_status = ensure_capture_lock(run_dir, capture_config)
        preflight = preflight_capture_artifacts(run_dir, capture_config)
        if not preflight["ok"]:
            raise RuntimeError(f"capture preflight failed: {preflight.get('errors', preflight)}")
        print(f"\n===== rollout {k+1}/{args.rollouts}  run_id={run_id}  model={args.model}  provider={args.provider}  temp={args.temperature}  workers={args.workers} =====", flush=True)
        print(f"  [capture lock] {lock_status}", flush=True)
        print(f"  [capture preflight] {preflight['state']}", flush=True)
        t0 = time.time()
        results = run_rollout(args.model, args.temperature, suites, args.benchmark_version, args.smoke, run_dir, args.provider, args.workers, args.max_cost_usd)
        raw_summary = summarize_raw_responses(run_dir / "raw_responses.jsonl")
        raw_validation = validate_raw_response_summary(
            raw_summary,
            model_id=args.model,
            provider=args.provider,
            response_model=args.model,
            endpoint_revision=args.endpoint_revision,
            max_cost_usd=args.max_cost_usd,
            expected_cells=expected_cells,
        )
        rec = validate_trace_corpus(
            run_dir / "agentdojo",
            expected_cells,
            expected_provenance={
                "benchmark_version": args.benchmark_version,
                "agentdojo_package_version": adojo_version(),
            },
        )
        validation = {
            "ok": raw_validation["ok"] and rec["ok"],
            "raw_responses": raw_validation,
            "trace_corpus": rec,
        }
        manifest = {
            "run_id": run_id, "model_id": args.model, "temperature": args.temperature, "rollout": k,
            "workers": args.workers, "attack": ATTACK, "suites": suites,
            "benchmark_version": args.benchmark_version, "smoke": args.smoke,
            "provider": args.provider, "response_model": args.model,
            "endpoint_revision": args.endpoint_revision,
            "max_cost_usd": args.max_cost_usd, "openrouter_base": "https://openrouter.ai/api/v1",
            "capture_lock": lock_status,
            "agentdojo_version": adojo_version(), "agentdojo_path": getattr(agentdojo, "__file__", "?"),
            "eval_git": git_sha(EVAL_ROOT.parent), "started": started, "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "wall_seconds": round(time.time() - t0, 1), "results": results,
            "raw_response_summary": {key: value for key, value in raw_summary.items() if key != "records"},
            "expected_trace_cells": expected_cells,
            "validation": validation,
        }
        json.dump(manifest, open(run_dir / "manifest.json", "w"), indent=2)
        json.dump(results, open(run_dir / "results.json", "w"), indent=2)
        print(f"  [validate] ok={validation['ok']} trace={rec['valid_count']}/{rec['expected_count']} raw={raw_validation['response_count']}  [wall] {manifest['wall_seconds']}s", flush=True)
        summary.append((run_id, results, validation))
        if not validation["ok"]:
            sys.exit(f"ERROR: capture validation failed for {run_id}")

    cost1 = usage_usd()
    print("\n===== DONE =====")
    for run_id, results, validation in summary:
        print(f"  {run_id}: {results} | validation_ok={validation['ok']}")
    if cost0 is not None and cost1 is not None:
        print(f"\n[cost] this invocation ${cost1 - cost0:.4f} | total used ${cost1:.4f}")
    print(f"corpus at: {corpus}")


if __name__ == "__main__":
    main()
