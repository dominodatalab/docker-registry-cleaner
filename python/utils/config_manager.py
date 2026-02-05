#!/usr/bin/env python3
"""
Configuration Manager for Docker Registry Cleaner

This module handles loading and managing configuration from config.yaml
and environment variables.
"""

import base64
import json
import logging
import os
import re
import subprocess
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import yaml

from utils.cache_utils import cached_image_inspect, cached_tag_list
from utils.retry_utils import is_retryable_error, retry_with_backoff


class ConfigValidationError(Exception):
    """Raised when configuration validation fails"""


def _load_kubernetes_config():
    """Helper function to load Kubernetes configuration.

    Tries in-cluster config first, then falls back to local kubeconfig.

    Returns:
        None

    Raises:
        Exception if both methods fail
    """
    try:
        from kubernetes.config import load_incluster_config

        load_incluster_config()
    except Exception:
        from kubernetes.config import load_kube_config

        load_kube_config()


def _get_kubernetes_clients() -> Tuple[Any, Any]:
    """Helper function to get Kubernetes API clients.

    Returns:
        Tuple of (CoreV1Api, AppsV1Api)

    Raises:
        ImportError if kubernetes package is not available
    """
    from kubernetes import client as k8s_client

    _load_kubernetes_config()

    return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()


def is_registry_in_cluster(registry_url: str, namespace: str) -> bool:
    """Check if the registry service exists in the Kubernetes cluster.

    Parses the registry URL to extract the service name and checks if it exists
    as a Service or StatefulSet in the cluster. Use this when you need to know
    if Docker/registry is running in-cluster without creating a SkopeoClient.

    Args:
        registry_url: Registry URL (e.g. "docker-registry:5000", "registry.namespace.svc:5000").
        namespace: Kubernetes namespace to check when URL has no namespace segment.

    Returns:
        True if registry Service or StatefulSet is found in the cluster, False otherwise.
    """
    try:
        from kubernetes.client.rest import ApiException

        url = registry_url.split(":")[0]
        parts = url.split(".")
        service_name = parts[0]
        check_namespace = parts[1] if len(parts) > 1 and parts[1] != "svc" else namespace

        try:
            core_v1, apps_v1 = _get_kubernetes_clients()
        except Exception as e:
            logging.debug(f"Could not load Kubernetes config: {e}")
            return False

        try:
            core_v1.read_namespaced_service(name=service_name, namespace=check_namespace)
            logging.debug(f"Found {service_name} Service in namespace {check_namespace}")
            return True
        except ApiException as e:
            if e.status != 404:
                logging.debug(f"Error checking for Service: {e}")

        try:
            apps_v1.read_namespaced_stateful_set(name=service_name, namespace=check_namespace)
            logging.debug(f"Found {service_name} StatefulSet in namespace {check_namespace}")
            return True
        except ApiException as e:
            if e.status != 404:
                logging.debug(f"Error checking for StatefulSet: {e}")

        logging.debug(f"Registry '{service_name}' not found in namespace {check_namespace}")
        return False
    except ImportError:
        logging.debug("Kubernetes client not available")
        return False
    except Exception as e:
        logging.debug(f"Unexpected error checking registry in cluster: {e}")
        return False


