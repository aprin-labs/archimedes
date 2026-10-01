# Applying a CloudFront cache-behaviour change

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

A change to `infra/cloudfront.tf`'s cache behaviours does **nothing** when the PR merges.
CI never runs `terraform apply` — `infra-gate.yml` is `fmt -check` + `validate` only,
deliberately (a plan needs credentials and reads a state file that holds a private key).
Until someone runs `infra/apply.sh --apply`, the merged file describes an edge configuration
that is not live, and the tests that guard it are asserting about the *repo*, not about
production.

Since #1799 CI *can* run a plan, in one place: `terraform-drift.yml`, a separate advisory
workflow on a separate read-only role, which reports drift and still applies nothing. See
[`terraform-apply-and-task-definition-ownership.md`](terraform-apply-and-task-definition-ownership.md)
for that and for the ECS task-definition ownership split, which changes what an `infra/ecs.tf`
edit means but leaves everything on this page intact.

This runbook is the step between those two facts. It carries one section per change —
the applied ones kept as worked examples, the pending one first in the operator's mind —
and then generalises. Today: **#1768 applied**, **#1776 pending**.

## Applied: issue #1768 — `/app` and `/sign-in*` on CachingDisabled

**Live as of 2026-09-03.** Verified read-only, no apply involved:

```bash
aws cloudfront get-distribution-config --id "$(cd infra && terraform output -raw cloudfront_distribution_id)" \
  --query 'DistributionConfig.CacheBehaviors.Items[].PathPattern' --output text
# /health  /health/*  /api/*  /events/*  /assets/*  /static/*  /app/*  /app  /sign-in*  *.js  *.css
```

Kept below as the worked example the generalised section refers back to.

**What merged.** Three `ordered_cache_behavior` blocks — `/app/*`, `/app`, `/sign-in*` —
bound to `data.aws_cloudfront_cache_policy.caching_disabled`, the `all_viewer` origin
request policy, and the same response headers policy the default behaviour uses. They sit
after `/static/*` and ahead of `*.js` / `*.css`.

