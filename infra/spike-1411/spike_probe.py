"""Measurement harness for the Lambda generation-offload spike (issue #1411).

Throwaway by design — it exists to produce the numbers in
``docs/adr/lambda-generation-offload.md`` and is not imported by anything the
product runs. It ships only in the spike image, never in the backend package.

Every action is **read-only against production data** except ``generate``, which
runs the real pipeline and is therefore only ever invoked with ``DATABASE_URL``
pointed at a throwaway SQLite file in ``/tmp`` (see the ADR § "What the spike
deliberately did not do"): the point of the spike is to measure the compute, and
a measurement run has no business creating strategy rows in the production
library.

Actions
-------
``noop``     — return immediately. Isolates Lambda's own ``Init Duration``
               (image start + interpreter start) from anything this code does.
``imports``  — time the backend package's import graph in groups, with RSS after
               each, because "cold start" for this workload is mostly imports.
``deps``     — prove or disprove each production dependency from inside the
               Lambda's VPC ENI: Redis, Aurora, Bedrock, the MiniLM reranker.
               Each is timed and each failure is reported verbatim, since a
               precise blocker is the deliverable when a full run is infeasible.
``generate`` — the real thing: delegate to the committed entrypoint
               ``archimedes.scripts.run_generation_job.handler``.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
import traceback
from typing import Any


def _rss_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes (Lambda is Linux); Darwin reports bytes.
    return round((raw if sys.platform == "darwin" else raw * 1024) / 1e6, 1)


def _timed(label: str, fn) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        value = fn()
        return {
            "step": label,
            "ok": True,
            "seconds": round(time.perf_counter() - started, 3),
            "rss_mb": _rss_mb(),
            "detail": value,
        }
    except Exception as exc:
        # Exception, not BaseException: a probe step's failure is data, but
        # KeyboardInterrupt/SystemExit must still tear the probe down.
        return {
            "step": label,
            "ok": False,
            "seconds": round(time.perf_counter() - started, 3),
            "rss_mb": _rss_mb(),
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc().splitlines()[-6:],
        }


# ── actions ──────────────────────────────────────────────────────────────────


def _action_noop(_event: dict) -> dict:
    return {"action": "noop", "python": sys.version.split()[0], "rss_mb": _rss_mb()}


def _import_groups() -> list[tuple[str, str]]:
    return [
        ("bootstrap_env", ""),  # special-cased below
        ("job_queue", "archimedes.services.job_queue"),
        ("db", "archimedes.db"),
        ("generation_pipeline", "archimedes.agents.generation_pipeline"),
        ("debate_engine", "archimedes.agents.debate_engine"),
        ("fusion_evaluator", "archimedes.services.fusion_evaluator"),  # pulls backtrader
        ("paper_rag", "archimedes.services.paper_rag"),
        ("sentence_transformers", "sentence_transformers"),
        ("entrypoint", "archimedes.scripts.run_generation_job"),
    ]


def _action_imports(_event: dict) -> dict:
    import importlib

    steps = []
    for label, module in _import_groups():
        if label == "bootstrap_env":
            from archimedes.scripts.run_generation_job import bootstrap_environment

            steps.append(_timed(label, lambda: {"ssm_parameters_loaded": bootstrap_environment()}))
            continue
        cached = module in sys.modules
        steps.append(
            _timed(
                label,
                lambda m=module, c=cached: {"cached": c, "module": importlib.import_module(m).__name__},
            )
        )
    return {
        "action": "imports",
        "steps": steps,
        "total_seconds": round(sum(s["seconds"] for s in steps), 3),
        "modules_loaded": len(sys.modules),
        "rss_mb": _rss_mb(),
    }


def _ssm_value(name: str) -> str:
    import boto3

    client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    prefix = os.environ.get("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")
    return client.get_parameter(Name=f"{prefix}{name}", WithDecryption=True)["Parameter"]["Value"]


def _probe_redis() -> dict:
    """PING + one read. No key is written: production's event log is not ours."""
    import redis

    url = os.environ.get("REDIS_URL") or _ssm_value("REDIS_URL")
    client = redis.from_url(url, socket_connect_timeout=8, socket_timeout=8, decode_responses=True)
    t0 = time.perf_counter()
    client.ping()
    ping_ms = round((time.perf_counter() - t0) * 1000, 2)
    scheme = url.split("://", 1)[0]
    return {"ping_ms": ping_ms, "scheme": scheme, "existing_job_keys": len(client.keys("archimedes:job:*")[:50])}


def _probe_aurora() -> dict:
    """SELECT-only: connectivity, server version, and the corpus row count."""
    from sqlalchemy import create_engine, text

    url = os.environ.get("DATABASE_URL_PROBE") or _ssm_value("DATABASE_URL")
    engine = create_engine(url, connect_args={"connect_timeout": 8}, pool_pre_ping=False)
    with engine.connect() as conn:
        t0 = time.perf_counter()
        conn.execute(text("SELECT 1"))
        rtt_ms = round((time.perf_counter() - t0) * 1000, 2)
        version = conn.execute(text("SHOW server_version")).scalar()
        papers = conn.execute(text("SELECT count(*) FROM papers")).scalar()
    engine.dispose()
    return {"select1_ms": rtt_ms, "server_version": version, "papers": papers}


