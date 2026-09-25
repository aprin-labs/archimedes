"""Unit tests for archimedes.marketplace.config.payments_halted (#1240)."""

from __future__ import annotations

from archimedes.marketplace.config import payments_halted


def test_unset_is_not_halted(monkeypatch):
    monkeypatch.delenv("PAYMENTS_HALT", raising=False)
    assert payments_halted() is False


def test_truthy_values_halt(monkeypatch):
    for value in ("true", "TRUE", "True", "1", "yes", "YES"):
        monkeypatch.setenv("PAYMENTS_HALT", value)
        assert payments_halted() is True, f"{value!r} should be truthy"


def test_falsy_and_garbage_values_do_not_halt(monkeypatch):
    for value in ("false", "0", "no", "", "banana"):
        monkeypatch.setenv("PAYMENTS_HALT", value)
        assert payments_halted() is False, f"{value!r} should not halt"


def test_reads_fresh_every_call_not_cached(monkeypatch):
    """Pins the mechanism: payments_halted() calls os.getenv() directly on
    every invocation rather than snapshotting a module- or instance-level
    value once (the way payments_dry_run is snapshotted once into
    MarketService.__init__).

    Correction (post-review): per-call freshness is NOT what makes
    PAYMENTS_HALT a no-redeploy lever in production — nothing mutates
    os.environ mid-process (load_ssm_secrets() runs once, at import time),
    so within one running task a per-call os.getenv() and a cached
    self.payments_dry_run are equally stale until the process restarts;
    this test's three assertions only demonstrate that within THIS test
    process, via monkeypatch, which is not how a real deploy changes the
    value. The actual operational property is that PAYMENTS_HALT lives in
    SSM/env rather than being baked into the task definition, so flipping
    it needs only a same-image `aws ecs update-service
    --force-new-deployment` restart — not a new task-definition revision
    from the full build -> ECR -> roll Fargate pipeline. Kept anyway: it is
    a harmless, accurate unit pin of the read-mechanism itself."""
    monkeypatch.delenv("PAYMENTS_HALT", raising=False)
    assert payments_halted() is False
    monkeypatch.setenv("PAYMENTS_HALT", "true")
    assert payments_halted() is True
    monkeypatch.setenv("PAYMENTS_HALT", "false")
    assert payments_halted() is False
