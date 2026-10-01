"""Public static assets must not ride the 60s cookie-blind `html` behaviour (#1776).

Found while verifying PR #1772. CloudFront resolves a request against the FIRST
`ordered_cache_behavior` whose pattern matches, and before this change no
pattern in `infra/cloudfront.tf` matched the images, icons or webfonts under
`ui/public/`. They fell through to `default_cache_behavior` and the `html` cache
policy — `default_ttl = 60`, `query_string_behavior = "all"`::

    /og-image.png            the Open Graph card, fetched by every link unfurl
    /product-workspace.png   136 KB — the largest single asset on the landing page
    /fonts/gabarito-latin.woff2   preloaded from ui/index.html, critical render path

Two consequences, and the second is the one that outlives the cost argument:

1. **Cost.** The 136 KB PNG was re-fetched from the origin every 60s per edge
   POP, once per distinct query string on top of that, for a file that only
   changes when the build does.
2. **Hygiene.** That 60s policy is the cookie-blind one that carried #1767's
   cache-poisoning shape (`cookie_behavior = "none"` on a response that can
   depend on the session). Keeping non-HTML off it is worth doing on its own
   terms, independent of bytes saved.

The fix is seven `ordered_cache_behavior` blocks bound to
`aws_cloudfront_cache_policy.static_assets` — 1h default TTL and a cache key
into which no viewer cookie, header or query string enters (the policy's
accept-encoding normalisation still splits it by compression variant, as it
already does for `/assets/*`) — the same policy `/assets/*`, `/static/*`,
`*.js` and `*.css` already use.

**Ordering is the load-bearing half.** A suffix pattern matches ANY path with
that suffix, `/app/hero.png` included. Bound ahead of the `/app` behaviours it
would put a gated page on a 1h cookie-blind cache: #1768's defect with a longer
TTL. `TestOrdering` pins that the six suffix blocks sit behind every
CachingDisabled behaviour, because `terraform validate` accepts either order and
a diff that only shows added lines makes position invisible.

This suite reads `infra/cloudfront.tf` as text, reusing the reader and the
CloudFront first-match resolver from the sibling guards
``test_cloudfront_health_uncached.py`` (#1520) and
``test_cloudfront_session_paths_uncached.py`` (#1768) rather than growing a
third copy of them. Hermetic: two directories in the repo, no AWS, no terraform
binary, no network, no DB.

`TestEveryPublicFileHasAPolicyOnPurpose` enumerates `ui/public/` from disk
rather than hard-coding a list, so an asset type added later — a `.avif` hero,
a `.gif`, a `.jpeg` — is a named failure here on the day it is added instead of
a silent 60s TTL nobody measures.

MUTATION: delete the ``*.png`` behaviour → ``test_the_behaviour_is_declared``
and the resolution assertions for ``/og-image.png``,
``/product-workspace.png`` and every favicon go red naming the file.

MUTATION: move the suffix group ahead of ``/app/*`` → ``TestOrdering`` goes red
and ``/app/hero.png`` is reported as resolving to ``static_assets``.

Run:
    /path/to/env/bin/pytest backend/tests/test_cloudfront_static_assets_cached.py -q
"""

from __future__ import annotations

import re

import pytest

from tests.test_cloudfront_health_uncached import (
    CACHING_DISABLED,
    HTML_POLICY,
    REPO_ROOT,
    STATIC_POLICY,
    _attrs,
    _default_behaviour,
    _header_comment,
    _ordered_behaviours,
    _pattern_matches,
    _resolve,
    _resource_span,
    _tf_source,
)
from tests.test_cloudfront_session_paths_uncached import (
    ALL_VIEWER,
    SECURITY_HEADERS,
    _by_pattern,
    _comment_above,
    _index_of,
)

# Vite copies `ui/public/**` to the build root verbatim, so a file's URL path is
# "/" + its path relative to this directory. That mapping is the reason this
# suite can enumerate the real asset set instead of trusting a hand-kept list.
PUBLIC_DIR = REPO_ROOT / "ui" / "public"

STATIC_ASSETS_POLICY = 'resource "aws_cloudfront_cache_policy" "static_assets"'

# The behaviours this issue adds. Spelled out as literals, not derived from the
# file, so deleting a block is a failure rather than a quietly smaller set.
PREFIX_PATTERNS = ("/fonts/*",)
SUFFIX_PATTERNS = ("*.png", "*.svg", "*.jpg", "*.webp", "*.woff2", "*.ico")
NEW_PATTERNS = PREFIX_PATTERNS + SUFFIX_PATTERNS

