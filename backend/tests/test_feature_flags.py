import pytest
from archimedes.feature_flags import FeatureFlags, require_feature, resolve_feature_flags
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient


def test_quant_defaults_off_in_production_and_on_elsewhere():
    assert resolve_feature_flags({"APP_ENV": "production"}).quant is False
    assert resolve_feature_flags({"APP_ENV": "development"}).quant is True
    assert resolve_feature_flags({"APP_ENV": "test"}).quant is True


def test_explicit_quant_flag_overrides_environment():
    assert resolve_feature_flags({"APP_ENV": "production", "FEATURE_QUANT": "true"}).quant is True
    assert resolve_feature_flags({"APP_ENV": "development", "FEATURE_QUANT": "false"}).quant is False


def test_invalid_feature_flag_fails_closed():
    with pytest.raises(ValueError, match="FEATURE_QUANT"):
        resolve_feature_flags({"FEATURE_QUANT": "sometimes"})


def test_disabled_feature_api_gate_returns_not_found():
    with pytest.raises(HTTPException) as exc:
        require_feature("quant", FeatureFlags(quant=False))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_disabled_quant_feature_hides_computation_apis(monkeypatch):
    monkeypatch.setenv("FEATURE_QUANT", "false")
    from archimedes.api.account_auth import CurrentUser, require_current_user
    from archimedes.main import app

    app.dependency_overrides[require_current_user] = lambda: CurrentUser(
        id="user-1", name="User", email="user@example.test", email_verified=True
    )
    calls = (
        ("POST", "/api/portfolio/optimize"),
        ("POST", "/api/portfolio/parameter-sweep"),
        ("GET", "/api/risk/cvar"),
        ("GET", "/api/risk/greeks"),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for method, path in calls:
                response = await client.request(method, path, json={} if method == "POST" else None)
                assert response.status_code == 404, path
    finally:
        app.dependency_overrides.pop(require_current_user, None)


# ── Roadmap surfaces (#1432): the server-side twin of the UI's build flag ────


@pytest.mark.parametrize("app_env", [None, "development", "test", "production"])
def test_roadmap_surfaces_are_off_unless_explicitly_true_in_every_environment(app_env):
    """No APP_ENV-dependent default, unlike FEATURE_QUANT: an unset value must
    never be what makes a roadmap surface reachable, in dev or in prod."""
    from archimedes.feature_flags import roadmap_surfaces_enabled

    base = {} if app_env is None else {"APP_ENV": app_env}
    assert roadmap_surfaces_enabled(base) is False
    for value in ("", "  ", "false", "0", "1", "yes", "on", "ture"):
        assert roadmap_surfaces_enabled({**base, "FEATURE_ROADMAP_SURFACES": value}) is False, value
    for value in ("true", "TRUE", " true "):
        assert roadmap_surfaces_enabled({**base, "FEATURE_ROADMAP_SURFACES": value}) is True, value


def test_disabled_roadmap_gate_returns_not_found(monkeypatch):
    from archimedes.feature_flags import require_roadmap_surfaces

    monkeypatch.delenv("FEATURE_ROADMAP_SURFACES", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_roadmap_surfaces()
    assert exc.value.status_code == 404
    assert exc.value.detail == "Not offered: roadmap, not shipped"


@pytest.mark.asyncio
async def test_features_endpoint_does_not_report_the_roadmap_flag(monkeypatch):
    """GET /api/features is unchanged by the roadmap gate. The UI's roadmap flag
    is build-time by design (#1266; ui/src/routes.js featureEnabled), so the
    server never hands it a runtime roadmap value."""
    monkeypatch.setenv("FEATURE_ROADMAP_SURFACES", "true")
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/features")
    assert response.status_code == 200
    assert set(response.json()) == {"quant"}
