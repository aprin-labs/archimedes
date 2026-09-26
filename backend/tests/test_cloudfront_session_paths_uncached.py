"""Session-dependent paths must bypass the CloudFront cache (#1768).

2026-09-01 sign-in outage. nginx's ``auth_request`` gate on ``^~ /app`` answers
an anonymous visitor with ``302 /sign-in?next=/app/generate``. That response
matched no ``ordered_cache_behavior`` in ``infra/cloudfront.tf``, so it fell
through to ``default_cache_behavior`` and the ``html`` cache policy —
``default_ttl = 60``, ``cookie_behavior = "none"``. Keyed WITHOUT the session
cookie, CloudFront replayed the anonymous redirect to the next request that
carried one::

    GET /app/generate                            -> 302  x-cache: Miss
    GET /app/generate                            -> 302  x-cache: Hit
    GET /app/generate  (Cookie: __Secure-...=…)  -> 302  x-cache: Hit   # the bug

The SPA then ping-ponged: 165 ``GET /api/auth/get-session`` calls in 45 minutes
for one user, with the backend seeing no failing request at all.

PR #1767 closed it at the origin — ``Cache-Control: private, no-store`` on the
gated ``/app`` locations and the ``@sign_in`` redirect, honoured because the
``html`` policy has ``min_ttl = 0``, and pinned by
``backend/tests/test_nginx_gated_responses_uncached.py``. **Necessary, not
sufficient.** That header is opt-IN per response, in a different file, and its
guard enumerates the locations that gate on ``auth_request /_auth_session`` —
so it catches a deleted header but cannot see a gated response produced
anywhere else. The edge behaviour is opt-OUT per path: with CachingDisabled
there is nothing at the edge to replay even when the origin forgets to say so.

This suite is the CDN half. It reads ``infra/cloudfront.tf`` as text — reusing
the reader and the CloudFront first-match resolver from the sibling guard
``test_cloudfront_health_uncached.py`` (#1520) rather than growing a second
copy of them — and models the resolution CloudFront would actually perform.
Hermetic: one file in the repo, no AWS, no terraform binary, no network, no DB.

``terraform validate`` catches none of this. A missing behaviour, a behaviour
bound to the wrong cache policy, and a behaviour ordered behind a broader
pattern that swallows it are all syntactically valid HCL.

Not every path these patterns cover is session-dependent, and the suite says so
rather than papering over it. ``nginx/nginx.conf`` carries the anonymous-browse
carve-out block (#1753, narrowing #1194 revision d) — bare ``/app``,
``/app/explore`` and ``/app/corpus`` are PUBLIC, ungated, identical for every
viewer. (``/app/leaderboard`` and ``/app/strategy/*`` were carve-outs under #1194
rev d and are gated as of #1753; nothing here changed for them, because ``/app/*``
already covered gated and public alike — which is the point of the ruling below.)
They are de-cached here **on purpose** (the owner's
ruling on the review of PR #1772, 2026-09-01) and
``TestThePublicCarveOutsAreDeCachedOnPurpose`` pins that, so a later "optimisation"
that quietly puts them back on the ``html`` policy is a failing test rather than a
silent reopening of #1768's blast radius.

MUTATION: delete any one of the three ``ordered_cache_behavior`` blocks →
``test_the_behaviour_is_declared`` and ``test_the_path_resolves_to_caching_disabled``
go red naming the pattern.

MUTATION: rebind ``/app/*`` to ``aws_cloudfront_cache_policy.html.id`` → **9 failed**
— the binding assertion, the four gated ``/app/`` resolutions, and all four public
carve-outs under ``/app/``. (Bare ``/app`` stays green: it has its own behaviour, which
is exactly why there are two patterns and not one.)

Run:
    /path/to/env/bin/pytest backend/tests/test_cloudfront_session_paths_uncached.py -q
"""

from __future__ import annotations

import re

import pytest

from tests.test_cloudfront_health_uncached import (
    CACHING_DISABLED,
    HTML_POLICY,
    STATIC_POLICY,
    _default_behaviour,
    _ordered_behaviours,
    _pattern_matches,
    _resolve,
    _resource_span,
    _tf_source,
)

