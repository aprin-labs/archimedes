"""Tests for the visitor-insights instrument — geography + device class (#787).

Hermetic — mocks the Redis boundary. Covers the store (record + read + fail-safe),
country normalization, the device-class derivation (CloudFront headers + UA
fallback), and the humans-only capture gate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Single import style per module (CodeQL: no mixed ``import`` / ``from ... import``
# for the same module). These three modules are also monkeypatched by attribute in
# some tests, so we bind them as module aliases and reference their symbols through
# the alias everywhere — one consistent style, and the monkeypatch targets stay live.
import archimedes.api.metrics_routes as mr
import archimedes.api.visitor_insights as vmod
import archimedes.services.visitor_insights_store as vstore

_device_class = vmod._device_class
record_visitor_insight = vmod.record_visitor_insight
DEVICE_CLASSES = vstore.DEVICE_CLASSES
VisitorInsightsStore = vstore.VisitorInsightsStore
_norm_country = vstore._norm_country


def _mock_redis(pfcounts=None, members=None, first_seen=1):
    pipe = MagicMock()
    pipe.pfadd = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.sadd = MagicMock(return_value=pipe)
    pipe.pfcount = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=pfcounts if pfcounts is not None else [])
    r = MagicMock()
    r.pipeline = MagicMock(return_value=pipe)
    r.smembers = AsyncMock(return_value=members or set())
    # Top-level (non-pipelined) calls made by `record()`'s first-seen-only
    # attribution gate (#1908): SET NX on the visitor's marker — True = newly
    # attributed (proceed), None = already attributed on an earlier visit
    # (short-circuit, no re-bucketing) — then the pre-#1908 set's EXPIRE NX
    # and SISMEMBER (not a member here).
    r.set = AsyncMock(return_value=True if first_seen else None)
    r.expire = AsyncMock(return_value=True)
    r.sismember = AsyncMock(return_value=0)
    return r, pipe


def _req(headers=None):
    return SimpleNamespace(headers=headers or {}, state=SimpleNamespace(visitor_id="vid-1"))


# ─── country normalization ───────────────────────────────────────────────


def test_norm_country_valid():
    assert _norm_country("us") == "US"
    assert _norm_country("DE") == "DE"


def test_norm_country_invalid_or_missing():
    assert _norm_country(None) == "ZZ"
    assert _norm_country("") == "ZZ"
    assert _norm_country("USA") == "ZZ"  # not 2 letters
    assert _norm_country("1!") == "ZZ"


# ─── device class derivation ─────────────────────────────────────────────


def test_device_class_cloudfront_headers_win():
    assert _device_class(_req({"cloudfront-is-mobile-viewer": "true"})) == "mobile"
    assert _device_class(_req({"cloudfront-is-tablet-viewer": "true"})) == "tablet"
    assert _device_class(_req({"cloudfront-is-desktop-viewer": "true"})) == "desktop"
    assert _device_class(_req({"cloudfront-is-smarttv-viewer": "true"})) == "tv"


def test_device_class_ua_fallback():
    assert _device_class(_req({"user-agent": "Mozilla/5.0 (iPhone) Mobile"})) == "mobile"
    assert _device_class(_req({"user-agent": "Mozilla/5.0 (iPad)"})) == "tablet"
    assert _device_class(_req({"user-agent": "Mozilla/5.0 (Macintosh)"})) == "desktop"
    assert _device_class(_req({})) == "unknown"


# ─── store record + read ─────────────────────────────────────────────────


async def test_record_pfadds_country_and_device_and_indexes():
    store = VisitorInsightsStore()
    r, pipe = _mock_redis()
    store._get_redis = AsyncMock(return_value=r)

    await store.record("US", "mobile", "vid-1")

    # 4 pfadds: country total + device total + country day + device day
    assert pipe.pfadd.call_count == 4
    # country code indexed for enumeration on read
    pipe.sadd.assert_called_once()
    assert pipe.sadd.call_args.args[1] == "US"
    pipe.execute.assert_awaited_once()


async def test_record_normalizes_bad_country_and_device():
    store = VisitorInsightsStore()
    r, pipe = _mock_redis()
    store._get_redis = AsyncMock(return_value=r)
    await store.record("not-a-country", "watch", "vid-1")
    # bad country → ZZ, bad device → unknown (in the key names)
    keys = [c.args[0] for c in pipe.pfadd.call_args_list]
    assert any(k.endswith(":ZZ") for k in keys)
    assert any(":device:" in k and k.endswith(":unknown") for k in keys)


async def test_record_noop_without_visitor_id():
    store = VisitorInsightsStore()
    r, _ = _mock_redis()
    store._get_redis = AsyncMock(return_value=r)
    await store.record("US", "mobile", "")
    r.pipeline.assert_not_called()


async def test_record_fails_open():
    store = VisitorInsightsStore()
    store._get_redis = AsyncMock(side_effect=ConnectionError("down"))
    await store.record("US", "mobile", "vid-1")  # must not raise


async def test_get_insights_reads_countries_and_devices():
    store = VisitorInsightsStore()
    # smembers → {US, DE}; first pipeline (countries) → [10, 4]; second (devices, 5
    # classes in DEVICE_CLASSES order) → [7, 1, 6, 0, 0]
    r, pipe = _mock_redis(members={"US", "DE"})
    pipe.execute = AsyncMock(side_effect=[[10, 4], [7, 1, 6, 0, 0]])
    store._get_redis = AsyncMock(return_value=r)

    countries, devices = await store.get_insights()

    assert countries == {"DE": 10, "US": 4}  # sorted(codes) = [DE, US]
    assert devices == dict(zip(DEVICE_CLASSES, [7, 1, 6, 0, 0], strict=False))


async def test_get_insights_fails_open():
    store = VisitorInsightsStore()
    store._get_redis = AsyncMock(side_effect=ConnectionError("down"))
    countries, devices = await store.get_insights()
    assert countries == {}
    assert devices == dict.fromkeys(DEVICE_CLASSES, 0)


# ─── capture gate (humans only) ──────────────────────────────────────────


async def test_capture_skips_agents(monkeypatch):
    calls = {"n": 0}

    class FakeStore:
        async def record(self, *a):
            calls["n"] += 1

        async def close(self):
            pass

    monkeypatch.setattr("archimedes.services.visitor_insights_store.VisitorInsightsStore", FakeStore)
    await record_visitor_insight(_req({"cloudfront-viewer-country": "US"}), is_agent=True)
    assert calls["n"] == 0  # agents are NOT recorded — keep geography human-only


async def test_capture_records_humans(monkeypatch):
    recorded = {}

    class FakeStore:
        async def record(self, country, device, vid):
            recorded["args"] = (country, device, vid)

        async def close(self):
            pass

    monkeypatch.setattr("archimedes.services.visitor_insights_store.VisitorInsightsStore", FakeStore)
    req = _req({"cloudfront-viewer-country": "DE", "cloudfront-is-mobile-viewer": "true"})
    await record_visitor_insight(req, is_agent=False)
    assert recorded["args"] == ("DE", "mobile", "vid-1")


# ─── one-source-of-truth reconciliation (issue #830) ──────────────────────
#
# The funnel `landed` population and the geo/device population MUST be the same
# set of distinct visitors, keyed on the same archimedes_vid. A tiny in-memory
# HLL (distinct-set) Redis lets us drive N synthetic visitors through BOTH stores
# and assert country-sum == device-sum == funnel `landed`.


class _FakeHLLRedis:
    """Minimal in-memory Redis with HLL semantics (PFADD/PFCOUNT via a set) + SET NX.

    Mirrors the boundary the stores use (``pipeline`` → ``pfadd``/``pfcount``/
    ``sadd``/``expire`` → ``execute``). PFCOUNT is exact here (a set), which is
    fine for a small deterministic reconciliation test.
    """

    def __init__(self) -> None:
        self.hll: dict[str, set[str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}

    def pipeline(self):
        return _FakePipe(self)

    async def smembers(self, key: str):
        return set(self.sets.get(key, set()))

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        """Top-level SET NX — the first-seen gate's per-visitor marker (#1908).

        True if the key was created, None if it already existed (redis-py's
        return values for SET NX). TTLs are not modelled here; the retention
        tests at the bottom of this file use fakeredis for that.
        """
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def expire(self, *a, **kw) -> bool:
        return False

    async def sismember(self, key: str, member: str) -> int:
        return int(member in self.sets.get(key, set()))


class _FakePipe:
    def __init__(self, r: _FakeHLLRedis) -> None:
        self._r = r
        self._ops: list = []

    def pfadd(self, key: str, member: str):
        self._ops.append(("pfadd", key, member))
        return self

    def pfcount(self, key: str):
        self._ops.append(("pfcount", key, None))
        return self

    def sadd(self, key: str, member: str):
        self._ops.append(("sadd", key, member))
        return self

    def expire(self, *a, **kw):
        self._ops.append(("expire", None, None))
        return self

    async def execute(self):
        results: list = []
        for op, key, member in self._ops:
            if op == "pfadd":
                self._r.hll.setdefault(key, set()).add(member)
                results.append(1)
            elif op == "pfcount":
                results.append(len(self._r.hll.get(key, set())))
            elif op == "sadd":
                self._r.sets.setdefault(key, set()).add(member)
                results.append(1)
            else:
                results.append(True)
        self._ops = []
        return results


async def test_landed_population_reconciles_funnel_and_geo_device():
    """N distinct landed visitors → country-sum == device-sum == funnel `landed`.

    Drives the SAME visitor ids through the funnel store (`landed`) and the
    visitor-insights store (country + device), on a shared HLL Redis, then reads
    both back. The three distinct counts must agree — that is the invariant the
    single-source-of-truth reconciliation guarantees (issue #830).
    """
    from unittest.mock import AsyncMock

    from archimedes.services.funnel_store import FunnelStore

    shared = _FakeHLLRedis()

    funnel = FunnelStore()
    funnel._get_redis = AsyncMock(return_value=shared)
    insights = VisitorInsightsStore()
    insights._get_redis = AsyncMock(return_value=shared)

    n = 7
    for i in range(n):
        vid = f"vid-{i}"
        # Same trigger (landed), same dedup key (vid), for both surfaces.
        await funnel.record("landed", vid)
        # Country cycles US/DE, device cycles mobile/desktop — the SUM across
        # buckets must still equal the distinct landed population.
        country = "US" if i % 2 == 0 else "DE"
        device = "mobile" if i % 3 == 0 else "desktop"
        await insights.record(country, device, vid)

    landed = (await funnel.get_totals())["landed"]
    countries, devices = await insights.get_insights()
    country_sum = sum(countries.values())
    device_sum = sum(devices.values())

    assert landed == n
    assert country_sum == n
    assert device_sum == n
    assert country_sum == device_sum == landed


async def test_repeat_visits_with_drifting_country_device_still_reconcile():
    """A returning visitor classified under a DIFFERENT country/device on a
    later visit must NOT be double-bucketed (issue #854 finding #6).

    Before first-seen attribution, Insights showed Top Countries summing to
    106 and Device summing to 104 against a funnel `landed` of 61 — because
    a repeat visitor recorded under >1 country or device across visits was
    counted once per bucket even though the funnel counts them once overall.
    This drives 5 distinct visitors through 2 visits each, with country and
    device DRIFTING on the second visit, and asserts the sums still equal
    the funnel population.
    """
    from unittest.mock import AsyncMock

    from archimedes.services.funnel_store import FunnelStore

    shared = _FakeHLLRedis()

    funnel = FunnelStore()
    funnel._get_redis = AsyncMock(return_value=shared)
    insights = VisitorInsightsStore()
    insights._get_redis = AsyncMock(return_value=shared)

    n = 5
    for i in range(n):
        vid = f"vid-{i}"
        # First visit.
        await funnel.record("landed", vid)
        await insights.record("US", "desktop", vid)
        # Second visit, later — classified differently (VPN / new device /
        # flaky UA sniff). The funnel sees the SAME distinct visitor again
        # (idempotent); the naive pre-fix behavior would double-bucket them
        # into a second country AND a second device.
        await funnel.record("landed", vid)
        await insights.record("DE", "mobile", vid)

    landed = (await funnel.get_totals())["landed"]
    countries, devices = await insights.get_insights()
    country_sum = sum(countries.values())
    device_sum = sum(devices.values())

    assert landed == n
    assert country_sum == n
    assert device_sum == n
    # First-seen attribution: every visitor's ONE bucket is their first visit.
    assert countries == {"US": n}
    assert devices["desktop"] == n
    assert devices.get("mobile", 0) == 0


async def test_beacon_path_records_both_funnel_and_visitor_insight():
    """The JS-gated `landed` beacon endpoint records funnel AND geo/device (issue #830).

    Confirms the single trigger drives both surfaces with the same visitor id —
    this is the wiring that keeps the two distinct-visitor counts in lockstep.
    """
    from archimedes.models.telemetry import FunnelEventRequest

    funnel_calls: list = []
    insight_calls: list = []

    async def _fake_record_funnel(request, stage):
        funnel_calls.append((getattr(request.state, "visitor_id", None), stage))

    async def _fake_record_insight(request, is_agent=False):
        insight_calls.append((getattr(request.state, "visitor_id", None), is_agent))

    orig_funnel = mr.record_funnel
    orig_insight = mr.record_visitor_insight
    mr.record_funnel = _fake_record_funnel
    mr.record_visitor_insight = _fake_record_insight
    try:
        req = _req({"user-agent": "Mozilla/5.0"})
        req.state.visitor_id = "vid-beacon"
        # No is_agent on state → the beacon defaults it to False (ordinary human).
        await mr.record_funnel_event(FunnelEventRequest(stage="landed"), req)
    finally:
        mr.record_funnel = orig_funnel
        mr.record_visitor_insight = orig_insight

    # Same vid drove both surfaces off the single landed beacon; is_agent=False.
    assert funnel_calls == [("vid-beacon", "landed")]
    assert insight_calls == [("vid-beacon", False)]


async def test_beacon_passes_is_agent_through_to_visitor_insight():
    """The beacon forwards ``request.state.is_agent`` so the agent-skip fires (issue #830 review).

    Copilot flagged that ``record_funnel_event`` dropped the classifier's ``is_agent``
    verdict, defeating the defense-in-depth skip in ``record_visitor_insight``. This
    pins that the beacon now passes it through — an agent-classified beacon reaches
    ``record_visitor_insight`` with ``is_agent=True``.
    """
    from archimedes.models.telemetry import FunnelEventRequest

    seen: list = []

    async def _noop_funnel(request, stage):
        pass

    async def _capture_insight(request, is_agent=False):
        seen.append(is_agent)

    orig_funnel = mr.record_funnel
    orig_insight = mr.record_visitor_insight
    mr.record_funnel = _noop_funnel
    mr.record_visitor_insight = _capture_insight
    try:
        req = _req({"user-agent": "curl/8.0"})
        req.state.visitor_id = "vid-agent"
        req.state.is_agent = True  # what the telemetry middleware would have set
        await mr.record_funnel_event(FunnelEventRequest(stage="landed"), req)
    finally:
        mr.record_funnel = orig_funnel
        mr.record_visitor_insight = orig_insight

    assert seen == [True]


async def test_non_landed_beacon_records_neither():
    """A non-emittable stage records nothing (only `landed` is client-emittable)."""
    from archimedes.models.telemetry import FunnelEventRequest

    calls = {"n": 0}

    async def _boom(*a, **kw):
        calls["n"] += 1

    orig_funnel = mr.record_funnel
    orig_insight = mr.record_visitor_insight
    mr.record_funnel = _boom
    mr.record_visitor_insight = _boom
    try:
        out = await mr.record_funnel_event(FunnelEventRequest(stage="wallet_connected"), _req())
    finally:
        mr.record_funnel = orig_funnel
        mr.record_visitor_insight = orig_insight

    assert out == {"recorded": False}
    assert calls["n"] == 0


async def test_browser_ua_no_session_is_recorded_by_the_classifier():
    """A browser-UA-no-session request (open-demo human) IS recorded (issue #830).

    Pins label-to-behavior: the classifier's open-demo default is `human`, and the
    beacon path DOES record such a visitor — so the docstring must NOT claim the
    population is "real visitors, not crawlers". The companion docstring assertion
    below proves the over-claiming language is gone.
    """
    recorded = {}

    class FakeStore:
        async def record(self, country, device, vid):
            recorded["args"] = (country, device, vid)

        async def close(self):
            pass

    orig = vstore.VisitorInsightsStore
    vstore.VisitorInsightsStore = FakeStore
    try:
        # Browser UA, NO session, NO CloudFront country header → open-demo human,
        # device via UA fallback. This is exactly the population the old docstring
        # falsely claimed was excluded.
        req = _req({"user-agent": "Mozilla/5.0 (Macintosh)"})
        await record_visitor_insight(req)  # default is_agent=False (beacon call)
    finally:
        vstore.VisitorInsightsStore = orig

    # It WAS recorded (country ZZ via missing header, device desktop via UA).
    assert recorded["args"][2] == "vid-1"
    assert recorded["args"][1] == "desktop"


def test_store_docstring_does_not_overclaim_real_visitors():
    """The store docstring must not claim 'real visitors, not datacenter crawler IPs' (#830)."""
    doc = (vstore.__doc__ or "") + (vstore.VisitorInsightsStore.__doc__ or "")
    assert "real visitors, not datacenter" not in doc
    assert "human-ish" not in doc


def test_capture_helper_docstring_does_not_overclaim():
    """The capture helper docstring must not claim it excludes crawlers as 'real visitors' (#830)."""
    doc = vmod.__doc__ or ""
    assert "real visitors, not datacenter" not in doc


# ─── retention: the first-seen gate expires with the visitor cookie (#1908) ──
#
# The gate has to remember a raw visitor id for as long as that id can still
# arrive, and no longer. The id rides in the ``archimedes_vid`` cookie, which
# the middleware mints once with a fixed max-age and never refreshes, so the
# cookie's lifetime is the bound. These run against fakeredis so TTLs are real.

_VID_TTL_SECONDS = 180 * 24 * 60 * 60  # api/funnel_middleware._VID_TTL_SECONDS, pinned below
_GATE_PATTERN = "archimedes:visitors:attributed*"


def _fakeredis_store():
    import fakeredis

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = VisitorInsightsStore()
    store._get_redis = AsyncMock(return_value=r)
    return store, r


async def _gate_ttls(r) -> dict[str, int]:
    return {k: await r.ttl(k) async for k in r.scan_iter(_GATE_PATTERN)}


async def _holds_member(r, key: str, member: str) -> bool:
    return await r.type(key) == "set" and bool(await r.sismember(key, member))


def test_cookie_lifetime_this_retention_is_pinned_to_is_still_180_days():
    """The retention bound below is the cookie's max-age. If that changes, so must this."""
    from archimedes.api.funnel_middleware import _VID_TTL_SECONDS as cookie_ttl

    assert cookie_ttl == _VID_TTL_SECONDS


async def test_first_seen_gate_holds_no_raw_visitor_id_past_the_cookie_lifetime():
    """Every gate key that remembers a visitor id carries a TTL no longer than the cookie's.

    Before #1908 the gate was one Redis SET of every raw id ever seen, with no
    TTL: a visitor id outlived its cookie forever.
    """
    store, r = _fakeredis_store()

    await store.record("US", "desktop", "vid-retention")

    ttls = await _gate_ttls(r)
    assert ttls, "the first-seen gate wrote nothing to Redis"
    for key, ttl in ttls.items():
        assert 0 < ttl <= _VID_TTL_SECONDS, f"{key} keeps visitor ids with TTL {ttl} (-1 = never expires)"


async def test_each_visitor_gets_a_full_cookie_lifetime_of_first_seen_protection():
    """Expiry is per visitor, not one shared clock.

    A single shared key with a TTL would forget a visitor first seen yesterday
    as soon as the key's clock (started by someone else, months ago) ran out,
    and that visitor's next landing would be counted again. Simulate an old
    gate by cutting every existing gate key down to 10 days left, then record a
    new visitor: their protection must still last a full cookie lifetime.
    """
    store, r = _fakeredis_store()
    await store.record("US", "desktop", "vid-early")
    for key in await _gate_ttls(r):
        await r.expire(key, 10 * 24 * 60 * 60)

    await store.record("DE", "mobile", "vid-late")

    late = {}
    for key, ttl in (await _gate_ttls(r)).items():
        if "vid-late" in key or await _holds_member(r, key, "vid-late"):
            late[key] = ttl
    assert late, "vid-late was not remembered by the gate"
    for key, ttl in late.items():
        assert ttl > _VID_TTL_SECONDS - 60, f"{key}: vid-late protected for only {ttl}s"


async def test_repeat_visit_inside_the_cookie_lifetime_is_still_not_rebucketed():
    """First-seen counting is unchanged: a second landing never re-buckets."""
    store, _r = _fakeredis_store()

    await store.record("US", "desktop", "vid-repeat")
    await store.record("DE", "mobile", "vid-repeat")

    countries, devices = await store.get_insights()
    assert countries == {"US": 1}
    assert devices["desktop"] == 1
    assert devices["mobile"] == 0


async def test_ids_in_the_pre_1908_unbounded_set_are_honoured_then_expire():
    """The old SET is read during the transition and given a TTL, never re-counted.

    Every id in it was added by the old code, through a cookie minted no later
    than the end of the rolling deploy, so it can arrive for at most one cookie
    lifetime after the rollout ends. Expiring the whole key one lifetime after
    the first post-deploy write can therefore forget an id that can still come
    back only for at most the length of the rollout (see the comment on
    ``_LEGACY_ATTRIBUTED_KEY``).
    """
    store, r = _fakeredis_store()
    await r.sadd("archimedes:visitors:attributed", "vid-legacy")
    assert await r.ttl("archimedes:visitors:attributed") == -1  # as found in production

    await store.record("DE", "mobile", "vid-legacy")

    countries, devices = await store.get_insights()
    assert countries == {}, "a visitor attributed before #1908 was counted a second time"
    assert devices["mobile"] == 0
    legacy_ttl = await r.ttl("archimedes:visitors:attributed")
    assert 0 < legacy_ttl <= _VID_TTL_SECONDS, f"the pre-#1908 set still never expires (TTL {legacy_ttl})"


def _trace_top_level_commands(r) -> list[str]:
    """Record the name of every non-pipelined command sent to ``r`` from now on."""
    sent: list[str] = []
    execute = r.execute_command

    async def traced(*args, **options):
        sent.append(str(args[0]).upper())
        return await execute(*args, **options)

    r.execute_command = traced
    return sent


async def test_the_pre_1908_set_expires_a_lifetime_after_the_first_recording_and_nothing_later_moves_it():
    """The old SET gets ONE TTL, from the first post-deploy recording, and keeps it.

    "The old SET expires 180 days after the first post-deploy recording" needs
    two things in ``_claim_first_seen``. EXPIRE must say NX: without it every
    new visitor restarts the old set's clock, so on a live site it never runs
    out. And it must come after the SET NX first-seen check: issued before it,
    it would also run on every repeat visit, which then costs two round trips
    instead of one (and, without NX, restarts the clock on every visit).
    """
    legacy = "archimedes:visitors:attributed"
    store, r = _fakeredis_store()
    await r.sadd(legacy, "vid-legacy")
    await store.record("US", "desktop", "vid-first")
    assert 0 < await r.ttl(legacy) <= _VID_TTL_SECONDS

    # 170 days on: cut what is left of the old set's TTL to 10 days.
    ten_days = 10 * 24 * 60 * 60
    await r.expire(legacy, ten_days)

    # A new visitor and an id from the old set both pass the first-seen check,
    # so both reach the EXPIRE; neither may move the expiry back.
    for vid in ("vid-new", "vid-legacy"):
        await store.record("DE", "mobile", vid)
        ttl = await r.ttl(legacy)
        assert 0 < ttl <= ten_days, f"recording {vid} pushed the old set's expiry back to {ttl}s"

    # A repeat visitor stops at the first-seen check: one command, the SET NX.
    sent = _trace_top_level_commands(r)
    await store.record("FR", "tablet", "vid-first")
    assert sent == ["SET"], f"a repeat visit sent {sent}; the first-seen check alone decides it"
    ttl = await r.ttl(legacy)
    assert 0 < ttl <= ten_days, f"a repeat visit pushed the old set's expiry back to {ttl}s"