# The behaviour every new block is modelled on. Its attributes are read from the
# file rather than restated here, so "matches /assets/*" cannot drift into
# "matched /assets/* when this was written".
TEMPLATE_PATTERN = "/assets/*"

# The three paths the issue names, measured against the live file on main.
ISSUE_PATHS = ("/og-image.png", "/product-workspace.png", "/fonts/gabarito-latin.woff2")

# Never-cached behaviours the suffix patterns must stay BEHIND. A suffix pattern
# ordered in front of any of these captures the path with that suffix under it.
MUST_PRECEDE_SUFFIXES = ("/health", "/health/*", "/api/*", "/events/*", "/app/*", "/app", "/sign-in*")

# Concrete gated URLs that end in a newly-patterned suffix. None of these exists
# today — that is the point: the ordering has to be right BEFORE someone adds one.
GATED_ASSET_PATHS = (
    "/app/hero.png",
    "/app/generate/chart.svg",
    "/app/library/icon.webp",
    "/sign-in-illustration.png",
    "/api/render/card.png",
    "/health/probe.ico",
)

# Files under ui/public/ that stay on the 60s `html` policy deliberately. They
# are rewritten by the build, they are small, and 60s is the right staleness for
# a file a crawler or a wallet re-reads. Asserted to still EXIST as well as to
# still resolve to `html`, so a rename cannot leave a stale exemption behind.
STILL_HTML_URLS = frozenset(
    {
        "/robots.txt",
        "/llms.txt",
        "/sitemap.xml",
        "/site.webmanifest",
        "/.well-known/agent.json",
        "/.well-known/agent-registration.json",
        "/.well-known/agent-registration.domain.json",
    }
)

# Anti-goal: this change adds behaviours, it must not move anything else.
STILL_UNCHANGED = {
    "/": HTML_POLICY,
    "/index.html": HTML_POLICY,
    "/explore": HTML_POLICY,
    "/app/generate": CACHING_DISABLED,
    "/sign-in": CACHING_DISABLED,
    "/health": CACHING_DISABLED,
    "/api/strategies": CACHING_DISABLED,
    "/events/stream": CACHING_DISABLED,
    "/assets/index-C4f9aB2x.js": STATIC_POLICY,
    "/static/thing.txt": STATIC_POLICY,
}

# CloudFront's default quota is 25 cache behaviours per distribution (the
# default behaviour plus 24 ordered ones). Every suffix pattern spends one, so
# the "just add a pattern for it" reflex has a ceiling worth seeing in a test.
CLOUDFRONT_BEHAVIOUR_QUOTA = 25


def _public_urls() -> list[str]:
    """Every file under `ui/public/`, as the URL path it is served at."""
    assert PUBLIC_DIR.is_dir(), f"missing {PUBLIC_DIR}"
    return sorted("/" + f.relative_to(PUBLIC_DIR).as_posix() for f in PUBLIC_DIR.rglob("*") if f.is_file())


def _expected_policy(url: str) -> str:
    return HTML_POLICY if url in STILL_HTML_URLS else STATIC_POLICY


class TestTheReaderIsNotVacuous:
    """Guards the guard. Every assertion below is conditional on the shared
    reader finding blocks and on `ui/public/` being enumerable; a silently-empty
    parse or an empty directory walk would make this file pass against anything.
    """

    def test_the_reader_finds_every_behaviour(self):
        behaviours = _ordered_behaviours()
        assert len(behaviours) >= 18, f"only parsed {len(behaviours)} ordered_cache_behavior blocks"

    def test_patterns_are_distinct_and_well_formed(self):
        patterns = [b["path_pattern"] for b in _ordered_behaviours()]
        assert len(patterns) == len(set(patterns)), f"duplicate path_pattern: {patterns}"
        assert all(p.startswith(("/", "*")) for p in patterns), patterns

    def test_the_public_directory_walk_finds_the_assets(self):
        urls = _public_urls()
        assert len(urls) >= 15, urls
        for named in ISSUE_PATHS:
            assert named in urls, f"{named} is gone from ui/public/ — the issue's premise moved"

    def test_the_template_behaviour_still_exists(self):
        """Every "matches /assets/*" assertion below is vacuous without it."""
        assert TEMPLATE_PATTERN in _by_pattern(), f"{TEMPLATE_PATTERN!r} behaviour was removed"

    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            ("*.png", "/og-image.png", True),
            ("*.png", "/app/hero.png", True),  # a suffix pattern spans `/` — the ordering hazard
            ("*.png", "/og-image.png.txt", False),
            ("*.jpg", "/photo.jpeg", False),  # .jpeg is NOT covered by .jpg — named residual
            ("/fonts/*", "/fonts/gabarito-latin.woff2", True),
            ("/fonts/*", "/fonts/OFL-Gabarito.txt", True),
            ("/fonts/*", "/fonts", False),  # trailing `/` is literal
        ],
    )
    def test_the_pattern_matcher_models_cloudfront(self, pattern: str, path: str, expected: bool):
        assert _pattern_matches(pattern, path) is expected


