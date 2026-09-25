"""Tests for archimedes.services.secrets_service (SSM Parameter Store loader).

All tests mock boto3 — no real AWS calls.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure test secrets don't leak into the real environment."""
    # Remove any SSM-related env vars that might interfere
    for key in ("AWS_SSM_PATH_PREFIX", "AWS_REGION", "TEST_SECRET_1", "TEST_SECRET_2", "DATABASE_URL_FROM_SSM"):
        monkeypatch.delenv(key, raising=False)


class TestExtractEnvName:
    """Unit tests for the _extract_env_name helper."""

    def test_simple_uppercase(self):
        from archimedes.services.secrets_service import _extract_env_name

        assert _extract_env_name("/archimedes/prod/LLM_AUTH_TOKEN", "/archimedes/prod/") == "LLM_AUTH_TOKEN"

    def test_nested_path(self):
        from archimedes.services.secrets_service import _extract_env_name

        assert _extract_env_name("/archimedes/prod/circle/api-key", "/archimedes/prod/") == "CIRCLE_API_KEY"

    def test_hyphen_to_underscore(self):
        from archimedes.services.secrets_service import _extract_env_name

        assert (
            _extract_env_name("/archimedes/prod/dev-wallet-private-key", "/archimedes/prod/")
            == "DEV_WALLET_PRIVATE_KEY"
        )

    def test_already_uppercase(self):
        from archimedes.services.secrets_service import _extract_env_name

        assert _extract_env_name("/archimedes/prod/DATABASE_URL", "/archimedes/prod/") == "DATABASE_URL"


class TestLoadSsmSecrets:
    """Integration-style tests for load_ssm_secrets with mocked boto3."""

    def _mock_ssm_response(self, parameters: list[dict]) -> MagicMock:
        """Create a mock SSM client that returns the given parameters."""
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.return_value = {
            "Parameters": parameters,
        }
        return mock_client

    @patch("boto3.client")
    def test_loads_parameters_into_env(self, mock_boto3, monkeypatch):
        """SSM parameters are injected into os.environ."""
        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_client = self._mock_ssm_response(
            [
                {"Name": "/archimedes/prod/TEST_SECRET_1", "Value": "secret-value-1"},
                {"Name": "/archimedes/prod/TEST_SECRET_2", "Value": "secret-value-2"},
            ]
        )
        mock_boto3.return_value = mock_client

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets()

        assert count == 2
        assert os.environ.get("TEST_SECRET_1") == "secret-value-1"
        assert os.environ.get("TEST_SECRET_2") == "secret-value-2"

    @patch("boto3.client")
    def test_does_not_override_existing_by_default(self, mock_boto3, monkeypatch):
        """Existing env vars are NOT overwritten when override_existing=False."""
        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("TEST_SECRET_1", "original-value")

        mock_client = self._mock_ssm_response(
            [
                {"Name": "/archimedes/prod/TEST_SECRET_1", "Value": "ssm-value"},
            ]
        )
        mock_boto3.return_value = mock_client

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets()

        assert count == 0  # Nothing loaded (already set)
        assert os.environ.get("TEST_SECRET_1") == "original-value"

    @patch("boto3.client")
    def test_override_existing_when_flag_set(self, mock_boto3, monkeypatch):
        """With override_existing=True, SSM values replace existing env vars."""
        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("TEST_SECRET_1", "original-value")

        mock_client = self._mock_ssm_response(
            [
                {"Name": "/archimedes/prod/TEST_SECRET_1", "Value": "ssm-override"},
            ]
        )
        mock_boto3.return_value = mock_client

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets(override_existing=True)

        assert count == 1
        assert os.environ.get("TEST_SECRET_1") == "ssm-override"

    def test_noop_when_prefix_not_set(self, monkeypatch):
        """No AWS calls when AWS_SSM_PATH_PREFIX is unset."""
        monkeypatch.delenv("AWS_SSM_PATH_PREFIX", raising=False)

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets()
        assert count == 0

    @patch("boto3.client")
    def test_handles_no_credentials_gracefully(self, mock_boto3, monkeypatch):
        """NoCredentialsError → returns 0, does not crash."""
        from botocore.exceptions import NoCredentialsError

        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")

        mock_boto3.return_value.get_parameters_by_path.side_effect = NoCredentialsError()

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets()
        assert count == 0

    @patch("boto3.client")
    def test_handles_client_error_gracefully(self, mock_boto3, monkeypatch):
        """ClientError → returns 0, does not crash."""
        from botocore.exceptions import ClientError

        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")

        mock_boto3.return_value.get_parameters_by_path.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetParametersByPath",
        )

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets()
        assert count == 0

    @patch("boto3.client")
    def test_pagination(self, mock_boto3, monkeypatch):
        """Handles paginated SSM responses correctly."""
        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_client = MagicMock()
        # First page returns a NextToken
        mock_client.get_parameters_by_path.side_effect = [
            {
                "Parameters": [{"Name": "/archimedes/prod/TEST_SECRET_1", "Value": "val1"}],
                "NextToken": "token-abc",
            },
            {
                "Parameters": [{"Name": "/archimedes/prod/TEST_SECRET_2", "Value": "val2"}],
            },
        ]
        mock_boto3.return_value = mock_client

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets()

        assert count == 2
        assert os.environ.get("TEST_SECRET_1") == "val1"
        assert os.environ.get("TEST_SECRET_2") == "val2"
        # Verify pagination was followed
        assert mock_client.get_parameters_by_path.call_count == 2

    @patch("boto3.client")
    def test_explicit_prefix_and_region(self, mock_boto3, monkeypatch):
        """Explicit prefix/region args override env vars."""
        # Don't set env vars — use explicit args
        mock_client = self._mock_ssm_response(
            [
                {"Name": "/custom/path/MY_KEY", "Value": "my-val"},
            ]
        )
        mock_boto3.return_value = mock_client

        from archimedes.services.secrets_service import load_ssm_secrets

        count = load_ssm_secrets(prefix="/custom/path/", region="us-east-1")

        assert count == 1
        assert os.environ.get("MY_KEY") == "my-val"
        mock_boto3.assert_called_with("ssm", region_name="us-east-1")


