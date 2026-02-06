"""Unit tests for utils/skopeo_client.py"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))


@pytest.fixture(autouse=True)
def patch_environment():
    """Patch environment for all tests"""
    with patch.dict(os.environ, {"SKIP_CONFIG_VALIDATION": "true"}):
        yield


class TestIsRegistryInCluster:
    """Tests for is_registry_in_cluster function"""

    def test_returns_true_when_service_found(self):
        """Test returns True when registry Service exists"""
        from utils.skopeo_client import is_registry_in_cluster

        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()

        with patch("utils.skopeo_client._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1)):
            result = is_registry_in_cluster("docker-registry:5000", "domino-platform")
            assert result is True
            mock_core_v1.read_namespaced_service.assert_called_once_with(
                name="docker-registry", namespace="domino-platform"
            )

    def test_returns_true_when_statefulset_found(self):
        """Test returns True when registry StatefulSet exists"""
        from kubernetes.client.rest import ApiException

        from utils.skopeo_client import is_registry_in_cluster

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_service.side_effect = ApiException(status=404)

        mock_apps_v1 = MagicMock()

        with patch("utils.skopeo_client._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1)):
            result = is_registry_in_cluster("docker-registry:5000", "domino-platform")
            assert result is True
            mock_apps_v1.read_namespaced_stateful_set.assert_called_once_with(
                name="docker-registry", namespace="domino-platform"
            )

    def test_returns_false_when_not_found(self):
        """Test returns False when neither Service nor StatefulSet exists"""
        from kubernetes.client.rest import ApiException

        from utils.skopeo_client import is_registry_in_cluster

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_service.side_effect = ApiException(status=404)

        mock_apps_v1 = MagicMock()
        mock_apps_v1.read_namespaced_stateful_set.side_effect = ApiException(status=404)

        with patch("utils.skopeo_client._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1)):
            result = is_registry_in_cluster("docker-registry:5000", "domino-platform")
            assert result is False

    def test_returns_false_when_k8s_not_available(self):
        """Test returns False when Kubernetes client is not available"""
        from utils.skopeo_client import is_registry_in_cluster

        with patch("utils.skopeo_client._get_kubernetes_clients", side_effect=Exception("No cluster")):
            result = is_registry_in_cluster("docker-registry:5000", "domino-platform")
            assert result is False

    def test_parses_namespace_from_url(self):
        """Test that namespace is parsed from URL when included"""
        from utils.skopeo_client import is_registry_in_cluster

        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()

        with patch("utils.skopeo_client._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1)):
            result = is_registry_in_cluster("docker-registry.other-namespace:5000", "domino-platform")
            assert result is True
            # Should use namespace from URL, not the provided one
            mock_core_v1.read_namespaced_service.assert_called_once_with(
                name="docker-registry", namespace="other-namespace"
            )


class TestSkopeoClientInitialization:
    """Tests for SkopeoClient initialization"""

    @pytest.fixture
    def mock_config_manager(self):
        """Create a mock ConfigManager"""
        mock = MagicMock()
        mock.get_registry_url.return_value = "registry.example.com:5000"
        mock.get_repository.return_value = "myrepo"
        mock.get_domino_platform_namespace.return_value = "domino-platform"
        mock.get_output_dir.return_value = "/tmp/output"
        mock.get_skopeo_rate_limit_enabled.return_value = True
        mock.get_skopeo_rate_limit_rps.return_value = 10.0
        mock.get_skopeo_rate_limit_burst.return_value = 20
        mock.get_max_retries.return_value = 3
        mock.get_retry_initial_delay.return_value = 1.0
        mock.get_retry_max_delay.return_value = 60.0
        mock.get_retry_exponential_base.return_value = 2.0
        mock.get_retry_jitter.return_value = True
        mock.get_retry_timeout.return_value = 300
        return mock

    def test_initializes_with_config_manager(self, mock_config_manager):
        """Test SkopeoClient initializes with ConfigManager"""
        from utils.skopeo_client import SkopeoClient

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                client = SkopeoClient(mock_config_manager)
                assert client.registry_url == "registry.example.com:5000"
                assert client.repository == "myrepo"
                assert client.namespace == "domino-platform"

    def test_uses_custom_namespace(self, mock_config_manager):
        """Test SkopeoClient uses custom namespace when provided"""
        from utils.skopeo_client import SkopeoClient

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                client = SkopeoClient(mock_config_manager, namespace="custom-namespace")
                assert client.namespace == "custom-namespace"

    def test_initializes_rate_limiter_when_enabled(self, mock_config_manager):
        """Test rate limiter is initialized when enabled"""
        from utils.skopeo_client import SkopeoClient

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                client = SkopeoClient(mock_config_manager)
                assert client.rate_limit_enabled is True
                assert client._tokens == 20.0  # burst size

    def test_skips_rate_limiter_when_disabled(self, mock_config_manager):
        """Test rate limiter is skipped when disabled"""
        from utils.skopeo_client import SkopeoClient

        mock_config_manager.get_skopeo_rate_limit_enabled.return_value = False

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                client = SkopeoClient(mock_config_manager)
                assert client.rate_limit_enabled is False


class TestSkopeoClientCredentials:
    """Tests for SkopeoClient credential retrieval"""

    @pytest.fixture
    def mock_config_manager(self):
        """Create a mock ConfigManager"""
        mock = MagicMock()
        mock.get_registry_url.return_value = "registry.example.com:5000"
        mock.get_repository.return_value = "myrepo"
        mock.get_domino_platform_namespace.return_value = "domino-platform"
        mock.get_output_dir.return_value = "/tmp/output"
        mock.get_skopeo_rate_limit_enabled.return_value = False
        mock.get_max_retries.return_value = 3
        mock.get_retry_initial_delay.return_value = 1.0
        mock.get_retry_max_delay.return_value = 60.0
        mock.get_retry_exponential_base.return_value = 2.0
        mock.get_retry_jitter.return_value = True
        mock.get_retry_timeout.return_value = 300
        return mock

    def test_gets_username_from_environment(self, mock_config_manager):
        """Test username is retrieved from environment variable"""
        from utils.skopeo_client import SkopeoClient

        with patch.dict(os.environ, {"REGISTRY_USERNAME": "env-user"}):
            with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
                with patch.object(SkopeoClient, "_ensure_logged_in"):
                    client = SkopeoClient(mock_config_manager)
                    assert client.username == "env-user"

    def test_gets_password_from_environment(self, mock_config_manager):
        """Test password is retrieved from environment variable"""
        from utils.skopeo_client import SkopeoClient

        with patch.dict(os.environ, {"REGISTRY_PASSWORD": "env-password"}):
            with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
                with patch.object(SkopeoClient, "_ensure_logged_in"):
                    client = SkopeoClient(mock_config_manager)
                    assert client.password == "env-password"

    def test_gets_credentials_from_custom_secret(self, mock_config_manager):
        """Test credentials from custom auth secret"""
        from utils.skopeo_client import SkopeoClient

        with patch.dict(os.environ, {"REGISTRY_AUTH_SECRET": "my-custom-secret"}):
            with patch(
                "utils.skopeo_client.get_credentials_from_k8s_secret", return_value=("secret-user", "secret-pass")
            ):
                with patch.object(SkopeoClient, "_ensure_logged_in"):
                    client = SkopeoClient(mock_config_manager)
                    assert client.username == "secret-user"
                    assert client.password == "secret-pass"

    def test_ecr_username_is_aws(self, mock_config_manager):
        """Test ECR registry username is 'AWS'"""
        from utils.skopeo_client import SkopeoClient

        mock_config_manager.get_registry_url.return_value = "123456789.dkr.ecr.us-west-2.amazonaws.com"

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
            with patch("utils.skopeo_client.authenticate_ecr"):
                with patch.object(SkopeoClient, "_ensure_logged_in"):
                    client = SkopeoClient(mock_config_manager)
                    assert client.username == "AWS"

    def test_acr_username_is_guid(self, mock_config_manager):
        """Test ACR registry username is placeholder GUID"""
        from utils.skopeo_client import SkopeoClient

        mock_config_manager.get_registry_url.return_value = "myregistry.azurecr.io"

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
            with patch("utils.skopeo_client.authenticate_acr"):
                with patch.object(SkopeoClient, "_ensure_logged_in"):
                    client = SkopeoClient(mock_config_manager)
                    assert client.username == "00000000-0000-0000-0000-000000000000"


class TestSkopeoClientOperations:
    """Tests for SkopeoClient registry operations"""

    @pytest.fixture
    def skopeo_client(self):
        """Create a SkopeoClient with mocked dependencies"""
        from utils.skopeo_client import SkopeoClient

        mock_config = MagicMock()
        mock_config.get_registry_url.return_value = "registry.example.com:5000"
        mock_config.get_repository.return_value = "myrepo"
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_output_dir.return_value = "/tmp/output"
        mock_config.get_skopeo_rate_limit_enabled.return_value = False
        mock_config.get_max_retries.return_value = 3
        mock_config.get_retry_initial_delay.return_value = 1.0
        mock_config.get_retry_max_delay.return_value = 60.0
        mock_config.get_retry_exponential_base.return_value = 2.0
        mock_config.get_retry_jitter.return_value = True
        mock_config.get_retry_timeout.return_value = 300

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=("user", "pass")):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                with patch.object(SkopeoClient, "_login_to_registry"):
                    client = SkopeoClient(mock_config)
                    client._logged_in = True
                    return client

    def test_list_tags_success(self, skopeo_client):
        """Test successful tag listing"""
        tags_response = {"Tags": ["v1.0", "v1.1", "latest"]}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps(tags_response))
            # Use a unique repository name to avoid cache hits
            tags = skopeo_client.list_tags(repository="test-repo-success")

            assert tags == ["v1.0", "v1.1", "latest"]
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert "list-tags" in call_args

    def test_list_tags_empty_tags_array(self, skopeo_client):
        """Test tag listing with empty Tags array"""
        tags_response = {"Tags": []}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps(tags_response))
            # Use a unique repository name to avoid cache hits
            tags = skopeo_client.list_tags(repository="test-repo-empty")

            assert tags == []

    def test_list_tags_no_tags_key(self, skopeo_client):
        """Test tag listing with missing Tags key"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="{}")
            # Use a unique repository name to avoid cache hits
            tags = skopeo_client.list_tags(repository="test-repo-nokey")

            assert tags == []

    def test_list_tags_custom_repository(self, skopeo_client):
        """Test tag listing with custom repository"""
        tags_response = {"Tags": ["tag1"]}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps(tags_response))
            # Use yet another unique repository name
            skopeo_client.list_tags(repository="custom-repo-test")

            call_args = mock_run.call_args[0][0]
            assert "docker://registry.example.com:5000/custom-repo-test" in call_args[-1]

    def test_inspect_image_success(self, skopeo_client):
        """Test successful image inspection"""
        inspect_response = {"Digest": "sha256:abc123", "Layers": ["layer1", "layer2"]}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=json.dumps(inspect_response))
            result = skopeo_client.inspect_image(None, "v1.0")

            assert result == inspect_response
            call_args = mock_run.call_args[0][0]
            assert "inspect" in call_args
            assert "docker://registry.example.com:5000/myrepo:v1.0" in call_args[-1]

    def test_inspect_image_not_found(self, skopeo_client):
        """Test image inspection when image not found"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "skopeo", stderr="manifest unknown")
            result = skopeo_client.inspect_image(None, "nonexistent")

            assert result is None

    def test_delete_image_success(self, skopeo_client):
        """Test successful image deletion"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = skopeo_client.delete_image(None, "v1.0")

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "delete" in call_args

    def test_delete_image_failure(self, skopeo_client):
        """Test image deletion failure"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "skopeo", stderr="deletion not enabled")
            result = skopeo_client.delete_image(None, "v1.0")

            assert result is False


class TestSkopeoClientRateLimiting:
    """Tests for SkopeoClient rate limiting"""

    @pytest.fixture
    def rate_limited_client(self):
        """Create a SkopeoClient with rate limiting enabled"""
        from utils.skopeo_client import SkopeoClient

        mock_config = MagicMock()
        mock_config.get_registry_url.return_value = "registry.example.com:5000"
        mock_config.get_repository.return_value = "myrepo"
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_output_dir.return_value = "/tmp/output"
        mock_config.get_skopeo_rate_limit_enabled.return_value = True
        mock_config.get_skopeo_rate_limit_rps.return_value = 10.0
        mock_config.get_skopeo_rate_limit_burst.return_value = 5
        mock_config.get_max_retries.return_value = 3
        mock_config.get_retry_initial_delay.return_value = 1.0
        mock_config.get_retry_max_delay.return_value = 60.0
        mock_config.get_retry_exponential_base.return_value = 2.0
        mock_config.get_retry_jitter.return_value = True
        mock_config.get_retry_timeout.return_value = 300

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=("user", "pass")):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                with patch.object(SkopeoClient, "_login_to_registry"):
                    client = SkopeoClient(mock_config)
                    client._logged_in = True
                    return client

    def test_rate_limiter_allows_burst(self, rate_limited_client):
        """Test rate limiter allows burst requests"""
        # Initial tokens should be burst size
        assert rate_limited_client._tokens == 5.0

    def test_rate_limiter_consumes_tokens(self, rate_limited_client):
        """Test rate limiter consumes tokens"""
        rate_limited_client._acquire_rate_limit_token()
        assert rate_limited_client._tokens < 5.0

    def test_rate_limiter_refills_tokens(self, rate_limited_client):
        """Test rate limiter refills tokens over time"""
        # Consume all tokens
        for _ in range(5):
            rate_limited_client._acquire_rate_limit_token()

        # Simulate time passing
        rate_limited_client._last_update = time.time() - 1.0  # 1 second ago

        # Acquire should work after refill
        rate_limited_client._acquire_rate_limit_token()
        # Should have refilled 10 tokens (10 RPS * 1s) but capped at burst
        # Then consumed 1, so should have at least some tokens


class TestSkopeoClientRegistryDeletion:
    """Tests for SkopeoClient enable/disable registry deletion"""

    @pytest.fixture
    def skopeo_client(self):
        """Create a SkopeoClient with mocked dependencies"""
        from utils.skopeo_client import SkopeoClient

        mock_config = MagicMock()
        mock_config.get_registry_url.return_value = "docker-registry:5000"
        mock_config.get_repository.return_value = "myrepo"
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_output_dir.return_value = "/tmp/output"
        mock_config.get_skopeo_rate_limit_enabled.return_value = False
        mock_config.get_max_retries.return_value = 3
        mock_config.get_retry_initial_delay.return_value = 1.0
        mock_config.get_retry_max_delay.return_value = 60.0
        mock_config.get_retry_exponential_base.return_value = 2.0
        mock_config.get_retry_jitter.return_value = True
        mock_config.get_retry_timeout.return_value = 300

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=("user", "pass")):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                with patch.object(SkopeoClient, "_login_to_registry"):
                    client = SkopeoClient(mock_config)
                    client._logged_in = True
                    return client

    def test_is_registry_in_cluster_with_override(self, skopeo_client):
        """Test is_registry_in_cluster returns True when deletion enabled"""
        skopeo_client.enable_docker_deletion = True
        assert skopeo_client.is_registry_in_cluster() is True

    def test_enable_registry_deletion_success(self, skopeo_client):
        """Test enabling registry deletion updates StatefulSet"""
        mock_sts = MagicMock()
        mock_sts.spec.template.spec.containers = [MagicMock(env=[])]

        mock_apps_v1 = MagicMock()
        mock_apps_v1.read_namespaced_stateful_set.return_value = mock_sts

        mock_core_v1 = MagicMock()
        mock_core_v1.list_namespaced_pod.return_value = MagicMock(
            items=[
                MagicMock(
                    metadata=MagicMock(name="docker-registry-0"),
                    status=MagicMock(conditions=[MagicMock(type="Ready", status="True")]),
                )
            ]
        )

        with patch("utils.skopeo_client._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1)):
            result = skopeo_client.enable_registry_deletion()

            assert result is True
            mock_apps_v1.patch_namespaced_stateful_set.assert_called_once()

    def test_enable_registry_deletion_failure(self, skopeo_client):
        """Test enabling registry deletion handles failure"""
        with patch("utils.skopeo_client._get_kubernetes_clients", side_effect=Exception("K8s error")):
            result = skopeo_client.enable_registry_deletion()
            assert result is False

    def test_disable_registry_deletion_success(self, skopeo_client):
        """Test disabling registry deletion removes env var"""
        env_var = MagicMock()
        env_var.name = "REGISTRY_STORAGE_DELETE_ENABLED"

        mock_sts = MagicMock()
        mock_sts.spec.template.spec.containers = [MagicMock(env=[env_var])]

        mock_apps_v1 = MagicMock()
        mock_apps_v1.read_namespaced_stateful_set.return_value = mock_sts

        mock_core_v1 = MagicMock()
        mock_core_v1.list_namespaced_pod.return_value = MagicMock(
            items=[
                MagicMock(
                    metadata=MagicMock(name="docker-registry-0"),
                    status=MagicMock(conditions=[MagicMock(type="Ready", status="True")]),
                )
            ]
        )

        with patch("utils.skopeo_client._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1)):
            result = skopeo_client.disable_registry_deletion()

            assert result is True
            mock_apps_v1.patch_namespaced_stateful_set.assert_called_once()


class TestSkopeoClientLogin:
    """Tests for SkopeoClient login functionality"""

    @pytest.fixture
    def mock_config_manager(self):
        """Create a mock ConfigManager"""
        mock = MagicMock()
        mock.get_registry_url.return_value = "registry.example.com:5000"
        mock.get_repository.return_value = "myrepo"
        mock.get_domino_platform_namespace.return_value = "domino-platform"
        mock.get_output_dir.return_value = "/tmp/output"
        mock.get_skopeo_rate_limit_enabled.return_value = False
        mock.get_max_retries.return_value = 3
        mock.get_retry_initial_delay.return_value = 1.0
        mock.get_retry_max_delay.return_value = 60.0
        mock.get_retry_exponential_base.return_value = 2.0
        mock.get_retry_jitter.return_value = True
        mock.get_retry_timeout.return_value = 300
        return mock

    def test_login_to_registry_success(self, mock_config_manager):
        """Test successful registry login"""
        from utils.skopeo_client import SkopeoClient

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=("user", "pass")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="Login Succeeded")
                _client = SkopeoClient(mock_config_manager)  # noqa: F841 - instantiation is the test

                mock_run.assert_called()
                call_args = mock_run.call_args
                cmd = call_args[0][0]
                assert "skopeo" in cmd
                assert "login" in cmd
                assert "--username" in cmd
                assert "user" in cmd
                assert call_args[1]["input"] == "pass"

    def test_login_to_registry_no_username_raises(self, mock_config_manager):
        """Test login raises error when no username available"""
        from utils.error_utils import ActionableError
        from utils.skopeo_client import SkopeoClient

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, "pass")):
            # The error is wrapped in an ActionableError by _ensure_logged_in
            with pytest.raises((RuntimeError, ActionableError)):
                SkopeoClient(mock_config_manager)

    def test_skips_login_for_ecr(self, mock_config_manager):
        """Test login is skipped for ECR (auth handled separately)"""
        from utils.skopeo_client import SkopeoClient

        mock_config_manager.get_registry_url.return_value = "123456789.dkr.ecr.us-west-2.amazonaws.com"

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=(None, None)):
            with patch("utils.skopeo_client.authenticate_ecr"):
                with patch("subprocess.run"):
                    # Should not call subprocess.run for login since ECR handles auth differently
                    client = SkopeoClient(mock_config_manager)
                    # ECR auth is handled by authenticate_ecr, not _login_to_registry
                    assert client._logged_in is True


class TestSkopeoClientCommandBuilding:
    """Tests for SkopeoClient command building"""

    @pytest.fixture
    def skopeo_client(self):
        """Create a SkopeoClient with mocked dependencies"""
        from utils.skopeo_client import SkopeoClient

        mock_config = MagicMock()
        mock_config.get_registry_url.return_value = "registry.example.com:5000"
        mock_config.get_repository.return_value = "myrepo"
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_output_dir.return_value = "/tmp/output"
        mock_config.get_skopeo_rate_limit_enabled.return_value = False
        mock_config.get_max_retries.return_value = 3
        mock_config.get_retry_initial_delay.return_value = 1.0
        mock_config.get_retry_max_delay.return_value = 60.0
        mock_config.get_retry_exponential_base.return_value = 2.0
        mock_config.get_retry_jitter.return_value = True
        mock_config.get_retry_timeout.return_value = 300

        with patch("utils.skopeo_client.get_credentials_from_k8s_secret", return_value=("user", "pass")):
            with patch.object(SkopeoClient, "_ensure_logged_in"):
                with patch.object(SkopeoClient, "_login_to_registry"):
                    client = SkopeoClient(mock_config)
                    client._logged_in = True
                    client.auth_file = "/tmp/auth.json"
                    return client

    def test_build_skopeo_command(self, skopeo_client):
        """Test building skopeo command with auth args"""
        cmd = skopeo_client._build_skopeo_command("list-tags", ["docker://registry/repo"])
        assert cmd[0] == "skopeo"
        assert cmd[1] == "list-tags"
        assert "--tls-verify=false" in cmd
        assert "--authfile" in cmd
        assert "/tmp/auth.json" in cmd

    def test_get_auth_args(self, skopeo_client):
        """Test getting authentication arguments"""
        args = skopeo_client._get_auth_args()
        assert "--tls-verify=false" in args
        assert "--authfile" in args
        assert "/tmp/auth.json" in args

    def test_redact_command_for_logging(self, skopeo_client):
        """Test that credentials are redacted in logs"""
        cmd = ["skopeo", "login", "--creds", "user:password", "registry"]
        redacted = skopeo_client._redact_command_for_logging(cmd)
        assert "user:****" in redacted
        assert "password" not in str(redacted)

    def test_redact_command_for_logging_with_password_flag(self, skopeo_client):
        """Test that --password flag value is redacted"""
        cmd = ["skopeo", "login", "--password", "secret", "registry"]
        redacted = skopeo_client._redact_command_for_logging(cmd)
        assert "****" in redacted
        assert "secret" not in str(redacted)
