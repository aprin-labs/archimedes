"""Readiness: a task that can no longer read its database must say so (#1818 P3).

INCIDENT 2026-09-03. Two paper-advance children ran ``init_db()``'s DDL patches
concurrently. One child's ``AccessExclusiveLock`` request queued behind its
sibling's open cycle transaction, and every later reader of ``papers`` /
``strategy_store`` queued behind that. From 03:32Z to 13:28Z ``/health``
answered **200 in ~1.05s** on both tasks — correctly, by its own #1592/#1594
contract: it reports what it knows, and what it knew was a set of
``stale_cached`` readings, each honestly labelled, whose ``cached_age`` reached
**35,797 s**. ECS and the ALB stayed green over a fleet that could not read its
own database; the wedge broke only when one task was OOM-killed, 11.5 h in.

Liveness was TRUE the whole time (the loop was turning, the endpoint answered).
Readiness was false and was represented nowhere. These are the guards for the
representation:

1. ``/health/ready`` answers **503** once the DB-backed probes have been
   ``stale_cached`` for longer than ``HEALTH_STALE_UNREADY_S`` (default 900s),
   so the CONTAINER health check fails and ECS replaces that one task.
2. ``/health`` is **unchanged**: still 200, still carrying the labelled stale
   state, now also publishing the same verdict as data. The ALB target-group
   check acts on every target at once and must stay unable to route a shared
   transient into a fleet-wide outage.
3. Stale but *within* the threshold, a never-completed probe (cold start), and
   a raising probe are all **ready** — the rule fires on a task that used to be
   able to read and stopped, never on one that has not read yet. The
   ``probe_error`` case is pinned on the dangerous shape specifically: a probe
   whose memo is ALREADY ten hours stale and which now raises.
4. The readiness fields cannot break ``/health``. They are reporting, and a
   raising verdict must leave the ALB's check answering 200 with ``ready:
   null`` — never a 500 (which drains every target at once) and never an
   optimistic ``ready: true``.

**These are guards, not coverage.** Each was demonstrated to REJECT: with
``main._stale_unready_probes`` stubbed to ``return []`` the 503 cases below go
red, and with it stubbed to return every probe the 200 cases go red. Verbatim
output in the PR body. A guard that has never been shown to fail is a guess.

Hermetic: no network, no DB, no Redis. Every probe boundary is patched, and the
staleness is injected by seeding ``HealthProbeCache`` (the conftest autouse
fixture clears it either side) rather than by waiting 900 real seconds.

Run: env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \
       backend/tests/test_health_readiness_stale_1818.py -q
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from archimedes.chain.client import chain_client
from archimedes.services import oracle_health as oracle_health_mod
from archimedes.services.health_cache import health_probe_cache
from archimedes.services.oracle_health import OracleHealth
from httpx import ASGITransport, AsyncClient

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A hard stop so a regression FAILS the suite instead of hanging it.
_HARD_STOP_SECONDS = 8.0

# Longer than any probe budget, so a stalled read is genuinely abandoned rather
# than merely slow. Released on context exit so the worker threads unwind.
_INJECTED_STALL_SECONDS = 30.0

# The default the code ships (main._DEFAULT_STALE_UNREADY_SECONDS). Restated
# here rather than imported: a test that reads the constant it is checking
# cannot notice the constant changing.
_DEFAULT_THRESHOLD_S = 900.0

# The five probes the readiness rule watches, with the module boundary each one
# is read through — the fault-injection point is always that boundary, never an
# internal (same rule as test_health_always_answers.py's _LOCAL_READS).
_READINESS_READS = {
    "corpus": "archimedes.services.corpus_service.count_corpus_papers",
    "corpus_db": "archimedes.services.corpus_service.get_paper_count",
    "corpus_meta": "archimedes.services.corpus_service.get_corpus_meta",
    "paper_rag": "archimedes.services.paper_rag.paper_rag_health",
    "risk_data": "archimedes.api.risk_routes.risk_data_health",
}


def _seed_stale(names, age_s: float) -> None:
    """Give ``names`` a last-known reading measured ``age_s`` seconds ago.

    Writes straight into the memo because the alternative is waiting out the
    real clock: the incident's ages ran to 35,797 s. The VALUES are deliberately
    plausible-but-irrelevant — the readiness rule reads state and age, never the
    reading itself, and a test that pinned the values would be asserting the
    wrong thing.
    """
    measured_at = time.time() - age_s
    for name in names:
        health_probe_cache._entries[name] = (0 if name.startswith("corpus") else None, measured_at)


@contextmanager
def _stalling(*names: str, seconds: float = _INJECTED_STALL_SECONDS):
    """Block each named probe's read in its CALLING THREAD for ``seconds``.

    ``threading.Event.wait``, not ``asyncio.sleep``: the failure mode under test
    is a read that BLOCKS — a Postgres lock queue — and an awaitable stand-in
    would leave the loop free and prove nothing. The event is set on exit so the
    abandoned worker threads unwind instead of parking the interpreter.
    """
    release = threading.Event()

    def _stall(*_args, **_kwargs):
        release.wait(seconds)

    # The probes NOT named still have to answer, or a single-probe test would
    # be indistinguishable from an all-probe one.
    def _quick(*_args, **_kwargs):
        return 0

    stalled = [_READINESS_READS[name] for name in names]
    healthy = [target for target in _READINESS_READS.values() if target not in stalled]
    with _patch_all(stalled, _stall), _patch_all(healthy, _quick):
        try:
            yield
        finally:
            release.set()


@contextmanager
def _patch_all(targets, replacement):
    if not targets:
        yield
        return
    with patch(targets[0], replacement), _patch_all(targets[1:], replacement):
        yield


@contextmanager
def _fast_reads():
    """Every readiness probe answers instantly — the healthy-task baseline."""

    def _quick(*_args, **_kwargs):
        return 0

    with _patch_all(list(_READINESS_READS.values()), _quick):
        yield


async def _get(path: str) -> tuple[int, dict]:
    """GET ``path``, returning ``(status_code, payload)``.

    ASGITransport rather than TestClient: entering TestClient's context manager
    runs the app's startup lifespan, which seeds the corpus and warms loader
    caches for every test that runs afterwards in the same process. Precedent:
    test_health_always_answers.py.
    """
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await asyncio.wait_for(client.get(path), timeout=_HARD_STOP_SECONDS)
    return response.status_code, response.json()


class TestStaleBeyondThresholdIsUnready:
    """The incident's own shape: cached readings, honestly labelled, hours old."""

    async def test_all_db_probes_stale_for_ten_hours_reports_503(self):
        """MUTATION: `_stale_unready_probes` -> `return []`. Then this returns 200
        and the ten-hour wedge is invisible again — which is the outage."""
        _seed_stale(_READINESS_READS, age_s=35797.0)  # the incident's own cached_age
        with _stalling(*_READINESS_READS):
            status, body = await _get("/health/ready")

        assert status == 503, f"a task {35797 / 3600:.1f}h into a DB wedge answered {status}"
        assert body["ready"] is False
        assert body["stale_unready_threshold_s"] == _DEFAULT_THRESHOLD_S
        stale = {entry["probe"] for entry in body["stale_probes"]}
        assert stale == set(_READINESS_READS), stale
        # The 503 has to say WHICH read went dark, or the operator is back to
        # reading Aurora logs to find out what the probe already knew.
        for entry in body["stale_probes"]:
            assert entry["age_s"] >= 35797.0
        for name in _READINESS_READS:
            assert body["probes"][name]["state"] == "stale_cached"

    async def test_one_stale_db_probe_is_enough(self):
        """ANY, not ALL. paper_rag is the one member that can keep answering with
        Postgres gone (it reads process state), so unanimity would let a wedged
        database hide behind it.

        MUTATION: `_stale_unready_probes` -> `return []` ⇒ 200, red."""
        _seed_stale(["corpus_db"], age_s=1200.0)
        with _stalling("corpus_db"):
            status, body = await _get("/health/ready")

        assert status == 503
        assert [entry["probe"] for entry in body["stale_probes"]] == ["corpus_db"]
        assert body["probes"]["paper_rag"]["state"] == "live"


