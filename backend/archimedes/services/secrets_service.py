"""AWS SSM Parameter Store secrets loader.

Production: reads all parameters under ``/archimedes/prod/*`` via boto3 SSM
and injects them into ``os.environ`` so downstream services (LLM, Circle,
chain client) work without any code changes.

Local development: no-op when ``AWS_SSM_PATH_PREFIX`` is unset/blank or when
boto3 cannot reach SSM (no instance profile, no credentials). Falls back
silently to .env-based values already loaded by python-dotenv.

Nothing in this module defaults the prefix to a real path. A missing prefix
means "load nothing", never "load production" — ambient AWS credentials on a
developer machine must not be able to promote a local run to prod-secret-backed
(issue #1044). The caller-side half of that guard lives in ``main.py``, which
only calls ``load_ssm_secrets()`` when ``PUBLIC_DOMAIN`` is set.

Usage (in main.py, BEFORE init_db / service imports):
    from archimedes.services.secrets_service import load_ssm_secrets
    load_ssm_secrets()

Security notes:
    - Never logs secret VALUES — only names + count.
    - Rotation: operator re-seeds SSM params, restarts the container.
    - IAM: scoped to ssm:GetParametersByPath on /archimedes/prod/*
      (see infra/iam/archimedes-backend-policy.json).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# NOTE: there is intentionally no module-level default prefix constant. The
# production prefix is declared where production is declared — infra/ecs.tf and
# infra/ecs_migrate.tf — never as an in-code fallback that an unset env var can
# silently select (issue #1044).

# Map SSM param names (last segment after prefix) → env var names.
# e.g. /archimedes/prod/LLM_AUTH_TOKEN → LLM_AUTH_TOKEN
# Identity mapping by default (uppercase last segment = env var name).
# Override with explicit entries if SSM naming diverges from env var naming.
_PARAM_TO_ENV: dict[str, str] = {
    # Add explicit mappings here if needed:
    # "anthropic-auth-token": "ANTHROPIC_AUTH_TOKEN",
}


def _extract_env_name(param_name: str, prefix: str) -> str:
    """Convert SSM parameter name to environment variable name.

    /archimedes/prod/LLM_AUTH_TOKEN → LLM_AUTH_TOKEN
    /archimedes/prod/circle/api-key → CIRCLE_API_KEY (nested path → underscore + upper)
    """
    # Strip the prefix to get the relative key
    relative = param_name.removeprefix(prefix).strip("/")

    # Check explicit mapping first
    if relative in _PARAM_TO_ENV:
        return _PARAM_TO_ENV[relative]

    # Default: replace slashes and hyphens with underscores, uppercase
    return relative.replace("/", "_").replace("-", "_").upper()


def load_ssm_secrets(
    prefix: str | None = None,
    region: str | None = None,
    override_existing: bool = False,
) -> int:
    """Load secrets from AWS SSM Parameter Store into os.environ.

    Args:
        prefix: SSM path prefix (default: the AWS_SSM_PATH_PREFIX env var).
            There is deliberately NO fallback path — blank means "load
            nothing" and returns 0 (issue #1044).
        region: AWS region (default: AWS_REGION env var or us-east-1)
        override_existing: If True, overwrite env vars that already have values.
            Default False — .env values take precedence (useful for local dev override).

    Returns:
        Number of parameters loaded.

    Raises:
        Nothing — all errors are caught and logged as warnings.
        The app boots degraded rather than crashing on SSM failure.
    """
    prefix = prefix or os.environ.get("AWS_SSM_PATH_PREFIX", "").strip()
    if not prefix:
        logger.debug("secrets_service: AWS_SSM_PATH_PREFIX not set — skipping SSM load")
        return 0

    region = region or os.environ.get("AWS_REGION", "us-east-1")

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        logger.warning("secrets_service: boto3 not installed — cannot load SSM secrets")
        return 0

    try:
        client = boto3.client("ssm", region_name=region)
        parameters = _fetch_all_parameters(client, prefix)
    except NoCredentialsError:
        logger.info("secrets_service: no AWS credentials available — skipping SSM (expected in local dev)")
        return 0
    except (BotoCoreError, ClientError) as exc:
        logger.warning("secrets_service: SSM fetch failed: %s — falling back to .env", exc)
        return 0

    loaded = 0
    for param in parameters:
        env_name = _extract_env_name(param["Name"], prefix)
        if not override_existing and os.environ.get(env_name):
            logger.debug("secrets_service: %s already set — skipping (override_existing=False)", env_name)
            continue
        os.environ[env_name] = param["Value"]
        loaded += 1
        logger.debug("secrets_service: loaded %s from SSM", env_name)

    logger.info("Loaded %d secrets from SSM (prefix=%s, region=%s)", loaded, prefix, region)
    return loaded


def _fetch_all_parameters(client: Any, prefix: str) -> list[dict[str, Any]]:
    """Paginate through all SSM parameters under the given prefix."""
    parameters: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "Path": prefix,
        "Recursive": True,
        "WithDecryption": True,
        "MaxResults": 10,
    }

    while True:
        response = client.get_parameters_by_path(**kwargs)
        parameters.extend(response.get("Parameters", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token

    return parameters


def list_ssm_parameters(prefix: str | None = None, region: str | None = None) -> list[str]:
    """List parameter names (not values) under the prefix. Useful for diagnostics.

    Blank prefix → ``[]``, matching :func:`load_ssm_secrets`. This helper used
    to fall back to ``/archimedes/prod/`` when the env var was unset, which
    made a bare ``list_ssm_parameters()`` from a developer shell with ambient
    AWS credentials enumerate the real production parameter store — the same
    ambient-credential promotion #1044 closed on the load path, left open on
    the diagnostic path. The prefix must now be stated, by env var or argument.
    """
    prefix = (prefix or os.environ.get("AWS_SSM_PATH_PREFIX", "")).strip()
    if not prefix:
        logger.debug("secrets_service: AWS_SSM_PATH_PREFIX not set — skipping SSM list")
        return []
    region = region or os.environ.get("AWS_REGION", "us-east-1")

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        logger.warning("secrets_service: boto3 not installed — cannot list SSM parameters")
        return []

    try:
        client = boto3.client("ssm", region_name=region)
        params = _fetch_all_parameters(client, prefix)
        return [p["Name"] for p in params]
    except (BotoCoreError, ClientError) as exc:
        logger.warning("secrets_service: list failed: %s", exc)
        return []