class ConfigManager:
    """Manages configuration for the Docker registry cleaner project"""

    def __init__(self, config_file: str = None, validate: bool = True):
        """Initialize ConfigManager

        Args:
            config_file: Path to configuration YAML file (defaults to ../config.yaml or CONFIG_FILE env var)
            validate: If True, validate configuration on initialization
        """
        # Allow override via environment variable for containerized deployments
        if config_file is None:
            config_file = os.environ.get("CONFIG_FILE", "../config.yaml")
        self.config_file = config_file
        self.config = self._load_config()

        # Set up skopeo auth file early (before any skopeo commands run)
        # This ensures nonroot users can authenticate (can't write to /run/containers)
        auth_dir = self.get_output_dir()
        os.makedirs(auth_dir, exist_ok=True)
        self.auth_file = os.path.join(auth_dir, ".registry-auth.json")
        os.environ["REGISTRY_AUTH_FILE"] = self.auth_file

        if validate:
            self.validate_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file with defaults"""
        default_config = {
            "registry": {"url": "docker-registry:5000", "repository": "dominodatalab"},
            "kubernetes": {"domino_platform_namespace": "domino-platform"},
            "mongo": {"host": "mongodb-replicaset", "port": 27017, "replicaset": "rs0", "db": "domino"},
            "analysis": {"max_workers": 4, "timeout": 300, "output_dir": "reports"},
            "retry": {
                "max_retries": 3,
                "initial_delay": 1.0,
                "max_delay": 60.0,
                "exponential_base": 2.0,
                "jitter": True,
                "timeout": 300,  # Timeout for subprocess calls in seconds
            },
            "s3": {"bucket": "", "region": "us-west-2"},
            "skopeo": {
                "rate_limit": {
                    "enabled": True,
                    "requests_per_second": 10.0,  # Max requests per second
                    "burst_size": 20,  # Allow burst of up to N requests
                },
            },
            "reports": {
                "archived_tags": "archived-tags.json",
                "deletion_analysis": "deletion-analysis.json",
                "filtered_layers": "filtered-layers.json",
                "image_analysis": "final-report.json",
                "images_report": "images-report",
                "layers_and_sizes": "layers-and-sizes.json",
                "tags_per_layer": "tags-per-layer.json",
                "tag_sums": "tag-sums.json",
                "unused_references": "unused-references.json",
                "mongodb_usage": "mongodb_usage_report.json",  # Consolidated report for all MongoDB usage data
            },
            "security": {"dry_run_by_default": True, "require_confirmation": True},
            "cache": {
                "enabled": True,
                "tag_list_ttl": 1800,
                "tag_list_max_size": 100,
                "image_inspect_ttl": 3600,
                "image_inspect_max_size": 1000,
                "mongo_query_ttl": 600,
                "mongo_query_max_size": 500,
                "layer_calc_ttl": 7200,
                "layer_calc_max_size": 2000,
            },
        }

        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r") as f:
                    user_config = yaml.safe_load(f) or {}
                return self._merge_config(default_config, user_config)
            else:
                logging.warning(f"Config file {self.config_file} not found, using defaults")
                return default_config
        except Exception as e:
            logging.error(f"Error loading config file: {e}")
            return default_config

    def _merge_config(self, default: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively merge user config with defaults"""
        result = default.copy()
        for key, value in user.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result

    # Registry configuration
    def get_registry_url(self) -> str:
        """Get registry URL from environment or config"""
        return os.environ.get("REGISTRY_URL") or self.config["registry"]["url"]

    def get_repository(self) -> str:
        """Get canonical repository value.
        Priority: env REPOSITORY -> config.registry.repository -> config.registry.repository_name
        """
        return os.environ.get("REPOSITORY") or self.config["registry"]["repository"]

    def _get_password_from_k8s_secret(self) -> Optional[str]:
        """Get Docker registry password from Kubernetes secret.

        Attempts to read credentials from the 'domino-registry' secret in the
        domino-platform namespace (or configured platform namespace). This secret
        should contain a .dockerconfigjson with Docker registry credentials.

        Returns:
            Password string if found, None otherwise
        """
        try:
            from kubernetes.client.rest import ApiException

            core_v1, _ = _get_kubernetes_clients()
            namespace = self.get_domino_platform_namespace()
            secret_name = "domino-registry"

            logging.debug(f"Attempting to read {secret_name} secret from namespace {namespace}")

            try:
                secret = core_v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            except ApiException as e:
                if e.status == 404:
                    logging.debug(f"Secret {secret_name} not found in namespace {namespace}")
                else:
                    logging.debug(f"Error reading secret {secret_name}: {e}")
                return None

            # Read .dockerconfigjson from the secret
            if not secret.data or ".dockerconfigjson" not in secret.data:
                logging.debug(f"Secret {secret_name} does not contain .dockerconfigjson")
                return None

            # Decode the dockerconfigjson
            dockerconfig_b64 = secret.data[".dockerconfigjson"]
            dockerconfig_json = base64.b64decode(dockerconfig_b64).decode("utf-8")
            dockerconfig = json.loads(dockerconfig_json)

            # Extract credentials for our registry
            registry_url = self.get_registry_url()
            # Normalize registry URL for matching (remove port, protocol, etc.)
            registry_host = registry_url.split(":")[0].split("/")[0]

            if "auths" not in dockerconfig:
                logging.debug("No 'auths' section in dockerconfigjson")
                return None

            # Try to find matching registry in auths
            for auth_url, auth_data in dockerconfig["auths"].items():
                # Check if this auth entry matches our registry
                auth_host = auth_url.split(":")[0].split("/")[0]
                if auth_host == registry_host or registry_host in auth_host:
                    # Try to get password directly
                    if "password" in auth_data:
                        logging.info(f"Found registry password in {secret_name} secret")
                        return auth_data["password"]
                    # Otherwise decode from 'auth' field (base64 encoded "username:password")
                    elif "auth" in auth_data:
                        auth_decoded = base64.b64decode(auth_data["auth"]).decode("utf-8")
                        if ":" in auth_decoded:
                            _, password = auth_decoded.split(":", 1)
                            logging.info(f"Found registry password in {secret_name} secret (from auth field)")
                            return password

            logging.debug(f"No matching registry credentials found in {secret_name} secret for {registry_url}")
            return None

        except Exception as e:
            # Log but don't fail - fall back to other auth methods
            logging.debug(f"Could not read credentials from Kubernetes secret: {e}")
            return None

    def get_registry_password(self) -> Optional[str]:
        """Get registry password from environment, Kubernetes secret, or handle ECR authentication.

        Priority order:
        1. REGISTRY_PASSWORD environment variable (explicit override)
        2. Kubernetes secret (domino-registry in platform namespace)
        3. ECR authentication (for AWS ECR registries)
        """
        # First check explicit password from environment
        password = os.environ.get("REGISTRY_PASSWORD")
        if password:
            return password

        # Try to get password from Kubernetes secret
        password = self._get_password_from_k8s_secret()
        if password:
            return password

        # If no password and registry is ECR, handle authentication
        registry_url = self.get_registry_url()
        if "amazonaws.com" in registry_url:
            self._authenticate_ecr(registry_url)
            # After authentication, we don't need to return a password
            # as skopeo will use the authenticated session
            return None

        return None

    def _authenticate_ecr(self, registry_url: str) -> None:
        """Authenticate with ECR using boto3 (no shell required)."""
        try:
            # Extract region from registry URL
            # ECR URLs are typically: account.dkr.ecr.region.amazonaws.com
            parts = registry_url.split(".")
            if len(parts) >= 4 and parts[-2] == "amazonaws" and parts[-1] == "com":
                region = parts[-3]  # Extract region from URL
            else:
                region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

            logging.info(f"Authenticating with ECR in region: {region}")

            # Get ECR login password via boto3 (no aws CLI or shell needed)
            import boto3

            client = boto3.client("ecr", region_name=region)
            response = client.get_authorization_token()
            token_b64 = response["authorizationData"][0]["authorizationToken"]
            token = base64.b64decode(token_b64).decode("utf-8")
            _, password = token.split(":", 1)

            # Run skopeo login with password on stdin (no shell)
            # Use explicit --authfile to ensure nonroot users can write auth
            subprocess.run(
                [
                    "skopeo",
                    "login",
                    "--authfile",
                    self.auth_file,
                    "--username",
                    "AWS",
                    "--password-stdin",
                    registry_url,
                ],
                input=password,
                capture_output=True,
                text=True,
                check=True,
            )
            logging.info("ECR authentication successful")

        except subprocess.CalledProcessError as e:
            logging.error(f"ECR authentication failed: {e}")
            if e.stderr:
                logging.error(f"  stderr: {e.stderr}")
            raise
        except Exception as e:
            logging.error(f"Unexpected error during ECR authentication: {e}")
            raise

    # Kubernetes configuration
    def get_domino_platform_namespace(self) -> str:
        """Get Domino Platform namespace from environment or config"""
        return os.environ.get("DOMINO_PLATFORM_NAMESPACE") or self.config["kubernetes"]["domino_platform_namespace"]

    # Analysis configuration
    def get_max_workers(self) -> int:
        """Get max workers from config, with type coercion"""
        workers = self.config["analysis"]["max_workers"]
        try:
            return int(workers)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"max_workers must be an integer, got: {workers} (type: {type(workers).__name__})"
            )

    def get_timeout(self) -> int:
        """Get timeout from config, with type coercion"""
        timeout = self.config["analysis"]["timeout"]
        try:
            return int(timeout)
        except (ValueError, TypeError):
            raise ConfigValidationError(f"timeout must be an integer, got: {timeout} (type: {type(timeout).__name__})")

    def get_output_dir(self) -> str:
        """Get output directory from config"""
        return self.config["analysis"]["output_dir"]

    # Retry configuration
    def get_max_retries(self) -> int:
        """Get max retries from config, with type coercion"""
        retries = self.config.get("retry", {}).get("max_retries", 3)
        try:
            return int(retries)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"retry.max_retries must be an integer, got: {retries} (type: {type(retries).__name__})"
            )

    def get_retry_initial_delay(self) -> float:
        """Get initial retry delay from config, with type coercion"""
        delay = self.config.get("retry", {}).get("initial_delay", 1.0)
        try:
            return float(delay)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"retry.initial_delay must be a number, got: {delay} (type: {type(delay).__name__})"
            )

    def get_retry_max_delay(self) -> float:
        """Get max retry delay from config, with type coercion"""
        delay = self.config.get("retry", {}).get("max_delay", 60.0)
        try:
            return float(delay)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"retry.max_delay must be a number, got: {delay} (type: {type(delay).__name__})"
            )

    def get_retry_exponential_base(self) -> float:
        """Get exponential base for retry backoff from config, with type coercion"""
        base = self.config.get("retry", {}).get("exponential_base", 2.0)
        try:
            return float(base)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"retry.exponential_base must be a number, got: {base} (type: {type(base).__name__})"
            )

    def get_retry_jitter(self) -> bool:
        """Get whether to use jitter in retry delays from config"""
        return self.config.get("retry", {}).get("jitter", True)

    def get_retry_timeout(self) -> int:
        """Get timeout for subprocess calls from config, with type coercion"""
        timeout = self.config.get("retry", {}).get("timeout", 300)
        try:
            return int(timeout)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"retry.timeout must be an integer, got: {timeout} (type: {type(timeout).__name__})"
            )

    # Cache configuration
    def is_cache_enabled(self) -> bool:
        """Get cache enabled setting from config"""
        return self.config.get("cache", {}).get("enabled", True)

    def get_cache_tag_list_ttl(self) -> int:
        """Get tag list cache TTL from config, with type coercion"""
        ttl = self.config.get("cache", {}).get("tag_list_ttl", 1800)
        try:
            return int(ttl)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"cache.tag_list_ttl must be an integer, got: {ttl} (type: {type(ttl).__name__})"
            )

    def get_cache_image_inspect_ttl(self) -> int:
        """Get image inspect cache TTL from config, with type coercion"""
        ttl = self.config.get("cache", {}).get("image_inspect_ttl", 3600)
        try:
            return int(ttl)
        except (ValueError, TypeError):
            raise ConfigValidationError(
                f"cache.image_inspect_ttl must be an integer, got: {ttl} (type: {type(ttl).__name__})"
            )

    # S3 Configuration
    def get_s3_bucket(self) -> Optional[str]:
        """Get S3 bucket from environment or config

        Returns empty string if not configured, which evaluates to falsy
        """
        bucket = os.environ.get("S3_BUCKET") or self.config.get("s3", {}).get("bucket", "")
        return bucket if bucket else None

    def get_s3_region(self) -> str:
        """Get S3 region from environment or config"""
        return os.environ.get("S3_REGION") or self.config.get("s3", {}).get("region", "us-west-2")

    def get_skopeo_rate_limit_enabled(self) -> bool:
        """Get whether rate limiting is enabled for Skopeo operations"""
        return self.config.get("skopeo", {}).get("rate_limit", {}).get("enabled", True)

    def get_skopeo_rate_limit_rps(self) -> float:
        """Get requests per second for Skopeo rate limiting"""
        return float(self.config.get("skopeo", {}).get("rate_limit", {}).get("requests_per_second", 10.0))

    def get_skopeo_rate_limit_burst(self) -> int:
        """Get burst size for Skopeo rate limiting"""
        return int(self.config.get("skopeo", {}).get("rate_limit", {}).get("burst_size", 20))

    # Report configuration
    def _resolve_report_path(self, path: str) -> str:
        """Resolve report file path under the configured output_dir unless absolute or already a path.
        If the value is just a filename, prefix it with output_dir.
        """
        # If absolute or contains a directory component, return as is
        if os.path.isabs(path) or os.path.basename(path) != path:
            return path
        return os.path.join(self.get_output_dir(), path)

    def get_mongodb_usage_path(self) -> str:
        """Get consolidated MongoDB usage report path from config"""
        return self._resolve_report_path(self.config["reports"]["mongodb_usage"])

    def get_image_analysis_path(self) -> str:
        """Get image analysis path from config"""
        return self._resolve_report_path(self.config["reports"]["image_analysis"])

    def get_deletion_analysis_path(self) -> str:
        """Get deletion analysis path from config"""
        return self._resolve_report_path(self.config["reports"]["deletion_analysis"])

    def get_tags_per_layer_path(self) -> str:
        """Get tags per layer report path from config"""
        return self._resolve_report_path(self.config["reports"]["tags_per_layer"])

    def get_layers_and_sizes_path(self) -> str:
        """Get layers and sizes report path from config"""
        return self._resolve_report_path(self.config["reports"]["layers_and_sizes"])

    def get_filtered_layers_path(self) -> str:
        """Get filtered layers report path from config"""
        return self._resolve_report_path(self.config["reports"]["filtered_layers"])

    def get_tag_sums_path(self) -> str:
        """Get tag sums report path from config"""
        return self._resolve_report_path(self.config["reports"]["tag_sums"])

    def get_images_report_path(self) -> str:
        """Get images report path from config"""
        # images_report is a base name used to save both .txt and .json
        base = self.config["reports"]["images_report"]
        # If it's just a filename, prefix with output_dir
        if os.path.isabs(base) or os.path.basename(base) != base:
            return base
        return os.path.join(self.get_output_dir(), base)

    def get_archived_tags_report_path(self) -> str:
        """Get archived tags report path from config"""
        return self._resolve_report_path(self.config["reports"]["archived_tags"])

    def get_unused_references_report_path(self) -> str:
        """Get unused references report path from config"""
        return self._resolve_report_path(self.config["reports"]["unused_references"])

    def get_archived_model_tags_report_path(self) -> str:
        """Get archived model tags report path from config"""
        return self._resolve_report_path(self.config["reports"]["archived_model_tags"])

    # Security configuration
    def is_dry_run_by_default(self) -> bool:
        """Get dry run default from config"""
        return self.config["security"]["dry_run_by_default"]

    def requires_confirmation(self) -> bool:
        """Get confirmation requirement from config"""
        return self.config["security"]["require_confirmation"]

    # Mongo configuration
    def get_mongo_host(self) -> str:
        return self.config["mongo"]["host"]

    def get_mongo_port(self) -> int:
        """Get MongoDB port from config, with type coercion"""
        port = self.config["mongo"]["port"]
        try:
            return int(port)
        except (ValueError, TypeError):
            raise ConfigValidationError(f"MongoDB port must be an integer, got: {port} (type: {type(port).__name__})")

    def get_mongo_replicaset(self) -> str:
        return self.config["mongo"]["replicaset"]

    def get_mongo_db(self) -> str:
        return self.config["mongo"]["db"]

    def get_mongo_auth(self) -> str:
        username = os.environ.get("MONGODB_USERNAME", "admin")
        password = os.environ.get("MONGODB_PASSWORD")

        if password:
            return f"{username}:{password}"

        # Fallback: read credentials from Kubernetes secret mongodb-replicaset-admin
        try:
            core_v1, _ = _get_kubernetes_clients()
            namespace = self.get_domino_platform_namespace()
            secret = core_v1.read_namespaced_secret(name="mongodb-replicaset-admin", namespace=namespace)
            data = secret.data or {}
            secret_user_b64 = data.get("user")
            secret_pass_b64 = data.get("password")

            if secret_pass_b64:
                decoded_password = base64.b64decode(secret_pass_b64).decode("utf-8")
                decoded_username = username
                if secret_user_b64:
                    decoded_username = base64.b64decode(secret_user_b64).decode("utf-8")
                return f"{decoded_username}:{decoded_password}"

            raise RuntimeError("Mongo password not found in secret 'mongodb-replicaset-admin'")
        except Exception as e:
            raise RuntimeError(
                "Mongo password is not set and failed to load from Kubernetes secret 'mongodb-replicaset-admin'"
            ) from e

    def get_mongo_connection_string(self) -> str:
        auth = self.get_mongo_auth()
        host = self.get_mongo_host()
        port = self.get_mongo_port()
        rs = self.get_mongo_replicaset()
        return f"mongodb://{auth}@{host}:{port}/?replicaSet={rs}"

    def validate_config(self) -> None:
        """Validate configuration values

        Raises:
            ConfigValidationError: If configuration is invalid
        """
        errors = []
        warnings = []

        # Validate registry configuration
        registry_url = self.get_registry_url()
        if not registry_url or not registry_url.strip():
            errors.append("Registry URL is required and cannot be empty")
        elif not self._is_valid_registry_url(registry_url):
            warnings.append(
                f"Registry URL '{registry_url}' may be invalid (expected format: hostname[:port] or hostname.namespace[:port])"
            )

        repository = self.get_repository()
        if not repository or not repository.strip():
            errors.append("Repository name is required and cannot be empty")
        elif not self._is_valid_repository_name(repository):
            errors.append(
                f"Repository name '{repository}' contains invalid characters (alphanumeric, hyphens, underscores, and slashes only)"
            )

        # Validate Kubernetes configuration
        namespace = self.get_domino_platform_namespace()
        if not namespace or not namespace.strip():
            errors.append("Domino platform namespace is required and cannot be empty")
        elif not self._is_valid_k8s_name(namespace):
            errors.append(
                f"Namespace '{namespace}' is not a valid Kubernetes name (lowercase alphanumeric and hyphens only)"
            )

        # Validate MongoDB configuration
        mongo_host = self.get_mongo_host()
        if not mongo_host or not mongo_host.strip():
            errors.append("MongoDB host is required and cannot be empty")

        mongo_port = self.get_mongo_port()
        if not isinstance(mongo_port, int) or mongo_port < 1 or mongo_port > 65535:
            errors.append(f"MongoDB port must be an integer between 1 and 65535, got: {mongo_port}")

        mongo_replicaset = self.get_mongo_replicaset()
        if not mongo_replicaset or not mongo_replicaset.strip():
            errors.append("MongoDB replicaset name is required and cannot be empty")

        mongo_db = self.get_mongo_db()
        if not mongo_db or not mongo_db.strip():
            errors.append("MongoDB database name is required and cannot be empty")

        # Validate analysis configuration
        max_workers = self.get_max_workers()
        if not isinstance(max_workers, int) or max_workers < 1:
            errors.append(f"max_workers must be a positive integer, got: {max_workers}")
        elif max_workers > 100:
            warnings.append(f"max_workers is very high ({max_workers}), this may cause resource issues")

        timeout = self.get_timeout()
        if not isinstance(timeout, int) or timeout < 1:
            errors.append(f"timeout must be a positive integer (seconds), got: {timeout}")
        elif timeout > 3600:
            warnings.append(f"timeout is very high ({timeout}s), operations may take a long time")

        output_dir = self.get_output_dir()
        if not output_dir or not output_dir.strip():
            errors.append("output_dir is required and cannot be empty")

        # Validate retry configuration
        max_retries = self.get_max_retries()
        if not isinstance(max_retries, int) or max_retries < 0:
            errors.append(f"retry.max_retries must be a non-negative integer, got: {max_retries}")
        elif max_retries > 10:
            warnings.append(f"max_retries is very high ({max_retries}), operations may take a long time")

        initial_delay = self.get_retry_initial_delay()
        if not isinstance(initial_delay, (int, float)) or initial_delay < 0:
            errors.append(f"retry.initial_delay must be a non-negative number, got: {initial_delay}")

        max_delay = self.get_retry_max_delay()
        if not isinstance(max_delay, (int, float)) or max_delay < 0:
            errors.append(f"retry.max_delay must be a non-negative number, got: {max_delay}")
        elif max_delay < initial_delay:
            errors.append(f"retry.max_delay ({max_delay}) must be >= retry.initial_delay ({initial_delay})")

        exponential_base = self.get_retry_exponential_base()
        if not isinstance(exponential_base, (int, float)) or exponential_base < 1.0:
            errors.append(f"retry.exponential_base must be >= 1.0, got: {exponential_base}")

        retry_timeout = self.get_retry_timeout()
        if not isinstance(retry_timeout, int) or retry_timeout < 1:
            errors.append(f"retry.timeout must be a positive integer (seconds), got: {retry_timeout}")

        # Validate cache configuration
        if self.is_cache_enabled():
            tag_list_ttl = self.get_cache_tag_list_ttl()
            if not isinstance(tag_list_ttl, int) or tag_list_ttl < 0:
                errors.append(f"cache.tag_list_ttl must be a non-negative integer, got: {tag_list_ttl}")

            image_inspect_ttl = self.get_cache_image_inspect_ttl()
            if not isinstance(image_inspect_ttl, int) or image_inspect_ttl < 0:
                errors.append(f"cache.image_inspect_ttl must be a non-negative integer, got: {image_inspect_ttl}")

        # Validate S3 configuration (if bucket is provided)
        s3_bucket = self.get_s3_bucket()
        if s3_bucket:
            if not self._is_valid_s3_bucket_name(s3_bucket):
                errors.append(
                    f"S3 bucket name '{s3_bucket}' is invalid (must be 3-63 characters, lowercase alphanumeric and hyphens only)"
                )

            s3_region = self.get_s3_region()
            if not s3_region or not s3_region.strip():
                errors.append("S3 region is required when S3 bucket is configured")

        # Log warnings
        for warning in warnings:
            logging.warning(f"Configuration warning: {warning}")

        # Raise error if there are validation errors
        if errors:
            error_msg = "Configuration validation failed:\n  " + "\n  ".join(errors)
            logging.error(error_msg)
            raise ConfigValidationError(error_msg)

    def _is_valid_registry_url(self, url: str) -> bool:
        """Validate registry URL format"""
        if not url:
            return False

        # Remove protocol if present
        url = url.replace("http://", "").replace("https://", "")

        # Basic validation: should contain hostname and optional port
        # Formats: "hostname:port", "hostname.namespace:port", "hostname.namespace.svc.cluster.local:port"
        pattern = r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?(:[0-9]{1,5})?$"
        return bool(re.match(pattern, url))

    def _is_valid_repository_name(self, name: str) -> bool:
        """Validate repository name format"""
        if not name:
            return False
        # Allow alphanumeric, hyphens, underscores, and slashes
        pattern = r"^[a-zA-Z0-9_\-/]+$"
        return bool(re.match(pattern, name))

    def _is_valid_k8s_name(self, name: str) -> bool:
        """Validate Kubernetes resource name format"""
        if not name:
            return False
        # Kubernetes names: lowercase alphanumeric and hyphens, max 253 chars
        pattern = r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$"
        return bool(re.match(pattern, name)) and len(name) <= 253

    def _is_valid_s3_bucket_name(self, name: str) -> bool:
        """Validate S3 bucket name format"""
        if not name:
            return False
        # S3 bucket names: 3-63 characters, lowercase alphanumeric and hyphens, not IP address format
        if len(name) < 3 or len(name) > 63:
            return False
        pattern = r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$"
        if not re.match(pattern, name):
            return False
        # Cannot be formatted as IP address
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", name):
            return False
        return True

    def print_config(self):
        """Print current configuration"""
        print("Current Configuration:")
        print(f"  Registry URL: {self.get_registry_url()}")
        print(f"  Repository Name: {self.get_repository()}")
        print(f"  Domino Platform Namespace: {self.get_domino_platform_namespace()}")
        print(f"  Max Workers: {self.get_max_workers()}")
        print(f"  Timeout: {self.get_timeout()}")
        print(f"  Output Directory: {self.get_output_dir()}")
        print(f"  Dry Run Default: {self.is_dry_run_by_default()}")
        print(f"  Require Confirmation: {self.requires_confirmation()}")

        # S3 Configuration
        s3_bucket = self.get_s3_bucket()
        s3_region = self.get_s3_region()
        print(f"  S3 Bucket: {s3_bucket or 'Not configured'}")
        print(f"  S3 Region: {s3_region}")

        password = self.get_registry_password()
        if password:
            print(f"  Registry Password: {'*' * len(password)}")
        else:
            print("  Registry Password: Not set")


