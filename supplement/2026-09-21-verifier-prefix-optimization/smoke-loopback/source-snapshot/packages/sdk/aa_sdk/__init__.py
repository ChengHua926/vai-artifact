"""aa_sdk — the provider's integration boundary (runs in-process with the agent).

``Session.guard(tool, args, executor)`` executes and records; ``record`` accepts externally
completed effects and ``record_authorization`` records native permission decisions. Complete
mediation remains conditional on faithful handlers and the bound structured tool vocabulary.

Custody: each record ships to the trace store at action time, and ``end()`` reads the store's
records back and commits THEIR hash on chain. The store's per-action records are the trace; the
in-process buffer is only a working copy. Without a store (``store=None``, the offline
self-check mode) the SDK hashes its own buffer, and no custody claim holds.
"""
from __future__ import annotations

import logging
import copy
import threading
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from aa_commons import ActionRecord, hash_obj, registry, trace_hash, validate_params
from aa_commons.ids import params_hash
from aa_commons.trace import check_prefixes

log = logging.getLogger("aa_sdk")


class StoreUnavailable(RuntimeError):
    """Raised by ``Session.end()`` when the store has not acknowledged every record, or the
    records cannot be read back: NOTHING is committed, because the commitment must be the hash
    of what the store holds. Retry ``end()`` once the store is reachable; after a crash, finalize
    with ``Accountability.recover``."""

    def __init__(self, session_id: str, unshipped: list, detail: str = ""):
        self.session_id = session_id
        self.unshipped = list(unshipped)
        if self.unshipped:
            what = (f"{len(self.unshipped)} record(s) not acknowledged by the store "
                    f"(seqs {self.unshipped})")
        else:
            what = "the store's records could not be read back" + (f": {detail}" if detail else "")
        super().__init__(f"session {session_id}: {what}; nothing committed — "
                         "retry end() when the store is reachable")


@dataclass
class Promise:
    promise_id: "int | str"   # on-chain uint when chained; "promise-N" off-chain
    predicate: str        # spec_id in the core registry
    params: dict
    payout_wei: int
    _predicate_hash: str = field(init=False, repr=False)
    _params_hash: str = field(init=False, repr=False)

    def __post_init__(self):
        import copy
        self.params = copy.deepcopy(self.params)
        self._predicate_hash = registry.predicate_hash_for(self.predicate)
        self._params_hash = params_hash(self.params)

    @property
    def predicate_hash(self) -> str:
        return self._predicate_hash

    @property
    def params_hash(self) -> str:
        return self._params_hash