class TestTheStaticBehavioursAreDeclared:
    """Fails against `main` before #1776: none of the seven patterns existed, so
    every image, icon and webfont resolved to `<default_cache_behavior>` and the
    `html` policy's 60s, cookie-blind, query-string-split TTL.
    """

    @pytest.mark.parametrize("pattern", NEW_PATTERNS)
    def test_the_behaviour_is_declared(self, pattern: str):
        assert pattern in _by_pattern(), (
            f"no ordered_cache_behavior for {pattern!r}. Files matching it are immutable "
            f"per build and are not HTML, but they fall through to the default behaviour's "
            f"60s html policy — the same cookie-blind policy #1767 was cached on."
        )

    @pytest.mark.parametrize("pattern", NEW_PATTERNS)
    def test_the_behaviour_binds_the_long_ttl_policy(self, pattern: str):
        policy = _by_pattern().get(pattern, {}).get("cache_policy_id")
        assert policy == STATIC_POLICY, (
            f"{pattern!r} is bound to {policy!r}, not {STATIC_POLICY}. Declaring the "
            f"behaviour and pointing it at the short-TTL policy is worse than not "
            f"declaring it: it reads as the fix while the edge still re-fetches every 60s."
        )

    @pytest.mark.parametrize("pattern", NEW_PATTERNS)
    def test_the_behaviour_mirrors_the_assets_behaviour(self, pattern: str):
        """`/assets/*` is the existing, reviewed shape for a cached static path.

        Read from the file rather than restated, so if that behaviour is
        hardened later the new blocks are held to the same bar instead of
        silently keeping the old one.
        """
        template = _by_pattern()[TEMPLATE_PATTERN]
        actual = _by_pattern().get(pattern, {})
        for attribute in (
            "target_origin_id",
            "viewer_protocol_policy",
            "allowed_methods",
            "cached_methods",
            "cache_policy_id",
            "origin_request_policy_id",
            "response_headers_policy_id",
            "compress",
        ):
            assert actual.get(attribute) == template[attribute], (
                f"{pattern!r} sets {attribute}={actual.get(attribute)!r}; "
                f"{TEMPLATE_PATTERN!r} sets {template[attribute]!r}"
            )

    @pytest.mark.parametrize("pattern", NEW_PATTERNS)
    def test_the_security_headers_reach_these_responses(self, pattern: str):
        """Named separately from the mirror test above because it is the one
        attribute whose absence is invisible in a browser until an audit runs:
        a behaviour with no response headers policy drops HSTS and
        frame-options from every asset it serves.
        """
        assert _by_pattern().get(pattern, {}).get("response_headers_policy_id") == SECURITY_HEADERS

    @pytest.mark.parametrize("pattern", NEW_PATTERNS)
    def test_the_origin_still_sees_a_full_request_on_a_miss(self, pattern: str):
        assert _by_pattern().get(pattern, {}).get("origin_request_policy_id") == ALL_VIEWER


class TestTheCacheKeyIsCookieBlindOnPurpose:
    """The policy's NAME is not its behaviour.

    A 1h TTL is only safe on a key that ignores cookies because these paths are
    the same bytes for every viewer. If `static_assets` were ever edited to key
    on cookies, the TTL would stop being the point and the cache-hit rate would
    collapse; if it were edited to key on a header the responses do not vary by,
    the same. Pinned here rather than trusted.
    """

    @pytest.mark.parametrize(
        ("attribute", "value"),
        [
            ("cookie_behavior", "none"),
            ("header_behavior", "none"),
            ("query_string_behavior", "none"),
        ],
    )
    def test_the_key_ignores_cookies_headers_and_query_strings(self, attribute: str, value: str):
        start, end = _resource_span(STATIC_ASSETS_POLICY)
        body = _tf_source()[start:end]
        assert re.search(rf'{attribute}\s*=\s*"{value}"', body), (
            f"aws_cloudfront_cache_policy.static_assets no longer sets {attribute} = {value!r}; "
            f"the 1h TTL these behaviours hand out assumes a key that varies by path alone"
        )

    @pytest.mark.parametrize(("ttl", "value"), [("default_ttl", "3600"), ("max_ttl", "86400")])
    def test_the_ttls_are_the_ones_the_issue_asked_for(self, ttl: str, value: str):
        start, end = _resource_span(STATIC_ASSETS_POLICY)
        assert _attrs(_tf_source()[start:end])[ttl] == value


