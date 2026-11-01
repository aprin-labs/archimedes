"""The #1411 spike probe builds its own Aurora engine from a raw URL.

``infra/spike-1411/spike_probe.py`` reads ``DATABASE_URL_PROBE`` (or the SSM
``DATABASE_URL``), which is a bare ``postgresql://`` URL. SQLAlchemy 2.1 resolves
that scheme to psycopg (v3), which is not installed, so both of its engine sites
must go through ``archimedes.db._pin_psycopg2_driver`` like ``db.DATABASE_URL``
does. ``infra/`` is outside pytest's collection (``pytest.ini`` norecursedirs),
so the probe is loaded by path here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROBE_PATH = Path(__file__).resolve().parents[2] / "infra" / "spike-1411" / "spike_probe.py"


class _EngineRequested(Exception):
    """Raised by the stub create_engine so no step goes past building the engine."""


def _load_probe():
    spec = importlib.util.spec_from_file_location("spike_1411_probe_under_test", _PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "run_step",
    [
        pytest.param(lambda probe: probe._probe_aurora(), id="aurora"),
        pytest.param(lambda probe: probe._prepare_write_isolated_db(1), id="prepare_db"),
    ],
)
def test_probe_engines_pin_the_psycopg2_driver(run_step, monkeypatch):
    import sqlalchemy
    from archimedes import db

    probe = _load_probe()
    requested: list[str] = []

    def _stub_create_engine(url, *_args, **_kwargs):
        requested.append(str(url))
        raise _EngineRequested

    monkeypatch.setattr(sqlalchemy, "create_engine", _stub_create_engine)
    # _prepare_write_isolated_db refuses a non-SQLite write target and then
    # builds the schema; neither is what this test is about.
    monkeypatch.setattr(db, "DATABASE_URL", "sqlite:////tmp/spike-1411-never-written.db")
    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setenv("DATABASE_URL_PROBE", "postgresql://u:p@h:5432/d")

    with pytest.raises(_EngineRequested):
        run_step(probe)
    assert requested == ["postgresql+psycopg2://u:p@h:5432/d"]
