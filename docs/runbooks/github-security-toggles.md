# GitHub Security Toggles — Setup Guide

This repo uses three GitHub security features that must be enabled manually
(repo Settings). They cannot be toggled via the API on public repos — a repo
admin must flip them in the GitHub UI.

## Toggles to enable

Navigate to **Settings → Code security and analysis** for the repo
(`aprin-labs/archimedes`).

### 1. Dependabot alerts

- **Toggle:** Dependabot alerts → Enable
- **What it does:** GitHub monitors dependency manifests (`package.json`,
  `requirements.txt`, etc.) and opens alerts when a known vulnerability is
  found in a transitive dependency.
- **Our config:** `.github/dependabot.yml` controls the weekly schedule and
  PR limits.

### 2. Secret scanning

- **Toggle:** Secret scanning → Enable
- **What it does:** GitHub scans every push (including history) for leaked
  secrets (AWS keys, API tokens, private keys, etc.) and alerts repo admins.

### 3. Push protection

- **Toggle:** Push protection → Enable
- **What it does:** Blocks a push if it contains a detected secret, giving the
  contributor a chance to remove it before it enters history. This is the
  server-side complement to our client-side `detect-secrets` pre-commit hook.

## Client-side: detect-secrets pre-commit hook

In addition to the GitHub server-side protections, contributors should install
the pre-commit hook:

```bash
pip install pre-commit detect-secrets
pre-commit install
```

This runs `detect-secrets` against every staged file before the commit is
created, catching secrets that would otherwise need to be pushed and caught
server-side.

### Updating the baseline

After intentionally changing a file that contains false-positive secrets
(e.g., `.env.example`, test fixtures):

```bash
detect-secrets scan --update .secrets.baseline
git add .secrets.baseline
```

Review the diff carefully — never add a real secret to the baseline.

## Related files

| File | Purpose |
|---|---|
| `.github/dependabot.yml` | Weekly dependency bump schedule |
| `.pre-commit-config.yaml` | Client-side hooks (detect-secrets, ruff, merge-conflict) |
| `.secrets.baseline` | Known false positives for detect-secrets |