class TestListSsmParameters:
    """Tests for the diagnostic list_ssm_parameters helper."""

    @patch("boto3.client")
    def test_returns_param_names(self, mock_boto3, monkeypatch):
        from archimedes.services.secrets_service import list_ssm_parameters

        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.return_value = {
            "Parameters": [
                {"Name": "/archimedes/prod/SECRET_A", "Value": "a"},
                {"Name": "/archimedes/prod/SECRET_B", "Value": "b"},
            ],
        }
        mock_boto3.return_value = mock_client

        names = list_ssm_parameters()
        assert names == ["/archimedes/prod/SECRET_A", "/archimedes/prod/SECRET_B"]

    @patch("boto3.client")
    def test_returns_empty_on_error(self, mock_boto3, monkeypatch):
        from archimedes.services.secrets_service import list_ssm_parameters
        from botocore.exceptions import ClientError

        monkeypatch.setenv("AWS_SSM_PATH_PREFIX", "/archimedes/prod/")
        mock_boto3.return_value.get_parameters_by_path.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "oops"}},
            "GetParametersByPath",
        )

        names = list_ssm_parameters()
        assert names == []

    @patch("boto3.client")
    def test_blank_prefix_does_not_fall_back_to_the_production_path(self, mock_boto3):
        """#1044, diagnostic path. This helper used to default to
        ``_DEFAULT_PREFIX = "/archimedes/prod/"`` when AWS_SSM_PATH_PREFIX was
        unset, so a bare ``list_ssm_parameters()`` from a developer shell with
        ambient AWS credentials enumerated the real production parameter store
        — the same ambient-credential promotion the load path closed, left open
        one function below it. Both tests above passed *because* of that
        fallback, which is what made it invisible.

        Asserted on the boto3 client, not just the return value: an
        implementation that built the client and then discarded the result
        would still have talked to production."""
        from archimedes.services.secrets_service import list_ssm_parameters

        assert list_ssm_parameters() == []
        mock_boto3.assert_not_called()
