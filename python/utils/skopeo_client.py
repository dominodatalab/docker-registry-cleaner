"""
Skopeo client for Docker registry operations.

This module provides a standardized client for interacting with Docker registries
using skopeo, with support for rate limiting, retries, and various authentication
methods.
"""

import json
import logging
import os
import subprocess
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from utils.auth import authenticate_acr, authenticate_ecr, get_credentials_from_k8s_secret
from utils.cache_utils import cached_image_inspect, cached_tag_list
from utils.retry_utils import is_retryable_error, retry_with_backoff


class ImageNotFoundError(Exception):
    """Raised when an image tag does not exist in the registry.

    This is a non-retryable condition — the tag was never there or was
    already deleted by a prior operation (e.g. delete_archived_tags).
    """

    pass


def _load_kubernetes_config():
    """Helper function to load Kubernetes configuration."""
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
    """
    from kubernetes import client as k8s_client

    _load_kubernetes_config()
    return k8s_client.CoreV1Api(), k8s_client.AppsV1Api()


def is_registry_in_cluster(registry_url: str, namespace: str) -> bool:
    """Check if the registry service exists in the Kubernetes cluster.

    Parses the registry URL to extract the service name and checks if it exists
    as a Service or StatefulSet in the cluster.

    Args:
        registry_url: Registry URL (e.g. "docker-registry:5000").
        namespace: Kubernetes namespace to check.

    Returns:
        True if registry Service or StatefulSet is found, False otherwise.
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