# ── trace sinks ────────────────────────────────────────────────────────────────
class HttpStore:
    """The one store client for the FastAPI + SQLite service (packages/store). Records ship one
    at a time as actions happen (append_record) and are read back at session end (get_records);
    promises ship their readable {predicate, params} at registration (put_promise).

    Local now (http://127.0.0.1:8000); to deploy, run that service anywhere and point STORE_URL at it
    — no code change. `store` is optional on Accountability; pass None for in-memory self-check demos."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def append_record(self, session_id: str, record: dict) -> None:
        import requests
        requests.post(f"{self.base_url}/sessions/{session_id}/records", json=record, timeout=10).raise_for_status()

    def get_records(self, session_id: str) -> list:
        import requests
        r = requests.get(f"{self.base_url}/sessions/{session_id}/records", timeout=10)
        r.raise_for_status()
        return r.json()

    def put_promise(self, promise_id: int, record: dict) -> None:
        import requests
        requests.put(f"{self.base_url}/promises/{promise_id}", json=record, timeout=10).raise_for_status()

    def get_promise(self, promise_id: int) -> dict:
        import requests
        r = requests.get(f"{self.base_url}/promises/{promise_id}", timeout=10)
        r.raise_for_status()
        return r.json()


def _differing_seqs(buffer: list, shipped: list) -> list:
    """seqs at which the in-process buffer and the store's records disagree (either side missing
    counts). Compared through canonical hashing so JSON round-tripping is not a difference."""
    a = {r.seq: hash_obj(r.to_dict()) for r in buffer}
    b = {r.seq: hash_obj(r.to_dict()) for r in shipped}
    return sorted(s for s in a.keys() | b.keys() if a.get(s) != b.get(s))


def _event_context(action_id, event_id, metadata):
    context = copy.deepcopy(metadata or {})
    for key, value in (("action_id", action_id), ("event_id", event_id)):
        if value is not None:
            if key in context and context[key] != value:
                raise ValueError(f"conflicting metadata {key}")
            context[key] = value
        if key in context and (not isinstance(context[key], str) or not context[key]):
            raise ValueError(f"{key} must be a nonempty string")
    return context


# ── session + chokepoint ────────────────────────────────────────────────────────
@dataclass
class Session:
    """Ordered observations with best-effort custody and asynchronous prefix anchoring.

    Native permission checks belong to the application. This SDK records their decisions and
    action outcomes, including execution errors; it never evaluates a predicate to prevent execution.
    """
    acc: "Accountability"
    session_id: str
    party: str
    records: list = field(default_factory=list)
    receipt: "dict | None" = None
    _seq: int = 0
    _shipped_through: int = 0
    _lock: Any = field(default_factory=threading.RLock, repr=False)
    _ship_lock: Any = field(default_factory=threading.Lock, repr=False)
    _commit_lock: Any = field(default_factory=threading.Lock, repr=False)
    _wake: Any = field(default_factory=threading.Event, repr=False)
    _stop: Any = field(default_factory=threading.Event, repr=False)
    _worker: Any = field(default=None, repr=False)
    _events: dict = field(default_factory=dict, repr=False)
    _guard_identities: set = field(default_factory=set, repr=False)
    _recorded_at: dict = field(default_factory=dict, repr=False)
    _summary: Any = field(default=None, repr=False)
    _closing: bool = False
    _final_submission_started: bool = False
    _active_executors: int = 0
    _anchored_count: int = 0
    _checkpoint_error: Any = None
    _checkpoint_in_flight: bool = False
    _retry_after: float = 0

    def _append_event(self, tool, args, result, *, action_id=None, event_id=None, metadata=None):
        context = _event_context(action_id, event_id, metadata)
        event_id = context.get("event_id")
        payload = {"tool": tool, "args": copy.deepcopy(args), "result": copy.deepcopy(result),
                   "metadata": context or None}
        digest = hash_obj(payload)  # reject noncanonical data before allocating a sequence number
        with self._lock:
            if self._closing or self._summary is not None:
                raise RuntimeError("session is closing or finalized")
            if event_id and event_id in self._events:
                previous_digest, previous_record = self._events[event_id]
                if previous_digest != digest:
                    raise ValueError(f"conflicting retransmission for event {event_id!r}")
                return previous_record
            self._seq += 1
            now = time.time()
            rec = ActionRecord(self._seq, self.session_id, tool, payload["args"],
                               payload["result"], int(now * 1000), context or None)
            self.records.append(rec)
            self._recorded_at[rec.seq] = now
            if event_id:
                self._events[event_id] = (digest, rec)
        self._flush()
        self._ensure_checkpoint_worker()
        self._wake.set()
        return rec

    def _flush(self) -> None:
        """One shipper preserves seq order; an outage leaves observations for retry."""
        if not self.acc.store:
            return
        with self._ship_lock:
            with self._lock:
                pending = [r for r in self.records if r.seq > self._shipped_through]
            for rec in pending:
                try:
                    self.acc.store.append_record(self.session_id, rec.to_dict())
                except Exception:
                    return
                with self._lock:
                    self._shipped_through = rec.seq

    def unshipped_seqs(self) -> list:
        with self._lock:
            return ([r.seq for r in self.records if r.seq > self._shipped_through]
                    if self.acc.store else [])

    def guard(self, tool: str, args: dict, executor: Callable[[], Any], *,
              action_id=None, event_id=None, metadata=None) -> Any:
        """Record a return or execution error after any authorizations emitted by the executor.

        An exception does not establish nonexecution: partial effects may already have happened.
        The original exception is re-raised after observing the error under the same action ID.
        Reusing an invocation/event identity is rejected before execution, including during an
        in-flight call. Transport-only retransmissions belong to record().
        """
        context = _event_context(action_id, event_id, metadata)
        observed_args = copy.deepcopy(args)
        hash_obj({"tool": tool, "args": observed_args, "metadata": context})
        identities = {(key, context[key]) for key in ("action_id", "event_id") if key in context}
        with self._lock:
            if self._closing or self._summary is not None:
                raise RuntimeError("session is closing or finalized")
            if identities & self._guard_identities or context.get("event_id") in self._events:
                raise ValueError("invocation identity already used; guard cannot execute it again")
            self._guard_identities.update(identities)
            self._active_executors += 1
        try:
            try:
                result = executor()
            except Exception as error:
                try:
                    self.record(tool, observed_args, {"error": {"type": type(error).__name__, "message": str(error)}},
                                metadata=context)
                except Exception:
                    # A malformed observation must not replace the caller's execution failure.
                    log.exception("session %s: could not record executor error", self.session_id)
                raise
            self.record(tool, observed_args, result, metadata=context)
            return result
        finally:
            with self._lock:
                self._active_executors -= 1

    def record(self, tool: str, args: dict, result: Any, *,
               action_id=None, event_id=None, metadata=None) -> None:
        """Record an observed outcome. Stable event IDs make transport retries idempotent."""
        self._append_event(tool, args, result, action_id=action_id, event_id=event_id, metadata=metadata)

    def record_authorization(self, *, event_id, action_id, request_id, tool, args,
                             decision, scope, authority, principal=None, native_session_id=None,
                             native_decision=None, policy_id=None) -> None:
        """Emit authorization schema v1 for one actual invocation, including its native scope.

        Session/persistent scope never expands this event to other invocations. Native policy
        reuse must emit another explicitly bound event with authority=policy and its policy_id.
        """
        for name, value in (("event_id", event_id), ("action_id", action_id),
                            ("request_id", request_id), ("tool", tool)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty string")
        if decision not in {"allow", "deny", "timeout"}:
            raise ValueError("invalid authorization decision")
        if scope not in {"once", "session", "persistent"}:
            raise ValueError("invalid native authorization scope")
        if authority not in {"human", "policy", "auto_review"}:
            raise ValueError("invalid authorization authority")
        payload = {"schema_version": 1,
                   "action_id": action_id, "request_id": request_id, "session_id": self.session_id,
                   "tool": tool, "args_hash": hash_obj(args), "decision": decision,
                   "scope": scope, "authority": authority}
        for key, value in (("principal", principal), ("native_session_id", native_session_id),
                           ("native_decision", native_decision), ("policy_id", policy_id)):
            if value is not None:
                payload[key] = value
        self.record("user_authorization", payload, decision, action_id=action_id, event_id=event_id)

    def grant(self, tool: str, args: dict) -> None:
        """Legacy structured user grant; native invocation decisions use record_authorization."""
        self.record(tool, args, result="granted")

    def revoke(self, tool: str, args: dict) -> None:
        self.record(tool, args, result="revoked")

    def _ensure_checkpoint_worker(self):
        if not (self.acc.store and self.acc.chain and callable(getattr(self.acc.chain, "checkpoint_trace", None))):
            return
        with self._lock:
            if self._worker is None and not self._closing and self._summary is None:
                self._worker = threading.Thread(target=self._checkpoint_loop, daemon=True,
                                                name="trace-checkpoint")
                self._worker.start()

    def checkpoint_status(self) -> dict:
        """Expose the observed unanchored interval; outage time is not hidden by retries."""
        with self._lock:
            pending_times = [t for seq, t in self._recorded_at.items() if seq > self._anchored_count]
            since = min(pending_times) if pending_times else None
            return {"record_count": self._seq, "anchored_record_count": self._anchored_count,
                    "finalized": self._summary is not None,
                    "final_submission_pending": self._final_submission_started and self._summary is None,
                    "unanchored_records": max(0, self._seq - self._anchored_count),
                    "unanchored_since": since,
                    "unanchored_seconds": max(0, time.time() - since) if since is not None else 0,
                    "last_error": self._checkpoint_error, "in_flight": self._checkpoint_in_flight}

    def _checkpoint_loop(self):
        while not self._stop.is_set():
            status = self.checkpoint_status()
            dirty = status["unanchored_records"] > 0
            due = dirty and (status["unanchored_records"] >= self.acc.checkpoint_records
                             or status["unanchored_seconds"] >= self.acc.checkpoint_seconds)
            if due and time.monotonic() >= self._retry_after:
                self.checkpoint()
            status = self.checkpoint_status()
            # A dirty timer runs even when the host emits no further actions.
            until_due = max(0.01, self.acc.checkpoint_seconds - status["unanchored_seconds"])
            retry_wait = max(0, self._retry_after - time.monotonic())
            self._wake.wait(max(0.01, retry_wait or min(until_due, self.acc.checkpoint_seconds)))
            self._wake.clear()

    def checkpoint(self, force: bool = False) -> dict:
        """Anchor the newest acknowledged store prefix. Failure is observable and retryable.

        Serialized with finalization: at most one prefix transaction is in flight per session.
        Re-read historical checkpoints first to reconcile an uncertain prior submission.
        """
        if not (self.acc.store and self.acc.chain):
            return {"success": False, "submitted": False, **self.checkpoint_status()}
        submitted = False
        with self._commit_lock:
            with self._lock:
                if self._closing or self._summary is not None:
                    return {"success": False, "submitted": False, **self.checkpoint_status()}
                status = self.checkpoint_status()
                if not force and status["unanchored_records"] < self.acc.checkpoint_records and status["unanchored_seconds"] < self.acc.checkpoint_seconds:
                    return {"success": True, "submitted": False, **status}
                self._checkpoint_in_flight = True
            try:
                self._flush()
                with self._lock:
                    acknowledged = self._shipped_through
                records = [r for r in self.acc._shipped(self.session_id) if r.seq <= acknowledged]
                checkpoints = self.acc.chain.get_checkpoints(self.session_id)
                check_prefixes(records, checkpoints, self.session_id)
                if (len(records) != acknowledged or
                        any(r.seq != i or r.session_id != self.session_id for i, r in enumerate(records, 1))):
                    raise ValueError("checkpoint needs a contiguous single-session prefix")
                landed = checkpoints[-1]["record_count"] if checkpoints else 0
                if len(records) < landed:
                    raise ValueError("checkpoint data truncated")
                if len(records) > landed:
                    self.acc.chain.checkpoint_trace(self.acc.provider_addr, self.session_id,
                                                    len(records), trace_hash(records))
                    landed = len(records)
                    submitted = True
                with self._lock:
                    self._anchored_count = landed
                    self._checkpoint_error = None
                    self._retry_after = 0
                return {"success": True, "submitted": submitted, **self.checkpoint_status(), "in_flight": False}
            except Exception as e:
                with self._lock:
                    self._checkpoint_error = str(e)
                    self._retry_after = time.monotonic() + min(self.acc.checkpoint_seconds, 5)
                log.warning("session %s: checkpoint failed; execution continues: %s", self.session_id, e)
                return {"success": False, "submitted": submitted, **self.checkpoint_status(), "in_flight": False}
            finally:
                with self._lock:
                    self._checkpoint_in_flight = False

    def _freeze_final_submission(self):
        # From this point a failed receipt can hide a mined or pending final commitment.
        # Only end() reconciliation may proceed; extending the trace would change its hash.
        with self._lock:
            self._final_submission_started = True
        self._stop.set()
        self._wake.set()

    def end(self) -> dict:
        """Flush and finalize the store's records, preserving all prior anchored prefixes."""
        with self._commit_lock:
            with self._lock:
                if self._summary is not None:
                    return self._summary
                if self._active_executors:
                    raise RuntimeError("cannot finalize while an executor is running")
                self._closing = True
            try:
                self._flush()
                pending = self.unshipped_seqs()
                if pending:
                    raise StoreUnavailable(self.session_id, pending)
                summary = (self.acc._commit_shipped(self.session_id, buffer=self.records,
                                                    expected_count=self._seq, before_submit=self._freeze_final_submission)
                           if self.acc.store else self.acc._finalize(self.session_id, self.records,
                                                                    before_submit=self._freeze_final_submission))
                with self._lock:
                    self._summary = summary
                    if self.acc.chain:
                        self._anchored_count = self._seq
                    self._checkpoint_error = None
                self._stop.set()
                self._wake.set()
                return summary
            finally:
                with self._lock:
                    self._closing = self._final_submission_started and self._summary is None

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> bool:
        self.end()
        return False