# The three behaviours this issue adds. Spelled out as literals, not derived
# from the file, so deleting a block is a failure rather than a smaller set.
SESSION_PATTERNS = ("/app/*", "/app", "/sign-in*")

ALL_VIEWER = "aws_cloudfront_origin_request_policy.all_viewer.id"
SECURITY_HEADERS = "aws_cloudfront_response_headers_policy.security.id"

ALL_VIEWER_POLICY = 'resource "aws_cloudfront_origin_request_policy" "all_viewer"'

# Real URLs whose response depends on the session cookie. `/app/nonsense` is
# included on purpose: the gated `^~ /app` block is a catch-all with an SPA
# fallback, so an unknown /app path gets the same anonymous 302 as a real one —
# and the same cached-redirect hazard.
SESSION_DEPENDENT_PATHS = (
    "/app",
    "/app/generate",
    "/app/insights",
    "/app/leaderboard",
    "/app/library",
    "/app/nonsense",
    "/app/strategy/abc123",
    "/sign-in",
)

# The anonymous-browse carve-outs (#1753, narrowing #1194 revision d): PUBLIC
# pages that live under /app and are NOT session-dependent — nginx serves them
# with no `auth_request`, and nginx's longest-prefix match puts them ahead of
# the gated `^~ /app`. Listed separately from SESSION_DEPENDENT_PATHS so the
# distinction survives in the file that asserts on it.
#
# `/app/leaderboard` and `/app/strategy/<id>` were on this list under #1194 rev
# d and moved to SESSION_DEPENDENT_PATHS with #1753. That move is the whole
# reason this file does not enumerate carve-outs in `infra/cloudfront.tf`: the
# edge behaviour for both sets is the same `/app*` CachingDisabled pair, so a
# page crossing the gated/public line needs no terraform change and cannot
# reintroduce #1768 by being forgotten here.
PUBLIC_APP_CARVE_OUTS = (
    "/app",
    "/app/explore",
    "/app/corpus",
)

# Anti-goal: this change must not pull anything else off its behaviour. The
# hashed bundle names are the live shape of a Vite build under /assets/.
STILL_CACHED = {
    "/": HTML_POLICY,
    "/explore": HTML_POLICY,
    "/index.html": HTML_POLICY,
    "/assets/index-C4f9aB2x.js": STATIC_POLICY,
    "/assets/index-9d2Ee1.css": STATIC_POLICY,
}

# Ordering anchors. The session behaviours sit AFTER these (nothing may get in
# front of the liveness or API paths) and BEFORE the suffix patterns.
MUST_PRECEDE_SESSION = ("/health", "/health/*", "/api/*", "/events/*", "/assets/*", "/static/*")
# `*.png` and its siblings joined this list in #1776: every suffix pattern
# bound to `static_assets` is a way for a gated /app path to acquire a 1h
# cookie-blind cache, not just the two that existed when #1768 was written.
MUST_FOLLOW_SESSION = ("*.js", "*.css", "*.png", "*.svg", "*.jpg", "*.webp", "*.woff2", "*.ico")


def _by_pattern() -> dict[str, dict[str, str]]:
    return {b["path_pattern"]: b for b in _ordered_behaviours()}


def _index_of(pattern: str) -> int:
    patterns = [b["path_pattern"] for b in _ordered_behaviours()]
    assert pattern in patterns, f"no ordered_cache_behavior for {pattern!r}; found {patterns}"
    return patterns.index(pattern)


def _comment_above(pattern: str) -> str:
    """The comment block immediately above a behaviour, as raw text.

    Bounded by the end of the previous top-level block (``\n  }\n``) rather
    than a fixed character window, so a comment that grows or shrinks does not
    quietly slide out of view and turn these assertions vacuous.
    """
    src = _tf_source()
    needle = f'path_pattern               = "{pattern}"'
    assert needle in src, f"{needle!r} not found — the block's formatting changed"
    head = src[: src.index(needle)]
    start = head.rfind("\n  }\n")
    assert start != -1, "no preceding block boundary — the file shape changed"
    comment = head[start:]
    assert comment.count("#") >= 5, f"no comment block above {pattern!r}"
    return comment


