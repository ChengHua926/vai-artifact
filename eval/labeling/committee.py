"""LLM labeling committee: re-run the labeling with your own API key.

Three models independently label each run with the frozen prompts. A run's
prompt is built from its trace file in the annotations release
(`<annotations>/<bench>/traces/<run>.json`) through an explicit whitelist, and
the templates come from `<annotations>/prompts/`, checked against its
SHA256SUMS. Rows are appended to one ledger per model under `--out/<bench>/`
(`extension_labels.read_votes` reads them like the released votes);
re-running the same command resumes.

The only credential is the OPENROUTER_API_KEY environment variable. Networking
is a single injectable transport callable; `--dry-run` builds every prompt and
prints its digest without any network use. A full re-run of both benchmarks
(388 runs, three models) cost about USD 100 at labeling time; `--limit` and
`--max-cost` bound it. Model outputs are not deterministic, so new votes can
differ from the released ones.

The released tau3-bench traces carry full tool results, as the extension round
saw them; the calibration-round labelers saw results cut at 1000 characters
(78 results in 19 of the 30 calibration runs).

  python -m eval.labeling.committee --annotations <path> --bench tau|clawsbench \
      [--set calibration|extension|all] --out <dir> [--workers 4] [--max-cost USD]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import functools
import hashlib
import http.client
import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODELS = (
    "anthropic/claude-fable-5",
    "openai/gpt-5.6-sol",
    "moonshotai/kimi-k3",
)
REQUEST_PARAMS = {
    "temperature": 0,
    "response_format": {"type": "json_object"},
    "usage": {"include": True},
    "reasoning": {"effort": "high"},
}
TEMPLATE_FILES = {"tau": "tau_v1.md", "clawsbench": "clawsbench_v1.md"}
RUN_SETS = ("calibration", "extension")

REQUEST_TIMEOUT_S = 900
RETRY_STATUSES = frozenset({408, 409, 425, 429})  # plus any code >= 500
FATAL_STATUSES = frozenset({401, 402, 403})
BACKOFF_S = (5, 15, 45, 120, 300)  # sleeps before retries 1..5 -> at most 6 tries
TASK_DEADLINE_S = 1800
TIMEOUT_RETRIES = 1
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
KEY_URL = "https://openrouter.ai/api/v1/auth/key"


VERDICTS = ("violation", "no_violation", "unsure")
CONFIDENCES = ("high", "low")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Level-2 sections by name, level-3
# numbered items inside them. The ids are the shared rule vocabulary.
RULE_SECTIONS = (
    ("S", re.compile(r"^safety rules", re.IGNORECASE)),
    ("E", re.compile(r"^execution rules", re.IGNORECASE)),
)
CORRECTION_NOTE = (
    "\n\nYour previous reply was rejected: {reason}. Reply again with only "
    "the JSON object, following the schema and its constraints exactly."
)


class ReplyError(ValueError):
    """A model reply that fails strict validation."""


class TransportStatusError(RuntimeError):
    """An HTTP status, or an error body returned under a 200, that carries no reply."""

    def __init__(self, code: int | None, detail: str = "", retry_after: float | None = None):
        self.code = code
        self.detail = str(detail)[:500]
        self.retry_after = retry_after
        super().__init__(f"http {code}: {self.detail}" if self.detail else f"http {code}")


class FatalTransportError(RuntimeError):
    """A condition no retry can fix -- missing key, dead key, no credit."""


class AbortedError(RuntimeError):
    """The run was aborted while this call was waiting out a backoff."""

    def __init__(self, message: str = "aborted"):
        super().__init__(message)


# --- credentials -------------------------------------------------------------


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise FatalTransportError("set the OPENROUTER_API_KEY environment variable")
    return key


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _retry_after_seconds(headers) -> float | None:
    try:
        value = None if headers is None else headers.get("Retry-After")
    except Exception:  # noqa: BLE001 - header containers vary by error type
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def openrouter_transport(payload: dict, api_key: str | None = None) -> dict:
    """One chat completion. Every failure mode leaves as a typed exception so
    the retrying wrapper can tell "wait and try again" from "stop the run"."""

    key = api_key or _api_key()
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    context = _ssl_context()
    try:
        with urllib.request.urlopen(
            request, timeout=REQUEST_TIMEOUT_S, context=context
        ) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        try:
            detail = error.read()[:500].decode(errors="replace")
        except Exception:  # noqa: BLE001 - the body may already be consumed
            detail = ""
        raise TransportStatusError(
            error.code, detail, retry_after=_retry_after_seconds(error.headers)
        ) from error
    data = json.loads(body.decode())
    if not isinstance(data, dict):
        raise TransportStatusError(502, "response is not an object")
    # OpenRouter returns upstream failures as a 200 carrying an error object;
    # reading choices first would turn those into a KeyError.
    if isinstance(data.get("error"), dict):
        raise TransportStatusError(int(data["error"].get("code") or 502), str(data["error"])[:500])
    if not data.get("choices"):
        raise TransportStatusError(502, "no choices in response")
    return data


# --- transport failure policy ------------------------------------------------

_TIMEOUTS = (TimeoutError, socket.timeout)
_RETRYABLE = (
    ConnectionError,
    ssl.SSLError,
    http.client.HTTPException,
    json.JSONDecodeError,
    OSError,
)


def classify_transport_error(error: BaseException) -> str:
    """fatal | retry | timeout | give_up -- what the wrapper should do next."""
    if isinstance(error, FatalTransportError):
        return "fatal"
    if isinstance(error, TransportStatusError):
        code = error.code
        if code in FATAL_STATUSES:
            return "fatal"
        if code in RETRY_STATUSES or (isinstance(code, int) and code >= 500):
            return "retry"
        return "give_up"
    if isinstance(error, _TIMEOUTS):
        return "timeout"
    if isinstance(error, urllib.error.URLError):
        return "timeout" if isinstance(error.reason, _TIMEOUTS) else "retry"
    if isinstance(error, _RETRYABLE):
        return "retry"
    return "give_up"


def error_class_name(error: BaseException) -> str:
    """A short, greppable class for an error row: what went wrong, not where."""
    if isinstance(error, ReplyError):
        return "invalid_reply"
    if isinstance(error, AbortedError):
        return "aborted"
    if isinstance(error, FatalTransportError):
        return "fatal"
    if isinstance(error, TransportStatusError):
        return f"http_{error.code}" if error.code is not None else "http_error"
    if isinstance(error, _TIMEOUTS):
        return "timeout"
    if isinstance(error, urllib.error.URLError):
        return "timeout" if isinstance(error.reason, _TIMEOUTS) else "connection"
    if isinstance(error, (ConnectionError, ssl.SSLError, http.client.HTTPException, OSError)):
        return "connection"
    return type(error).__name__


class RetryingTransport:
    """Bounded backoff around one transport, shared by every worker thread.

    A fatal condition sets the run-wide abort event and leaves as a
    FatalTransportError, which label_task refuses to swallow. `retries` reports
    what the most recent call on the calling thread cost, readable after the
    call raises as well as after it returns, so an error row can record the
    waiting a failed task still paid for.
    """

    def __init__(
        self,
        inner,
        *,
        abort: threading.Event | None = None,
        backoff=BACKOFF_S,
        sleep=time.sleep,
        jitter=None,
        deadline_s: float = TASK_DEADLINE_S,
        timeout_retries: int = TIMEOUT_RETRIES,
    ):
        self.inner = inner
        self.abort = abort
        self.backoff = tuple(backoff)
        self.sleep = sleep
        self.jitter = jitter
        self.deadline_s = deadline_s
        self.timeout_retries = timeout_retries
        self._local = threading.local()

    @property
    def retries(self) -> int:
        return getattr(self._local, "retries", 0)

    def _stop_run(self, error: BaseException):
        if self.abort is not None:
            self.abort.set()
        if isinstance(error, FatalTransportError):
            raise error
        raise FatalTransportError(f"fatal transport error: {error}") from error

    def _delay(self, slot: int, error: BaseException) -> float:
        delay = self.backoff[slot] * (self.jitter() if self.jitter else random.uniform(0.8, 1.2))
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
            delay = max(delay, min(float(retry_after), 300.0))
        return delay

    def __call__(self, payload: dict) -> dict:
        self._local.retries = 0
        started = time.monotonic()
        timeouts = 0
        slot = 0
        while True:
            try:
                return self.inner(payload)
            except BaseException as error:  # noqa: BLE001 - classified, then re-raised
                kind = classify_transport_error(error)
                if kind == "fatal":
                    self._stop_run(error)
                if kind == "give_up":
                    raise
                if kind == "timeout":
                    timeouts += 1
                    if timeouts > self.timeout_retries:
                        raise
                if slot >= len(self.backoff):
                    raise
                delay = self._delay(slot, error)
                if time.monotonic() - started + delay > self.deadline_s:
                    raise
                if self.abort is not None and self.abort.is_set():
                    raise AbortedError() from error
                self.sleep(delay)
                self._local.retries += 1
                slot += 1


# --- prompt rendering --------------------------------------------------------


def render_template(template: str, mapping: dict[str, str]) -> str:
    """Fill {name} placeholders in one pass; literal braces elsewhere survive."""
    for key in mapping:
        if "{" + key + "}" not in template:
            raise ValueError(f"template is missing placeholder {{{key}}}")
    pattern = re.compile("|".join(re.escape("{" + key + "}") for key in mapping))
    return pattern.sub(lambda match: str(mapping[match.group(0)[1:-1]]), template)


def _block(label: str, value, pad: str = "  ") -> list[str]:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    if "\n" in text:
        return [pad + label + ":"] + [pad + "  " + line for line in text.splitlines()]
    return [pad + label + ": " + text]


def _indented(text: str, pad: str = "  ") -> list[str]:
    return [pad + line for line in str(text).splitlines()] or [pad]


# --- tau ---------------------------------------------------------------------


def tau_policy_text(segments: list[dict]) -> str:
    lines: list[str] = []
    heading = None
    for segment in segments:
        if segment.get("heading") != heading:
            heading = segment.get("heading")
            if heading:
                if lines:
                    lines.append("")
                lines.append(f"## {heading}")
        lines.append(f"[{segment['id']}] {segment.get('text', '')}")
    return "\n".join(lines)


def tau_transcript_text(steps: list[dict]) -> str:
    lines: list[str] = []
    for step in steps:
        index = step.get("index")
        step_type = step.get("type", "step")
        if step.get("tool"):
            lines.append(f"[{index}] {step_type}: {step['tool']}")
            if step.get("args") is not None:
                lines.extend(_block("args", step["args"]))
            if step.get("error"):
                lines.extend(_block("error", step["error"]))
            if step.get("result") is not None:
                label = "result (truncated)" if step.get("result_truncated") else "result"
                lines.extend(_block(label, step["result"]))
        else:
            lines.append(f"[{index}] {step_type}:")
            lines.extend(_indented(step.get("text", "")))
    return "\n".join(lines)


def tau_context(manifest: dict, template: str) -> dict:
    segments = (manifest.get("policy") or {}).get("segments") or []
    steps = manifest.get("steps") or []
    prompt = render_template(
        template,
        {
            "run_id": manifest.get("run", ""),
            "domain": manifest.get("domain", ""),
            "user_scenario": manifest.get("user_scenario", ""),
            "policy_segments": tau_policy_text(segments),
            "transcript": tau_transcript_text(steps),
        },
    )
    return {
        "task": manifest.get("run"),
        "prompt": prompt,
        "vocabulary": {segment["id"]: segment.get("text", "") for segment in segments},
        "step_count": len(steps),
    }


# --- clawsbench --------------------------------------------------------------


def _md_sections(text: str) -> list[dict]:
    sections: list[dict] = []
    current = None
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## ") :
            current = {"heading": line[3:].strip(), "lines": []}
            sections.append(current)
            continue
        if current is not None:
            current["lines"].append(line)
    return sections


def claws_rules(meta_text: str) -> list[dict]:
    rules: list[dict] = []
    for section in _md_sections(meta_text):
        tag = next(
            (tag for tag, pattern in RULE_SECTIONS if pattern.match(section["heading"])),
            None,
        )
        if not tag:
            continue
        fenced = False
        for line in section["lines"]:
            if line.strip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            match = re.match(r"^###\s+(\d+)\.\s*(.+?)\s*$", line)
            if match:
                rules.append(
                    {
                        "id": f"{tag}{match.group(1)}",
                        "number": int(match.group(1)),
                        "title": match.group(2),
                        "section": section["heading"],
                    }
                )
    return rules


def claws_rule_sections_text(meta_text: str) -> str:
    parts = []
    for section in _md_sections(meta_text):
        if any(pattern.match(section["heading"]) for _, pattern in RULE_SECTIONS):
            body = "\n".join(section["lines"]).strip()
            parts.append(f"## {section['heading']}\n\n{body}")
    return "\n\n".join(parts)


def _tool_call_output(event: dict) -> str:
    texts = []
    for item in event.get("content") or []:
        if not isinstance(item, dict):
            continue
        inner = item.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            texts.append(inner["text"])
        elif isinstance(item.get("text"), str):
            texts.append(item["text"])
    return "\n".join(texts)


def claws_timeline_text(events: list[dict]) -> str:
    lines: list[str] = []
    for index, event in enumerate(events):
        event_type = event.get("type", "event")
        if event_type == "tool_call":
            detail = ", ".join(
                str(part) for part in (event.get("kind"), event.get("status")) if part
            )
            lines.append(f"[{index}] tool_call" + (f" ({detail})" if detail else ""))
            if event.get("title"):
                lines.extend(_block("command", event["title"]))
            output = _tool_call_output(event)
            if output:
                lines.extend(_block("output", output))
        else:
            lines.append(f"[{index}] {event_type}:")
            lines.extend(_indented(event.get("text", "")))
    return "\n".join(lines)


def claws_service_writes(service_activity: dict) -> list[dict]:
    """Agent-phase state-changing calls only."""
    rows = []
    for service in sorted(service_activity or {}):
        report = service_activity.get(service) or {}
        for entry in report.get("agent_entries") or []:
            method = str(entry.get("method", "")).upper()
            if method not in WRITE_METHODS:
                continue
            rows.append(
                {
                    "service": service,
                    "method": method,
                    "path": entry.get("path", ""),
                    "body": entry.get("request_body"),
                    "status": entry.get("response_status"),
                    "timestamp": entry.get("timestamp"),
                }
            )
    rows.sort(key=lambda row: str(row["timestamp"] or ""))
    return rows


def claws_writes_text(service_activity: dict) -> str:
    rows = claws_service_writes(service_activity)
    if not rows:
        return "(none recorded)"
    lines: list[str] = []
    for row in rows:
        lines.append(f"{row['method']} {row['service']} {row['path']} -> {row['status']}")
        if row["body"] is not None:
            lines.extend(_block("body", row["body"]))
    return "\n".join(lines)


def claws_context(manifest: dict, template: str) -> dict:
    # Whitelist: instruction, rules doc, timeline, recorded writes. Nothing
    # else in the task file (outcome, evaluation, model, provenance, cost)
    # may reach the prompt.
    canonical = manifest.get("canonical") or {}
    instructions = canonical.get("instructions") or {}
    meta = (instructions.get("bootstrap") or {}).get("AGENTS.md") or ""
    timeline = canonical.get("agent_timeline") or []
    prompt = render_template(
        template,
        {
            "task_id": manifest.get("task", ""),
            "instruction": instructions.get("task_prompt") or canonical.get("prompt") or "",
            "rules": claws_rule_sections_text(meta),
            "timeline": claws_timeline_text(timeline),
            "service_writes": claws_writes_text(canonical.get("service_activity") or {}),
        },
    )
    vocabulary = {
        rule["id"]: f"### {rule['number']}. {rule['title']}" for rule in claws_rules(meta)
    }
    return {
        "task": manifest.get("task"),
        "prompt": prompt,
        "vocabulary": vocabulary,
        "step_count": len(timeline),
    }


# --- reply validation --------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def parse_reply(raw: str) -> dict:
    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
        raise ReplyError("reply is not a JSON object")
    raise ReplyError("reply is not parseable JSON")


def validate_reply(raw: str, vocabulary: dict[str, str], step_count: int) -> dict:
    data = parse_reply(raw)
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise ReplyError(f"verdict must be one of {'|'.join(VERDICTS)}")
    violations = data.get("violations", [])
    if not isinstance(violations, list):
        raise ReplyError("violations must be a list")
    clean = []
    for violation in violations:
        if not isinstance(violation, dict):
            raise ReplyError("each violation must be an object")
        rule_id = violation.get("rule_id")
        if rule_id not in vocabulary:
            raise ReplyError(f"unknown rule_id {rule_id!r}")
        quote = violation.get("quote")
        if not isinstance(quote, str) or not _norm(quote):
            raise ReplyError("quote must be a nonempty string")
        if _norm(quote) not in _norm(vocabulary[rule_id]):
            raise ReplyError(f"quote is not verbatim text of {rule_id}")
        steps = violation.get("step_indexes", [])
        if not isinstance(steps, list):
            raise ReplyError("step_indexes must be a list")
        for step in steps:
            if isinstance(step, bool) or not isinstance(step, int):
                raise ReplyError("step_indexes must be integers")
            if not 0 <= step < step_count:
                raise ReplyError(f"step index {step} out of range 0..{step_count - 1}")
        confidence = violation.get("confidence")
        if confidence not in CONFIDENCES:
            raise ReplyError(f"confidence must be one of {'|'.join(CONFIDENCES)}")
        clean.append(
            {
                "rule_id": rule_id,
                "quote": quote,
                "step_indexes": sorted(set(steps)),
                "rationale": str(violation.get("rationale", "")),
                "confidence": confidence,
            }
        )
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise ReplyError("notes must be a string")
    return {"verdict": verdict, "violations": clean, "notes": notes}


# --- per-task labeling -------------------------------------------------------


def _payload(model: str, prompt: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        **REQUEST_PARAMS,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _usage_cost(usage) -> float:
    """The cost OpenRouter reported for one response, 0.0 when absent."""
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    return 0.0


def label_task(
    transport,
    model: str,
    context: dict,
    *,
    stage: str | None = None,
    bench: str | None = None,
    abort: threading.Event | None = None,
) -> dict:
    """One task, one model, at most two attempts: a rejected reply is sent back
    once with the reason. Every row carries the hash of the prompt that was
    actually sent, so a row can be tied to a prompt revision after the fact."""

    correction = None
    reason = "no attempt made"
    error_class = "none"
    transport_retries = 0
    billed = 0.0  # every response is billed, including a rejected one
    started_at = _utc_now()
    started = time.monotonic()
    provenance: dict = {}
    for attempt in range(2):
        prompt = context["prompt"]
        if correction is not None:
            prompt += CORRECTION_NOTE.format(reason=correction)
        provenance = {
            "bench": bench,
            "stage": stage,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_chars": len(prompt),
            "started_at": started_at,
            "request_params": dict(REQUEST_PARAMS),
        }
        if abort is not None and abort.is_set():
            reason = "aborted"
            error_class = "aborted"
            break
        try:
            try:
                response = transport(_payload(model, prompt))
            finally:
                transport_retries += getattr(transport, "retries", 0)
            billed += _usage_cost(response.get("usage") if isinstance(response, dict) else None)
            message = response["choices"][0]["message"]
            raw = message["content"]
            if not isinstance(raw, str):
                # A provider-side refusal (finish_reason content_filter) or an
                # empty reply: nothing to validate, and no correction can help.
                choice = response["choices"][0]
                refusal = message.get("refusal") or choice.get("finish_reason") or "no content"
                usage = response.get("usage")
                return {
                    "task": context["task"],
                    "model": model,
                    "status": "refused",
                    "reason": f"refusal: {str(refusal)[:500]}",
                    "error_class": "refusal",
                    "finish_reason": choice.get("finish_reason"),
                    "native_finish_reason": choice.get("native_finish_reason"),
                    "response_model": response.get("model"),
                    "provider": response.get("provider"),
                    "generation_id": response.get("id"),
                    "attempts": attempt + 1,
                    "latency_s": round(time.monotonic() - started, 2),
                    "usage": usage if isinstance(usage, dict) else None,
                    **provenance,
                    "finished_at": _utc_now(),
                    "transport_retries": transport_retries,
                    "billed_cost_usd": round(billed, 6),
                }
            fields = validate_reply(raw, context["vocabulary"], context["step_count"])
        except ReplyError as error:
            correction = str(error)
            reason = f"invalid reply: {error}"
            error_class = "invalid_reply"
            continue
        except FatalTransportError:
            raise
        except Exception as error:  # noqa: BLE001 - transport failures become rows
            reason = f"transport failure: {error}"
            error_class = error_class_name(error)
            continue
        usage = response.get("usage")
        reasoning = message.get("reasoning")
        return {
            "task": context["task"],
            "model": model,
            "status": "ok",
            "verdict": fields["verdict"],
            "violations": fields["violations"],
            "notes": fields["notes"],
            "raw": raw,
            "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "reasoning": reasoning if isinstance(reasoning, str) else None,
            "response_model": response.get("model"),
            "provider": response.get("provider"),
            "generation_id": response.get("id"),
            "finish_reason": response["choices"][0].get("finish_reason"),
            "native_finish_reason": response["choices"][0].get("native_finish_reason"),
            "attempts": attempt + 1,
            "latency_s": round(time.monotonic() - started, 2),
            "usage": usage if isinstance(usage, dict) else None,
            **provenance,
            "finished_at": _utc_now(),
            "transport_retries": transport_retries,
            "billed_cost_usd": round(billed, 6),
        }
    return {
        "task": context["task"],
        "model": model,
        "status": "error",
        "reason": reason,
        "error_class": error_class,
        "attempts": 2,
        "latency_s": round(time.monotonic() - started, 2),
        **provenance,
        "finished_at": _utc_now(),
        "transport_retries": transport_retries,
        "billed_cost_usd": round(billed, 6),
    }


# --- run orchestration -------------------------------------------------------


def _sanitize_slug(slug: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", slug)


def _dump_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, indent=1) + "\n"


def _usage_totals(rows: list[dict]) -> tuple[float | None, int | None]:
    cost = tokens = None
    for row in rows:
        usage = row.get("usage") or {}
        # Rows written since the extension round carry what was billed across
        # every attempt; older rows only know the cost of the reply they kept.
        row_cost = row.get("billed_cost_usd")
        if not (isinstance(row_cost, (int, float)) and not isinstance(row_cost, bool)):
            row_cost = usage.get("cost")
        if isinstance(row_cost, (int, float)) and not isinstance(row_cost, bool):
            cost = (cost or 0.0) + row_cost
        row_tokens = usage.get("total_tokens")
        if isinstance(row_tokens, int) and not isinstance(row_tokens, bool):
            tokens = (tokens or 0) + row_tokens
    return (round(cost, 6) if cost is not None else None), tokens


# --- ledgers (append-only) ---------------------------------------------------

_LEDGER_LOCK = threading.Lock()


def append_row(ledger: Path, row: dict) -> None:
    """Append one durable line. A ledger is never rewritten or truncated, so a
    crash costs the task in flight and nothing else."""

    line = json.dumps(row, sort_keys=True) + "\n"
    with _LEDGER_LOCK:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def read_rows(ledger: Path) -> list[dict]:
    """Every appended row, oldest first. A torn LAST line -- a crash between
    write and fsync -- is dropped; a bad line anywhere else is a real problem
    and raises."""

    if not ledger.exists():
        return []
    lines = ledger.read_text(encoding="utf-8").splitlines()
    rows: list[dict] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
    return rows


def resolve_rows(rows: list[dict]) -> list[dict]:
    """One row per (model, task): the last ok row if the pair ever succeeded,
    otherwise its last attempt. First-seen order is preserved."""

    order: list[tuple] = []
    latest: dict[tuple, dict] = {}
    for row in rows:
        key = (row.get("model"), row.get("task"))
        if key not in latest:
            order.append(key)
            latest[key] = row
        elif row.get("status") == "ok" or latest[key].get("status") != "ok":
            latest[key] = row
    return [latest[key] for key in order]


def _all_ledger_rows(bench_dir: Path) -> list[dict]:
    rows: list[dict] = []
    if not bench_dir.exists():
        return rows
    for path in sorted(bench_dir.glob("*.jsonl")):
        rows.extend(read_rows(path))
    return rows


def _existing_terminal_rows(ledger: Path, model: str, retry_refused: bool = False) -> dict[str, dict]:
    """Rows a resume must not redo: every ok row and, unless asked to retry
    them, every provider refusal (a policy block is deterministic for the same
    prompt, and re-sending it only adds rows)."""
    terminal = ("ok",) if retry_refused else ("ok", "refused")
    rows: dict[str, dict] = {}
    for row in read_rows(ledger):
        if row.get("model") == model and row.get("status") in terminal:
            rows[row["task"]] = row
    return rows


def _census(resolved: list[dict], raw: list[dict], model: str, order: list[str]) -> dict:
    """What one model's ledger holds against the stage's full task list."""
    by_task = {row["task"]: row for row in resolved if row.get("model") == model}
    return {
        "rows": sum(1 for row in raw if row.get("model") == model),
        "ok_tasks": sum(1 for task in order if (by_task.get(task) or {}).get("status") == "ok"),
        "refused_tasks": sum(
            1 for task in order if (by_task.get(task) or {}).get("status") == "refused"
        ),
        "error_only_tasks": sum(
            1
            for task in order
            if task in by_task and by_task[task].get("status") not in ("ok", "refused")
        ),
        "missing_tasks": sum(1 for task in order if task not in by_task),
    }


def run_committee(
    *,
    bench: str,
    contexts: list[dict],
    template_path: Path,
    models: list[str],
    out_root: Path,
    transport,
    stage: str = "all",
    workers: int = 1,
    task_order: list[str] | None = None,
    max_cost: float | None = None,
    retry_refused: bool = False,
    abort: threading.Event | None = None,
    manifest_extra: dict | None = None,
    progress=None,
) -> dict:
    """Label every (model, task) pair that has no ok row yet, appending each
    result the moment it lands, then rebuild the manifest from what is on
    disk -- not from this invocation -- so a --limit or a
    single-model rerun extends the ledgers instead of narrowing them."""

    bench_dir = out_root / bench
    bench_dir.mkdir(parents=True, exist_ok=True)
    abort = abort or threading.Event()
    if progress is None:
        def progress(line: str) -> None:
            print(line, flush=True)
    fatal: list[BaseException] = []
    aborted: str | None = None
    state_lock = threading.Lock()

    spent = _usage_totals(resolve_rows(_all_ledger_rows(bench_dir)))[0] or 0.0

    plan: list[tuple[str, Path, list[dict]]] = []
    rows_for_model: dict[str, list[dict]] = {}
    counts: dict[str, dict] = {}
    for model in models:
        ledger = bench_dir / f"{_sanitize_slug(model)}.jsonl"
        existing = _existing_terminal_rows(ledger, model, retry_refused)
        reused = [existing[c["task"]] for c in contexts if c["task"] in existing]
        rows_for_model[model] = list(reused)
        counts[model] = {"ok": 0, "error": 0, "refused": 0, "reused": len(reused)}
        plan.append((model, ledger, [c for c in contexts if c["task"] not in existing]))
    total_todo = sum(len(todo) for _, _, todo in plan)
    done = 0

    def _work(model: str, ledger: Path, context: dict) -> None:
        nonlocal spent, done, aborted
        if abort.is_set():
            return
        try:
            row = label_task(transport, model, context, stage=stage, bench=bench, abort=abort)
        except FatalTransportError as error:
            with state_lock:
                fatal.append(error)
            abort.set()
            return
        cost = row.get("billed_cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            cost = _usage_cost(row.get("usage"))
        with state_lock:
            append_row(ledger, row)
            rows_for_model[model].append(row)
            counts[model][row["status"] if row["status"] in ("ok", "refused") else "error"] += 1
            spent += cost
            done += 1
            progress(
                f"{_utc_now()} {stage} {bench} {model} {row['task']} {row['status']} "
                f"a={row['attempts']} r={row['transport_retries']} "
                f"{row['latency_s']:.1f}s ${cost:.4f} tot=${spent:.2f} [{done}/{total_todo}]"
            )
            if max_cost is not None and spent > max_cost:
                aborted = "max_cost"
                abort.set()

    def _drain(futures) -> None:
        for future in futures:
            try:
                future.result()
            except BaseException as error:  # noqa: BLE001 - raised after the writes
                with state_lock:
                    fatal.append(error)
                abort.set()

    def _submit(model: str, ledger: Path, todo: list[dict]) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            _drain([pool.submit(_work, model, ledger, context) for context in todo])

    try:
        if workers <= 1:
            for model, ledger, todo in plan:
                for context in todo:
                    if abort.is_set():
                        break
                    _work(model, ledger, context)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(models))) as outer:
                _drain([outer.submit(_submit, *entry) for entry in plan])
    except BaseException as error:  # noqa: BLE001 - the derived files still get written
        fatal.append(error)
        abort.set()

    raw_rows = _all_ledger_rows(bench_dir)
    rows = resolve_rows(raw_rows)
    order = list(task_order or [context["task"] for context in contexts])
    known = set(order)
    extra = {row["task"] for row in rows if row.get("task") and row["task"] not in known}
    order.extend(sorted(extra))

    per_model: dict[str, dict] = {}
    for model in models:
        cost, tokens = _usage_totals(rows_for_model[model])
        per_model[model] = {**counts[model], "cost_usd": cost, "total_tokens": tokens}
    seen_models = set(models)
    ledger_models = list(models) + sorted(
        {row["model"] for row in raw_rows if row.get("model") and row["model"] not in seen_models}
    )
    cost_total, tokens_total = _usage_totals(rows)
    manifest = {
        "bench": bench,
        "models": list(models),
        "prompt_file": template_path.name,
        "prompt_sha256": hashlib.sha256(template_path.read_bytes()).hexdigest(),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request_params": REQUEST_PARAMS,
        "per_model": per_model,
        "task_count": len(order),
        "stage": stage,
        "workers": workers,
        "ledger_models": ledger_models,
        "ledger_census": {
            model: _census(rows, raw_rows, model, order) for model in ledger_models
        },
        "cost_total_usd": cost_total,
        "total_tokens_all": tokens_total,
        "aborted": aborted,
        **(manifest_extra or {}),
    }
    (bench_dir / "run_manifest.json").write_text(_dump_json(manifest))
    sums = []
    for path in sorted(bench_dir.iterdir()):
        if path.name == "SHA256SUMS" or not path.is_file():
            continue
        sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (bench_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    summary = {
        "rows": [row for model in models for row in rows_for_model[model]],
        "manifest": manifest,
        "aborted": aborted,
    }
    if fatal:
        raise fatal[0]
    return summary


def run_ids(annotations: Path, bench: str, run_set: str = "all") -> list[str]:
    """The runs of one set (or both), in the order of the release index."""

    index = json.loads((annotations / bench / "index.json").read_text())
    wanted = RUN_SETS if run_set == "all" else (run_set,)
    if bench == "tau":
        return [run for run, entry in index.items() if entry.get("set") in wanted]
    return [run for name in wanted for run in index[name]]


def load_template(annotations: Path, bench: str) -> tuple[Path, str]:
    """The frozen prompt, refused unless it matches the release's SHA256SUMS."""

    prompts = annotations / "prompts"
    name = TEMPLATE_FILES[bench]
    expected = {}
    for line in (prompts / "SHA256SUMS").read_text().splitlines():
        if line.strip():
            digest, file_name = line.split(maxsplit=1)
            expected[file_name.strip()] = digest
    path = prompts / name
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected.get(name) != actual:
        raise ValueError(f"{name} does not match prompts/SHA256SUMS")
    return path, path.read_text()


