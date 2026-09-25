"""Per-generation cost instrumentation — raw measurement only, never pricing.

Issue #1217: *"we do not know what a generation costs."* The only figure anyone
has quoted for a generation is a Bedrock inference estimate, which is the
language-model term alone — it excludes the debate society's per-proposal
backtests and the rigor gate, which are plausibly the dominant term. Until the
raw numbers exist, every pricing decision downstream rests on an assumption.

This module is the measurement layer, and *only* the measurement layer:

* **Token counts** come from the provider's own ``usage`` block, recorded at the
  :mod:`archimedes.services.llm_backend` boundary — the one place every provider
  response passes through.
* **Wall time and CPU time** are recorded per named stage, so the per-phase
  breakdown (corpus retrieval / debate / backtest / rigor gate / persistence) is
  visible rather than assumed.
* **Write counts** are the pipeline's own tally of the rows it issued.

Three properties are enforced in code rather than asserted in prose:

1. **A missing measurement is never a zero.** A provider response that carries no
   usable ``usage`` block increments ``calls_missing_usage`` and flips
   ``usage_complete`` to ``False``; it does not add ``0`` tokens and quietly look
   like a cheap call. Same for a *partial* block — a call whose input count is
   readable but whose output count is not is not a measurement, and neither half
   is banked. (``CLAUDE.md`` § fail-soft: the correct degraded state is a loud,
   visible absence, never a plausible substitute.)
2. **An implausible count is refused, not accumulated.** Negative, non-finite,
   non-integral, stringly-typed, or absurd (> :data:`MAX_PLAUSIBLE_TOKENS`)
   counts are treated as missing.
3. **No pricing math lives here.** Every caller-chosen label — stage name,
   write-counter name, meta key — is screened against :data:`_PRICING_TOKENS` at
   write time, and a match raises :class:`PricingLeakError`. That screening is
   what generalizes: the rest of a snapshot's keys are this module's own fixed
   literals (see :meth:`CostMeter.snapshot`), so a caller label is the only route
   by which a pricing-shaped key could enter one at all. The quote seam
   (``generation_payment.quote()``) stays ``flat_v1``; this module feeds the
   flat-to-measured evolution with numbers, and the conversion from numbers to
   dollars happens outside the server.

The meter is bound to a :mod:`contextvars` context for the duration of one job,
so the LLM boundary can record usage without threading a parameter through the
debate engine and the fusion proposer. ``asyncio.to_thread`` and
``asyncio.create_task`` both copy the current context, so worker threads and
child tasks see the same meter object; mutations are lock-guarded because the
society proposes and backtests in parallel threads.

**What the context binding does and does not reach.** A recorder is a no-op when
no meter is bound, which is the correct behaviour outside a job — but it also
means instrumenting a code path proves nothing on its own about whether that path
is measured. Only ``run_generation`` binds a meter today. The one previously
documented gap here — an inert :func:`record_llm_call` inside
:mod:`archimedes.agents.portfolio_agent`'s ``propose_portfolio_with_tools()``
tool loop, which had no callers and therefore recorded nothing — is closed:
that loop was **deleted** on 2026-08-31, so there is no longer a
zero-caller instrumented path masquerading as coverage. See
``docs/generation-cost-instrumentation.md`` for the full coverage boundary.
"""

from __future__ import annotations

import contextvars
import logging
import math
import resource
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped whenever the snapshot shape changes, so a stored snapshot is
#: self-describing to whatever reads it later.
SCHEMA = "cost_v1"

#: A single completion cannot plausibly report more tokens than this (the
#: largest production context windows are ~2M). Anything above it is a
#: corrupted/misread field, not a measurement.
MAX_PLAUSIBLE_TOKENS = 10_000_000

#: ``time.process_time()`` is process-wide, so a stage delta also captures CPU
#: burned by anything else running in the same worker. Recorded honestly under
#: this label rather than presented as isolated per-job CPU.
CPU_ATTRIBUTION = "process_wide_delta"

#: ``ru_maxrss`` is a process high-water mark that never falls, so this is
#: "the peak this worker has ever reached", not "the peak this job caused".
RSS_ATTRIBUTION = "process_high_water"

# Substrings that mark a label as pricing rather than measurement. Screened on
# every caller-chosen label (stage / write-counter / meta key). Deliberately a
# deny-list of whole meaningful fragments — "cents" not "cent" (percent,
# recent), "billing" not "bill" (billion).
_PRICING_TOKENS = (
    "usd",
    "dollar",
    "cents",
    "price",
    "pricing",
    "cost",
    "fee",
    "charge",
    "spend",
    "billing",
    "invoice",
    "revenue",
)

