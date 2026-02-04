"""
Health check utilities for verifying system connectivity and configuration.

This module provides health checks for:
- Docker registry connectivity
- MongoDB connectivity
- Kubernetes API access (if needed)
- S3 access (if configured)
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from utils.config_manager import _get_kubernetes_clients, config_manager, is_registry_in_cluster
from utils.error_utils import (
    create_kubernetes_error,
    create_mongodb_connection_error,
    create_registry_connection_error,
    create_s3_error,
)
from utils.logging_utils import get_logger
from utils.mongo_utils import get_mongo_client

logger = get_logger(__name__)


@dataclass
class HealthCheckResult:
    """Result of a health check"""

    name: str
    status: bool  # True if healthy, False if unhealthy
    message: str
    details: Optional[Dict] = None


class HealthChecker:
    """Performs health checks on system components"""

    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)

    def check_registry_connectivity(self) -> HealthCheckResult:
        """Check if Docker registry is accessible

        Returns:
            HealthCheckResult indicating registry connectivity status
        """
        try:
            from utils.config_manager import SkopeoClient

            registry_url = config_manager.get_registry_url()
            base_repository = config_manager.get_repository()

            # Determine which repositories to probe.
            # If the configured repository already includes a type segment (e.g. "dominodatalab/environment"),
            # just use it as-is. Otherwise, try common sub-repositories that we actually use.
            if "/" in base_repository:
                candidate_repos = [base_repository]
            else:
                candidate_repos = [
                    f"{base_repository}/environment",
                    f"{base_repository}/model",
                ]

            self.logger.info(
                f"Checking registry connectivity at {registry_url} using repositories: " + ", ".join(candidate_repos)
            )

            # Try to create a SkopeoClient
            skopeo_client = SkopeoClient(config_manager, enable_docker_deletion=False)

            last_error: Optional[Exception] = None
            # Try each candidate repository until one succeeds
            for repo in candidate_repos:
                try:
                    tags = skopeo_client.list_tags(repo)
                    return HealthCheckResult(
                        name="registry_connectivity",
                        status=True,
                        message=f"Successfully connected to registry {registry_url}",
                        details={
                            "registry_url": registry_url,
                            "repository": repo,
                            "tags_count": len(tags) if tags else 0,
                        },
                    )
                except Exception as e_repo:
                    # Record the error and try the next candidate
                    last_error = e_repo
                    self.logger.debug(f"Failed to list tags for candidate repository '{repo}': {e_repo}")

            # If all candidates failed, raise the last error to be handled below
            if last_error is not None:
                raise last_error

            # Fallback (should not normally reach here)
            return HealthCheckResult(
                name="registry_connectivity",
                status=False,
                message=f"Failed to connect to registry {registry_url} using any candidate repository",
                details={
                    "registry_url": registry_url,
                    "repository": ",".join(candidate_repos),
                    "error": "Unknown error",
                    "suggestions": [],
                },
            )
        except Exception as e:
            # Use actionable error for better user guidance
            try:
                actionable_error = create_registry_connection_error(registry_url, e)
                error_message = actionable_error.message
            except:
                error_message = f"Failed to connect to registry: {str(e)}"

            return HealthCheckResult(
                name="registry_connectivity",
                status=False,
                message=error_message,
                details={
                    "registry_url": registry_url,
                    "repository": ",".join(candidate_repos) if "candidate_repos" in locals() else None,
                    "error": str(e),
                    "suggestions": actionable_error.suggestions if "actionable_error" in locals() else [],
                },
            )

    def check_mongodb_connectivity(self) -> HealthCheckResult:
        """Check if MongoDB is accessible

        Returns:
            HealthCheckResult indicating MongoDB connectivity status
        """
        try:
            mongo_client = get_mongo_client()

            # Try to ping the database
            mongo_client.admin.command("ping")

            # Get database info
            db_name = config_manager.get_mongo_db()
            db = mongo_client[db_name]

            # List collections to verify access
            collections = db.list_collection_names()

            return HealthCheckResult(
                name="mongodb_connectivity",
                status=True,
                message=f"Successfully connected to MongoDB",
                details={
                    "host": config_manager.get_mongo_host(),
                    "port": config_manager.get_mongo_port(),
                    "database": db_name,
                    "collections_count": len(collections),
                },
            )
        except Exception as e:
            # Use actionable error for better user guidance
            try:
                actionable_error = create_mongodb_connection_error(
                    config_manager.get_mongo_host(), config_manager.get_mongo_port(), e
                )
                error_message = actionable_error.message
            except:
                error_message = f"Failed to connect to MongoDB: {str(e)}"

            return HealthCheckResult(
                name="mongodb_connectivity",
                status=False,
                message=error_message,
                details={
                    "host": config_manager.get_mongo_host(),
                    "port": config_manager.get_mongo_port(),
                    "database": config_manager.get_mongo_db(),
                    "error": str(e),
                    "suggestions": actionable_error.suggestions if "actionable_error" in locals() else [],
                },
            )

    def check_kubernetes_access(self) -> HealthCheckResult:
        """Check if Kubernetes API is accessible (if needed)

        Returns:
            HealthCheckResult indicating Kubernetes access status
        """
        try:
            from kubernetes import client as k8s_client
            from kubernetes.client.rest import ApiException
            from kubernetes.config import load_incluster_config, load_kube_config

            # Try to load config
            try:
                load_incluster_config()
            except:
                load_kube_config()

            # Try to access the API
            core_v1 = k8s_client.CoreV1Api()
            namespace = config_manager.get_domino_platform_namespace()

            # Try to get namespace
            core_v1.read_namespace(name=namespace)

            return HealthCheckResult(
                name="kubernetes_access",
                status=True,
                message=f"Successfully connected to Kubernetes API",
                details={"namespace": namespace},
            )
        except ImportError:
            return HealthCheckResult(
                name="kubernetes_access",
                status=False,
                message="Kubernetes client not available (kubernetes package not installed)",
                details={},
            )
        except Exception as e:
            # Use actionable error for better user guidance
            try:
                actionable_error = create_kubernetes_error(f"Access namespace {namespace}", e)
                error_message = actionable_error.message
            except:
                error_message = f"Failed to access Kubernetes API: {str(e)}"

            return HealthCheckResult(
                name="kubernetes_access",
                status=False,
                message=error_message,
                details={
                    "namespace": namespace,
                    "error": str(e),
                    "suggestions": actionable_error.suggestions if "actionable_error" in locals() else [],
                },
            )

    def check_registry_deletion_rbac(self) -> HealthCheckResult:
        """Check if we can patch the docker-registry StatefulSet (required to enable deletion).

        Skips the check if the registry is not running inside the cluster (e.g. ECR);
        RBAC to patch the registry StatefulSet is only needed for in-cluster registries.
        Uses a dry-run patch so it does not modify the actual StatefulSet.
        """
        statefulset_name = "docker-registry"
        namespace = config_manager.get_domino_platform_namespace()
        registry_url = config_manager.get_registry_url()

        if not is_registry_in_cluster(registry_url, namespace):
            return HealthCheckResult(
                name="registry_deletion_rbac",
                status=True,
                message="Registry is not running in cluster; RBAC check skipped",
                details={
                    "namespace": namespace,
                    "registry_url": registry_url,
                },
            )

        try:
            from kubernetes.client.rest import ApiException

            # Initialize Kubernetes clients via shared helper
            try:
                _, apps_v1 = _get_kubernetes_clients()
            except Exception as e:
                return HealthCheckResult(
                    name="registry_deletion_rbac",
                    status=False,
                    message=f"Failed to load Kubernetes config for RBAC check: {str(e)}",
                    details={
                        "namespace": namespace,
                        "statefulset": statefulset_name,
                        "error": str(e),
                    },
                )

            # Minimal patch body; dryRun=All ensures no persistent change
            body = {"metadata": {"annotations": {"docker-registry-cleaner/rbac-test": "true"}}}

            apps_v1.patch_namespaced_stateful_set(
                name=statefulset_name,
                namespace=namespace,
                body=body,
                dry_run="All",
            )

            return HealthCheckResult(
                name="registry_deletion_rbac",
                status=True,
                message=(
                    f"ServiceAccount can PATCH StatefulSet '{statefulset_name}' "
                    f"in namespace '{namespace}' (dry-run succeeded)"
                ),
                details={
                    "namespace": namespace,
                    "statefulset": statefulset_name,
                },
            )

        except ImportError:
            return HealthCheckResult(
                name="registry_deletion_rbac",
                status=False,
                message="Kubernetes client not available (kubernetes package not installed)",
                details={
                    "namespace": namespace,
                    "statefulset": statefulset_name,
                },
            )
        except ApiException as e:
            # 403 is the most interesting case (missing RBAC)
            status = getattr(e, "status", None)
            if status == 403:
                msg = (
                    f"ServiceAccount does NOT have permission to PATCH StatefulSet "
                    f"'{statefulset_name}' in namespace '{namespace}' (HTTP 403 Forbidden)"
                )
            else:
                msg = (
                    f"Failed to PATCH StatefulSet '{statefulset_name}' in namespace "
                    f"'{namespace}' for RBAC check: {str(e)}"
                )

            try:
                actionable_error = create_kubernetes_error(
                    f"Patch StatefulSet {statefulset_name} in namespace {namespace}",
                    e,
                )
                msg = actionable_error.message
                suggestions = actionable_error.suggestions
            except Exception:
                suggestions = []

            return HealthCheckResult(
                name="registry_deletion_rbac",
                status=False,
                message=msg,
                details={
                    "namespace": namespace,
                    "statefulset": statefulset_name,
                    "error": str(e),
                    "status": status,
                    "suggestions": suggestions,
                },
            )
        except Exception as e:
            try:
                actionable_error = create_kubernetes_error(
                    f"Patch StatefulSet {statefulset_name} in namespace {namespace}",
                    e,
                )
                msg = actionable_error.message
                suggestions = actionable_error.suggestions
            except Exception:
                msg = f"Unexpected error during registry deletion RBAC check: {str(e)}"
                suggestions = []

            return HealthCheckResult(
                name="registry_deletion_rbac",
                status=False,
                message=msg,
                details={
                    "namespace": namespace,
                    "statefulset": statefulset_name,
                    "error": str(e),
                    "suggestions": suggestions,
                },
            )

    def check_s3_access(self) -> HealthCheckResult:
        """Check if S3 is accessible (if configured)

        Returns:
            HealthCheckResult indicating S3 access status
        """
        s3_bucket = config_manager.get_s3_bucket()

        if not s3_bucket:
            return HealthCheckResult(
                name="s3_access",
                status=True,
                message="S3 not configured (no bucket specified)",
                details={"bucket": None},
            )

        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError

            s3_region = config_manager.get_s3_region()

            # Try to create S3 client and check bucket access
            s3_client = boto3.client("s3", region_name=s3_region)

            # Try to head bucket (checks if bucket exists and we have access)
            s3_client.head_bucket(Bucket=s3_bucket)

            return HealthCheckResult(
                name="s3_access",
                status=True,
                message=f"Successfully accessed S3 bucket {s3_bucket}",
                details={"bucket": s3_bucket, "region": s3_region},
            )
        except ImportError:
            return HealthCheckResult(
                name="s3_access",
                status=False,
                message="boto3 not installed (required for S3 access)",
                details={"bucket": s3_bucket},
            )
        except NoCredentialsError:
            try:
                actionable_error = create_s3_error(
                    "head_bucket", s3_bucket, Exception("AWS credentials not configured")
                )
                error_message = actionable_error.message
            except:
                error_message = "AWS credentials not configured"
            return HealthCheckResult(
                name="s3_access",
                status=False,
                message=error_message,
                details={
                    "bucket": s3_bucket,
                    "region": config_manager.get_s3_region(),
                    "suggestions": actionable_error.suggestions if "actionable_error" in locals() else [],
                },
            )
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            try:
                actionable_error = create_s3_error("head_bucket", s3_bucket, e)
                error_message = actionable_error.message
            except:
                error_message = f"Failed to access S3 bucket: {error_code}"
            return HealthCheckResult(
                name="s3_access",
                status=False,
                message=error_message,
                details={
                    "bucket": s3_bucket,
                    "region": config_manager.get_s3_region(),
                    "error": str(e),
                    "error_code": error_code,
                    "suggestions": actionable_error.suggestions if "actionable_error" in locals() else [],
                },
            )
        except Exception as e:
            try:
                actionable_error = create_s3_error("head_bucket", s3_bucket, e)
                error_message = actionable_error.message
            except:
                error_message = f"Failed to access S3: {str(e)}"
            return HealthCheckResult(
                name="s3_access",
                status=False,
                message=error_message,
                details={
                    "bucket": s3_bucket,
                    "region": config_manager.get_s3_region(),
                    "error": str(e),
                    "suggestions": actionable_error.suggestions if "actionable_error" in locals() else [],
                },
            )

    def check_configuration(self) -> HealthCheckResult:
        """Check if configuration is valid

        Returns:
            HealthCheckResult indicating configuration validity
        """
        try:
            # This will raise ConfigValidationError if invalid
            config_manager.validate_config()

            return HealthCheckResult(
                name="configuration",
                status=True,
                message="Configuration is valid",
                details={
                    "registry_url": config_manager.get_registry_url(),
                    "repository": config_manager.get_repository(),
                    "namespace": config_manager.get_domino_platform_namespace(),
                },
            )
        except Exception as e:
            return HealthCheckResult(
                name="configuration",
                status=False,
                message=f"Configuration validation failed: {str(e)}",
                details={"error": str(e)},
            )

    def run_all_checks(self, skip_optional: bool = False) -> List[HealthCheckResult]:
        """Run all health checks

        Args:
            skip_optional: If True, skip optional checks (S3, Kubernetes if not needed)

        Returns:
            List of HealthCheckResult objects
        """
        results = []

        # Always check configuration first
        results.append(self.check_configuration())

        # Always check required services
        results.append(self.check_registry_connectivity())
        results.append(self.check_mongodb_connectivity())

        # Check optional services
        if not skip_optional:
            results.append(self.check_kubernetes_access())
            results.append(self.check_registry_deletion_rbac())
            results.append(self.check_s3_access())
        else:
            # Still check S3 if it's configured
            if config_manager.get_s3_bucket():
                results.append(self.check_s3_access())

        return results

    def print_health_report(self, results: List[HealthCheckResult]) -> bool:
        """Print a formatted health check report

        Args:
            results: List of HealthCheckResult objects

        Returns:
            True if all checks passed, False otherwise
        """
        print("\n" + "=" * 60)
        print("Health Check Report")
        print("=" * 60)

        all_healthy = True

        for result in results:
            status_icon = "✓" if result.status else "✗"
            status_text = "HEALTHY" if result.status else "UNHEALTHY"

            print(f"\n{status_icon} {result.name.upper().replace('_', ' ')}: {status_text}")
            print(f"   {result.message}")

            if result.details:
                for key, value in result.details.items():
                    if key != "error":  # Don't print error in details if it's already in message
                        print(f"   {key}: {value}")

            if not result.status:
                all_healthy = False

        print("\n" + "=" * 60)

        if all_healthy:
            print("✓ All health checks passed")
        else:
            print("✗ Some health checks failed - please review the issues above")

        print("=" * 60 + "\n")

        return all_healthy
