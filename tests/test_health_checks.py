"""Unit tests for utils/health_checks.py"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))

# Set SKIP_CONFIG_VALIDATION before importing to avoid validation errors
os.environ["SKIP_CONFIG_VALIDATION"] = "true"


class TestHealthCheckResult:
    """Tests for HealthCheckResult dataclass"""

    def test_health_check_result_creation(self):
        """Test creating a HealthCheckResult"""
        from utils.health_checks import HealthCheckResult

        result = HealthCheckResult(
            name="test_check",
            status=True,
            message="Test passed",
            details={"key": "value"},
        )

        assert result.name == "test_check"
        assert result.status is True
        assert result.message == "Test passed"
        assert result.details == {"key": "value"}

    def test_health_check_result_without_details(self):
        """Test creating a HealthCheckResult without details"""
        from utils.health_checks import HealthCheckResult

        result = HealthCheckResult(
            name="test_check",
            status=False,
            message="Test failed",
        )

        assert result.name == "test_check"
        assert result.status is False
        assert result.message == "Test failed"
        assert result.details is None


class TestHealthCheckerRegistryConnectivity:
    """Tests for HealthChecker registry connectivity checks"""

    def test_check_registry_connectivity_success(self):
        """Test successful registry connectivity check"""
        from utils.health_checks import HealthChecker

        mock_skopeo = MagicMock()
        mock_skopeo.list_tags.return_value = ["tag1", "tag2", "tag3"]

        mock_cm = MagicMock()
        mock_cm.get_registry_url.return_value = "registry.example.com:5000"
        mock_cm.get_repository.return_value = "myrepo"

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.config_manager.SkopeoClient", return_value=mock_skopeo):
                checker = HealthChecker()
                result = checker.check_registry_connectivity()

                assert result.name == "registry_connectivity"
                assert result.status is True
                assert "Successfully connected" in result.message

    def test_check_registry_connectivity_empty_tags_warning(self):
        """Test registry connectivity with empty tags (potential auth issue)"""
        from utils.health_checks import HealthChecker

        mock_skopeo = MagicMock()
        mock_skopeo.list_tags.return_value = []

        mock_cm = MagicMock()
        mock_cm.get_registry_url.return_value = "registry.example.com:5000"
        mock_cm.get_repository.return_value = "myrepo"

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.config_manager.SkopeoClient", return_value=mock_skopeo):
                checker = HealthChecker()
                result = checker.check_registry_connectivity()

                assert result.name == "registry_connectivity"
                assert result.status is False
                assert "0 tags" in result.message

    def test_check_registry_connectivity_failure(self):
        """Test registry connectivity check failure"""
        from utils.health_checks import HealthChecker

        mock_skopeo = MagicMock()
        mock_skopeo.list_tags.side_effect = Exception("Connection refused")

        mock_cm = MagicMock()
        mock_cm.get_registry_url.return_value = "registry.example.com:5000"
        mock_cm.get_repository.return_value = "myrepo"

        mock_error = MagicMock()
        mock_error.message = "Failed to connect to registry"
        mock_error.suggestions = ["Check network connectivity"]

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.config_manager.SkopeoClient", return_value=mock_skopeo):
                with patch("utils.health_checks.create_registry_connection_error", return_value=mock_error):
                    checker = HealthChecker()
                    result = checker.check_registry_connectivity()

                    assert result.name == "registry_connectivity"
                    assert result.status is False


class TestHealthCheckerMongoDBConnectivity:
    """Tests for HealthChecker MongoDB connectivity checks"""

    def test_check_mongodb_connectivity_success(self):
        """Test successful MongoDB connectivity check"""
        from utils.health_checks import HealthChecker

        mock_mongo_client = MagicMock()
        mock_mongo_client.admin.command.return_value = {"ok": 1}
        mock_db = MagicMock()
        mock_db.list_collection_names.return_value = ["collection1", "collection2"]
        mock_mongo_client.__getitem__ = MagicMock(return_value=mock_db)

        mock_cm = MagicMock()
        mock_cm.get_mongo_host.return_value = "mongodb.example.com"
        mock_cm.get_mongo_port.return_value = 27017
        mock_cm.get_mongo_db.return_value = "domino"

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.health_checks.get_mongo_client", return_value=mock_mongo_client):
                checker = HealthChecker()
                result = checker.check_mongodb_connectivity()

                assert result.name == "mongodb_connectivity"
                assert result.status is True
                assert "Successfully connected" in result.message

    def test_check_mongodb_connectivity_failure(self):
        """Test MongoDB connectivity check failure"""
        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.get_mongo_host.return_value = "mongodb.example.com"
        mock_cm.get_mongo_port.return_value = 27017
        mock_cm.get_mongo_db.return_value = "domino"

        mock_error = MagicMock()
        mock_error.message = "Failed to connect to MongoDB"
        mock_error.suggestions = ["Check MongoDB is running"]

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.health_checks.get_mongo_client", side_effect=Exception("Connection refused")):
                with patch("utils.health_checks.create_mongodb_connection_error", return_value=mock_error):
                    checker = HealthChecker()
                    result = checker.check_mongodb_connectivity()

                    assert result.name == "mongodb_connectivity"
                    assert result.status is False


class TestHealthCheckerKubernetesAccess:
    """Tests for HealthChecker Kubernetes access checks"""

    def test_check_kubernetes_access_success(self):
        """Test successful Kubernetes access check"""
        from utils.health_checks import HealthChecker

        mock_core_v1 = MagicMock()

        mock_cm = MagicMock()
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("kubernetes.config.load_incluster_config"):
                with patch("kubernetes.client.CoreV1Api", return_value=mock_core_v1):
                    checker = HealthChecker()
                    result = checker.check_kubernetes_access()

                    assert result.name == "kubernetes_access"
                    assert result.status is True
                    assert "Successfully connected" in result.message

    def test_check_kubernetes_access_failure(self):
        """Test Kubernetes access check failure when namespace read fails"""
        from kubernetes.client.rest import ApiException

        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"

        mock_core_v1 = MagicMock()
        # Simulate namespace read failure
        mock_core_v1.read_namespace.side_effect = ApiException(status=403, reason="Forbidden")

        mock_error = MagicMock()
        mock_error.message = "Failed to access Kubernetes"
        mock_error.suggestions = ["Check kubeconfig"]

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("kubernetes.config.load_incluster_config"):
                with patch("kubernetes.client.CoreV1Api", return_value=mock_core_v1):
                    with patch("utils.health_checks.create_kubernetes_error", return_value=mock_error):
                        checker = HealthChecker()
                        result = checker.check_kubernetes_access()

                        assert result.name == "kubernetes_access"
                        assert result.status is False


class TestHealthCheckerRBAC:
    """Tests for HealthChecker RBAC checks"""

    def test_check_registry_deletion_rbac_skipped_external_registry(self):
        """Test RBAC check is skipped for external registries"""
        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"
        mock_cm.get_registry_url.return_value = "external-registry.example.com:5000"

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.health_checks.is_registry_in_cluster", return_value=False):
                checker = HealthChecker()
                result = checker.check_registry_deletion_rbac()

                assert result.name == "registry_deletion_rbac"
                assert result.status is True
                assert "not running in cluster" in result.message

    def test_check_registry_deletion_rbac_success(self):
        """Test successful RBAC check for in-cluster registry"""
        from utils.health_checks import HealthChecker

        mock_apps_v1 = MagicMock()
        mock_core_v1 = MagicMock()

        mock_cm = MagicMock()
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"
        mock_cm.get_registry_url.return_value = "docker-registry:5000"

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.health_checks.is_registry_in_cluster", return_value=True):
                with patch("utils.health_checks._get_kubernetes_clients", return_value=(mock_core_v1, mock_apps_v1)):
                    checker = HealthChecker()
                    result = checker.check_registry_deletion_rbac()

                    assert result.name == "registry_deletion_rbac"
                    assert result.status is True
                    mock_apps_v1.patch_namespaced_stateful_set.assert_called_once()


class TestHealthCheckerS3Access:
    """Tests for HealthChecker S3 access checks"""

    def test_check_s3_access_not_configured(self):
        """Test S3 access check when S3 is not configured"""
        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.get_s3_bucket.return_value = None

        with patch("utils.health_checks.config_manager", mock_cm):
            checker = HealthChecker()
            result = checker.check_s3_access()

            assert result.name == "s3_access"
            assert result.status is True
            assert "not configured" in result.message

    def test_check_s3_access_success(self):
        """Test successful S3 access check"""
        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.get_s3_bucket.return_value = "my-bucket"
        mock_cm.get_s3_region.return_value = "us-west-2"

        mock_s3_client = MagicMock()

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("boto3.client", return_value=mock_s3_client):
                checker = HealthChecker()
                result = checker.check_s3_access()

                assert result.name == "s3_access"
                assert result.status is True
                assert "Successfully accessed" in result.message

    def test_check_s3_access_no_credentials(self):
        """Test S3 access check with missing credentials"""
        from botocore.exceptions import NoCredentialsError

        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.get_s3_bucket.return_value = "my-bucket"
        mock_cm.get_s3_region.return_value = "us-west-2"

        mock_error = MagicMock()
        mock_error.message = "AWS credentials not configured"
        mock_error.suggestions = ["Configure AWS credentials"]

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("boto3.client") as mock_boto3:
                mock_boto3.return_value.head_bucket.side_effect = NoCredentialsError()
                with patch("utils.health_checks.create_s3_error", return_value=mock_error):
                    checker = HealthChecker()
                    result = checker.check_s3_access()

                    assert result.name == "s3_access"
                    assert result.status is False


class TestHealthCheckerConfiguration:
    """Tests for HealthChecker configuration checks"""

    def test_check_configuration_valid(self):
        """Test configuration check with valid config"""
        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.get_registry_url.return_value = "registry.example.com:5000"
        mock_cm.get_repository.return_value = "myrepo"
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"

        with patch("utils.health_checks.config_manager", mock_cm):
            checker = HealthChecker()
            result = checker.check_configuration()

            assert result.name == "configuration"
            assert result.status is True
            assert "valid" in result.message.lower()

    def test_check_configuration_invalid(self):
        """Test configuration check with invalid config"""
        from utils.health_checks import HealthChecker

        mock_cm = MagicMock()
        mock_cm.validate_config.side_effect = Exception("Invalid config")
        mock_cm.get_registry_url.return_value = "registry.example.com:5000"
        mock_cm.get_repository.return_value = "myrepo"
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"

        with patch("utils.health_checks.config_manager", mock_cm):
            checker = HealthChecker()
            result = checker.check_configuration()

            assert result.name == "configuration"
            assert result.status is False
            assert "failed" in result.message.lower()


class TestHealthCheckerRunAllChecks:
    """Tests for HealthChecker run_all_checks method"""

    def test_run_all_checks(self):
        """Test running all health checks"""
        from utils.health_checks import HealthChecker

        mock_skopeo = MagicMock()
        mock_skopeo.list_tags.return_value = ["tag1"]

        mock_mongo_client = MagicMock()
        mock_mongo_client.admin.command.return_value = {"ok": 1}
        mock_db = MagicMock()
        mock_db.list_collection_names.return_value = []
        mock_mongo_client.__getitem__ = MagicMock(return_value=mock_db)

        mock_cm = MagicMock()
        mock_cm.get_registry_url.return_value = "registry.example.com:5000"
        mock_cm.get_repository.return_value = "myrepo"
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"
        mock_cm.get_mongo_host.return_value = "mongodb.example.com"
        mock_cm.get_mongo_port.return_value = 27017
        mock_cm.get_mongo_db.return_value = "domino"
        mock_cm.get_s3_bucket.return_value = None

        mock_core_v1 = MagicMock()
        mock_apps_v1 = MagicMock()

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.config_manager.SkopeoClient", return_value=mock_skopeo):
                with patch("utils.health_checks.get_mongo_client", return_value=mock_mongo_client):
                    with patch("kubernetes.config.load_incluster_config"):
                        with patch("kubernetes.client.CoreV1Api", return_value=mock_core_v1):
                            with patch("kubernetes.client.AppsV1Api", return_value=mock_apps_v1):
                                with patch("utils.health_checks.is_registry_in_cluster", return_value=False):
                                    checker = HealthChecker()
                                    results = checker.run_all_checks()

                                    # Should have at least configuration, registry, and mongodb checks
                                    assert len(results) >= 3
                                    check_names = [r.name for r in results]
                                    assert "configuration" in check_names
                                    assert "registry_connectivity" in check_names
                                    assert "mongodb_connectivity" in check_names

    def test_run_all_checks_skip_optional(self):
        """Test running all health checks with optional checks skipped"""
        from utils.health_checks import HealthChecker

        mock_skopeo = MagicMock()
        mock_skopeo.list_tags.return_value = ["tag1"]

        mock_mongo_client = MagicMock()
        mock_mongo_client.admin.command.return_value = {"ok": 1}
        mock_db = MagicMock()
        mock_db.list_collection_names.return_value = []
        mock_mongo_client.__getitem__ = MagicMock(return_value=mock_db)

        mock_cm = MagicMock()
        mock_cm.get_registry_url.return_value = "registry.example.com:5000"
        mock_cm.get_repository.return_value = "myrepo"
        mock_cm.get_domino_platform_namespace.return_value = "domino-platform"
        mock_cm.get_mongo_host.return_value = "mongodb.example.com"
        mock_cm.get_mongo_port.return_value = 27017
        mock_cm.get_mongo_db.return_value = "domino"
        mock_cm.get_s3_bucket.return_value = None

        with patch("utils.health_checks.config_manager", mock_cm):
            with patch("utils.config_manager.SkopeoClient", return_value=mock_skopeo):
                with patch("utils.health_checks.get_mongo_client", return_value=mock_mongo_client):
                    checker = HealthChecker()
                    results = checker.run_all_checks(skip_optional=True)

                    check_names = [r.name for r in results]
                    # Kubernetes access and RBAC should be skipped
                    assert "kubernetes_access" not in check_names
                    assert "registry_deletion_rbac" not in check_names


class TestHealthCheckerPrintReport:
    """Tests for HealthChecker print_health_report method"""

    def test_print_health_report_all_healthy(self, capsys):
        """Test printing health report when all checks pass"""
        from utils.health_checks import HealthChecker, HealthCheckResult

        results = [
            HealthCheckResult(name="check1", status=True, message="OK"),
            HealthCheckResult(name="check2", status=True, message="OK"),
        ]

        mock_cm = MagicMock()
        with patch("utils.health_checks.config_manager", mock_cm):
            checker = HealthChecker()
            all_healthy = checker.print_health_report(results)

            assert all_healthy is True
            captured = capsys.readouterr()
            assert "HEALTHY" in captured.out
            assert "All health checks passed" in captured.out

    def test_print_health_report_some_unhealthy(self, capsys):
        """Test printing health report when some checks fail"""
        from utils.health_checks import HealthChecker, HealthCheckResult

        results = [
            HealthCheckResult(name="check1", status=True, message="OK"),
            HealthCheckResult(name="check2", status=False, message="Failed"),
        ]

        mock_cm = MagicMock()
        with patch("utils.health_checks.config_manager", mock_cm):
            checker = HealthChecker()
            all_healthy = checker.print_health_report(results)

            assert all_healthy is False
            captured = capsys.readouterr()
            assert "UNHEALTHY" in captured.out
            assert "Some health checks failed" in captured.out