class TestTheReaderIsNotVacuous:
    """Guards the guard: every assertion below is conditional on the shared
    reader actually finding blocks. A silently-empty parse would make this
    whole file pass against any input at all.
    """

    def test_the_reader_finds_every_behaviour(self):
        behaviours = _ordered_behaviours()
        assert len(behaviours) >= 11, f"only parsed {len(behaviours)} ordered_cache_behavior blocks"

    def test_the_session_patterns_are_distinct_and_well_formed(self):
        patterns = [b["path_pattern"] for b in _ordered_behaviours()]
        assert len(patterns) == len(set(patterns)), f"duplicate path_pattern: {patterns}"
        assert all(p.startswith(("/", "*")) for p in patterns), patterns

    def test_the_ordering_anchors_still_exist(self):
        """If `/static/*` or `*.js` is renamed, the ordering tests below would
        silently stop asserting anything about position.
        """
        patterns = {b["path_pattern"] for b in _ordered_behaviours()}
        for anchor in (*MUST_PRECEDE_SESSION, *MUST_FOLLOW_SESSION):
            assert anchor in patterns, f"ordering anchor {anchor!r} is gone; found {sorted(patterns)}"


class TestSessionPathsBypassTheEdgeCache:
    """Fails against `main` before #1768: none of the three behaviours existed,
    so every `/app` and `/sign-in` URL resolved to `<default_cache_behavior>`
    and the `html` policy's 60s, cookie-blind TTL.
    """

    @pytest.mark.parametrize("pattern", SESSION_PATTERNS)
    def test_the_behaviour_is_declared(self, pattern: str):
        assert pattern in _by_pattern(), (
            f"no ordered_cache_behavior for {pattern!r}. Its responses depend on the "
            f"session cookie, and the default behaviour's html policy keys without one — "
            f"an anonymous 302 gets replayed to the next signed-in viewer (2026-09-01)."
        )

    @pytest.mark.parametrize("pattern", SESSION_PATTERNS)
    def test_the_behaviour_binds_caching_disabled(self, pattern: str):
        policy = _by_pattern().get(pattern, {}).get("cache_policy_id")
        assert policy == CACHING_DISABLED, (
            f"{pattern!r} is bound to {policy!r}, not {CACHING_DISABLED}. Declaring the "
            f"behaviour and pointing it at a caching policy is worse than not declaring "
            f"it: it reads as the fix while the edge still caches the response."
        )

    @pytest.mark.parametrize("path", SESSION_DEPENDENT_PATHS)
    def test_the_path_resolves_to_caching_disabled(self, path: str):
        """First-match resolution, not "a behaviour exists somewhere"."""
        pattern, policy = _resolve(path)
        assert policy == CACHING_DISABLED, (
            f"{path} resolves to the {pattern!r} behaviour, whose cache_policy_id is "
            f"{policy!r}, not {CACHING_DISABLED}. CloudFront will cache a response that "
            f"depends on the session cookie and serve it to a viewer with a different one."
        )

    @pytest.mark.parametrize("pattern", SESSION_PATTERNS)
    def test_the_session_cookie_and_query_string_reach_the_origin(self, pattern: str):
        """CachingDisabled alone would break the gate it is protecting.

        Without an origin request policy CloudFront forwards no cookies, so
        `auth_request /_auth_session` would see an anonymous request from every
        viewer and `?next=` would never reach the SPA — the page would be
        uncached and permanently signed out.
        """
        forwarded = _by_pattern().get(pattern, {}).get("origin_request_policy_id")
        assert forwarded == ALL_VIEWER, f"{pattern!r} forwards {forwarded!r}, not {ALL_VIEWER}"

    def test_all_viewer_really_forwards_cookies_headers_and_query_strings(self):
        """The name is not the behaviour. Pinned so a later edit that narrows
        the policy shows up here rather than as a signed-out /app.
        """
        start, end = _resource_span(ALL_VIEWER_POLICY)
        body = _tf_source()[start:end]
        assert re.search(r'cookie_behavior\s*=\s*"all"', body), "all_viewer no longer forwards cookies"
        assert re.search(r'header_behavior\s*=\s*"allViewer', body), "all_viewer no longer forwards viewer headers"
        assert re.search(r'query_string_behavior\s*=\s*"all"', body), "all_viewer no longer forwards query strings"

    @pytest.mark.parametrize("pattern", SESSION_PATTERNS)
    def test_the_security_headers_match_the_default_behaviour(self, pattern: str):
        """These are the same HTML pages as the default behaviour serves. A
        carve-out that forgets the response headers policy silently drops HSTS
        and frame-options from the gated half of the site.
        """
        expected = _default_behaviour().get("response_headers_policy_id")
        assert expected == SECURITY_HEADERS, f"the default behaviour's headers policy moved: {expected!r}"
        actual = _by_pattern().get(pattern, {}).get("response_headers_policy_id")
        assert actual == expected, f"{pattern!r} uses {actual!r}, the default behaviour uses {expected!r}"

    @pytest.mark.parametrize("pattern", SESSION_PATTERNS)
    def test_the_allowed_methods_match_the_default_behaviour(self, pattern: str):
        """The only thing this change may alter for these paths is caching. A
        narrower method list would turn a working request into a 405 at the
        edge, which is a new outage shipped inside an outage fix.
        """
        expected = _default_behaviour().get("allowed_methods")
        actual = _by_pattern().get(pattern, {}).get("allowed_methods")
        assert actual == expected, f"{pattern!r} allows {actual}, the default behaviour allows {expected}"