class TestStaleWithinThresholdStaysReady:
    """A blip is not a wedge. 900s = 30 consecutive misses of the ALB's poll."""

    async def test_sixty_seconds_stale_answers_200_with_the_labels_intact(self):
        """MUTATION: `_stale_unready_probes` -> return every probe ⇒ 503, red."""
        _seed_stale(_READINESS_READS, age_s=60.0)
        with _stalling(*_READINESS_READS):
            status, body = await _get("/health/ready")

        assert status == 200, "60s of stale probes must not replace a task"
        assert body["ready"] is True
        assert body["stale_probes"] == []
        # 200 is not silence: the staleness is still reported, probe by probe,
        # exactly as /health reports it. "Ready" here means "not wedged", never
        # "everything is fresh".
        for name in _READINESS_READS:
            entry = body["probes"][name]
            assert entry["state"] == "stale_cached", name
            assert 60.0 <= entry["age_s"] < _DEFAULT_THRESHOLD_S, name
            assert "probe_timeout" in entry["reason"], name

    async def test_a_never_completed_probe_is_not_unready(self):
        """Cold start. The memo is process-local and empty at boot, so a fresh
        task's probes report ``probe_timeout`` with nothing cached. Replacing a
        task for not yet having read once turns a slow boot into a replacement
        loop — that case belongs to the container check's startPeriod.

        MUTATION: treat probe_timeout as unready ⇒ 503, red."""
        with _stalling(*_READINESS_READS):
            status, body = await _get("/health/ready")

        assert status == 200
        assert body["ready"] is True
        for name in _READINESS_READS:
            assert body["probes"][name]["state"] == "probe_timeout"
            assert body["probes"][name]["age_s"] is None  # nothing cached ⇒ no age

    async def test_a_raising_probe_is_not_unready_even_when_its_cache_is_hours_old(self):
        """A broken PROBE must not be able to take the fleet down.

        ``probe_error`` is a defect in the probe, not a verdict about the
        database (services/health_cache.py draws the same line). The dangerous
        shape is this one: a probe whose memo is already hours stale AND which
        now raises — a naive rule would count the stale age and start replacing
        tasks because someone shipped a bad probe.

        The stale age here (10h) is deliberately past the threshold, so this
        test cannot pass by the age being small; it passes only because the
        state is ``probe_error``.

        MUTATION: in ``_stale_unready_probes``, make the ``BaseException`` branch
        append instead of skip ⇒ 503, red."""

        def _raises(*_args, **_kwargs):
            raise RuntimeError("probe blew up")

        _seed_stale(["corpus_db"], age_s=35797.0)
        stalled = [_READINESS_READS["corpus_db"]]
        healthy = [target for target in _READINESS_READS.values() if target not in stalled]

        def _quick(*_args, **_kwargs):
            return 0

        with _patch_all(stalled, _raises), _patch_all(healthy, _quick):
            status, body = await _get("/health/ready")

        assert status == 200, "a raising probe must not replace a task that can still read"
        assert body["ready"] is True
        assert body["stale_probes"] == []
        # The break is REPORTED, not swallowed — 200 here means "not wedged",
        # and the operator still sees which probe is broken.
        entry = body["probes"]["corpus_db"]
        assert entry["state"] == "probe_error", entry
        assert entry["age_s"] is None, entry
        assert "probe blew up" in entry["reason"], entry


