"""Guard test for issue #1044 leak 2 — SSM secrets must never load locally.

Before this fix, ``load_ssm_secrets()`` ran unconditionally at
``archimedes/main.py`` import time, and ``.env.example`` defaulted
``AWS_SSM_PATH_PREFIX`` to the real prod path with a comment (not enforced by
any code) saying "leave blank for local dev". On any dev machine with ambient
AWS creds, a plain ``docker compose up`` could silently pull real prod
secrets into the local process.

This test runs ``archimedes.main`` import in a **subprocess**, with a fake
``boto3``/``botocore`` shimmed into ``sys.modules`` so ``secrets_service``'s
lazy ``import boto3`` binds to a spy instead of ever touching real AWS or
requiring credentials. It counts ``boto3.client("ssm", ...)`` calls — the
observable point at which ``load_ssm_secrets()`` would actually try to talk
to SSM — to prove the gate directly, rather than inferring it from whether
an exception was raised. Runs
out-of-process (not a plain function call/monkeypatch) because the gate lives
in top-level module code that only executes once per interpreter; every other
test file in this suite already imports ``archimedes.main``, so re-exercising
that import path in-process would depend on cache/reload ordering across the
whole test session instead of being hermetic.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

_BACKEND_DIR = Path(__file__).resolve().parents[1]

_HARNESS = dedent(
    """
    import os
    import sys
    import types

    # Spy boto3/botocore BEFORE archimedes.main (and its secrets_service
    # import) ever runs — secrets_service does a lazy `import boto3` inside
    # load_ssm_secrets(), so shadowing sys.modules here intercepts it without
    # touching the real AWS SDK or needing real credentials.
    ssm_client_calls = []

    class _FakeSsmClient:
        def get_parameters_by_path(self, **kwargs):
            return {"Parameters": []}

    def _fake_client(service, region_name=None):
        ssm_client_calls.append((service, region_name))
        return _FakeSsmClient()

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = _fake_client
    sys.modules["boto3"] = fake_boto3

    fake_botocore = types.ModuleType("botocore")
    fake_botocore_exceptions = types.ModuleType("botocore.exceptions")

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        pass

    class NoCredentialsError(Exception):
        pass

    fake_botocore_exceptions.BotoCoreError = BotoCoreError
    fake_botocore_exceptions.ClientError = ClientError
    fake_botocore_exceptions.NoCredentialsError = NoCredentialsError
    sys.modules["botocore"] = fake_botocore
    sys.modules["botocore.exceptions"] = fake_botocore_exceptions

    # Neutralize load_dotenv BEFORE archimedes.main runs its own
    # load_dotenv("../.env", override=True) — otherwise a populated repo-root
    # .env (e.g. PUBLIC_DOMAIN set for local prod-mimicry, as .env.example's
    # comment suggests) overrides this test's carefully-constructed env and
    # flips the local-mode assertion: local-red/CI-green, the class the
    # testing conventions ban (pattern: test_security_hardening.py).
    import dotenv as _dotenv
    _dotenv.load_dotenv = lambda *_a, **_kw: False

    import archimedes.main  # noqa: F401  (import-time side effect under test)

    print(len(ssm_client_calls))
    """
)


def _run(env: dict[str, str]) -> int:
    result = subprocess.run(
        [sys.executable, "-c", _HARNESS],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"subprocess failed (rc={result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return int(result.stdout.strip().splitlines()[-1])


def _base_env() -> dict[str, str]:
    """Whitelist-only env (testing conventions; pattern:
    test_security_hardening._clean_subprocess_env). Inheriting os.environ
    leaks the developer's .env — loaded into the parent pytest process by
    earlier imports — into the subprocess: ambient AWS creds, a real
    DATABASE_URL pointing at compose's postgres hostname, or a locally-set
    PUBLIC_DOMAIN would all change what this test measures. The gate is on
    PUBLIC_DOMAIN, not credential presence, so nothing ambient may vary."""
    import os

    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "DATABASE_URL": "sqlite:////tmp/archimedes-ssm-gate-subprocess.db",
    }


def test_local_mode_never_calls_ssm_even_with_prod_shaped_prefix():
    """PUBLIC_DOMAIN unset (local mode) — boto3 SSM client must never be built,
    even when AWS_SSM_PATH_PREFIX resolves to the real prod path (e.g. via the
    compose file's legacy default) — the #1044 leak scenario exactly."""
    env = _base_env()
    env.pop("PUBLIC_DOMAIN", None)
    env["AWS_SSM_PATH_PREFIX"] = "/archimedes/prod/"
    assert _run(env) == 0


def test_ambient_aws_credentials_do_not_promote_a_local_run():
    """The #1044 leak exactly as onboarding produces it: a developer whose
    shell already has working AWS credentials (our own setup docs tell people
    to configure them) runs a plain local `docker compose up`.

    Distinct from the test above, which varies only the prefix. This one adds
    the credentials, because the tempting wrong fix is to gate on credential
    *presence* — "no creds, no SSM" — which is exactly the gate that fails on
    the machine it matters on. The observable must stay zero as a function of
    PUBLIC_DOMAIN alone, with credentials in hand.

    The credential values are deliberately NOT AWS-shaped: a literal matching
    detect-secrets' access-key pattern (AKIA + 16 chars) would trip the
    pre-commit scan for a string that is not a credential. boto3 is spied here
    and never parses them — only their presence in the subprocess env matters.
    The real-credential version of this run is in the PR body."""
    env = _base_env()
    env.pop("PUBLIC_DOMAIN", None)
    env["AWS_SSM_PATH_PREFIX"] = "/archimedes/prod/"
    env["AWS_REGION"] = "us-east-1"
    env["AWS_ACCESS_KEY_ID"] = "not-a-real-access-key-id"
    env["AWS_SECRET_ACCESS_KEY"] = "not-a-real-secret-access-key"
    assert _run(env) == 0


def test_production_mode_does_call_ssm():
    """PUBLIC_DOMAIN set (production mode) — the SSM load path still runs.
    Guards against the fix over-correcting into never loading secrets in prod."""
    env = _base_env()
    env["PUBLIC_DOMAIN"] = "https://example.com"
    env["AWS_SSM_PATH_PREFIX"] = "/archimedes/prod/"
    # Avoid tripping the unrelated fail-closed EMAIL_ENCRYPTION_KEY check
    # (main.py) so the process reaches a clean, observable exit.
    env["EMAIL_ENCRYPTION_KEY"] = "test-only-not-a-real-key"
    assert _run(env) == 1
