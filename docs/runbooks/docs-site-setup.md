# Docs Site — S3 + CloudFront at `docs.archimedes-arc.com`

> **Status:** runbook. **Owner:** Dan Browne. **Updated:** 2026-09-01.

`docs/` **and the agent-generated `openwiki/` tree** are built into a static
site by [`mkdocs.yml`](../../mkdocs.yml) (theme: mkdocs-material) and published
to **`docs.archimedes-arc.com`** — an S3 bucket behind a CloudFront
distribution **in our own AWS account**, described by
[`docs-site/infra/main.tf`](../../docs-site/infra/main.tf) and published by
[`.github/workflows/docs-site.yml`](../../.github/workflows/docs-site.yml).

This is issue #1634, Dan's hosting call recorded there on 2026-08-31. It
replaces the earlier GitHub Pages plan (#1381), which never went live: the
domain is in our Route 53 zone, in the account that already terminates TLS and
keeps access logs, and publishing from a third party would have meant a second
control plane and no way to put the docs behind the same edge policy as
everything else. The **build** half is unchanged — `mkdocs build --strict`
still runs on every docs-path push and PR, and is still the gate described at
the bottom of this page.

`openwiki/` lives at the repository root, outside `docs_dir`, so mkdocs cannot
see it by itself; [`.github/scripts/mkdocs_hooks.py`](../../.github/scripts/mkdocs_hooks.py)
mounts it at `/openwiki/` and stamps the agent-generated banner on each page.
The section index, with the full provenance note, is
[`../agent-wiki.md`](../agent-wiki.md).

## What is where

| Piece | Where |
|---|---|
| Bucket, CloudFront distribution, ACM cert, Route 53 alias | [`docs-site/infra/main.tf`](../../docs-site/infra/main.tf) — its own terraform root, its own state key (`docs-site/terraform.tfstate`) |
| Build (`mkdocs build --strict`) | `build` job in [`docs-site.yml`](../../.github/workflows/docs-site.yml) |
| Publish (`aws s3 sync` + CloudFront invalidation) | `deploy` job in the same workflow, on pushes to `main` only |
| CI's AWS identity | the existing OIDC role `archimedes-github-deploy` ([`infra/scripts/setup-github-oidc.sh`](../../infra/scripts/setup-github-oidc.sh)) |
| Terraform `fmt` + `validate` in CI | [`infra-gate.yml`](../../.github/workflows/infra-gate.yml)'s matrix (`docs-site/infra` is one of its roots) |

There is **no repo-variable gate and no console step.** The bucket is the gate:
until the terraform below has been applied, the publish job prints a NO-OP
notice and skips, and the build keeps running as normal.

## Going live (one time)

Everything here runs from a checkout with `AWS_PROFILE=ArchimedesDanAdmin`.

### 1. Apply the terraform

```bash
cd docs-site/infra
terraform init
terraform plan      # read it: ~12 resources, all new, none outside this root
terraform apply
```

ACM validation is DNS-based against the `archimedes-arc.com` zone the same
root reads, so `apply` blocks for a few minutes on
`aws_acm_certificate_validation.docs` and then on the CloudFront distribution
reaching `Deployed`. Expect 15–20 minutes end to end; that is CloudFront, not
a hang.

Outputs you will want:

```bash
terraform output -raw bucket           # archimedes-docs-site-037613907429
terraform output -raw distribution_id
terraform output -raw url              # https://docs.archimedes-arc.com
```

### 2. Grant CI the publish permissions

The publish job authenticates as `archimedes-github-deploy` (OIDC, no stored
keys). That role's inline policy is written by
[`infra/scripts/setup-github-oidc.sh`](../../infra/scripts/setup-github-oidc.sh),
which now includes the docs-site statements (`s3:ListBucket` /
`GetObject` / `PutObject` / `DeleteObject` on the bucket,
`cloudfront:CreateInvalidation` on this account's distributions,
`cloudfront:ListDistributions`). Re-run it to apply them:

```bash
./infra/scripts/setup-github-oidc.sh            # dry run — prints what it would do
./infra/scripts/setup-github-oidc.sh --apply
```

> **Also re-run it after any org/repo rename or transfer.** The same script owns
> the role's *trust* policy, and the OIDC `sub` claim GitHub mints carries the
> repository's current identity — in this repo's case the **id-qualified** form
> (`repo:aprin-labs@284008417/archimedes@1236816811:ref:refs/heads/main`), not
> the plain `repo:ORG/REPO` one. On 2026-09-01 the `a-apin` → `aprin-labs`
> rename broke every deploy for ~3h on
> `Not authorized to perform sts:AssumeRoleWithWebIdentity`, docs-site publish
> included. The dry run prints the exact subjects it will trust; compare them
> against CloudTrail (`AssumeRoleWithWebIdentity` → `userIdentity.userName` on
> the failed event). The script's own header has the full story.

Skipping this does not break the build. It does **not** silently skip either:
once the bucket exists, a role that cannot read it gets a 403 from
`head-bucket`, and the workflow fails with "the OIDC role is missing the
docs-site grants" rather than reporting the stack as unapplied. A permissions
outage that renders as a routine skip is the failure mode
[`docs/architectural-principles.md`](../architectural-principles.md) § fail-soft
exists to prevent.

### 3. Publish the first build

```bash
gh workflow run docs-site.yml
```

Or push any docs change to `main`. Then check:

```bash
curl -sSI https://docs.archimedes-arc.com/ | head -1                    # HTTP/2 200
curl -sSI http://docs.archimedes-arc.com/  | head -1                    # HTTP/1.1 301
curl -sS  https://docs.archimedes-arc.com/openwiki/quickstart/ \
  | grep -c "Agent-generated page"                                      # >= 1
dig +short docs.archimedes-arc.com                                      # CloudFront, no github.io
```

The `openwiki/` check is the one worth keeping: mkdocs uses **directory URLs**
(`/openwiki/quickstart/` → `openwiki/quickstart/index.html`), and CloudFront's
`default_root_object` only covers `/`. The `archimedes-docs-directory-index`
CloudFront Function in the terraform is what maps the rest; if that check
returns 0 while `/` returns 200, the function is the thing to look at, not DNS.

## Publishing by hand

The workflow does this on every docs-path push to `main`. The manual
equivalent — for the first apply, for a rollback, or when Actions is down:

```bash
# from the repo root
pip install -r docs/requirements.txt        # the same file docs-site.yml installs
mkdocs build --strict --site-dir _site

BUCKET="$(terraform -chdir=docs-site/infra output -raw bucket)"
DIST="$(terraform -chdir=docs-site/infra output -raw distribution_id)"

aws s3 sync _site "s3://$BUCKET" --delete
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*"
```

> **`--delete` is why the workflow refuses to sync a truncated build.** A sync
> from an empty or half-built `_site` would *delete the live site* rather than
> update it. The workflow's "Refuse to publish an empty or truncated site" step
> requires `index.html`, `404.html`, and at least 50 `index.html` pages (there
> are 177 as of 2026-08-31) before the sync runs. Doing it by hand, look at
> `find _site -name index.html | wc -l` before you paste the sync.

## Rollback

There is no "previous deployment" button to press: the bucket holds one copy of
the site, and rolling back means rebuilding an older commit and syncing it.

```bash
git switch --detach <last-good-sha>
mkdocs build --strict --site-dir _site
aws s3 sync _site "s3://$BUCKET" --delete
aws cloudfront create-invalidation --distribution-id "$DIST" --paths "/*"
git switch -                                  # back to where you were
```

The invalidation is the part people forget — without it CloudFront keeps
serving the bad build from the edge for hours, and the bucket looking correct
is not evidence the site is.

If the *infrastructure* is what went wrong rather than the content,
`terraform apply` in `docs-site/infra` against the previous commit of
`main.tf` is the fix; the root shares no resource with `infra/`, so a revert
here cannot move anything in the product stack.

## Local preview

```bash
pip install -r docs/requirements.txt   # mkdocs-material, the git-date plugin, pymdown-extensions
mkdocs serve
```

`docs/requirements.txt` is the single pin: the build job installs that file, and
so should you. The `git-revision-date-localized` plugin in it reads each page's
last commit for the footer date, so a page you have created but not committed
yet has no date to read — `mkdocs build --strict` will say so. Commit the file
(or `git add` it) and rebuild; the workflow checks out with `fetch-depth: 0` for
the same reason.

Serves at `http://127.0.0.1:8000` with live reload on any `docs/**` or
`mkdocs.yml` edit. `mkdocs build --strict` (what CI runs) writes the static
site to `site/` (or `_site/` — CI passes `--site-dir _site`); either is
gitignored-equivalent scratch output, not something to commit.

`mkdocs serve` watches `docs_dir` and `mkdocs.yml` and nothing else by
default, so `mkdocs.yml` adds `watch: [openwiki]` to pick up wiki edits too.
Editing the hook itself still needs a **restart**, not just a save: mkdocs
`lru_cache`s hook modules for the life of the process.

## Adding a page: publication is default-deny

A file under `docs/` does **not** publish because it exists. Since #1751 the
build keeps it only if [`../../mkdocs.yml`](../../mkdocs.yml) says so, in one
of three ways:

| Put it in | Effect |
|---|---|
| `nav:` | Published, and listed in the left rail. This is the normal answer. |
| `not_in_nav:` | Published with no nav entry. The site's own assets only — the patterns name file types (`assets/*.svg`), never directories, so it cannot admit a page. |
| `exclude_docs:` | Never built. Records that the file is internal; port it to the private docs repo first ([`../CONVENTIONS.md`](../CONVENTIONS.md) § Content routing). |

A file in none of the three is off the site, and two things say so out loud:
`backend/tests/test_docs_default_deny.py` fails on every PR, and
[`../../.github/scripts/mkdocs_hooks.py`](../../.github/scripts/mkdocs_hooks.py)'s
`deny_unlisted` logs a `WARNING` that `--strict` turns into a failed build.

It reads backwards from how mkdocs works, and that is the point. Nav removal
never unpublished anything: `/team/`, `/runbooks/cost-kill-switch/` and
`/api/admin-private/` were all live with no nav entry on 2026-09-01, and
`exclude_docs` on its own is a deny-list — it can only catch the pages someone
already thought of, so a new runbook was public the day it was committed.

**To publish a new page:** add the file, add one nav row in the same commit,
and run `mkdocs build --strict` (below). The row is the review step — a page
goes in front of readers because a person put it there.

## `mkdocs --strict` is the gate

`mkdocs build --strict` is what `docs-site.yml` runs, and it is clean. Run it
yourself before changing anything about the build:

```bash
mkdocs build --strict
```

It was not always clean, and the history is worth keeping because it explains
the hook. On the 2026-08-20 scaffold the same command produced **315
warnings**, and on `main` at 2026-08-31 it produced **371** — every single one
of them the same shape:

```
WARNING - Doc file 'X.md' contains a link '../../backend/…', but the target
          '../backend/…' is not found among documentation files.
```

`docs_dir: docs` means mkdocs only ever sees files under `docs/`. This repo's
own convention (`architecture.md`: *"Every claim is a link to a file"*) has
many docs link out to source and to repo-root files — `../CLAUDE.md`,
`../SETUP.md`, `../README.md`, `../AGENTS.md`, and paths into `backend/`,
`ui/`, `contracts/`, `analytics-engine/`, `infra/`, `submodules/`, `cli/`,
plus this runbook's own links to [`../../mkdocs.yml`](../../mkdocs.yml) and
the `.github/workflows/*.yml` files below. Every one resolves correctly on
GitHub — where these docs are read day to day, and where `docs-gate.yml`'s
`docs_links.py` already blocks PRs on them against the real repo tree. But on
the *published site* they resolve to nothing: 371 links that would have
served a 404 to anyone who clicked them.

The scaffold's answer was to drop `--strict` and live with the noise. The
answer now is [`.github/scripts/mkdocs_hooks.py`](../../.github/scripts/mkdocs_hooks.py),
which rewrites those targets **at build time** to
`https://github.com/aprin-labs/archimedes/blob/main/<path>` (preserving `#Lnn`
line anchors, and using `/tree/` for directories). No committed markdown was
touched — the ~47 unique out-of-tree targets stay exactly as written, correct
on GitHub, and now also correct on the site.

**What the hook deliberately does *not* rewrite, and why that matters.** A
link whose target lands *inside* `docs/` or `openwiki/` and names a file that
exists nowhere in the repository is left exactly as written. Laundering it
into a GitHub URL would convert a broken link into a plausible-looking one
that still 404s, and would silence the only check on it. Left alone, mkdocs
warns and `--strict` fails the build. That branch is covered by
`backend/tests/test_docs_site.py::test_broken_in_site_link_is_left_for_strict_to_catch`.

**Known remaining noise: 20 `INFO`-level anchor mismatches** (in-page `#…`
links whose slug does not exist on the target page, mostly in `archive/`).
`INFO` never escalates under `--strict`, so they do not fail the build. They
are real, small, and a separate cleanup — raising `validation.links.anchors`
to `warn` is the change that would force it.

## Related files

| File | Purpose |
|---|---|
| [`../../mkdocs.yml`](../../mkdocs.yml) | Site config: theme, nav, repo/site URLs, hooks. `site_url` is the canonical host, and `ui/test/docs-link.test.js` holds the UI links to it. |
| [`../../docs-site/infra/main.tf`](../../docs-site/infra/main.tf) | The bucket, OAC, CloudFront distribution + directory-index function, ACM cert and Route 53 alias. Standalone root, own state key. |
| [`../../.github/scripts/mkdocs_hooks.py`](../../.github/scripts/mkdocs_hooks.py) | Mounts `openwiki/` into the build, enforces default-deny publication (`deny_unlisted`), stamps the provenance banner, and repoints out-of-`docs_dir` links at GitHub. |
| [`../../.github/workflows/docs-site.yml`](../../.github/workflows/docs-site.yml) | Build + publish workflow. The build always runs; the publish no-ops until the terraform is applied. |
| [`../../.github/workflows/infra-gate.yml`](../../.github/workflows/infra-gate.yml) | `terraform fmt` + `validate` over every root, including `docs-site/infra`. |
| [`../../infra/scripts/setup-github-oidc.sh`](../../infra/scripts/setup-github-oidc.sh) | The CI role and its permissions, including the docs-site publish grants. |
| [`../agent-wiki.md`](../agent-wiki.md) | Provenance note for the agent-generated section, and the section's index page. |
| [`../../backend/tests/test_docs_site.py`](../../backend/tests/test_docs_site.py) | Drift guard: nav ↔ tree, the provenance label, the workflow's `paths:` filter and `--strict` flag, the link rewriter, and that the site is still served from our own infra. |
| [`../CONVENTIONS.md`](../CONVENTIONS.md) | Where a new doc goes in the repository, and the public/internal routing rule the `exclude_docs` block applies. |
| [`../../backend/tests/test_docs_default_deny.py`](../../backend/tests/test_docs_default_deny.py) | Publication guard: every file under `docs/` must be in the nav, `not_in_nav` or `exclude_docs`, and the hook must actually deny the rest. |
| [`../../.github/workflows/docs-gate.yml`](../../.github/workflows/docs-gate.yml) | The blocking link/index checker this runbook defers to for `docs/**`'s real (in-repo) links. |