class SkopeoClient:
    """Standardized Skopeo client for registry operations."""

    def __init__(
        self,
        config_manager,
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

        # Set up auth file
        auth_dir = config_manager.get_output_dir()
        self.auth_file = os.path.join(auth_dir, ".registry-auth.json")
        os.environ["REGISTRY_AUTH_FILE"] = self.auth_file

        # Get credentials
        self.username = self._get_registry_username()
        self.password = self._get_registry_password()

        if self.rate_limit_enabled:
            self._init_rate_limiter()

        self._ensure_logged_in()

    def _get_registry_username(self) -> Optional[str]:
        """Get registry username from environment or Kubernetes secret."""
        # Check explicit username from environment
        username = os.environ.get("REGISTRY_USERNAME")
        if username:
            return username

        # Try custom auth secret first (for external registries)
        custom_secret = os.environ.get("REGISTRY_AUTH_SECRET")
        if custom_secret:
            username, _ = get_credentials_from_k8s_secret(custom_secret, self.namespace, self.registry_url)
            if username:
                return username

        # Fall back to domino-registry secret (for in-cluster registries)
        username, _ = get_credentials_from_k8s_secret("domino-registry", self.namespace, self.registry_url)
        if username:
            return username

        # For ECR registries, username is always "AWS"
        if "amazonaws.com" in self.registry_url:
            return "AWS"

        # For ACR registries, username is a placeholder GUID
        if "azurecr.io" in self.registry_url:
            return "00000000-0000-0000-0000-000000000000"

        return None

    def _get_registry_password(self) -> Optional[str]:
        """Get registry password from environment, Kubernetes secret, or cloud provider."""
        # Check explicit password from environment
        password = os.environ.get("REGISTRY_PASSWORD")
        if password:
            return password

        # Try custom auth secret first (for external registries)
        custom_secret = os.environ.get("REGISTRY_AUTH_SECRET")
        if custom_secret:
            _, password = get_credentials_from_k8s_secret(custom_secret, self.namespace, self.registry_url)
            if password:
                return password

        # Fall back to domino-registry secret (for in-cluster registries)
        _, password = get_credentials_from_k8s_secret("domino-registry", self.namespace, self.registry_url)
        if password:
            return password

        # For ECR, authenticate and return None (auth handled via auth file)
        if "amazonaws.com" in self.registry_url:
            authenticate_ecr(self.registry_url, self.auth_file)
            return None

        # For ACR, authenticate and return None (auth handled via auth file)
        if "azurecr.io" in self.registry_url:
            authenticate_acr(self.registry_url, self.auth_file)
            return None

        return None

    def _init_rate_limiter(self):
        """Initialize token bucket rate limiter."""
        self._tokens = float(self.rate_limit_burst)
        self._last_update = time.time()
        self._token_refill_rate = self.rate_limit_rps

    def _acquire_rate_limit_token(self):
        """Acquire a token from the rate limiter, waiting if necessary."""
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
        """Ensure skopeo is logged in to the registry before operations."""
        if self._logged_in:
            return

        try:
            # For ECR/ACR, authentication is handled by _get_registry_password()
            # For other registries, we'll try to login if we have credentials
            if self.password and "amazonaws.com" not in self.registry_url and "azurecr.io" not in self.registry_url:
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
        """Login to the registry using skopeo login."""
        if not self.username:
            raise RuntimeError(
                "Registry username not configured. Set REGISTRY_USERNAME environment variable "
                "or ensure the domino-registry Kubernetes secret contains valid credentials."
            )

        cmd = ["skopeo", "login"]
        if self.auth_file:
            cmd.extend(["--authfile", self.auth_file])
        cmd.extend(
            [
                "--username",
                self.username,
                "--password-stdin",
                "--tls-verify=false",
                self.registry_url,
            ]
        )

        result = subprocess.run(cmd, input=self.password, capture_output=True, text=True, check=True)
        if "Login Succeeded" not in result.stdout:
            raise RuntimeError(f"Login failed: {result.stdout}")

    def _get_auth_args(self) -> List[str]:
        """Get authentication arguments for Skopeo commands."""
        args = ["--tls-verify=false"]
        if self.auth_file:
            args.extend(["--authfile", self.auth_file])
        return args

    def _build_skopeo_command(self, subcommand: str, args: List[str]) -> List[str]:
        """Build a complete Skopeo command with authentication."""
        return ["skopeo", subcommand] + self._get_auth_args() + args

    @staticmethod
    def _redact_command_for_logging(cmd: List[str]) -> List[str]:
        """Return a copy of the command with any credentials redacted."""
        redacted = list(cmd)

        creds_flags = ("--creds", "--src-creds", "--dest-creds")
        token_flags = ("--password", "--src-registry-token", "--dest-registry-token")

        for i, token in enumerate(redacted):
            if token in creds_flags and i + 1 < len(redacted):
                value = redacted[i + 1]
                if isinstance(value, str) and ":" in value:
                    user, _ = value.split(":", 1)
                    redacted[i + 1] = f"{user}:****"
            if token in token_flags and i + 1 < len(redacted):
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
                if (
                    "manifest unknown" in error_str
                    or "name unknown" in error_str
                    or "not found" in error_str
                    or "404" in error_str
                ):
                    raise ImageNotFoundError(
                        f"Image not found in registry (may have already been deleted): {e.stderr.strip()}"
                    )
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
        except ImageNotFoundError as e:
            logging.warning(str(e))
            return None
        except Exception as e:
            is_retryable, error_type = is_retryable_error(e)
            if not is_retryable:
                logging.error(f"Non-retryable error in Skopeo command: {e}")
            return None

    @cached_tag_list(ttl_seconds=1800)
    def list_tags(self, repository: Optional[str] = None) -> List[str]:
        """List all tags for a repository."""
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

    @cached_image_inspect(ttl_seconds=3600)
    def inspect_image(self, repository: Optional[str], tag: str) -> Optional[Dict]:
        """Inspect a specific image tag."""
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
        """Delete a specific image tag."""
        repo_path = repository or self.repository
        args = [f"docker://{self.registry_url}/{repo_path}:{tag}"]

        output = self.run_skopeo_command("delete", args)
        return output is not None

    def copy_image(
        self,
        src_ref: str,
        dest_ref: str,
        dest_creds: Optional[str] = None,
        dest_registry_token: Optional[str] = None,
        dest_tls_verify: bool = False,
    ) -> bool:
        """Copy an image from source to destination registry.

        Uses the SkopeoClient's existing auth for the source side and accepts
        separate destination auth parameters.

        Args:
            src_ref: Full source image reference (e.g. "docker://registry:5000/repo:tag")
            dest_ref: Full destination image reference (e.g. "docker://ecr.example.com/repo:tag")
            dest_creds: Destination credentials in "user:password" format
            dest_registry_token: Destination registry token (e.g. for GCR/GAR)
            dest_tls_verify: Whether to verify TLS for the destination registry

        Returns:
            True if copy succeeded, False otherwise
        """
        self._ensure_logged_in()
        self._acquire_rate_limit_token()

        timeout = self.config_manager.get_retry_timeout()

        # Build copy command with separate src/dest auth
        cmd = ["skopeo", "copy", "--src-tls-verify=false"]
        if self.auth_file:
            cmd.extend(["--src-authfile", self.auth_file])

        cmd.append(f"--dest-tls-verify={'true' if dest_tls_verify else 'false'}")
        if dest_creds:
            cmd.extend(["--dest-creds", dest_creds])
        if dest_registry_token:
            cmd.extend(["--dest-registry-token", dest_registry_token])

        cmd.extend([src_ref, dest_ref])

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
                subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
                return True
            except subprocess.TimeoutExpired as e:
                logging.error(f"Skopeo copy timed out after {timeout}s: {log_cmd}")
                from utils.error_utils import create_registry_connection_error

                raise create_registry_connection_error(self.registry_url, e)
            except subprocess.CalledProcessError as e:
                error_str = (e.stderr or "").lower()
                if "429" in error_str or "rate limit" in error_str or "too many requests" in error_str:
                    from utils.error_utils import create_rate_limit_error

                    raise create_rate_limit_error("skopeo copy", retry_after=1.0)
                logging.error(f"Skopeo copy failed: {log_cmd}")
                logging.error(f"Error: {e.stderr}")
                from utils.error_utils import create_registry_connection_error

                raise create_registry_connection_error(self.registry_url, e)

        try:
            return _execute()
        except Exception as e:
            is_retryable, error_type = is_retryable_error(e)
            if not is_retryable:
                logging.error(f"Non-retryable error in Skopeo copy: {e}")
            return False

    def is_registry_in_cluster(self) -> bool:
        """Check if the registry service exists in the Kubernetes cluster."""
        if self.enable_docker_deletion:
            logging.info(f"Registry deletion enabled (using statefulset: {self.registry_statefulset})")
            return True
        return is_registry_in_cluster(self.registry_url, self.namespace)

    def _parse_registry_name(self) -> Tuple[str, str]:
        """Parse registry URL to extract service name and namespace."""
        if self.enable_docker_deletion:
            return self.registry_statefulset, self.namespace

        url = self.registry_url.split(":")[0]
        parts = url.split(".")

        service_name = parts[0]
        check_namespace = parts[1] if len(parts) > 1 and parts[1] != "svc" else self.namespace

        return service_name, check_namespace

    def _wait_for_pod_ready(self, label_selector: str, namespace: str, timeout: int = 300) -> bool:
        """Wait for pod to be ready after a configuration change."""
        from kubernetes.client.rest import ApiException

        try:
            core_v1, _ = _get_kubernetes_clients()

            logging.info(f"Waiting for pod with selector '{label_selector}' to be ready...")
            start_time = time.time()

            while time.time() - start_time < timeout:
                try:
                    pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

                    if not pods.items:
                        logging.debug(f"No pods found with selector '{label_selector}', waiting...")
                        time.sleep(5)
                        continue

                    for pod in pods.items:
                        if pod.status.conditions:
                            for condition in pod.status.conditions:
                                if condition.type == "Ready" and condition.status == "True":
                                    elapsed = int(time.time() - start_time)
                                    logging.info(f"Pod {pod.metadata.name} is ready (waited {elapsed}s)")
                                    return True

                    time.sleep(5)

                except ApiException as e:
                    logging.debug(f"Error checking pod status: {e}")
                    time.sleep(5)

            logging.error(f"Timeout waiting for pod to be ready after {timeout}s")
            return False

        except Exception as e:
            logging.error(f"Error waiting for pod readiness: {e}")
            return False

    def enable_registry_deletion(self, namespace: str = None) -> bool:
        """Enable deletion of Docker images in the registry."""
        service_name, parsed_ns = self._parse_registry_name()
        ns = namespace or parsed_ns

        try:
            from kubernetes import client as k8s_client

            try:
                _, apps_v1 = _get_kubernetes_clients()
            except Exception as e:
                logging.error(f"Failed to load Kubernetes config: {e}")
                return False

            sts_data = apps_v1.read_namespaced_stateful_set(name=service_name, namespace=ns)

            env_exists = False
            for env in sts_data.spec.template.spec.containers[0].env or []:
                if env.name == "REGISTRY_STORAGE_DELETE_ENABLED":
                    env.value = "true"
                    env_exists = True
                    break

            if not env_exists:
                new_env = k8s_client.V1EnvVar(name="REGISTRY_STORAGE_DELETE_ENABLED", value="true")
                if sts_data.spec.template.spec.containers[0].env is None:
                    sts_data.spec.template.spec.containers[0].env = []
                sts_data.spec.template.spec.containers[0].env.append(new_env)

            apps_v1.patch_namespaced_stateful_set(name=service_name, namespace=ns, body=sts_data)
            logging.info(f"Deletion enabled in {service_name} StatefulSet")

            label_selector = f"app.kubernetes.io/name={service_name}"
            if not self._wait_for_pod_ready(label_selector, ns):
                logging.warning("Pod may not be ready yet, but continuing anyway")

            return True

        except Exception as e:
            logging.error(f"Failed to enable registry deletion: {e}")
            return False

    def disable_registry_deletion(self, namespace: str = None) -> bool:
        """Disable deletion of Docker images in the registry."""
        service_name, parsed_ns = self._parse_registry_name()
        ns = namespace or parsed_ns

        try:
            try:
                _, apps_v1 = _get_kubernetes_clients()
            except Exception as e:
                logging.error(f"Failed to load Kubernetes config: {e}")
                return False

            sts_data = apps_v1.read_namespaced_stateful_set(name=service_name, namespace=ns)

            if sts_data.spec.template.spec.containers[0].env:
                sts_data.spec.template.spec.containers[0].env = [
                    env
                    for env in sts_data.spec.template.spec.containers[0].env
                    if env.name != "REGISTRY_STORAGE_DELETE_ENABLED"
                ]

            apps_v1.patch_namespaced_stateful_set(name=service_name, namespace=ns, body=sts_data)
            logging.info(f"Deletion disabled in {service_name} StatefulSet")

            label_selector = f"app.kubernetes.io/name={service_name}"
            if not self._wait_for_pod_ready(label_selector, ns):
                logging.warning("Pod may not be ready yet, but continuing anyway")

            return True

        except Exception as e:
            logging.error(f"Failed to disable registry deletion: {e}")
            return False