class TestOrdering:
    """CloudFront takes the FIRST matching behaviour, not the most specific.
    Position is as load-bearing as the policy id, and invisible in a diff that
    only shows added lines.
    """

    @pytest.mark.parametrize("pattern", SESSION_PATTERNS)
    def test_nothing_ordered_in_front_captures_a_session_path(self, pattern: str):
        behaviours = _ordered_behaviours()
        for earlier in behaviours[: _index_of(pattern)]:
            for path in SESSION_DEPENDENT_PATHS:
                if _pattern_matches(pattern, path):
                    assert not _pattern_matches(earlier["path_pattern"], path), (
                        f"{earlier['path_pattern']!r} is ordered before {pattern!r} and matches "
                        f"{path}, so it wins first-match resolution"
                    )

    @pytest.mark.parametrize("anchor", MUST_PRECEDE_SESSION)
    def test_the_session_behaviours_stay_behind_health_and_api(self, anchor: str):
        """Liveness and API paths must keep their precedence 0..n; a session
        behaviour inserted above `/health` would recapture the probes.
        """
        for pattern in SESSION_PATTERNS:
            assert _index_of(anchor) < _index_of(pattern), (
                f"{pattern!r} is ordered in front of {anchor!r}, which owns its paths first"
            )

    @pytest.mark.parametrize("anchor", MUST_FOLLOW_SESSION)
    def test_the_session_behaviours_stay_ahead_of_the_suffix_patterns(self, anchor: str):
        """`*.js` / `*.css` match ANY path with that suffix, `/app/x.js`
        included, and bind it to `static_assets` — 1h at the edge with
        `cookie_behavior = "none"`. That is the same defect with a longer TTL,
        so the session behaviours have to win first.
        """
        for pattern in SESSION_PATTERNS:
            assert _index_of(pattern) < _index_of(anchor), (
                f"{anchor!r} is ordered in front of {pattern!r}; a gated {anchor} under "
                f"/app would be cached for an hour, keyed without the session cookie"
            )