# Provider-specific names for the same two numbers. Anthropic SDK →
# ``input_tokens``/``output_tokens``; Bedrock Converse → ``inputTokens``/
# ``outputTokens``; OpenAI-compatible → ``prompt_tokens``/``completion_tokens``;
# Ollama → ``prompt_eval_count``/``eval_count`` (top level, no usage block).
_INPUT_TOKEN_KEYS = ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens", "prompt_eval_count")
_OUTPUT_TOKEN_KEYS = ("output_tokens", "outputTokens", "completion_tokens", "completionTokens", "eval_count")


class PricingLeakError(ValueError):
    """A label was supplied that would put pricing math in the measurement layer.

    Raised eagerly at the call site so the violation surfaces as a hard failure
    in the first test run that exercises it, never as a silently-priced field in
    a persisted job record.
    """


def _assert_no_pricing(label: str, kind: str) -> str:
    """Screen a caller-chosen label; return it unchanged when clean."""
    text = str(label)
    lowered = text.lower()
    for token in _PRICING_TOKENS:
        if token in lowered:
            raise PricingLeakError(
                f"{kind} name {text!r} contains {token!r}: the cost meter records raw counts only. "
                "Pricing math belongs outside the server (the quote seam stays flat_v1)."
            )
    return text


#: Dotted paths inside a snapshot whose IMMEDIATE keys are runtime **data**
#: rather than caller-authored labels, and are therefore exempt from
#: :func:`assert_measurement_only`'s key screen.
#:
#: Only ``llm.by_model`` qualifies today: its keys are provider model
#: identifiers copied verbatim off a response (``response.model``), never
#: something this codebase chose. Screening them would let a vendor's naming
#: decide whether a generation's measurement gets persisted at all — a model id
#: as ordinary as ``llama-3-feedback-tuned`` contains ``fee``, and one marketed
#: as cost- or price-optimized contains the words outright. The write would then
#: raise inside the pipeline's ``finally``, be swallowed, and drop the durable
#: row: the exact silent-loss failure this instrumentation exists to end.
#:
#: The exemption is one level deep. Values beneath an exempt key are still
#: walked, so ``llm.by_model["some-model"]["cost_usd"]`` still raises — the model
#: id is data, the counters hanging off it are ours.
DATA_KEYED_PATHS = frozenset({"llm.by_model"})


def assert_measurement_only(payload: Any, *, where: str = "snapshot") -> None:
    """Refuse a payload whose KEYS carry pricing vocabulary, at any depth.

    :func:`_assert_no_pricing` screens one caller-chosen label at write time,
    which is what keeps a live meter's snapshot clean. This is the same screen
    applied to a *whole assembled payload* — the check a persistence boundary
    needs, because by the time a snapshot reaches storage the labels that built
    it are long gone and the only thing left to inspect is the document.

    It exists for the durable ``generation_costs`` row (#1326): that row stores
    the measurement and the price quote in **two separate columns** precisely so
    the two never share a namespace, and this is the guard that makes "separate"
    enforced rather than merely intended. Merging the quote into the measurement
    — the obvious future shortcut — raises here instead of silently shipping a
    priced ``cost_v1`` record.

    Values are deliberately not screened: a *quote* is a legitimately priced
    document and stores fine in its own column, and a measurement's values are
    numbers, not labels. Keys are the namespace; keys are what is policed —
    except where the keys are themselves data, which is what
    :data:`DATA_KEYED_PATHS` records and why.

    Each key is screened on its own, with the dotted path carried only for the
    error message. Screening the concatenated path would be equivalent for
    ordinary keys (no deny-listed token contains a ``.``, and every ancestor
    segment is screened when it is visited), but it would leak an exempt key's
    text into its children's screens and re-raise on exactly the model ids the
    exemption exists to allow.

    Raises :class:`PricingLeakError` on the first offending key.
    """

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            keys_are_data = path in DATA_KEYED_PATHS
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if not keys_are_data:
                    _assert_no_pricing(str(key), f"{where} key at {child}")
                _walk(value, child)
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}]")

    _walk(payload, "")


def _peak_rss_bytes() -> int | None:
    """Process peak resident set size in bytes, or ``None`` if unavailable.

    ``ru_maxrss`` is bytes on macOS/Darwin and kilobytes on Linux — getrusage(2).
    Getting this unit wrong is a 1024× error in a number someone will size an
    ECS task from, so it is branched explicitly.
    """
    try:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:  # pragma: no cover — platform without getrusage
        return None
    return int(raw) if sys.platform == "darwin" else int(raw) * 1024


