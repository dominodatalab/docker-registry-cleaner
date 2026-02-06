"""Unit tests for utils/config_manager.py"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))


# Patch the global config_manager creation to avoid validation during import
@pytest.fixture(autouse=True)
def patch_config_manager_import():
    """Patch the config_manager module to avoid auto-validation on import"""
    with patch.dict(os.environ, {"SKIP_CONFIG_VALIDATION": "true"}):
        yield


class TestConfigManagerInitialization:
    """Tests for ConfigManager initialization"""

    def test_loads_default_config_when_file_not_found(self):
        """Test that defaults are used when config file doesn't exist"""
        from utils.config_manager import ConfigManager

        cm = ConfigManager(config_file="/nonexistent/config.yaml", validate=False)

        # Should have default values
        assert cm.get_registry_url() == "docker-registry:5000"
        assert cm.get_repository() == "dominodatalab"
        assert cm.get_domino_platform_namespace() == "domino-platform"

    def test_loads_config_from_yaml_file(self):
        """Test loading configuration from a YAML file"""
        from utils.config_manager import ConfigManager

        config = {
            "registry": {"url": "custom-registry:5000", "repository": "custom-repo"},
            "kubernetes": {"domino_platform_namespace": "custom-namespace"},
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            assert cm.get_registry_url() == "custom-registry:5000"
            assert cm.get_repository() == "custom-repo"
            assert cm.get_domino_platform_namespace() == "custom-namespace"
        finally:
            os.unlink(temp_path)

    def test_merges_user_config_with_defaults(self):
        """Test that user config is merged with defaults"""
        from utils.config_manager import ConfigManager

        # Only override some values
        config = {"registry": {"url": "custom-registry:5000"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            # Custom value
            assert cm.get_registry_url() == "custom-registry:5000"
            # Default value preserved
            assert cm.get_repository() == "dominodatalab"
        finally:
            os.unlink(temp_path)

    def test_environment_variables_override_config(self):
        """Test that environment variables take precedence"""
        from utils.config_manager import ConfigManager

        with patch.dict(
            os.environ,
            {
                "REGISTRY_URL": "env-registry:5000",
                "REPOSITORY": "env-repo",
                "DOMINO_PLATFORM_NAMESPACE": "env-namespace",
                "SKIP_CONFIG_VALIDATION": "true",
            },
        ):
            cm = ConfigManager(validate=False)
            assert cm.get_registry_url() == "env-registry:5000"
            assert cm.get_repository() == "env-repo"
            assert cm.get_domino_platform_namespace() == "env-namespace"


class TestConfigManagerGetters:
    """Tests for ConfigManager getter methods"""

    @pytest.fixture
    def config_manager(self):
        """Create a ConfigManager with default config"""
        from utils.config_manager import ConfigManager

        return ConfigManager(config_file="/nonexistent/config.yaml", validate=False)

    def test_get_registry_url(self, config_manager):
        """Test get_registry_url returns correct value"""
        assert config_manager.get_registry_url() == "docker-registry:5000"

    def test_get_repository(self, config_manager):
        """Test get_repository returns correct value"""
        assert config_manager.get_repository() == "dominodatalab"

    def test_get_domino_platform_namespace(self, config_manager):
        """Test get_domino_platform_namespace returns correct value"""
        assert config_manager.get_domino_platform_namespace() == "domino-platform"

    def test_get_max_workers(self, config_manager):
        """Test get_max_workers returns integer"""
        assert config_manager.get_max_workers() == 4
        assert isinstance(config_manager.get_max_workers(), int)

    def test_get_timeout(self, config_manager):
        """Test get_timeout returns integer"""
        assert config_manager.get_timeout() == 300
        assert isinstance(config_manager.get_timeout(), int)

    def test_get_output_dir(self, config_manager):
        """Test get_output_dir returns correct value"""
        assert config_manager.get_output_dir() == "reports"

    def test_get_max_retries(self, config_manager):
        """Test get_max_retries returns integer"""
        assert config_manager.get_max_retries() == 3
        assert isinstance(config_manager.get_max_retries(), int)

    def test_get_retry_initial_delay(self, config_manager):
        """Test get_retry_initial_delay returns float"""
        assert config_manager.get_retry_initial_delay() == 1.0
        assert isinstance(config_manager.get_retry_initial_delay(), float)

    def test_get_retry_max_delay(self, config_manager):
        """Test get_retry_max_delay returns float"""
        assert config_manager.get_retry_max_delay() == 60.0
        assert isinstance(config_manager.get_retry_max_delay(), float)

    def test_get_retry_exponential_base(self, config_manager):
        """Test get_retry_exponential_base returns float"""
        assert config_manager.get_retry_exponential_base() == 2.0
        assert isinstance(config_manager.get_retry_exponential_base(), float)

    def test_get_retry_jitter(self, config_manager):
        """Test get_retry_jitter returns boolean"""
        assert config_manager.get_retry_jitter() is True

    def test_get_retry_timeout(self, config_manager):
        """Test get_retry_timeout returns integer"""
        assert config_manager.get_retry_timeout() == 300
        assert isinstance(config_manager.get_retry_timeout(), int)

    def test_is_cache_enabled(self, config_manager):
        """Test is_cache_enabled returns boolean"""
        assert config_manager.is_cache_enabled() is True

    def test_get_cache_tag_list_ttl(self, config_manager):
        """Test get_cache_tag_list_ttl returns integer"""
        assert config_manager.get_cache_tag_list_ttl() == 1800
        assert isinstance(config_manager.get_cache_tag_list_ttl(), int)

    def test_get_cache_image_inspect_ttl(self, config_manager):
        """Test get_cache_image_inspect_ttl returns integer"""
        assert config_manager.get_cache_image_inspect_ttl() == 3600
        assert isinstance(config_manager.get_cache_image_inspect_ttl(), int)

    def test_get_s3_bucket_returns_none_when_empty(self, config_manager):
        """Test get_s3_bucket returns None when not configured"""
        assert config_manager.get_s3_bucket() is None

    def test_get_s3_region_returns_default(self, config_manager):
        """Test get_s3_region returns default value"""
        assert config_manager.get_s3_region() == "us-west-2"

    def test_get_skopeo_rate_limit_enabled(self, config_manager):
        """Test get_skopeo_rate_limit_enabled returns boolean"""
        assert config_manager.get_skopeo_rate_limit_enabled() is True

    def test_get_skopeo_rate_limit_rps(self, config_manager):
        """Test get_skopeo_rate_limit_rps returns float"""
        assert config_manager.get_skopeo_rate_limit_rps() == 10.0
        assert isinstance(config_manager.get_skopeo_rate_limit_rps(), float)

    def test_get_skopeo_rate_limit_burst(self, config_manager):
        """Test get_skopeo_rate_limit_burst returns integer"""
        assert config_manager.get_skopeo_rate_limit_burst() == 20
        assert isinstance(config_manager.get_skopeo_rate_limit_burst(), int)

    def test_is_dry_run_by_default(self, config_manager):
        """Test is_dry_run_by_default returns boolean"""
        assert config_manager.is_dry_run_by_default() is True

    def test_requires_confirmation(self, config_manager):
        """Test requires_confirmation returns boolean"""
        assert config_manager.requires_confirmation() is True

    def test_get_mongo_host(self, config_manager):
        """Test get_mongo_host returns correct value"""
        assert config_manager.get_mongo_host() == "mongodb-replicaset"

    def test_get_mongo_port(self, config_manager):
        """Test get_mongo_port returns integer"""
        assert config_manager.get_mongo_port() == 27017
        assert isinstance(config_manager.get_mongo_port(), int)

    def test_get_mongo_replicaset(self, config_manager):
        """Test get_mongo_replicaset returns correct value"""
        assert config_manager.get_mongo_replicaset() == "rs0"

    def test_get_mongo_db(self, config_manager):
        """Test get_mongo_db returns correct value"""
        assert config_manager.get_mongo_db() == "domino"


class TestConfigManagerReportPaths:
    """Tests for ConfigManager report path methods"""

    @pytest.fixture
    def config_manager(self):
        """Create a ConfigManager with default config"""
        from utils.config_manager import ConfigManager

        return ConfigManager(config_file="/nonexistent/config.yaml", validate=False)

    def test_get_mongodb_usage_path(self, config_manager):
        """Test get_mongodb_usage_path returns correct path"""
        path = config_manager.get_mongodb_usage_path()
        assert "mongodb_usage_report.json" in path

    def test_get_image_analysis_path(self, config_manager):
        """Test get_image_analysis_path returns correct path"""
        path = config_manager.get_image_analysis_path()
        assert "final-report.json" in path

    def test_get_deletion_analysis_path(self, config_manager):
        """Test get_deletion_analysis_path returns correct path"""
        path = config_manager.get_deletion_analysis_path()
        assert "deletion-analysis.json" in path

    def test_report_paths_use_output_dir(self, config_manager):
        """Test that report paths are relative to output_dir"""
        output_dir = config_manager.get_output_dir()
        path = config_manager.get_mongodb_usage_path()
        assert path.startswith(output_dir)


class TestConfigManagerValidation:
    """Tests for ConfigManager validation"""

    def test_validate_config_success(self):
        """Test validate_config passes with valid config"""
        from utils.config_manager import ConfigManager

        # Default config should be valid
        cm = ConfigManager(config_file="/nonexistent/config.yaml", validate=False)
        # Should not raise
        cm.validate_config()

    def test_validate_config_empty_registry_url(self):
        """Test validate_config fails with empty registry URL"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"registry": {"url": "", "repository": "myrepo"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="Registry URL is required"):
                cm.validate_config()
        finally:
            os.unlink(temp_path)

    def test_validate_config_empty_repository(self):
        """Test validate_config fails with empty repository"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"registry": {"url": "registry:5000", "repository": ""}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="Repository name is required"):
                cm.validate_config()
        finally:
            os.unlink(temp_path)

    def test_validate_config_invalid_repository_chars(self):
        """Test validate_config fails with invalid repository characters"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"registry": {"url": "registry:5000", "repository": "invalid@repo!"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="invalid characters"):
                cm.validate_config()
        finally:
            os.unlink(temp_path)

    def test_validate_config_invalid_namespace(self):
        """Test validate_config fails with invalid Kubernetes namespace"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"kubernetes": {"domino_platform_namespace": "Invalid_Namespace"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="not a valid Kubernetes name"):
                cm.validate_config()
        finally:
            os.unlink(temp_path)

    def test_validate_config_invalid_mongo_port(self):
        """Test validate_config fails with invalid MongoDB port"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"mongo": {"host": "mongodb", "port": 99999, "replicaset": "rs0", "db": "domino"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="port must be an integer between"):
                cm.validate_config()
        finally:
            os.unlink(temp_path)

    def test_validate_config_invalid_s3_bucket_name(self):
        """Test validate_config fails with invalid S3 bucket name"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"s3": {"bucket": "INVALID_BUCKET", "region": "us-west-2"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="S3 bucket name.*is invalid"):
                cm.validate_config()
        finally:
            os.unlink(temp_path)


class TestConfigManagerValidationHelpers:
    """Tests for ConfigManager validation helper methods"""

    @pytest.fixture
    def config_manager(self):
        """Create a ConfigManager with default config"""
        from utils.config_manager import ConfigManager

        return ConfigManager(config_file="/nonexistent/config.yaml", validate=False)

    def test_is_valid_registry_url_valid(self, config_manager):
        """Test _is_valid_registry_url with valid URLs"""
        assert config_manager._is_valid_registry_url("registry.example.com") is True
        assert config_manager._is_valid_registry_url("registry.example.com:5000") is True
        assert config_manager._is_valid_registry_url("docker-registry") is True
        assert config_manager._is_valid_registry_url("https://registry.example.com") is True

    def test_is_valid_registry_url_invalid(self, config_manager):
        """Test _is_valid_registry_url with invalid URLs"""
        assert config_manager._is_valid_registry_url("") is False
        assert config_manager._is_valid_registry_url("registry with spaces") is False

    def test_is_valid_repository_name_valid(self, config_manager):
        """Test _is_valid_repository_name with valid names"""
        assert config_manager._is_valid_repository_name("myrepo") is True
        assert config_manager._is_valid_repository_name("my-repo") is True
        assert config_manager._is_valid_repository_name("my_repo") is True
        assert config_manager._is_valid_repository_name("my/repo") is True
        assert config_manager._is_valid_repository_name("dominodatalab/environment") is True

    def test_is_valid_repository_name_invalid(self, config_manager):
        """Test _is_valid_repository_name with invalid names"""
        assert config_manager._is_valid_repository_name("") is False
        assert config_manager._is_valid_repository_name("invalid@repo") is False
        assert config_manager._is_valid_repository_name("invalid repo") is False

    def test_is_valid_k8s_name_valid(self, config_manager):
        """Test _is_valid_k8s_name with valid names"""
        assert config_manager._is_valid_k8s_name("valid-name") is True
        assert config_manager._is_valid_k8s_name("validname") is True
        assert config_manager._is_valid_k8s_name("valid123") is True
        assert config_manager._is_valid_k8s_name("domino-platform") is True

    def test_is_valid_k8s_name_invalid(self, config_manager):
        """Test _is_valid_k8s_name with invalid names"""
        assert config_manager._is_valid_k8s_name("") is False
        assert config_manager._is_valid_k8s_name("Invalid-Name") is False  # Uppercase
        assert config_manager._is_valid_k8s_name("_invalid") is False  # Underscore
        assert config_manager._is_valid_k8s_name("-invalid") is False  # Starts with dash
        assert config_manager._is_valid_k8s_name("a" * 254) is False  # Too long

    def test_is_valid_s3_bucket_name_valid(self, config_manager):
        """Test _is_valid_s3_bucket_name with valid names"""
        assert config_manager._is_valid_s3_bucket_name("my-bucket") is True
        assert config_manager._is_valid_s3_bucket_name("mybucket123") is True
        assert config_manager._is_valid_s3_bucket_name("abc") is True  # Minimum length

    def test_is_valid_s3_bucket_name_invalid(self, config_manager):
        """Test _is_valid_s3_bucket_name with invalid names"""
        assert config_manager._is_valid_s3_bucket_name("") is False
        assert config_manager._is_valid_s3_bucket_name("ab") is False  # Too short
        assert config_manager._is_valid_s3_bucket_name("a" * 64) is False  # Too long
        assert config_manager._is_valid_s3_bucket_name("MyBucket") is False  # Uppercase
        assert config_manager._is_valid_s3_bucket_name("192.168.1.1") is False  # IP address


class TestConfigManagerMongoAuth:
    """Tests for ConfigManager MongoDB authentication"""

    def test_get_mongo_auth_from_env(self):
        """Test get_mongo_auth uses environment variables"""
        from utils.config_manager import ConfigManager

        with patch.dict(
            os.environ,
            {
                "MONGODB_USERNAME": "admin",
                "MONGODB_PASSWORD": "secret",
                "SKIP_CONFIG_VALIDATION": "true",
            },
        ):
            cm = ConfigManager(config_file="/nonexistent/config.yaml", validate=False)
            auth = cm.get_mongo_auth()
            assert auth == "admin:secret"

    def test_get_mongo_auth_from_k8s_secret(self):
        """Test get_mongo_auth falls back to Kubernetes secret"""
        import base64

        from utils.config_manager import ConfigManager

        mock_secret = MagicMock()
        mock_secret.data = {
            "user": base64.b64encode(b"k8s-user").decode(),
            "password": base64.b64encode(b"k8s-password").decode(),
        }

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_secret.return_value = mock_secret

        with patch.dict(os.environ, {"SKIP_CONFIG_VALIDATION": "true"}, clear=True):
            # Remove MONGODB_PASSWORD from environment
            os.environ.pop("MONGODB_PASSWORD", None)
            with patch("utils.config_manager._get_kubernetes_core_client", return_value=mock_core_v1):
                cm = ConfigManager(config_file="/nonexistent/config.yaml", validate=False)
                auth = cm.get_mongo_auth()
                assert auth == "k8s-user:k8s-password"

    def test_get_mongo_connection_string(self):
        """Test get_mongo_connection_string returns correct format"""
        from utils.config_manager import ConfigManager

        with patch.dict(
            os.environ,
            {
                "MONGODB_USERNAME": "admin",
                "MONGODB_PASSWORD": "secret",
                "SKIP_CONFIG_VALIDATION": "true",
            },
        ):
            cm = ConfigManager(config_file="/nonexistent/config.yaml", validate=False)
            conn_str = cm.get_mongo_connection_string()
            assert conn_str.startswith("mongodb://")
            assert "admin:secret@" in conn_str
            assert "replicaSet=rs0" in conn_str


class TestConfigManagerTypeCoercion:
    """Tests for ConfigManager type coercion error handling"""

    def test_get_max_workers_type_error(self):
        """Test get_max_workers raises error for invalid type"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"analysis": {"max_workers": "not-a-number"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="max_workers must be an integer"):
                cm.get_max_workers()
        finally:
            os.unlink(temp_path)

    def test_get_timeout_type_error(self):
        """Test get_timeout raises error for invalid type"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"analysis": {"timeout": "not-a-number"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="timeout must be an integer"):
                cm.get_timeout()
        finally:
            os.unlink(temp_path)

    def test_get_mongo_port_type_error(self):
        """Test get_mongo_port raises error for invalid type"""
        from utils.config_manager import ConfigManager, ConfigValidationError

        config = {"mongo": {"port": "not-a-number"}}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f)
            temp_path = f.name

        try:
            cm = ConfigManager(config_file=temp_path, validate=False)
            with pytest.raises(ConfigValidationError, match="MongoDB port must be an integer"):
                cm.get_mongo_port()
        finally:
            os.unlink(temp_path)