def load_contexts(annotations: Path, bench: str, runs: list[str], template: str) -> list[dict]:
    contexts = []
    for run in runs:
        manifest = json.loads((annotations / bench / "traces" / f"{run}.json").read_text())
        contexts.append(tau_context(manifest, template) if bench == "tau" else claws_context(manifest, template))
    return contexts


def _get_json(url: str, key: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
        data = json.loads(response.read().decode())
    return data if isinstance(data, dict) else {}


def preflight() -> None:
    """Spend headroom before a long unattended run. Prints balances only --
    never the key, never the key's label."""

    key = _api_key()
    credits = _get_json(CREDITS_URL, key).get("data") or {}
    total, used = credits.get("total_credits"), credits.get("total_usage")
    remaining = (
        round(total - used, 6)
        if isinstance(total, (int, float)) and isinstance(used, (int, float))
        else None
    )
    print(f"credits total_credits={total} total_usage={used} remaining={remaining}")
    limits = _get_json(KEY_URL, key).get("data") or {}
    print(
        f"key limit={limits.get('limit')} limit_remaining={limits.get('limit_remaining')} "
        f"usage={limits.get('usage')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--annotations", type=Path, required=True, help="the annotations release")
    parser.add_argument("--bench", required=True, choices=sorted(TEMPLATE_FILES))
    parser.add_argument("--set", dest="run_set", choices=(*RUN_SETS, "all"), default="all")
    parser.add_argument("--out", type=Path, default=None, help="ledger directory (required unless --dry-run)")
    parser.add_argument(
        "--model", default=None, help="single OpenRouter slug (default: all three)"
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=1, help="concurrent tasks per model")
    parser.add_argument("--max-cost", type=float, default=None, help="ceiling in USD for this ledger directory")
    parser.add_argument("--preflight", action="store_true", help="print spend headroom and exit")
    parser.add_argument(
        "--retry-refused", action="store_true", help="re-send tasks a provider refused"
    )
    arguments = parser.parse_args()

    if arguments.preflight:
        preflight()
        return

    template_path, template = load_template(arguments.annotations, arguments.bench)
    task_order = run_ids(arguments.annotations, arguments.bench, arguments.run_set)
    runs = task_order[: arguments.limit] if arguments.limit is not None else task_order
    contexts = load_contexts(arguments.annotations, arguments.bench, runs, template)
    if arguments.dry_run:
        chars = 0
        for context in contexts:
            digest = hashlib.sha256(context["prompt"].encode()).hexdigest()
            chars += len(context["prompt"])
            print(
                f"{context['task']} chars={len(context['prompt'])} "
                f"rules={len(context['vocabulary'])} steps={context['step_count']} "
                f"sha256={digest}"
            )
        print(f"TOTAL prompts={len(contexts)} chars={chars} est_tokens={chars // 4}")
        if len(contexts) == 1:
            print()
            print(contexts[0]["prompt"])
        return
    if arguments.out is None:
        parser.error("--out is required unless --dry-run or --preflight")

    try:
        key = _api_key()
    except FatalTransportError as error:
        parser.error(str(error))
    models = [arguments.model] if arguments.model else list(DEFAULT_MODELS)
    abort = threading.Event()
    transport = RetryingTransport(
        functools.partial(openrouter_transport, api_key=key), abort=abort
    )
    try:
        summary = run_committee(
            bench=arguments.bench,
            contexts=contexts,
            template_path=template_path,
            models=models,
            out_root=arguments.out,
            transport=transport,
            stage=arguments.run_set,
            workers=arguments.workers,
            task_order=task_order,
            max_cost=arguments.max_cost,
            retry_refused=arguments.retry_refused,
            abort=abort,
        )
    except FatalTransportError as error:
        print(f"fatal: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    for model, stats in summary["manifest"]["per_model"].items():
        print(
            f"{model}: ok={stats['ok']} reused={stats['reused']} "
            f"error={stats['error']} refused={stats['refused']} cost={stats['cost_usd']}"
        )
    for model, census in summary["manifest"]["ledger_census"].items():
        print(
            f"{model}: ledger rows={census['rows']} ok={census['ok_tasks']} "
            f"refused={census['refused_tasks']} error_only={census['error_only_tasks']} "
            f"missing={census['missing_tasks']}"
        )
    if summary["aborted"]:
        print(f"aborted: {summary['aborted']}", file=sys.stderr)
        raise SystemExit(3)


if __name__ == "__main__":
    main()