class Accountability:
    """Top-level SDK handle. Tool-agnostic — it mediates abstract actions, not any agent."""

    def __init__(self, provider_id: str, store=None, chain=None, provider_addr: str = None, *,
                 checkpoint_records: int = 10, checkpoint_seconds: float = 30):
        if checkpoint_records < 1 or checkpoint_seconds <= 0:
            raise ValueError("checkpoint cadence must be positive")
        self.checkpoint_records = checkpoint_records
        self.checkpoint_seconds = checkpoint_seconds
        self.provider_id = provider_id
        self.store = store                 # an HttpStore, or None for in-memory self-check demos
        self.chain = chain                 # optional EscrowClient (aa_sdk.chain); None = off-chain mode
        self.provider_addr = provider_addr
        self.promises: list[Promise] = []
        self._n = 0

    def post_bond(self, amount_wei: int) -> None:
        """Deprecated no-op: promises are born funded — the reserve rides in register_promise
        (reserve_wei). Kept one cycle so the pre-lifecycle integrations keep running; migrate
        them to reserve_wei and delete this."""
        import warnings
        warnings.warn("post_bond is deprecated: pass reserve_wei to register_promise instead",
                      DeprecationWarning, stacklevel=2)

    def register_promise(self, predicate: str, params: dict, payout_wei: int,
                         reserve_wei: "int | None" = None):
        """Validate the params against the predicate's spec (a promise that would crash at
        adjudication is rejected here), then register born-funded: the initial reserve
        (default: one payout) rides in the registration transaction. On-chain only the hashes
        + payout travel; readable {predicate, params} goes to the store by promiseId."""
        validate_params(predicate, params)
        promise = Promise("pending", predicate, params, payout_wei)
        if self.chain:
            pid = self.chain.register_promise(
                self.provider_addr, promise.predicate_hash, promise.params_hash,
                payout_wei, reserve_wei if reserve_wei is not None else payout_wei
            )
            if self.store:
                self.store.put_promise(pid, {"predicate": predicate, "params": promise.params})
        else:
            self._n += 1
            pid = f"promise-{self._n}"
        promise.promise_id = pid
        self.promises.append(promise)
        return pid

    def _finalize(self, session_id: str, records: list, before_submit=None) -> dict:
        """Commit the canonical hash of ``records`` on chain (when chained) — the one commit
        path. With a store, both ``Session.end`` and ``recover`` hand it the store's records."""
        checkpoints = (self.chain.get_checkpoints(session_id)
                       if self.chain and callable(getattr(self.chain, "get_checkpoints", None)) else [])
        check_prefixes(records, checkpoints, session_id)
        h = trace_hash(records)
        if self.chain:
            if callable(getattr(self.chain, "get_session", None)):
                raw_hash = self.chain.get_session(session_id)[2]
                committed = raw_hash if isinstance(raw_hash, str) else "0x" + bytes(raw_hash).hex()
                if committed != "0x" + "00" * 32:
                    if committed != h:
                        raise ValueError("final commitment differs from the store's records")
                    return {"session_id": session_id, "trace_hash": h, "n_actions": len(records)}
            if before_submit is not None:
                before_submit()
            self.chain.commit_trace(self.provider_addr, session_id, h)
        return {"session_id": session_id, "trace_hash": h, "n_actions": len(records)}

    def _shipped(self, session_id: str) -> list:
        """The store's records for a session, as ActionRecords — the trace."""
        return sorted([ActionRecord.from_dict(d) for d in self.store.get_records(session_id)],
                      key=lambda r: r.seq)

    def _commit_shipped(self, session_id: str, buffer: "list | None" = None,
                        expected_count: int | None = None, before_submit=None) -> dict:
        """Read the shipped records back and commit THEIR hash. ``buffer`` is the in-process copy,
        compared only to warn: differing seqs mean something edited the buffer after shipping."""
        try:
            shipped = self._shipped(session_id)
        except Exception as e:
            raise StoreUnavailable(session_id, [], detail=repr(e)) from e
        if expected_count is not None and (
            len(shipped) != expected_count or any(r.seq != i or r.session_id != session_id
                                                  for i, r in enumerate(shipped, 1))
        ):
            raise ValueError("store records do not match the observed session boundary")
        if buffer is not None:
            differing = _differing_seqs(buffer, shipped)
            if differing:
                log.warning("session %s: in-process buffer differs from the store's records at seqs %s; "
                            "committing the store's copy", session_id, differing)
        return self._finalize(session_id, shipped, before_submit=before_submit)

    def recover(self, session_id: str, party: str) -> dict:
        """Finalize a session whose process died before ``end()``: read the records the SDK
        shipped while it ran back from the store and commit their hash, exactly as ``end()``
        would have (same read-back, same commit path). Without this, a crashed session settles
        as withheld even though its evidence survived in the store — the provider would pay for
        an infrastructure failure. ``party`` is the session's party as opened on chain; it is
        informational here (the chain fixed it at ``openSession``)."""
        if not self.store:
            raise RuntimeError("recover needs a store")
        records = self._shipped(session_id)
        if not records:
            raise RuntimeError(f"no shipped records for session {session_id}")
        return self._finalize(session_id, records)

    def session(self, party: str) -> Session:
        """Open a covered session. With a chain configured, ``openSession`` lands BEFORE any covered
        action runs — coverage means exactly "opened on chain", and the returned receipt is the
        party's proof (they can check the session is open and that ``party`` is their address at
        session start, when they can still walk away). If the open fails, the exception propagates:
        run uncovered only by explicit choice (chain=None), never by silent fallback."""
        # bytes32-shaped id: usable directly on-chain AND reversible to the store key
        session_id = "0x" + os.urandom(32).hex()
        receipt = None
        if self.chain:
            rcpt = self.chain.open_session(self.provider_addr, session_id, party)
            receipt = {"session_id": session_id, "open_tx": rcpt.transactionHash.hex(),
                       "escrow": self.chain.contract.address}
        return Session(self, session_id, party, receipt=receipt)

    def self_check(self, records) -> dict:
        """Run every registered promise's predicate over a trace — exactly what the verifier does."""
        results = {}
        for promise in self.promises:
            spec = registry.resolve_hash(promise.predicate_hash)
            if spec is None:
                raise ValueError(f"committed predicate is not installed: {promise.predicate_hash}")
            if params_hash(promise.params) != promise.params_hash:
                raise ValueError("promise no longer matches its committed parameters")
            results[promise.promise_id] = spec.evaluate(records, promise.params)
        return results


__all__ = ["Accountability", "Session", "Promise", "StoreUnavailable", "HttpStore"]