class TestEveryPublicFileHasAPolicyOnPurpose:
    """Enumerated from `ui/public/`, not hard-coded.

    A hand-kept list of asset paths goes stale the first time someone adds a
    hero image, and the failure mode of a stale list is silence: the new file
    rides the 60s policy and nothing says so. Walking the directory means the
    guard fails, by name, on the commit that adds the file — and the fix is
    either a new pattern or an entry in `STILL_HTML_URLS`, which is a decision
    someone has to write down.
    """

    @pytest.mark.parametrize("url", _public_urls())
    def test_the_file_resolves_to_the_policy_it_should(self, url: str):
        expected = _expected_policy(url)
        pattern, actual = _resolve(url)
        assert actual == expected, (
            f"{url} resolves to the {pattern!r} behaviour with cache_policy_id {actual!r}, "
            f"expected {expected!r}. Either add an ordered_cache_behavior for its suffix, "
            f"or add it to STILL_HTML_URLS with a reason — a public file on the 60s "
            f"cookie-blind html policy by accident is exactly what #1776 is about."
        )

    @pytest.mark.parametrize("url", ISSUE_PATHS)
    def test_the_three_paths_the_issue_names_are_cached(self, url: str):
        """The regression the issue was filed for, stated in its own words."""
        pattern, policy = _resolve(url)
        assert policy == STATIC_POLICY, f"{url} resolves to {pattern!r} with policy {policy!r}"

    def test_no_exemption_outlives_its_file(self):
        """A `STILL_HTML_URLS` entry for a file that no longer exists is a
        licence nobody is using — and it hides the fact that the exemption list
        stopped describing the repo.
        """
        orphans = sorted(STILL_HTML_URLS - set(_public_urls()))
        assert not orphans, f"STILL_HTML_URLS names files that are gone from ui/public/: {orphans}"

    def test_the_fonts_directory_is_covered_wholesale(self):
        """`/fonts/*` is a prefix, not a suffix, precisely so the OFL licence
        `.txt` files beside the fonts do not need `*.txt` — which would also
        capture `/robots.txt` and `/llms.txt`.
        """
        fonts = [u for u in _public_urls() if u.startswith("/fonts/")]
        assert len(fonts) >= 5, fonts
        assert any(u.endswith(".txt") for u in fonts), "no OFL licence file — the premise moved"
        for url in fonts:
            pattern, policy = _resolve(url)
            assert (pattern, policy) == ("/fonts/*", STATIC_POLICY), f"{url} → {pattern!r} / {policy!r}"


class TestOrdering:
    """CloudFront takes the FIRST matching behaviour, not the most specific.

    This is the half `terraform validate` cannot see and a diff of added lines
    does not show. A suffix pattern moved above the `/app` behaviours binds
    every gated path with that suffix to a 1h cookie-blind cache — #1768 again,
    with a longer TTL.
    """

    @pytest.mark.parametrize("pattern", SUFFIX_PATTERNS)
    @pytest.mark.parametrize("anchor", MUST_PRECEDE_SUFFIXES)
    def test_the_suffix_patterns_stay_behind_every_uncached_behaviour(self, pattern: str, anchor: str):
        assert _index_of(anchor) < _index_of(pattern), (
            f"{pattern!r} is ordered in front of {anchor!r}, so a {pattern[1:]} under "
            f"{anchor!r} would be cached for an hour on a key that ignores the session cookie"
        )

    @pytest.mark.parametrize("path", GATED_ASSET_PATHS)
    def test_a_gated_path_with_an_asset_suffix_is_still_never_cached(self, path: str):
        """The ordering test above states the rule; this states the consequence
        in the terms an operator would check it in.
        """
        pattern, policy = _resolve(path)
        assert policy == CACHING_DISABLED, (
            f"{path} resolves to the {pattern!r} behaviour with policy {policy!r}. "
            f"Nothing is served at that URL today — which is why the ordering has to "
            f"be right before someone puts one there."
        )

    def test_the_prefix_behaviour_is_not_dragged_behind_the_suffixes(self):
        """`/fonts/*` cannot collide with a gated path, so it sits with the other
        prefix behaviours rather than at the end. Pinned so a later tidy-up that
        moves all seven blocks together does not quietly reorder the file
        without anyone deciding to.
        """
        for suffix in SUFFIX_PATTERNS:
            assert _index_of("/fonts/*") < _index_of(suffix)

    def test_the_behaviour_count_stays_inside_the_cloudfront_quota(self):
        """One pattern per file type does not scale for free."""
        total = len(_ordered_behaviours()) + 1  # + default_cache_behavior
        assert total <= CLOUDFRONT_BEHAVIOUR_QUOTA, (
            f"{total} cache behaviours; CloudFront's default quota is "
            f"{CLOUDFRONT_BEHAVIOUR_QUOTA} per distribution. Adding one more suffix "
            f"pattern needs a quota increase, not just a commit."
        )