def _coerce_count(value: Any) -> int | None:
    """Return ``value`` as a plausible non-negative count, or ``None`` if it is not one.

    Used for token counts and write tallies alike. ``None`` means *not measured*
    — the caller must record that as missing, never as zero. ``bool`` is
    rejected explicitly (``True`` is an ``int`` in Python and would otherwise
    bank as one token).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        count = value
    elif isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        count = int(value)
    else:
        # Strings and everything else: a stringly-typed count is a parse we did
        # not do, so it is not a measurement we are entitled to report.
        return None
    if count < 0 or count > MAX_PLAUSIBLE_TOKENS:
        return None
    return count


def _get_field(container: Any, key: str) -> Any:
    if isinstance(container, Mapping):
        return container.get(key)
    return getattr(container, key, None)


def _usage_containers(response: Any) -> Iterator[Any]:
    """Yield the places a provider might have put its usage block."""
    usage = _get_field(response, "usage")
    if usage is not None:
        yield usage
    if response is not None:
        yield response


def _lookup(container: Any, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        count = _coerce_count(_get_field(container, key))
        if count is not None:
            return count
    return None


def extract_token_usage(response: Any) -> tuple[int | None, int | None]:
    """Pull ``(input_tokens, output_tokens)`` out of any provider response shape.

    Returns ``(None, None)`` when nothing usable is present. A half-readable
    block returns the readable half and ``None`` for the other; the recorder
    treats that as a missing measurement rather than banking half a call.
    """
    for container in _usage_containers(response):
        input_tokens = _lookup(container, _INPUT_TOKEN_KEYS)
        output_tokens = _lookup(container, _OUTPUT_TOKEN_KEYS)
        if input_tokens is not None or output_tokens is not None:
            return input_tokens, output_tokens
    return None, None


class CostMeter:
    """Accumulates the raw measurements for one generation job."""

    def __init__(self, job_id: str = "") -> None:
        self.job_id = job_id
        self._lock = threading.Lock()
        self._wall_start = time.monotonic()
        self._cpu_start = time.process_time()
        self._stages: dict[str, dict[str, Any]] = {}
        self._writes: dict[str, int] = {}
        self._meta: dict[str, Any] = {}
        self._calls = 0
        self._calls_missing_usage = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._by_model: dict[str, dict[str, int]] = {}

    # ── LLM usage ────────────────────────────────────────────────────────

    def record_usage(self, *, model: str, input_tokens: int | None, output_tokens: int | None) -> None:
        """Bank one LLM call's token counts.

        Both counts must be present and plausible for the call to be banked.
        Anything else increments ``calls_missing_usage`` and flips
        ``usage_complete`` — the call still happened and still cost money, and
        saying so is the point.
        """
        clean_in = _coerce_count(input_tokens)
        clean_out = _coerce_count(output_tokens)
        model_id = str(model or "unknown")
        with self._lock:
            self._calls += 1
            per_model = self._by_model.setdefault(
                model_id,
                {"calls": 0, "calls_missing_usage": 0, "input_tokens": 0, "output_tokens": 0},
            )
            per_model["calls"] += 1
            if clean_in is None or clean_out is None:
                self._calls_missing_usage += 1
                per_model["calls_missing_usage"] += 1
                return
            self._input_tokens += clean_in
            self._output_tokens += clean_out
            per_model["input_tokens"] += clean_in
            per_model["output_tokens"] += clean_out

    def record_response(self, *, model: str, response: Any) -> None:
        """Bank one LLM call from the raw provider response object/dict."""
        input_tokens, output_tokens = extract_token_usage(response)
        self.record_usage(model=model, input_tokens=input_tokens, output_tokens=output_tokens)

    # ── Stage timing ─────────────────────────────────────────────────────

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Accumulate wall + CPU seconds for a named phase.

        Re-entering the same name accumulates rather than overwriting, so a
        stage run once per candidate reports the total and its ``runs`` count.
        """
        clean = _assert_no_pricing(name, "stage")
        wall0 = time.monotonic()
        cpu0 = time.process_time()
        try:
            yield
        finally:
            wall = time.monotonic() - wall0
            cpu = time.process_time() - cpu0
            with self._lock:
                bucket = self._stages.setdefault(clean, {"wall_seconds": 0.0, "cpu_seconds": 0.0, "runs": 0})
                bucket["wall_seconds"] += wall
                bucket["cpu_seconds"] += cpu
                bucket["runs"] += 1

    # ── Write counts + metadata ──────────────────────────────────────────

    def record_write(self, table: str, count: int = 1) -> None:
        """Tally rows the pipeline wrote (upserts count as one write each)."""
        clean = _assert_no_pricing(table, "write-counter")
        n = _coerce_count(count)
        if n is None:
            logger.debug("cost meter: ignoring implausible write count %r for %s", count, clean)
            return
        with self._lock:
            self._writes[clean] = self._writes.get(clean, 0) + n

    def set_meta(self, key: str, value: Any) -> None:
        """Attach a descriptive scalar (pipeline name, outcome, candidate count).

        Meta exists so a stored snapshot can be interpreted later — in
        particular whether the run passed or failed the rigor gate, since the
        rejected path burns the same backtest compute and is the common case.
        """
        clean = _assert_no_pricing(key, "meta")
        with self._lock:
            self._meta[clean] = value

    # ── Readout ──────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """The raw measurement record. Contains counts and seconds; no money."""
        with self._lock:
            wall = time.monotonic() - self._wall_start
            cpu = time.process_time() - self._cpu_start
            return {
                "schema": SCHEMA,
                "job_id": self.job_id,
                "wall_seconds": round(wall, 4),
                "cpu_seconds": round(cpu, 4),
                "cpu_attribution": CPU_ATTRIBUTION,
                "peak_rss_bytes": _peak_rss_bytes(),
                "rss_attribution": RSS_ATTRIBUTION,
                "llm": {
                    "calls": self._calls,
                    "calls_missing_usage": self._calls_missing_usage,
                    "usage_complete": self._calls_missing_usage == 0,
                    "input_tokens": self._input_tokens,
                    "output_tokens": self._output_tokens,
                    "total_tokens": self._input_tokens + self._output_tokens,
                    "by_model": {k: dict(v) for k, v in self._by_model.items()},
                },
                "stages": {
                    name: {
                        "wall_seconds": round(float(bucket["wall_seconds"]), 4),
                        "cpu_seconds": round(float(bucket["cpu_seconds"]), 4),
                        "runs": int(bucket["runs"]),
                    }
                    for name, bucket in self._stages.items()
                },
                "writes": dict(self._writes),
                "meta": dict(self._meta),
            }


