"""
Kubernetes integration tests for docker-registry-cleaner.

These tests verify the Kubernetes-related functionality:
- Registry in-cluster detection
- Health checks for K8s access and RBAC
- Registry garbage collection orchestration
- Registry deletion mode management
"""

from unittest.mock import MagicMock, Mock

import pytest

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_k8s_clients(mocker):
    """Fixture providing mocked Kubernetes clients."""
    mock_core_v1 = MagicMock()
    mock_apps_v1 = MagicMock()
    mocker.patch("utils.config_manager._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))
    return mock_core_v1, mock_apps_v1


@pytest.fixture
def mock_config_manager(mocker):
    """Fixture providing mocked config_manager."""
    mock = mocker.patch("utils.health_checks.config_manager")
    mock.get_domino_platform_namespace.return_value = "domino-platform"
    mock.get_registry_url.return_value = "docker-registry.domino-platform.svc.cluster.local:5000"
    mock.get_repository.return_value = "dominodatalab"
    mock.get_mongo_host.return_value = "localhost"
    mock.get_mongo_port.return_value = 27017
    mock.get_mongo_db.return_value = "domino"
    mock.get_s3_bucket.return_value = None
    mock.get_s3_region.return_value = "us-east-1"
    return mock


@pytest.fixture
def mock_in_cluster_registry(mocker):
    """Fixture that mocks registry as in-cluster."""
    mocker.patch("utils.health_checks.is_registry_in_cluster", return_value=True)
    mocker.patch("utils.registry_maintenance.is_registry_in_cluster", return_value=True)


@pytest.fixture
def mock_external_registry(mocker):
    """Fixture that mocks registry as external (ECR, etc.)."""
    mocker.patch("utils.health_checks.is_registry_in_cluster", return_value=False)
    mocker.patch("utils.registry_maintenance.is_registry_in_cluster", return_value=False)


# ============================================================================
# Tests: is_registry_in_cluster()
# ============================================================================


class TestIsRegistryInCluster:
    """Tests for the is_registry_in_cluster() function."""

    def test_ecr_registry_returns_false(self, mocker):
        """ECR registries should be detected as external."""
        from utils.config_manager import is_registry_in_cluster

        # Mock the K8s API call to avoid actual cluster access
        mocker.patch("utils.config_manager._get_kubernetes_clients", side_effect=Exception("No cluster"))

        # ECR URLs should return False without even checking K8s
        result = is_registry_in_cluster("123456789.dkr.ecr.us-east-1.amazonaws.com", "domino-platform")
        assert result is False

    def test_gcr_registry_returns_false(self, mocker):
        """GCR registries should be detected as external."""
        from utils.config_manager import is_registry_in_cluster

        mocker.patch("utils.config_manager._get_kubernetes_clients", side_effect=Exception("No cluster"))

        result = is_registry_in_cluster("gcr.io/my-project/my-repo", "domino-platform")
        assert result is False

    def test_acr_registry_returns_false(self, mocker):
        """Azure Container Registry should be detected as external."""
        from utils.config_manager import is_registry_in_cluster

        mocker.patch("utils.config_manager._get_kubernetes_clients", side_effect=Exception("No cluster"))

        result = is_registry_in_cluster("myregistry.azurecr.io", "domino-platform")
        assert result is False

    def test_in_cluster_registry_with_statefulset_returns_true(self, mocker):
        """In-cluster registry with existing StatefulSet should return True."""
        from utils.config_manager import is_registry_in_cluster

        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mocker.patch("utils.config_manager._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))

        # Mock successful StatefulSet read
        mock_apps_v1.read_namespaced_stateful_set.return_value = Mock()

        result = is_registry_in_cluster("docker-registry.domino-platform.svc.cluster.local:5000", "domino-platform")
        assert result is True

    def test_in_cluster_registry_without_statefulset_returns_false(self, mocker):
        """In-cluster URL but no StatefulSet or Service should return False."""
        from kubernetes.client.rest import ApiException

        from utils.config_manager import is_registry_in_cluster

        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mocker.patch("utils.config_manager._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))

        # Mock 404 - Service not found (checked first)
        mock_core_v1.read_namespaced_service.side_effect = ApiException(status=404)
        # Mock 404 - StatefulSet not found (checked second)
        mock_apps_v1.read_namespaced_stateful_set.side_effect = ApiException(status=404)

        result = is_registry_in_cluster("docker-registry.domino-platform.svc.cluster.local:5000", "domino-platform")
        assert result is False


# ============================================================================
# Tests: HealthChecker.check_kubernetes_access()
# ============================================================================


class TestHealthCheckerKubernetesAccess:
    """Tests for HealthChecker.check_kubernetes_access()."""

    def test_successful_kubernetes_access(self, mocker, mock_config_manager):
        """Test successful K8s API access."""
        from utils.health_checks import HealthChecker

        # Mock K8s config loading (patch where it's imported from)
        mocker.patch("kubernetes.config.load_incluster_config")

        # Mock CoreV1Api
        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespace.return_value = Mock()
        mocker.patch("kubernetes.client.CoreV1Api", return_value=mock_core_v1)

        checker = HealthChecker()
        result = checker.check_kubernetes_access()

        assert result.status is True
        assert result.name == "kubernetes_access"
        assert "Successfully" in result.message

    def test_kubernetes_access_forbidden(self, mocker, mock_config_manager):
        """Test handling of 403 Forbidden errors."""
        from kubernetes.client.rest import ApiException

        from utils.health_checks import HealthChecker

        # Mock K8s config loading (patch where it's imported from)
        mocker.patch("kubernetes.config.load_incluster_config")

        # Mock CoreV1Api with 403 error
        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespace.side_effect = ApiException(status=403, reason="Forbidden")
        mocker.patch("kubernetes.client.CoreV1Api", return_value=mock_core_v1)

        checker = HealthChecker()
        result = checker.check_kubernetes_access()

        assert result.status is False
        assert result.name == "kubernetes_access"

    def test_kubernetes_client_not_installed(self, mocker, mock_config_manager):
        """Test graceful handling when kubernetes package is not installed."""
        from utils.health_checks import HealthChecker

        # Mock import error by making load_incluster_config and load_kube_config raise ImportError
        # The code catches ImportError at the top level, so we need to make the import fail
        mocker.patch("kubernetes.config.load_incluster_config", side_effect=ImportError("No kubernetes"))
        mocker.patch("kubernetes.config.load_kube_config", side_effect=ImportError("No kubernetes"))

        checker = HealthChecker()
        result = checker.check_kubernetes_access()

        assert result.status is False
        assert "not available" in result.message.lower() or "not installed" in result.message.lower()


# ============================================================================
# Tests: HealthChecker.check_registry_deletion_rbac()
# ============================================================================


class TestHealthCheckerRegistryDeletionRBAC:
    """Tests for HealthChecker.check_registry_deletion_rbac()."""

    def test_rbac_check_skipped_for_external_registry(self, mocker, mock_config_manager, mock_external_registry):
        """RBAC check should be skipped for external registries like ECR."""
        from utils.health_checks import HealthChecker

        checker = HealthChecker()
        result = checker.check_registry_deletion_rbac()

        assert result.status is True
        assert "skipped" in result.message.lower()

    def test_rbac_dry_run_succeeds(self, mocker, mock_config_manager, mock_in_cluster_registry):
        """Test that dry-run patch succeeds with proper RBAC."""
        from utils.health_checks import HealthChecker

        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mocker.patch("utils.health_checks._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))

        # Mock successful dry-run patch
        mock_apps_v1.patch_namespaced_stateful_set.return_value = Mock()

        checker = HealthChecker()
        result = checker.check_registry_deletion_rbac()

        assert result.status is True
        assert "PATCH" in result.message
        assert "dry-run" in result.message.lower()

    def test_rbac_forbidden_error(self, mocker, mock_config_manager, mock_in_cluster_registry):
        """Test handling of 403 Forbidden when patching StatefulSet."""
        from kubernetes.client.rest import ApiException

        from utils.health_checks import HealthChecker

        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mocker.patch("utils.health_checks._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))

        # Mock 403 Forbidden error
        mock_apps_v1.patch_namespaced_stateful_set.side_effect = ApiException(status=403, reason="Forbidden")

        checker = HealthChecker()
        result = checker.check_registry_deletion_rbac()

        assert result.status is False
        # The 403 status code may be in message or details
        assert (
            "403" in result.message
            or "permission" in result.message.lower()
            or "forbidden" in result.message.lower()
            or result.details.get("status") == 403
        )


# ============================================================================
# Tests: run_registry_garbage_collection()
# ============================================================================


class TestRegistryGarbageCollection:
    """Tests for run_registry_garbage_collection()."""

    def test_gc_skipped_for_external_registry(self, mocker, mock_external_registry):
        """GC should be skipped for external registries."""
        from utils.registry_maintenance import run_registry_garbage_collection

        mocker.patch("utils.registry_maintenance.config_manager")

        result = run_registry_garbage_collection()

        # Returns False when skipped (no GC was run)
        assert result is False

    def test_gc_executes_in_registry_pod(self, mocker, mock_in_cluster_registry):
        """Test that GC command is executed in the registry pod."""
        from utils.registry_maintenance import run_registry_garbage_collection

        # Mock config_manager
        mock_config = mocker.patch("utils.registry_maintenance.config_manager")
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_registry_url.return_value = "docker-registry:5000"

        # Mock K8s clients
        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mocker.patch("utils.registry_maintenance._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))

        # Mock StatefulSet lookup
        mock_sts = Mock()
        mock_sts.spec.selector.match_labels = {"app": "docker-registry"}
        mock_apps_v1.read_namespaced_stateful_set.return_value = mock_sts

        # Mock pod listing
        mock_pod = Mock()
        mock_pod.metadata.name = "docker-registry-0"
        mock_pod.status.phase = "Running"
        mock_pod.spec.containers = [Mock(name="registry")]
        mock_core_v1.list_namespaced_pod.return_value = Mock(items=[mock_pod])

        # Mock exec
        mock_core_v1.connect_get_namespaced_pod_exec.return_value = "GC output"

        result = run_registry_garbage_collection()

        assert result is True

        # Verify exec was called
        mock_core_v1.connect_get_namespaced_pod_exec.assert_called_once()
        call_kwargs = mock_core_v1.connect_get_namespaced_pod_exec.call_args.kwargs
        assert "garbage-collect" in call_kwargs.get("command", [])

    def test_gc_fails_when_no_pods_found(self, mocker, mock_in_cluster_registry):
        """Test that GC fails gracefully when no registry pods are found."""
        from utils.registry_maintenance import run_registry_garbage_collection

        # Mock config_manager
        mock_config = mocker.patch("utils.registry_maintenance.config_manager")
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_registry_url.return_value = "docker-registry:5000"

        # Mock K8s clients
        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mocker.patch("utils.registry_maintenance._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))

        # Mock StatefulSet lookup
        mock_sts = Mock()
        mock_sts.spec.selector.match_labels = {"app": "docker-registry"}
        mock_apps_v1.read_namespaced_stateful_set.return_value = mock_sts

        # Mock empty pod list
        mock_core_v1.list_namespaced_pod.return_value = Mock(items=[])

        result = run_registry_garbage_collection()

        assert result is False

    def test_gc_fails_when_statefulset_not_found(self, mocker, mock_in_cluster_registry):
        """Test that GC fails gracefully when StatefulSet is not found."""
        from kubernetes.client.rest import ApiException

        from utils.registry_maintenance import run_registry_garbage_collection

        # Mock config_manager
        mock_config = mocker.patch("utils.registry_maintenance.config_manager")
        mock_config.get_domino_platform_namespace.return_value = "domino-platform"
        mock_config.get_registry_url.return_value = "docker-registry:5000"

        # Mock K8s clients
        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()
        mocker.patch("utils.registry_maintenance._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1))

        # Mock StatefulSet not found
        mock_apps_v1.read_namespaced_stateful_set.side_effect = ApiException(status=404)

        result = run_registry_garbage_collection()

        assert result is False


# ============================================================================
# Tests: HealthChecker.run_all_checks()
# ============================================================================


class TestHealthCheckerRunAllChecks:
    """Tests for HealthChecker.run_all_checks()."""

    def test_run_all_checks_returns_list(self, mocker, mock_config_manager):
        """Test that run_all_checks returns a list of results."""
        from utils.health_checks import HealthChecker

        # Mock all the individual checks to return success
        checker = HealthChecker()
        mocker.patch.object(checker, "check_configuration", return_value=Mock(status=True, name="configuration"))
        mocker.patch.object(checker, "check_registry_connectivity", return_value=Mock(status=True, name="registry"))
        mocker.patch.object(checker, "check_mongodb_connectivity", return_value=Mock(status=True, name="mongodb"))
        mocker.patch.object(checker, "check_kubernetes_access", return_value=Mock(status=True, name="kubernetes"))
        mocker.patch.object(checker, "check_registry_deletion_rbac", return_value=Mock(status=True, name="rbac"))
        mocker.patch.object(checker, "check_s3_access", return_value=Mock(status=True, name="s3"))

        results = checker.run_all_checks()

        assert isinstance(results, list)
        assert len(results) >= 3  # At least config, registry, mongodb

    def test_run_all_checks_skip_optional(self, mocker, mock_config_manager):
        """Test that skip_optional=True skips K8s and S3 checks."""
        from utils.health_checks import HealthChecker

        checker = HealthChecker()
        mocker.patch.object(checker, "check_configuration", return_value=Mock(status=True, name="configuration"))
        mocker.patch.object(checker, "check_registry_connectivity", return_value=Mock(status=True, name="registry"))
        mocker.patch.object(checker, "check_mongodb_connectivity", return_value=Mock(status=True, name="mongodb"))
        mock_k8s = mocker.patch.object(checker, "check_kubernetes_access")
        mock_rbac = mocker.patch.object(checker, "check_registry_deletion_rbac")
        mock_s3 = mocker.patch.object(checker, "check_s3_access")

        _ = checker.run_all_checks(skip_optional=True)

        # K8s and RBAC checks should not be called
        mock_k8s.assert_not_called()
        mock_rbac.assert_not_called()
        # S3 should not be called since bucket is None
        mock_s3.assert_not_called()