class TestFreshProbesAreReady:
    async def test_a_healthy_task_answers_200_and_live(self):
        """MUTATION: `_stale_unready_probes` -> return every probe ⇒ 503 on a
        healthy task, red — and in production, a permanent replacement loop."""
        with _fast_reads():
            status, body = await _get("/health/ready")

        assert status == 200
        assert body["ready"] is True
        assert body["stale_probes"] == []
        for name in _READINESS_READS:
            entry = body["probes"][name]
            assert entry["state"] == "live", name
            assert entry["age_s"] == 0.0, name


class TestTheThresholdIsOperable:
    """The knob and its off switch, because the cost of getting 900s wrong is a
    task replaced once per threshold interval, forever."""

    async def test_env_threshold_is_honoured(self, monkeypatch):
        monkeypatch.setenv("HEALTH_STALE_UNREADY_S", "30")
        _seed_stale(_READINESS_READS, age_s=60.0)
        with _stalling(*_READINESS_READS):
            status, body = await _get("/health/ready")

        assert status == 503, "60s stale under a 30s threshold must be unready"
        assert body["stale_unready_threshold_s"] == 30.0

    async def test_zero_disables_the_rule(self, monkeypatch):
        """The pull-back an operator can reach without a deploy."""
        monkeypatch.setenv("HEALTH_STALE_UNREADY_S", "0")
        _seed_stale(_READINESS_READS, age_s=35797.0)
        with _stalling(*_READINESS_READS):
            status, body = await _get("/health/ready")

        assert status == 200
        assert body["ready"] is True
        # Publishing the 0 is what stops this 200 from being read as a verdict.
        assert body["stale_unready_threshold_s"] == 0.0

    async def test_an_unparseable_threshold_falls_back_to_the_default(self, monkeypatch):
        """A typo must not silently switch a safety rule off."""
        monkeypatch.setenv("HEALTH_STALE_UNREADY_S", "fifteen minutes")
        _seed_stale(_READINESS_READS, age_s=35797.0)
        with _stalling(*_READINESS_READS):
            status, body = await _get("/health/ready")

        assert status == 503
        assert body["stale_unready_threshold_s"] == _DEFAULT_THRESHOLD_S