# ── Context binding ──────────────────────────────────────────────────────

_CURRENT: contextvars.ContextVar[CostMeter | None] = contextvars.ContextVar("archimedes_cost_meter", default=None)


def current_meter() -> CostMeter | None:
    """The meter bound to this context, or ``None`` outside a measured job."""
    return _CURRENT.get()


def bind(meter: CostMeter) -> contextvars.Token:
    """Bind ``meter`` to the current context; pass the token back to :func:`unbind`."""
    return _CURRENT.set(meter)


def unbind(token: contextvars.Token) -> None:
    try:
        _CURRENT.reset(token)
    except ValueError:  # pragma: no cover — token from a different context
        logger.debug("cost meter: token reset out of context", exc_info=True)


@contextmanager
def measure(job_id: str = "") -> Iterator[CostMeter]:
    """Bind a fresh meter for the duration of the block."""
    meter = CostMeter(job_id=job_id)
    token = bind(meter)
    try:
        yield meter
    finally:
        unbind(token)


# ── Module-level recorders (no-ops outside a measured job) ───────────────


def record_llm_call(*, model: str, response: Any) -> None:
    """Record one LLM call from the provider boundary. Never raises.

    Called from inside every backend's ``complete()``. Instrumentation must not
    be able to fail a real generation, so unexpected errors are logged and
    swallowed — the honest consequence of a swallow here is a snapshot with
    fewer calls in it, which the reader can see.
    """
    meter = _CURRENT.get()
    if meter is None:
        return
    try:
        meter.record_response(model=model, response=response)
    except Exception:  # pragma: no cover — defensive
        logger.debug("cost meter: failed to record LLM usage", exc_info=True)


def record_write(table: str, count: int = 1) -> None:
    """Tally a persisted write. :class:`PricingLeakError` propagates by design."""
    meter = _CURRENT.get()
    if meter is None:
        _assert_no_pricing(table, "write-counter")
        return
    meter.record_write(table, count)


def set_meta(key: str, value: Any) -> None:
    """Attach descriptive metadata. :class:`PricingLeakError` propagates by design."""
    meter = _CURRENT.get()
    if meter is None:
        _assert_no_pricing(key, "meta")
        return
    meter.set_meta(key, value)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Time a named phase against the bound meter (no-op when unbound)."""
    meter = _CURRENT.get()
    if meter is None:
        _assert_no_pricing(name, "stage")
        yield
        return
    with meter.stage(name):
        yield