class TestThePublicCarveOutsAreDeCachedOnPurpose:
    """The public half of /app is de-cached deliberately. Owner's ruling.

    Reviewed on PR #1772 (2026-09-01): `/app/*` and `/app` do not only cover
    session-dependent pages. The anonymous-browse carve-outs of #1194 revision
    d, as narrowed by #1753 — bare `/app` (the SPA's alias for Explore),
    `/app/explore` and `/app/corpus` — are public, ungated
    at nginx (no `auth_request`, no
    `error_page 401 = @sign_in`), and identical for every viewer. Carving them
    back onto the `html` policy would be a defensible cache optimisation and
    the owner ruled AGAINST it, for two reasons:

    1. A carve-out promoted to gated later must not be able to reintroduce
       #1767's cached anonymous 302. The product has already moved pages across
       that line; the edge must not be the thing that has to be remembered.
    2. Per-carve-out behaviours here would make `infra/cloudfront.tf` a fourth
       copy of the anon-page list that `nginx/nginx.conf` and `ANON_APP_PAGES`
       in `ui/src/routes.js` already have to keep in lockstep — and a fourth
       copy needs a fourth lockstep guard. #1753 moved two pages across the
       line and this file needed no edit for it, which is reason 1 and reason 2
       demonstrated rather than argued.

    The price paid is named in the comment above the blocks and asserted below:
    an origin hit per anonymous visitor for a ~4 KB static shell.

    So this class is a DECISION pin, not a correctness pin. It fails if someone
    reverses the ruling without the ruling being reversed.
    """

    @pytest.mark.parametrize("path", PUBLIC_APP_CARVE_OUTS)
    def test_the_public_carve_out_resolves_to_caching_disabled(self, path: str):
        pattern, policy = _resolve(path)
        assert policy == CACHING_DISABLED, (
            f"{path} is a PUBLIC anonymous-browse page (#1194 rev d) and it now resolves "
            f"to the {pattern!r} behaviour with policy {policy!r}. Putting the carve-outs "
            f"back on a caching policy is the owner's call to reverse, not a cleanup: it "
            f"buys edge hits on a ~4 KB static shell and sells the guarantee that a "
            f"carve-out promoted to gated cannot reintroduce #1768."
        )

    def test_the_comment_says_the_de_caching_is_deliberate(self):
        """The pin is worthless if the file still calls these paths session-
        dependent — the next reader would "fix" the prose by deleting the
        behaviour instead of reading the ruling.
        """
        comment = _comment_above("/app/*")
        assert "#1753" in comment, "the comment does not cite the carve-out decision (#1753, narrowing #1194 rev d)"
        for page in ("/app/explore", "/app/corpus"):
            assert page in comment, f"the comment does not name {page} as a public page de-cached on purpose"
        assert "PUBLIC" in comment, "the comment does not say these carve-outs are public"

    def test_the_comment_states_the_cost_of_de_caching_them(self):
        """A deliberate cost that is not written down reads as an oversight."""
        comment = _comment_above("/app/*").lower()
        assert "origin" in comment and "index.html" in comment, (
            "the comment must say what de-caching the public pages costs: every anonymous "
            "visitor reaches the origin for the static index.html shell"
        )


class TestAntiGoals:
    """The change adds three behaviours. It must not move anything else."""

    @pytest.mark.parametrize(("path", "policy"), sorted(STILL_CACHED.items()))
    def test_unrelated_paths_keep_their_policy(self, path: str, policy: str):
        pattern, actual = _resolve(path)
        assert actual == policy, f"{path} now resolves to the {pattern!r} behaviour with policy {actual!r}"

    def test_the_default_behaviour_still_uses_the_html_policy(self):
        """The fix is "these paths stop matching the html policy", never "the
        html policy caches less" — that would trade one page's correctness for
        every page's cost.
        """
        assert _default_behaviour().get("cache_policy_id") == HTML_POLICY


class TestTheCommentTellsTheTruth:
    """Prose that asserts a property is the same defect surface as code that
    does. The comment above these blocks is the only place a reader learns why
    three behaviours exist for paths that already send `no-store`.
    """

    def test_the_comment_cites_the_incident_and_the_origin_side_fix(self):
        comment = _comment_above("/app/*")
        for citation in ("2026-09-01", "#1767", "#1768"):
            assert citation in comment, (
                f"the comment above the session behaviours does not cite {citation!r}; "
                f"the next reader cannot tell these blocks from redundant belt-and-braces"
            )

    def test_the_comment_says_why_the_origin_header_is_not_enough(self):
        comment = _comment_above("/app/*").lower()
        assert "no-store" in comment and "not sufficient" in comment, (
            "the comment must say that #1767's nginx `no-store` is necessary but not "
            "sufficient — without that, deleting these blocks looks like removing duplication"
        )

    def test_the_residual_gap_is_named(self):
        """`^~ /app` in nginx is a prefix match, so `/appfoo` is gated and is
        NOT covered by `/app` or `/app/*`. A guard that pretends otherwise is
        worse than the gap.
        """
        assert "/appfoo" in _comment_above("/app/*"), "the comment does not name what these patterns leave uncovered"