class SkopeoClient:
    """Standardized Skopeo client for registry operations"""

    def __init__(
        self,
        config_manager: ConfigManager,
        namespace: str = None,
        enable_docker_deletion: bool = False,
        registry_statefulset: str = None,
    ):
        """Initialize SkopeoClient.

        Args:
            config_manager: ConfigManager instance for accessing configuration
            namespace: Kubernetes namespace (defaults to platform namespace from config)
            enable_docker_deletion: If True, enable registry deletion by treating registry as in-cluster
            registry_statefulset: Name of the registry StatefulSet to modify for deletion.
                                  Defaults to "docker-registry" if enable_docker_deletion is True.
                                  Only used when enable_docker_deletion is True.
        """
        self.config_manager = config_manager
        self.namespace = namespace or config_manager.get_domino_platform_namespace()
        self.registry_url = config_manager.get_registry_url()
        self.repository = config_manager.get_repository()
        self._logged_in = False

        # Registry deletion override settings
        self.enable_docker_deletion = enable_docker_deletion
        self.registry_statefulset = registry_statefulset or "docker-registry"

        # Rate limiting
        self.rate_limit_enabled = config_manager.get_skopeo_rate_limit_enabled()
        self.rate_limit_rps = config_manager.get_skopeo_rate_limit_rps()
        self.rate_limit_burst = config_manager.get_skopeo_rate_limit_burst()
        self._rate_limiter = None
        self._rate_limiter_lock = Lock()

        # Point skopeo at a writable auth file before any skopeo call
        # (nonroot can't write /run/containers; must be set before get_registry_password() which may run ECR login)
        auth_dir = config_manager.get_output_dir()
        auth_file = os.path.join(auth_dir, ".registry-auth.json")
        os.environ["REGISTRY_AUTH_FILE"] = auth_file

        self.password = config_manager.get_registry_password()

        if self.rate_limit_enabled:
            self._init_rate_limiter()

        self._ensure_logged_in()

    def _init_rate_limiter(self):
        """Initialize token bucket rate limiter"""
        # Simple token bucket implementation
        self._tokens = float(self.rate_limit_burst)
        self._last_update = time.time()
        self._token_refill_rate = self.rate_limit_rps  # tokens per second

    def _acquire_rate_limit_token(self):
        """Acquire a token from the rate limiter, waiting if necessary"""
        if not self.rate_limit_enabled:
            return

        with self._rate_limiter_lock:
            now = time.time()
            elapsed = now - self._last_update

            # Refill tokens based on elapsed time
            self._tokens = min(self.rate_limit_burst, self._tokens + elapsed * self._token_refill_rate)
            self._last_update = now

            # If we have tokens, use one
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            # Otherwise, calculate wait time
            wait_time = (1.0 - self._tokens) / self._token_refill_rate

            if wait_time > 0:
                logging.debug(f"Rate limiting: waiting {wait_time:.2f}s (tokens: {self._tokens:.2f})")
                time.sleep(wait_time)
                self._tokens = 0.0
                self._last_update = time.time()

    def _ensure_logged_in(self):
        """Ensure skopeo is logged in to the registry before operations.

        Raises:
            RuntimeError: If authentication fails or is required but credentials are missing
        """
        if self._logged_in:
            return

        try:
            # For ECR, authentication is handled by get_registry_password()
            # For other registries, we'll try to login if we have credentials
            if self.password and "amazonaws.com" not in self.registry_url:
                logging.info(f"Logging in to registry: {self.registry_url}")
                self._login_to_registry()

            self._logged_in = True
            logging.info("Skopeo authentication ready")

        except Exception as e:
            logging.error(f"Failed to authenticate with registry: {self.registry_url}")
            logging.error(f"Authentication error: {e}")
            from utils.error_utils import create_registry_auth_error

            raise create_registry_auth_error(self.registry_url, e)

    def _login_to_registry(self):
        """Login to the registry using skopeo login"""
        try:
            # Get auth file from environment (set by ConfigManager)
            auth_file = os.environ.get("REGISTRY_AUTH_FILE")
            cmd = [
                "skopeo",
                "login",
            ]
            # Add --authfile if available (for nonroot compatibility)
            if auth_file:
                cmd.extend(["--authfile", auth_file])
            cmd.extend(
                [
                    "--username",
                    "domino-registry",
                    "--password",
                    self.password,
                    "--tls-verify=false",
                    self.registry_url,
                ]
            )

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            if "Login Succeeded" not in result.stdout:
                raise RuntimeError(f"Login failed: {result.stdout}")

        except Exception as e:
            logging.error(f"Registry login failed: {e}")
            raise

    def _get_auth_args(self) -> List[str]:
        """Get authentication arguments for Skopeo commands"""
        base = ["--tls-verify=false"]
        # Allow unauthenticated operations when password is not set
        if self.password:
            return base + ["--creds", f"domino-registry:{self.password}"]
        return base

    def _build_skopeo_command(self, subcommand: str, args: List[str]) -> List[str]:
        """Build a complete Skopeo command with authentication"""
        return ["skopeo", subcommand] + self._get_auth_args() + args

    @staticmethod
    def _redact_command_for_logging(cmd: List[str]) -> List[str]:
        """Return a copy of the command with any credentials redacted.

        This prevents secrets such as registry passwords from being written
        to logs when we include the command for debugging.
        """
        redacted = list(cmd)

        # Redact --creds user:password style arguments
        for i, token in enumerate(redacted):
            if token == "--creds" and i + 1 < len(redacted):
                value = redacted[i + 1]
                if isinstance(value, str) and ":" in value:
                    user, _ = value.split(":", 1)
                    redacted[i + 1] = f"{user}:****"
            # Redact explicit --password arguments if ever used
            if token == "--password" and i + 1 < len(redacted):
                redacted[i + 1] = "****"

        return redacted

    def run_skopeo_command(self, subcommand: str, args: List[str]) -> Optional[str]:
        """Run a Skopeo command with standardized configuration."""
        self._ensure_logged_in()
        self._acquire_rate_limit_token()

        timeout = self.config_manager.get_retry_timeout()
        cmd = self._build_skopeo_command(subcommand, args)
        log_cmd = " ".join(self._redact_command_for_logging(cmd))

        @retry_with_backoff(
            max_retries=self.config_manager.get_max_retries(),
            initial_delay=self.config_manager.get_retry_initial_delay(),
            max_delay=self.config_manager.get_retry_max_delay(),
            exponential_base=self.config_manager.get_retry_exponential_base(),
            jitter=self.config_manager.get_retry_jitter(),
        )
        def _execute():
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=timeout,
                )
                return result.stdout
            except subprocess.TimeoutExpired as e:
                logging.error(f"Skopeo command timed out after {timeout}s: {log_cmd}")
                from utils.error_utils import create_registry_connection_error

                raise create_registry_connection_error(self.registry_url, e)
            except subprocess.CalledProcessError as e:
                error_str = (e.stderr or "").lower()
                if "429" in error_str or "rate limit" in error_str or "too many requests" in error_str:
                    from utils.error_utils import create_rate_limit_error

                    raise create_rate_limit_error(f"skopeo {subcommand}", retry_after=1.0)
                logging.error(f"Skopeo command failed: {log_cmd}")
                logging.error(f"Error: {e.stderr}")
                from utils.error_utils import create_registry_connection_error

                raise create_registry_connection_error(self.registry_url, e)
            except Exception as e:
                logging.error(f"Unexpected error running Skopeo: {e}")
                from utils.error_utils import create_registry_connection_error

                raise create_registry_connection_error(self.registry_url, e)

        try:
            return _execute()
        except Exception as e:
            # Check if error is retryable - if not, return None immediately
            is_retryable, error_type = is_retryable_error(e)
            if not is_retryable:
                logging.error(f"Non-retryable error in Skopeo command: {e}")
            return None

    @cached_tag_list(ttl_seconds=1800)  # Cache for 30 minutes
    def list_tags(self, repository: Optional[str] = None) -> List[str]:
        """List all tags for a repository.
        If repository is provided, it should be the full path under the registry
        (e.g., "dominodatalab/environment"). Otherwise uses the default from config.
        """
        repo_path = repository or self.repository
        args = [f"docker://{self.registry_url}/{repo_path}"]

        output = self.run_skopeo_command("list-tags", args)
        if output:
            try:
                tags_data = json.loads(output)
                return tags_data.get("Tags", [])
            except json.JSONDecodeError:
                logging.error(f"Failed to parse tags for {self.repository}")
                return []
        return []

    @cached_image_inspect(ttl_seconds=3600)  # Cache for 1 hour
    def inspect_image(self, repository: Optional[str], tag: str) -> Optional[Dict]:
        """Inspect a specific image tag.
        repository should be the full path under the registry (e.g., "dominodatalab/environment").
        If None, falls back to the default repository from config.
        """
        repo_path = repository or self.repository
        args = [f"docker://{self.registry_url}/{repo_path}:{tag}"]

        output = self.run_skopeo_command("inspect", args)
        if output:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse image inspection for {self.repository}:{tag}")
                return None
        return None

    def delete_image(self, repository: Optional[str], tag: str) -> bool:
        """Delete a specific image tag.
        repository should be the full path under the registry (e.g., "dominodatalab/environment").
        If None, falls back to the default repository from config.
        """
        repo_path = repository or self.repository
        args = [f"docker://{self.registry_url}/{repo_path}:{tag}"]

        output = self.run_skopeo_command("delete", args)
        return output is not None

    def is_registry_in_cluster(self) -> bool:
        """Check if the registry service exists in the Kubernetes cluster.

        First checks if enable_docker_deletion override is enabled. If so, returns True.
        Otherwise uses the shared is_registry_in_cluster(registry_url, namespace) helper.

        Returns:
            True if registry service/workload is found in the cluster (or forced), False otherwise.
        """
        if self.enable_docker_deletion:
            logging.info(f"Registry deletion enabled (using statefulset: {self.registry_statefulset})")
            return True
        return is_registry_in_cluster(self.registry_url, self.namespace)

    def _parse_registry_name(self) -> Tuple[str, str]:
        """Parse registry URL to extract service name and namespace.

        If enable_docker_deletion is enabled, uses the override statefulset name instead.

        Returns:
            Tuple of (service_name, namespace)
        """
        # Use override statefulset name if deletion is enabled
        if self.enable_docker_deletion:
            return self.registry_statefulset, self.namespace

        url = self.registry_url.split(":")[0]  # Remove port
        parts = url.split(".")

        service_name = parts[0]
        # If URL has namespace in it (service.namespace), use that; otherwise use config namespace
        check_namespace = parts[1] if len(parts) > 1 and parts[1] != "svc" else self.namespace

        return service_name, check_namespace

    def _wait_for_pod_ready(self, label_selector: str, namespace: str, timeout: int = 300) -> bool:
        """Wait for pod to be ready after a configuration change.

        Args:
            label_selector: Kubernetes label selector to find the pod (e.g., "app.kubernetes.io/name=docker-registry")
            namespace: Kubernetes namespace
            timeout: Maximum time to wait in seconds (default: 300)

        Returns:
            True if pod becomes ready, False if timeout or error
        """
        import time

        from kubernetes.client.rest import ApiException

        try:
            core_v1, _ = _get_kubernetes_clients()

            logging.info(f"Waiting for pod with selector '{label_selector}' to be ready in namespace '{namespace}'...")
            start_time = time.time()

            while time.time() - start_time < timeout:
                try:
                    # List pods matching the label selector
                    pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

                    if not pods.items:
                        logging.debug(f"No pods found with selector '{label_selector}', waiting...")
                        time.sleep(5)
                        continue

                    # Check if at least one pod is ready
                    for pod in pods.items:
                        if pod.status.conditions:
                            for condition in pod.status.conditions:
                                if condition.type == "Ready" and condition.status == "True":
                                    elapsed = int(time.time() - start_time)
                                    logging.info(f"✓ Pod {pod.metadata.name} is ready (waited {elapsed}s)")
                                    return True

                    # No ready pods yet, wait and retry
                    time.sleep(5)

                except ApiException as e:
                    logging.debug(f"Error checking pod status: {e}")
                    time.sleep(5)

            # Timeout reached
            logging.error(f"Timeout waiting for pod to be ready after {timeout}s")
            return False

        except Exception as e:
            logging.error(f"Error waiting for pod readiness: {e}")
            return False

    def enable_registry_deletion(self, namespace: str = None) -> bool:
        """Enable deletion of Docker images in the registry by setting REGISTRY_STORAGE_DELETE_ENABLED=true

        Args:
            namespace: Kubernetes namespace where registry workload is located
                      (defaults to parsed namespace from URL or config namespace)

        Returns:
            True if successful, False otherwise
        """
        service_name, parsed_ns = self._parse_registry_name()
        ns = namespace or parsed_ns

        try:
            from kubernetes import client as k8s_client

            # Initialize Kubernetes clients
            try:
                _, apps_v1 = _get_kubernetes_clients()
            except Exception as e:
                logging.error(f"Failed to load Kubernetes config: {e}")
                return False

            # Find and update the StatefulSet
            sts_data = apps_v1.read_namespaced_stateful_set(name=service_name, namespace=ns)

            # Check if the environment variable already exists
            env_exists = False
            for env in sts_data.spec.template.spec.containers[0].env or []:
                if env.name == "REGISTRY_STORAGE_DELETE_ENABLED":
                    env.value = "true"
                    env_exists = True
                    break

            # Add the environment variable if it doesn't exist
            if not env_exists:
                new_env = k8s_client.V1EnvVar(name="REGISTRY_STORAGE_DELETE_ENABLED", value="true")
                if sts_data.spec.template.spec.containers[0].env is None:
                    sts_data.spec.template.spec.containers[0].env = []
                sts_data.spec.template.spec.containers[0].env.append(new_env)

            # Update the StatefulSet
            apps_v1.patch_namespaced_stateful_set(name=service_name, namespace=ns, body=sts_data)
            logging.info(f"✓ Deletion enabled in {service_name} StatefulSet")

            # Wait for pod to restart and become ready
            label_selector = f"app.kubernetes.io/name={service_name}"
            if not self._wait_for_pod_ready(label_selector, ns):
                logging.warning("Pod may not be ready yet, but continuing anyway")

            return True

        except Exception as e:
            logging.error(f"Failed to enable registry deletion: {e}")
            return False

    def disable_registry_deletion(self, namespace: str = None) -> bool:
        """Disable deletion of Docker images in the registry by removing REGISTRY_STORAGE_DELETE_ENABLED

        Args:
            namespace: Kubernetes namespace where registry workload is located
                      (defaults to parsed namespace from URL or config namespace)

        Returns:
            True if successful, False otherwise
        """
        service_name, parsed_ns = self._parse_registry_name()
        ns = namespace or parsed_ns

        try:
            pass

            # Initialize Kubernetes clients
            try:
                _, apps_v1 = _get_kubernetes_clients()
            except Exception as e:
                logging.error(f"Failed to load Kubernetes config: {e}")
                return False

            # Find and update the StatefulSet
            sts_data = apps_v1.read_namespaced_stateful_set(name=service_name, namespace=ns)

            # Remove the environment variable if it exists
            if sts_data.spec.template.spec.containers[0].env:
                sts_data.spec.template.spec.containers[0].env = [
                    env
                    for env in sts_data.spec.template.spec.containers[0].env
                    if env.name != "REGISTRY_STORAGE_DELETE_ENABLED"
                ]

            # Update the StatefulSet
            apps_v1.patch_namespaced_stateful_set(name=service_name, namespace=ns, body=sts_data)
            logging.info(f"✓ Deletion disabled in {service_name} StatefulSet")

            # Wait for pod to restart and become ready
            label_selector = f"app.kubernetes.io/name={service_name}"
            if not self._wait_for_pod_ready(label_selector, ns):
                logging.warning("Pod may not be ready yet, but continuing anyway")

            return True

        except Exception as e:
            logging.error(f"Failed to disable registry deletion: {e}")
            return False


# Global config manager instance
# Validation can be disabled by setting SKIP_CONFIG_VALIDATION=true environment variable
# This is useful for testing or when you know the config is valid
config_manager = ConfigManager(
    validate=os.environ.get("SKIP_CONFIG_VALIDATION", "").lower() not in ("true", "1", "yes")
)