def _probe_bedrock() -> dict:
    """One real inference through the production LLM boundary, metered.

    Going through ``make_llm_backend`` rather than a raw boto3 call is the
    point: it proves the *configured* provider/model/IAM combination the
    pipeline would use, and it exercises the ``cost_meter`` recording hook, so
    the token counts here are produced by the same instrumentation that would
    price a real generation.
    """
    from archimedes.services import cost_meter
    from archimedes.services.llm_backend import make_llm_backend

    backend = make_llm_backend()
    with cost_meter.measure(job_id="spike-1411-probe") as meter:
        t0 = time.perf_counter()
        text = backend.complete(
            "You are a terse assistant.",
            "Reply with exactly the word: ok",
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    snapshot = meter.snapshot()
    return {
        "provider": os.environ.get("LLM_PROVIDER"),
        "model": os.environ.get("LLM_BEDROCK_MODEL"),
        "latency_ms": latency_ms,
        "reply": (text or "")[:40],
        "llm": snapshot["llm"],
    }


def _probe_minilm() -> dict:
    """Load the baked reranker and run its own health probe (encode included)."""
    from archimedes.services.paper_rag import paper_rag_health

    health = paper_rag_health(probe=True)
    return {k: v for k, v in vars(health).items() if not k.startswith("_")}


def _action_deps(_event: dict) -> dict:
    from archimedes.scripts.run_generation_job import bootstrap_environment

    bootstrap_environment()
    return {
        "action": "deps",
        "steps": [
            _timed("redis", _probe_redis),
            _timed("aurora", _probe_aurora),
            _timed("bedrock", _probe_bedrock),
            _timed("minilm", _probe_minilm),
        ],
        "rss_mb": _rss_mb(),
    }


def _prepare_write_isolated_db(paper_limit: int) -> dict:
    """Build the throwaway SQLite DB the ``generate`` action writes into.

    The spike must measure the real pipeline without leaving a spike-authored
    strategy in the production library, so ``DATABASE_URL`` is pointed at
    ``/tmp`` and the schema is created from the same models production uses.

    Corpus retrieval, however, is a *read* the pipeline genuinely depends on —
    an empty ``papers`` table would abort the run long before the debate and
    backtest stages this spike exists to time. So the papers are COPIED out of
    Aurora (SELECT only) into the throwaway DB through the same ORM model, which
    keeps the retrieval stage representative while every write stays local.
    """
    from archimedes.db import DATABASE_URL, init_db
    from archimedes.models.corpus_store import PaperRecord
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    if not DATABASE_URL.startswith("sqlite"):
        raise RuntimeError(
            f"refusing to run the generate probe against {DATABASE_URL.split('://', 1)[0]}: "
            "set DATABASE_URL to a sqlite:////tmp/... path so the run cannot write to production"
        )
    init_db()

    source_url = os.environ.get("DATABASE_URL_PROBE") or _ssm_value("DATABASE_URL")
    source_engine = create_engine(source_url, connect_args={"connect_timeout": 8})
    source_session = sessionmaker(bind=source_engine)()
    try:
        rows = source_session.query(PaperRecord).limit(paper_limit).all()
        columns = [c.name for c in PaperRecord.__table__.columns]
        payload = [{c: getattr(row, c) for c in columns} for row in rows]
    finally:
        source_session.close()
        source_engine.dispose()

    from archimedes.db import get_session

    with get_session() as session:
        session.bulk_insert_mappings(PaperRecord, payload)
        session.commit()
    return {"papers_copied": len(payload), "sqlite_url": DATABASE_URL}


def _read_cost_rows(job_id: str) -> list[dict]:
    """The durable ``cost_v1`` measurement(s) the run just wrote (#1217/#1326).

    This is the number the cost model consumes, read back from the row the
    pipeline itself persisted rather than recomputed here — a spike that quotes
    its own arithmetic instead of the product's instrumentation would be
    measuring the wrong thing.
    """
    from archimedes.db import get_session
    from archimedes.models.generation_cost import GenerationCostRecord

    with get_session() as session:
        rows = session.query(GenerationCostRecord).filter(GenerationCostRecord.job_id == job_id).all()
        return [
            {
                "strategy_id": row.strategy_id,
                "schema_version": row.schema_version,
                "measurement": json.loads(row.measurement_json),
                "quote": json.loads(row.quote_json) if row.quote_json else None,
            }
            for row in rows
        ]


def _action_generate(event: dict) -> dict:
    from archimedes.scripts.run_generation_job import bootstrap_environment

    bootstrap_environment()
    prep = _timed("prepare_db", lambda: _prepare_write_isolated_db(int(event.get("paper_limit") or 2000)))

    from archimedes.scripts.run_generation_job import handler as run_handler

    started = time.perf_counter()
    outcome = _timed("run_generation", lambda: run_handler(event.get("job") or event))
    return {
        "action": "generate",
        "prepare": prep,
        "run": outcome,
        "cost_rows": _timed("cost_meter_snapshot", lambda: _read_cost_rows(str(event.get("job_id") or ""))),
        "seconds": round(time.perf_counter() - started, 3),
        "rss_mb": _rss_mb(),
        "database_url_scheme": (os.environ.get("DATABASE_URL", "")).split("://", 1)[0] or "unset",
    }


_ACTIONS = {
    "noop": _action_noop,
    "imports": _action_imports,
    "deps": _action_deps,
    "generate": _action_generate,
}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    action = str((event or {}).get("action") or "noop")
    if action not in _ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {sorted(_ACTIONS)}")
    started = time.perf_counter()
    payload = _ACTIONS[action](event or {})
    payload["handler_seconds"] = round(time.perf_counter() - started, 3)
    if context is not None and getattr(context, "process_start_epoch", None):
        payload["process_age_s"] = round(time.time() - context.process_start_epoch, 3)
    return payload


if __name__ == "__main__":
    print(json.dumps(handler(json.loads(sys.argv[1] if len(sys.argv) > 1 else "{}")), indent=2, default=str))
