"""Server-owned feature flags with production-safe defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from fastapi import HTTPException

FeatureName = Literal["quant"]


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    quant: bool

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


def _bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def resolve_feature_flags(environ: Mapping[str, str] | None = None) -> FeatureFlags:
    environ = os.environ if environ is None else environ
    production = environ.get("APP_ENV", "development").strip().lower() in {"prod", "production"}
    value = environ.get("FEATURE_QUANT")
    return FeatureFlags(quant=not production if value is None or not value.strip() else _bool(value, "FEATURE_QUANT"))


def require_feature(name: FeatureName, flags: FeatureFlags | None = None) -> None:
    flags = flags or resolve_feature_flags()
    if not getattr(flags, name):
        raise HTTPException(status_code=404, detail="Not found")


def require_quant_feature() -> None:
    require_feature("quant")


def roadmap_surfaces_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Server-side twin of the UI's ``ROADMAP_SURFACES_ENABLED`` (#1266, #1432).

    The UI keeps out-of-scope surfaces (vaults, marketplace, publish, ...) out
    of every shipped build behind ``VITE_ROADMAP_SURFACES``; this keeps the
    backend routes behind those surfaces equally closed, so a direct API call
    cannot reach what the site says it does not offer.

    Only the word ``true`` (any case, surrounding space ignored) enables it.
    Unset, blank, ``false`` and anything else, a typo included, is OFF in every
    environment. Unlike ``FEATURE_QUANT`` there is no ``APP_ENV``-dependent
    default and no error on an unrecognised value: an unset or mistyped value
    must never be what makes a roadmap surface reachable, and it must not take
    a route down with a 500 either.

    Deliberately not a ``FeatureFlags`` field, so ``GET /api/features`` does not
    report it. The UI's roadmap gate is build-time by design
    (``ui/src/routes.js`` ``featureEnabled``), and a runtime value it ignores
    would only be a second answer to the same question.
    """
    environ = os.environ if environ is None else environ
    return environ.get("FEATURE_ROADMAP_SURFACES", "").strip().lower() == "true"


def require_roadmap_surfaces() -> None:
    """Route dependency: 404 while roadmap surfaces are off.

    Put it in the route's ``dependencies=[...]`` so FastAPI resolves it before
    the endpoint's own parameters: a caller gets this 404 before any auth
    check, body validation, or side effect.
    """
    if not roadmap_surfaces_enabled():
        raise HTTPException(status_code=404, detail="Not offered: roadmap, not shipped")