class TestAntiGoals:
    """The change adds seven behaviours. It must not move anything else — least
    of all the never-cached paths it is ordered behind.
    """

    @pytest.mark.parametrize(("path", "policy"), sorted(STILL_UNCHANGED.items()))
    def test_unrelated_paths_keep_their_policy(self, path: str, policy: str):
        pattern, actual = _resolve(path)
        assert actual == policy, f"{path} now resolves to the {pattern!r} behaviour with policy {actual!r}"

    def test_the_default_behaviour_still_uses_the_html_policy(self):
        """The fix is "these paths stop matching the html policy", never "the
        html policy caches longer" — that would hand a 1h TTL to the SPA shell.
        """
        assert _default_behaviour().get("cache_policy_id") == HTML_POLICY

    @pytest.mark.parametrize("pattern", ("/assets/*", "/static/*", "*.js", "*.css"))
    def test_the_preexisting_static_behaviours_are_untouched(self, pattern: str):
        assert _by_pattern()[pattern]["cache_policy_id"] == STATIC_POLICY


class TestTheCommentsTellTheTruth:
    """Prose that asserts a property is the same defect surface as code that
    does (CLAUDE.md § "A guard must be shown to reject something"). The comments
    above these blocks are where a reader learns why a suffix pattern sits at
    the bottom of the list and what it does not cover.
    """

    def test_the_suffix_comment_cites_the_issue_and_the_two_named_files(self):
        comment = _comment_above("*.png")
        assert "#1776" in comment, "the comment does not cite the issue"
        for named in ("/og-image.png", "/product-workspace.png"):
            assert named in comment, f"the comment does not name {named}, the file the issue was filed for"

    def test_the_suffix_comment_explains_why_it_is_ordered_last(self):
        """Without this, the blocks look like they belong beside `/assets/*` and
        the next tidy-up moves them there.
        """
        comment = _comment_above("*.png")
        assert "ORDERED LAST" in comment, "the comment does not say the position is deliberate"
        assert "/app/hero.png" in comment, "the comment does not give the concrete path the ordering protects"
        assert "#1768" in comment, "the comment does not connect the ordering to the outage it prevents"

    def test_the_suffix_comment_names_what_it_does_not_cover(self):
        """A guard that pretends there is no gap is worse than the gap."""
        comment = _comment_above("*.png")
        for uncovered in (".jpeg", ".gif", ".avif", ".woff"):
            assert uncovered in comment, f"the comment does not name {uncovered} as uncovered"
        assert "try_files" in comment, (
            "the comment does not name the nonexistent-path residual: nginx answers "
            "/typo.png with the SPA shell, which this change now caches for an hour"
        )

    def test_the_suffix_comment_states_the_cache_key_is_cookie_blind(self):
        """The 1h TTL is only defensible because the key ignores cookies, and the
        `all_viewer` line one block below reads like it contradicts that.
        """
        comment = _comment_above("*.png")
        assert "cookie_behavior" in comment and "cache key" in comment.lower()

    def test_the_fonts_comment_says_why_it_is_a_prefix(self):
        comment = _comment_above("/fonts/*")
        assert "#1776" in comment
        assert "preload" in comment, "the comment does not say the fonts are on the critical render path"
        assert "OFL" in comment, "the comment does not say why a prefix beats a suffix here"


class TestTheFileHeaderTellsTheTruth:
    """The header comment is the list a reader skims instead of counting blocks."""

    @pytest.mark.parametrize("pattern", NEW_PATTERNS)
    def test_the_header_names_every_newly_cached_pattern(self, pattern: str):
        assert pattern in _header_comment(), (
            f"the file header enumerates the 1h-cached patterns but omits {pattern!r}. "
            f"The next reader trusts a list that has silently gone stale."
        )

    def test_the_header_says_the_suffix_patterns_are_ordered_last(self):
        header = _header_comment()
        assert "LAST" in header, (
            "the header lists the suffix patterns without saying their position is "
            "load-bearing — the one property of this change that can be broken by an edit "
            "that touches none of these lines"
        )
