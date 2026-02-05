#!/usr/bin/env python3
"""
Configuration Manager for Docker Registry Cleaner

This module handles loading and managing configuration from config.yaml
and environment variables.
"""

import base64
import logging
import os
import re
from typing import Any, Dict, Optional

import yaml


class ConfigValidationError(Exception):
    """Raised when configuration validation fails"""


def _load_kubernetes_config():
    """Helper function to load Kubernetes configuration.

    Tries in-cluster config first, then falls back to local kubeconfig.
    """
    try:
        from kubernetes.config import load_incluster_config

        load_incluster_config()
    except Exception:
        from kubernetes.config import load_kube_config

        load_kube_config()


def _get_kubernetes_core_client():
    """Helper function to get Kubernetes CoreV1Api client."""
    from kubernetes import client as k8s_client

    _load_kubernetes_config()
    return k8s_client.CoreV1Api()


class ConfigManager:
    """Manages configuration for the Docker registry cleaner project"""

    def __init__(self, config_file: str = None, validate: bool = True):
        """Initialize ConfigManager

        Args:
            config_file: Path to configuration YAML file (defaults to ../config.yaml or CONFIG_FILE env var)
            validate: If True, validate configuration on initialization
        """
        if config_file is None:
            config_file = os.environ.get("CONFIG_FILE", "../config.yaml")
        self.config_file = config_file
        self.config = self._load_config()

        # Set up skopeo auth file early (before any skopeo commands run)
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
                "timeout": 300,
            },
            "s3": {"bucket": "", "region": "us-west-2"},
            "skopeo": {
                "rate_limit": {
                    "enabled": True,
                    "requests_per_second": 10.0,
                    "burst_size": 20,
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
                "mongodb_usage": "mongodb_usage_report.json",
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
        """Get canonical repository value."""
        return os.environ.get("REPOSITORY") or self.config["registry"]["repository"]

    def get_registry_auth_secret(self) -> Optional[str]:
        """Get the name of a custom Kubernetes secret for registry authentication."""
        return os.environ.get("REGISTRY_AUTH_SECRET")

    def _get_credentials_from_k8s_secret(
        self, secret_name: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """Get Docker registry username and password from Kubernetes secret.

        Attempts to read credentials from a Kubernetes secret containing
        .dockerconfigjson with Docker registry credentials.

        Args:
            secret_name: Name of the secret to read. If None, defaults to 'domino-registry'.

        Returns:
            Tuple of (username, password) - either or both may be None if not found
        """
        try:
            from kubernetes.client.rest import ApiException

            core_v1, _ = _get_kubernetes_clients()
            namespace = self.get_domino_platform_namespace()
            secret_name = secret_name or "domino-registry"

            logging.debug(f"Attempting to read {secret_name} secret from namespace {namespace}")

            try:
                secret = core_v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            except ApiException as e:
                if e.status == 404:
                    logging.debug(f"Secret {secret_name} not found in namespace {namespace}")
                else:
                    logging.debug(f"Error reading secret {secret_name}: {e}")
                return None, None

            # Read .dockerconfigjson from the secret
            if not secret.data or ".dockerconfigjson" not in secret.data:
                logging.debug(f"Secret {secret_name} does not contain .dockerconfigjson")
                return None, None

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
                return None, None

            # Try to find matching registry in auths
            for auth_url, auth_data in dockerconfig["auths"].items():
                # Check if this auth entry matches our registry
                auth_host = auth_url.split(":")[0].split("/")[0]
                if auth_host == registry_host or registry_host in auth_host:
                    username = auth_data.get("username")
                    password = auth_data.get("password")

                    # If username/password not directly available, decode from 'auth' field
                    if (not username or not password) and "auth" in auth_data:
                        auth_decoded = base64.b64decode(auth_data["auth"]).decode("utf-8")
                        if ":" in auth_decoded:
                            decoded_user, decoded_pass = auth_decoded.split(":", 1)
                            username = username or decoded_user
                            password = password or decoded_pass

                    if username or password:
                        logging.info(f"Found registry credentials in {secret_name} secret")
                        return username, password

            logging.debug(f"No matching registry credentials found in {secret_name} secret for {registry_url}")
            return None, None

        except Exception as e:
            # Log but don't fail - fall back to other auth methods
            logging.debug(f"Could not read credentials from Kubernetes secret: {e}")
            return None, None

    def _get_password_from_k8s_secret(self, secret_name: Optional[str] = None) -> Optional[str]:
        """Get Docker registry password from Kubernetes secret.

        Args:
            secret_name: Name of the secret to read. If None, defaults to 'domino-registry'.

        Returns:
            Password string if found, None otherwise
        """
        _, password = self._get_credentials_from_k8s_secret(secret_name)
        return password

    def get_registry_password(self) -> Optional[str]:
        """Get registry password from environment, Kubernetes secret, or handle cloud registry authentication.

        Priority order:
        1. REGISTRY_PASSWORD environment variable (explicit override)
        2. Custom Kubernetes secret (REGISTRY_AUTH_SECRET, for external registries)
        3. domino-registry Kubernetes secret (for in-cluster registries)
        4. ECR authentication (for AWS ECR registries)
        5. ACR authentication (for Azure ACR registries)
        """
        # First check explicit password from environment
        password = os.environ.get("REGISTRY_PASSWORD")
        if password:
            return password

        # Try custom auth secret first (for external registries)
        custom_secret = self.get_registry_auth_secret()
        if custom_secret:
            password = self._get_password_from_k8s_secret(custom_secret)
            if password:
                return password

        # Fall back to domino-registry secret (for in-cluster registries)
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

        # If no password and registry is ACR, handle authentication
        if "azurecr.io" in registry_url:
            self._authenticate_acr(registry_url)
            # After authentication, we don't need to return a password
            # as skopeo will use the authenticated session
            return None

        return None

    def get_registry_username(self) -> Optional[str]:
        """Get registry username from environment or Kubernetes secret.

        Priority order:
        1. REGISTRY_USERNAME environment variable (explicit override)
        2. Custom Kubernetes secret (REGISTRY_AUTH_SECRET, for external registries)
        3. domino-registry Kubernetes secret (for in-cluster registries)
        4. For ECR registries, always returns "AWS"
        5. For ACR registries, always returns a placeholder GUID
        """
        # First check explicit username from environment
        username = os.environ.get("REGISTRY_USERNAME")
        if username:
            return username

        # Try custom auth secret first (for external registries)
        custom_secret = self.get_registry_auth_secret()
        if custom_secret:
            username, _ = self._get_credentials_from_k8s_secret(custom_secret)
            if username:
                return username

        # Fall back to domino-registry secret (for in-cluster registries)
        username, _ = self._get_credentials_from_k8s_secret()
        if username:
            return username

        # For ECR registries, username is always "AWS"
        registry_url = self.get_registry_url()
        if "amazonaws.com" in registry_url:
            return "AWS"

        # For ACR registries with AAD auth, username is a placeholder GUID
        if "azurecr.io" in registry_url:
            return "00000000-0000-0000-0000-000000000000"

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

    def _authenticate_acr(self, registry_url: str) -> None:
        """Authenticate with Azure Container Registry using managed identity (no shell required)."""
        try:
            import urllib.parse
            import urllib.request

            logging.info(f"Authenticating with ACR: {registry_url}")

            # Get Azure AD access token using managed identity
            # If AZURE_CLIENT_ID is set, use ManagedIdentityCredential directly
            # (required when multiple user-assigned identities exist on the AKS cluster)
            client_id = os.environ.get("AZURE_CLIENT_ID")
            if client_id:
                from azure.identity import ManagedIdentityCredential

                logging.info(f"Using managed identity with client ID: {client_id}")
                credential = ManagedIdentityCredential(client_id=client_id)
            else:
                from azure.identity import DefaultAzureCredential

                credential = DefaultAzureCredential()
            # Scope for ACR is the registry URL itself
            token = credential.get_token("https://management.azure.com/.default")
            access_token = token.token

            # Exchange the AAD token for an ACR refresh token
            # POST to https://<registry>/oauth2/exchange
            exchange_url = f"https://{registry_url}/oauth2/exchange"
            data = urllib.parse.urlencode(
                {
                    "grant_type": "access_token",
                    "service": registry_url,
                    "access_token": access_token,
                }
            ).encode("utf-8")

            req = urllib.request.Request(exchange_url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

            with urllib.request.urlopen(req, timeout=30) as response:
                import json

                result = json.loads(response.read().decode("utf-8"))
                refresh_token = result["refresh_token"]

            # Run skopeo login with the refresh token as password
            # Username for AAD auth is a placeholder GUID
            subprocess.run(
                [
                    "skopeo",
                    "login",
                    "--authfile",
                    self.auth_file,
                    "--username",
                    "00000000-0000-0000-0000-000000000000",
                    "--password-stdin",
                    registry_url,
                ],
                input=refresh_token,
                capture_output=True,
                text=True,
                check=True,
            )
            logging.info("ACR authentication successful")

        except subprocess.CalledProcessError as e:
            logging.error(f"ACR authentication failed during skopeo login: {e}")
            if e.stderr:
                logging.error(f"  stderr: {e.stderr}")
            raise
        except urllib.error.HTTPError as e:
            logging.error(f"ACR token exchange failed: HTTP {e.code} - {e.reason}")
            raise
        except Exception as e:
            logging.error(f"Unexpected error during ACR authentication: {e}")
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
        """Get S3 bucket from environment or config"""
        bucket = os.environ.get("S3_BUCKET") or self.config.get("s3", {}).get("bucket", "")
        return bucket if bucket else None

    def get_s3_region(self) -> str:
        """Get S3 region from environment or config"""
        return os.environ.get("S3_REGION") or self.config.get("s3", {}).get("region", "us-west-2")

    # Skopeo rate limiting configuration
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
        """Resolve report file path under the configured output_dir unless absolute."""
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
        base = self.config["reports"]["images_report"]
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
            core_v1 = _get_kubernetes_core_client()
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
            warnings.append(f"Registry URL '{registry_url}' may be invalid (expected format: hostname[:port])")

        repository = self.get_repository()
        if not repository or not repository.strip():
            errors.append("Repository name is required and cannot be empty")
        elif not self._is_valid_repository_name(repository):
            errors.append(f"Repository name '{repository}' contains invalid characters")

        # Validate Kubernetes configuration
        namespace = self.get_domino_platform_namespace()
        if not namespace or not namespace.strip():
            errors.append("Domino platform namespace is required and cannot be empty")
        elif not self._is_valid_k8s_name(namespace):
            errors.append(f"Namespace '{namespace}' is not a valid Kubernetes name")

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
                    f"S3 bucket name '{s3_bucket}' is invalid (must be 3-63 characters, lowercase alphanumeric)"
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
        url = url.replace("http://", "").replace("https://", "")
        pattern = r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?(:[0-9]{1,5})?$"
        return bool(re.match(pattern, url))

    def _is_valid_repository_name(self, name: str) -> bool:
        """Validate repository name format"""
        if not name:
            return False
        pattern = r"^[a-zA-Z0-9_\-/]+$"
        return bool(re.match(pattern, name))

    def _is_valid_k8s_name(self, name: str) -> bool:
        """Validate Kubernetes resource name format"""
        if not name:
            return False
        pattern = r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$"
        return bool(re.match(pattern, name)) and len(name) <= 253

    def _is_valid_s3_bucket_name(self, name: str) -> bool:
        """Validate S3 bucket name format"""
        if not name:
            return False
        if len(name) < 3 or len(name) > 63:
            return False
        pattern = r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$"
        if not re.match(pattern, name):
            return False
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

        s3_bucket = self.get_s3_bucket()
        s3_region = self.get_s3_region()
        print(f"  S3 Bucket: {s3_bucket or 'Not configured'}")
        print(f"  S3 Region: {s3_region}")


# Global config manager instance
# Validation can be disabled by setting SKIP_CONFIG_VALIDATION=true environment variable
config_manager = ConfigManager(
    validate=os.environ.get("SKIP_CONFIG_VALIDATION", "").lower() not in ("true", "1", "yes")
)


# Re-export SkopeoClient and is_registry_in_cluster for backwards compatibility
# These are now defined in utils.skopeo_client but we expose them here to avoid
# breaking existing imports
from utils.skopeo_client import SkopeoClient, _get_kubernetes_clients, is_registry_in_cluster

__all__ = [
    "ConfigManager",
    "ConfigValidationError",
    "config_manager",
    "SkopeoClient",
    "is_registry_in_cluster",
    "_get_kubernetes_clients",
]