class TestHealthItselfIsUnchanged:
    """The ALB polls /health. Its contract does not move (#1592's N2 argument):
    one shared transient must not be able to pull every target at once."""

    async def test_health_stays_200_and_publishes_the_same_verdict(self):
        """MUTATION: give /health the 503 instead of adding a second endpoint ⇒
        this goes red, and in production the ALB drains the whole service on the
        first shared DB blip."""
        _seed_stale(_READINESS_READS, age_s=35797.0)
        with (
            _stalling(*_READINESS_READS),
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            patch("archimedes.services.llm_backend.make_llm_backend", _fake_llm_backend),
        ):
            status, body = await _get("/health")

        assert status == 200, "/health is the ALB's check — it answers 200 or the fleet drains"
        # The human-readable body keeps the labelled stale state...
        for name in _READINESS_READS:
            assert body[f"{name}_probe_state"] == "stale_cached", name
            assert body[f"{name}_probe_age_s"] >= 35797.0, name
        # ...and now also carries the verdict the container check is acting on,
        # so the two endpoints can never be read as disagreeing.
        assert body["ready"] is False
        assert {entry["probe"] for entry in body["stale_unready_probes"]} == set(_READINESS_READS)
        assert body["stale_unready_threshold_s"] == _DEFAULT_THRESHOLD_S

    async def test_a_raising_readiness_verdict_cannot_turn_health_into_a_500(self):
        """The readiness fields are REPORTING. They must not be able to outrank
        the endpoint reporting them.

        /health is the ALB target-group check with ``matcher = "200"``, so an
        exception escaping the readiness block would answer 500 on every target
        at once and drain the service — the fleet-wide failure this PR avoided
        by putting the 503 on /health/ready instead. And the fallback must not
        be optimistic: ``ready`` goes null, never true, because "we could not
        compute the verdict" is not "the task is ready".

        MUTATION: drop the try/except around the readiness block in the /health
        handler ⇒ 500, red."""

        def _explodes(*_args, **_kwargs):
            raise RuntimeError("readiness verdict blew up")

        _seed_stale(_READINESS_READS, age_s=35797.0)
        with (
            _stalling(*_READINESS_READS),
            patch.object(chain_client, "is_connected", _returns_connected),
            patch.object(oracle_health_mod, "oracle_health", _fast_oracle),
            patch("archimedes.services.llm_backend.make_llm_backend", _fake_llm_backend),
            patch("archimedes.main._stale_unready_probes", _explodes),
        ):
            status, body = await _get("/health")

        assert status == 200, "/health is the ALB's check — it answers 200 or the fleet drains"
        assert body["ready"] is None, "an uncomputable verdict must not publish `ready: true`"
        assert body["stale_unready_threshold_s"] is None
        assert body["stale_unready_probes"] == []
        # Everything else /health reports still came through — the failure is
        # confined to the three readiness fields.
        assert body["service"] == "archimedes-backend"
        for name in _READINESS_READS:
            assert body[f"{name}_probe_state"] == "stale_cached", name
            assert body[f"{name}_probe_age_s"] >= 35797.0, name


class TestTheProbeIsActuallyWiredToTheContainerCheck:
    """A readiness endpoint nothing polls is a JSON field, not a fix. These pin
    the wiring in both places it has to exist, and pin that the ALB's did not
    move with it."""

    def test_dockerfile_healthcheck_targets_the_readiness_endpoint(self):
        dockerfile = (_REPO_ROOT / "backend" / "Dockerfile").read_text()
        assert "urlopen('http://localhost:8000/health/ready')" in dockerfile
        assert "urlopen('http://localhost:8000/health')" not in dockerfile

    def test_ecs_task_definition_mirrors_it(self):
        """An image-only HEALTHCHECK is invisible to the ECS agent for container
        dependencies, so the task definition has to carry the same command."""
        ecs = (_REPO_ROOT / "infra" / "ecs.tf").read_text()
        assert "urlopen('http://localhost:8000/health/ready')" in ecs
        assert "urlopen('http://localhost:8000/health')\\\" || exit 1" not in ecs

    def test_the_alb_target_group_still_checks_plain_health_for_200(self):
        """Readiness belongs to the container check ONLY. The ALB check acts on
        every target at once; moving it here is how a P3 fix becomes a P0."""
        alb = (_REPO_ROOT / "infra" / "alb.tf").read_text()
        block = re.search(
            r'resource "aws_lb_target_group" "backend".*?\n}\n',
            alb,
            re.DOTALL,
        )
        assert block, "backend target group not found in infra/alb.tf"
        health_check = re.search(r"health_check \{.*?\n  \}", block.group(0), re.DOTALL)
        assert health_check, "backend target group has no health_check block"
        assert re.search(r'path\s+=\s+"/health"', health_check.group(0))
        assert "/health/ready" not in health_check.group(0)
        assert re.search(r'matcher\s+=\s+"200"', health_check.group(0))


async def _returns_connected() -> bool:
    return True


async def _fast_oracle(*_args, **_kwargs) -> OracleHealth:
    return OracleHealth(
        status="fresh",
        oracle_fresh=True,
        oracle_oldest_age_s=45,
        oracle_probed_count=2,
        oracle_universe_count=281,
        reason="2/2 probed oracle(s) fresh (of 281 in the universe)",
    )


def _fake_llm_backend(*_args, **_kwargs):
    class _Backend:
        available = True
        model_id = "test-model"
        unavailable_reason = ""

    return _Backend()