`/app/*` and `/app` cover more than the gated pages. The anonymous-browse carve-outs —
bare `/app`, `/app/explore`, `/app/corpus` (#1753, the owner's call, which
narrowed #1194 revision d by gating `/app/leaderboard` and `/app/strategy/*`) — are
public and ungated at nginx, and they come off the 60s `html` policy too. That is the owner's ruling (2026-09-01, on the review of PR #1772), not an
oversight: it costs an origin hit per anonymous visitor for a ~4 KB static shell, and it
buys the guarantee that promoting a carve-out to gated later cannot reintroduce the cached
anonymous 302.

**Why it is not already fixed.** PR #1767 shipped `Cache-Control: private, no-store` from
nginx on the gated `/app` locations and the `@sign_in` redirect, and that is live — the
2026-09-01 ping-pong is stopped today. The behaviours close the structural half: the origin
header is opt-in per response, in a different file, and any new gated path (or an edit that
drops the header) puts the edge back in the business of replaying one viewer's redirect to
another.

### 1. Preconditions

```bash
aws sts get-caller-identity     # must be account 037613907429
ls infra/terraform.tfvars       # must exist — apply.sh refuses without it
```

The guards that describe the intended end state should be green on the merge commit:

```bash
cd backend && pytest tests/test_cloudfront_session_paths_uncached.py \
                     tests/test_cloudfront_health_uncached.py \
                     tests/test_nginx_gated_responses_uncached.py -q
```

### 2. Plan, and read it

```bash
infra/apply.sh          # plan is the default; --apply is a separate, later step
```

**Expected:** exactly three new cache behaviours on the distribution and nothing else.

One rendering caveat, so it is not mistaken for scope creep: the three blocks are inserted
in the *middle* of the behaviour list, ahead of `*.js` / `*.css`. Terraform may render that
index shift as changes to the trailing behaviours rather than as three clean insertions. If
it does, check that every rendered change is a re-ordering only — the `path_pattern`,
`cache_policy_id` and `origin_request_policy_id` values before and after must be the same
set. Three new patterns appear; no existing pattern's policy changes.

```
  # aws_cloudfront_distribution.main will be updated in-place
  ~ resource "aws_cloudfront_distribution" "main" {
      + ordered_cache_behavior {
          + path_pattern               = "/app/*"
          + cache_policy_id            = "<the Managed-CachingDisabled id>"
          ...
        }
      + ordered_cache_behavior { + path_pattern = "/app"      ... }
      + ordered_cache_behavior { + path_pattern = "/sign-in*" ... }
    }

Plan: 0 to add, 1 to change, 0 to destroy.
```

Read the summary line first: **`1 to change`, nothing to add, nothing to destroy**, and the
one resource is `aws_cloudfront_distribution.main`. Any other resource in the plan — an
ECS task definition, a security group, an ACM certificate — means the working tree is
carrying something other than this change, or `terraform.tfvars` drifted. Stop and find out
which; do not apply through it.

### 3. Apply

```bash
infra/apply.sh --apply      # interactive confirmation; --yes skips it, don't
```

The distribution goes to `InProgress` and takes roughly 3–5 minutes to reach `Deployed`
across the edge:

```bash
aws cloudfront get-distribution --id "$(cd infra && terraform output -raw cloudfront_distribution_id)" \
  --query 'Distribution.Status' --output text
```

### 4. Purge what the old behaviour already cached

The new behaviour stops the edge caching *new* responses. Objects cached under the old
default behaviour can still be served until they age out, so invalidate them once:

```bash
DIST=$(cd infra && terraform output -raw cloudfront_distribution_id)
aws cloudfront create-invalidation --distribution-id "$DIST" \
  --paths '/app*' '/app/*' '/sign-in*'
```

`/app*`, not `/app` — the `html` cache policy sets `query_string_behavior = "all"`, so every
distinct query string is a distinct cache entry and an un-wildcarded path invalidates only
the bare URL. (Same reasoning as `deploy.yml`'s "Invalidate CloudFront" step. The incident
mitigation on 2026-09-01 was invalidation `I9XCS33QBAVEUFMJRO9R0NJLAU` on these paths.)

### 5. Verify against production

```bash
# Anonymous: gated /app must 302 and must NEVER be a cache hit, on any repeat.
for i in 1 2 3; do
  curl -sSI https://archimedes-arc.com/app/generate | grep -iE '^HTTP/|^x-cache|^cache-control'
done
```

Expected on every iteration: `302`, `x-cache: Miss from cloudfront` (CachingDisabled never
produces a `Hit`), and `cache-control: private, no-store` from nginx (#1767 — both halves
should now be visible on the same response). A `Hit from cloudfront` on any repeat means the
apply did not take or an invalidation is still propagating.

Then the public carve-out, which must also be a `Miss` — deliberately, per the ruling
above. Twice, because one `Miss` proves nothing:

```bash
for i in 1 2; do
  curl -sI https://archimedes-arc.com/app/explore | grep -iE '^HTTP/|^x-cache'
done
```

Expected on both: `200` and `x-cache: Miss from cloudfront`. A `Hit` on the second means a
carve-out has been put back on the `html` policy — check
`backend/tests/test_cloudfront_session_paths_uncached.py::TestThePublicCarveOutsAreDeCachedOnPurpose`,
which exists to make that a red test rather than a surprise here.

Then the real check, which needs a browser: sign in at `https://archimedes-arc.com/sign-in`
and confirm you land on `/app/...` once, without the address bar bouncing. The failure this
closes is a loop, not a status code.

### 6. If it goes wrong

The dangerous failure mode is not "still cached" — it is **`/app` signed-out for everyone**,
which happens if the behaviours end up without the `all_viewer` origin request policy:
CloudFront then forwards no cookies, `auth_request /_auth_session` sees an anonymous request
from every viewer, and every signed-in user is bounced to `/sign-in`.

```bash
git revert <merge sha> && infra/apply.sh && infra/apply.sh --apply
```

Reverting restores the previous behaviour list; the distribution converges in the same 3–5
minutes. The 2026-09-01 defect is *not* reintroduced by the revert on its own, because
#1767's origin `no-store` is a separate change in the nginx image and stays live.

## Pending: issue #1776 — images, icons and fonts off the 60s html behaviour

**What merged.** Seven `ordered_cache_behavior` blocks bound to
`aws_cloudfront_cache_policy.static_assets` (1h default TTL, 1d max, cache key with
`cookie_behavior`, `header_behavior` and `query_string_behavior` all `"none"`):

| pattern | position | covers |
|---|---|---|
| `/fonts/*` | after `/static/*`, before `/app/*` | the self-hosted `.woff2` files and the OFL licence `.txt` beside them |
| `*.png` `*.svg` `*.jpg` `*.webp` `*.woff2` `*.ico` | **last**, after `*.js` / `*.css` | `/og-image.png`, `/product-workspace.png`, the favicons and PWA icons |

**Why the suffix patterns are last, and why that is the only thing in this change that
can hurt.** A suffix pattern matches any path with that suffix — `/app/hero.png`
included. Ordered ahead of the `/app` behaviours it would bind a gated page to a 1h
cookie-blind cache: #1768's defect with a longer TTL. Nothing is served under
`/app/*.png` today, which is exactly why the order has to be right before someone puts
one there. `backend/tests/test_cloudfront_static_assets_cached.py::TestOrdering` and
`test_cloudfront_session_paths_uncached.py::TestOrdering` both fail if it moves.

**What it was doing before.** Measured on the live site, 2026-09-03:

```
$ curl -sSI https://archimedes-arc.com/og-image.png
HTTP/2 200
content-length: 69408
x-cache: Hit from cloudfront
age: 12
                       # ...and NO cache-control header from the origin at all
```

nginx serves these off disk with no `Cache-Control`, so nothing overrode the behaviour's
policy — and the behaviour was `default_cache_behavior`, i.e. the `html` policy's
`default_ttl = 60`. `age` can never exceed 60 on that policy. `/product-workspace.png`
is 135,993 bytes; that is the number being re-fetched from every edge POP once a minute.

### 1. Preconditions

```bash
aws sts get-caller-identity     # must be account 037613907429
ls infra/terraform.tfvars       # must exist — apply.sh refuses without it
```

```bash
cd backend && pytest tests/test_cloudfront_static_assets_cached.py \
                     tests/test_cloudfront_session_paths_uncached.py \
                     tests/test_cloudfront_health_uncached.py -q
```

### 2. Plan, and read it

```bash
infra/apply.sh          # plan is the default; --apply is a separate, later step
```

**Expected:** `Plan: 0 to add, 1 to change, 0 to destroy`, the one resource being
`aws_cloudfront_distribution.main`, and the behaviour list going from **11 patterns to
18** — the eleven above plus `/fonts/*`, `*.png`, `*.svg`, `*.jpg`, `*.webp`, `*.woff2`,
`*.ico`. No existing pattern changes policy.

**The mid-list-insert caveat is not hypothetical here — read this before the plan, not
after.** A read-only `terraform plan -target=aws_cloudfront_distribution.main -lock=false`
against live state on 2026-09-03 rendered the seven insertions as **five `~` blocks and
seven `+` blocks**, because `/fonts/*` lands at the index `/app/*` currently occupies and
everything after it shifts down:

```
      ~ ordered_cache_behavior {
          ~ allowed_methods            = [ - "DELETE", - "PATCH", - "POST", - "PUT", ... ]
          ~ cache_policy_id            = "4135ea2d-…" -> "8cbade1c-…"
          ~ compress                   = false -> true
          ~ path_pattern               = "/app/*" -> "/fonts/*"
        }
      ~ ordered_cache_behavior { ~ path_pattern = "/app"      -> "/app/*"    }
      ~ ordered_cache_behavior { ~ path_pattern = "/sign-in*" -> "/app"      }
      ~ ordered_cache_behavior { ~ path_pattern = "*.js"      -> "/sign-in*" ; … }
      ~ ordered_cache_behavior { ~ path_pattern = "*.css"     -> "*.js"      }
      + ordered_cache_behavior { + path_pattern = "*.css"   … }
      + ordered_cache_behavior { + path_pattern = "*.png"   … }
      + ordered_cache_behavior { + path_pattern = "*.svg"   … }
      + ordered_cache_behavior { + path_pattern = "*.jpg"   … }
      + ordered_cache_behavior { + path_pattern = "*.webp"  … }
      + ordered_cache_behavior { + path_pattern = "*.woff2" … }
      + ordered_cache_behavior { + path_pattern = "*.ico"   … }

Plan: 0 to add, 1 to change, 0 to destroy.
```

Read that as a **re-indexing, not a rewrite**. The first `~` block looks alarming — it
appears to move `/app/*` off CachingDisabled onto `static_assets` — but the `+ *.css` and
`+ *.js`-shaped blocks lower down put the shifted patterns back. The check that matters is
on the SETS, not the diff hunks:

- before: `/health /health/* /api/* /events/* /assets/* /static/* /app/* /app /sign-in* *.js *.css`
- after: the same eleven, **plus** the seven new patterns, each `(pattern → policy)` pair
  unchanged for the eleven.

Get the "before" list without touching anything:

```bash
aws cloudfront get-distribution-config --id "$(cd infra && terraform output -raw cloudfront_distribution_id)" \
  --query 'DistributionConfig.CacheBehaviors.Items[].PathPattern' --output text
```

If the summary line is anything but `1 to change` on `aws_cloudfront_distribution.main`,
stop — the working tree is carrying something other than this change.

### 3. Apply

```bash
infra/apply.sh --apply      # interactive confirmation; --yes skips it, don't
```

Roughly 3–5 minutes to `Deployed`:

```bash
aws cloudfront get-distribution --id "$(cd infra && terraform output -raw cloudfront_distribution_id)" \
  --query 'Distribution.Status' --output text
```

### 4. Invalidation — deliberately NOT needed here

Unlike #1768, **do not invalidate.** That change had to purge poisoned entries; this one
only lengthens a TTL. Every object cached under the old behaviour was cached by the `html`
policy, so it expires on its own within 60 seconds, and the new behaviour keys differently
anyway (`query_string_behavior` goes `"all"` → `"none"`), so post-apply requests miss into
a fresh key space regardless. Spending an invalidation path here buys nothing.

**The cost this change does introduce, and the one thing to remember later:** these assets
are now cached at the edge for an hour, and `deploy.yml`'s invalidation is deliberately
narrow — `/`, `/?*`, `/index.html*`. It does **not** cover them. If a deploy changes
`ui/public/og-image.png`, `product-workspace.png`, a favicon or a font *in place*, the edge
serves the old bytes for up to an hour. Either rename the file (the `/assets/*` bundles get
this for free via content hashing) or add its path to that deploy's invalidation.

### 5. Verify against production

`age` is the decisive signal, and it is a clean one: under the old `html` policy
(`max_ttl = 60`) an `age` above 60 was impossible. Request twice, then once more after a
minute:

```bash
curl -sSI https://archimedes-arc.com/og-image.png            | grep -iE '^HTTP/|^x-cache|^age'
curl -sSI https://archimedes-arc.com/product-workspace.png   | grep -iE '^HTTP/|^x-cache|^age'
sleep 90
curl -sSI https://archimedes-arc.com/og-image.png            | grep -iE '^HTTP/|^x-cache|^age'
curl -sSI https://archimedes-arc.com/fonts/gabarito-latin.woff2 | grep -iE '^HTTP/|^x-cache|^age'
```

Expected: `200`, `x-cache: Hit from cloudfront`, and on the third request an `age` that has
kept climbing **past 60**. An `age` that resets below 60 on every check means the apply did
not take and these paths are still on the default behaviour.

Then the check that the ordering held — the failure this change could actually cause:

```bash
for i in 1 2; do curl -sSI https://archimedes-arc.com/app/generate | grep -iE '^HTTP/|^x-cache'; done
for i in 1 2; do curl -sSI https://archimedes-arc.com/app/explore  | grep -iE '^HTTP/|^x-cache'; done
```

Expected on every iteration: `Miss from cloudfront` — unchanged from #1768's verification.
A `Hit` on `/app/*` means a suffix pattern got ordered in front of the gated behaviours,
which is #1768 reopened with a 1h TTL. Roll back immediately (below) rather than debugging
it live.

### 6. If it goes wrong

There is no signed-out-`/app` failure mode in this change: the new behaviours carry the
same `all_viewer` origin request policy as `/assets/*`, and none of them is ordered in
front of a gated path. The realistic failures are (a) a gated path answering `Hit`, per the
check above, and (b) a stale asset after an in-place file change, which is the named cost
in step 4, not a fault.

```bash
git revert <merge sha> && infra/apply.sh && infra/apply.sh --apply
```

Reverting drops the seven behaviours; the assets fall back to the 60s `html` policy, which
is where they were before this change. Objects already cached for an hour keep serving
until they age out — invalidate them if the revert was for stale content:

```bash
DIST=$(cd infra && terraform output -raw cloudfront_distribution_id)
aws cloudfront create-invalidation --distribution-id "$DIST" \
  --paths '/og-image.png*' '/product-workspace.png*' '/fonts/*'
```


## Generalising

For any future cache-behaviour edit, the shape is the same and only step 2's expectation
changes:

1. State the expected plan **before** running it — how many behaviours, on which resource,
   and as a *set* of `(path_pattern → cache_policy_id)` pairs. Terraform renders a mid-list
   insert as edits to the trailing behaviours, so the diff hunks will not match the sentence
   even when the outcome does (#1776, step 2, has the rendered example).
2. Plan, and compare against that sentence rather than skimming for red text.
3. Apply, wait for `Deployed`.
4. Invalidate the paths whose *old* cached copies are now wrong (wildcard them if the cache
   policy forwards query strings). A change that only **lengthens** a TTL needs no
   invalidation — the old entries expire under the old, shorter TTL on their own (#1776,
   step 4). A change that de-caches or re-keys a path that was being served wrongly does
   (#1768, step 4). Ask which of the two you have before spending a path.
5. Check what the new TTL takes *out* of `deploy.yml`'s invalidation reach: anything cached
   longer than the deploy cycle and served from an unhashed filename is now yours to purge
   by hand when it changes.
5. Verify from outside: `x-cache` on repeat requests, then the user journey the behaviour
   exists to protect.

Two properties of this file are load-bearing enough to be guarded rather than trusted, both
by text-parsing tests that need no AWS: the behaviour list's **order** (CloudFront takes the
first matching pattern, not the most specific) and each behaviour's **cache policy id**.
`terraform validate` accepts a behaviour bound to the wrong policy or ordered behind a
pattern that swallows it — see `backend/tests/test_cloudfront_session_paths_uncached.py`,
`backend/tests/test_cloudfront_health_uncached.py` and
`backend/tests/test_cloudfront_static_assets_cached.py`. The last of those also enumerates
`ui/public/` from disk, so a new asset type added to the site is a red test rather than a
file that quietly rides the 60s policy.
