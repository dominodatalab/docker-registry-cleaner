#!/usr/bin/env python3
"""
Utilities for maintaining the Docker registry, such as running garbage collection.

These helpers are intended to be used both from standalone scripts and as
post-processing steps after tag deletion workflows.
"""

import logging
from typing import Optional

from utils.config_manager import config_manager, _get_kubernetes_clients, is_registry_in_cluster
from utils.logging_utils import get_logger
from utils.error_utils import create_kubernetes_error


logger = get_logger(__name__)


def run_registry_garbage_collection(
    registry_statefulset: Optional[str] = None,
    namespace: Optional[str] = None,
) -> bool:
    """
    Run Docker registry garbage collection inside the registry pod.

    This executes:
        registry garbage-collect --delete-untagged /etc/docker/registry/config.yml

    against a pod belonging to the specified StatefulSet. Intended for in-cluster
    Docker Registry deployments (e.g. the classic `docker-registry` chart). Managed
    registries such as ECR handle their own garbage collection and do not require
    this. Skips if the registry is not running in the cluster (no K8s API calls).

    Args:
        registry_statefulset: Name of the registry StatefulSet/Deployment. If not
            provided, defaults to "docker-registry".
        namespace: Kubernetes namespace for the registry workload. If not provided,
            defaults to the Domino platform namespace from config.

    Returns:
        True if the garbage collection command completed successfully, False otherwise.
    """
    workload_name = registry_statefulset or "docker-registry"
    ns = namespace or config_manager.get_domino_platform_namespace()
    registry_url = config_manager.get_registry_url() or ""

    if not is_registry_in_cluster(registry_url, ns):
        logger.info(
            "Registry is not running in cluster (%s); skipping garbage collection.",
            registry_url,
        )
        return False

    logger.info(
        "Running Docker registry garbage collection via StatefulSet '%s' in namespace '%s'...",
        workload_name,
        ns,
    )

    try:
        from kubernetes.client.rest import ApiException

        # Get Kubernetes clients
        try:
            core_v1, apps_v1 = _get_kubernetes_clients()
        except Exception as e:
            logger.error("Failed to load Kubernetes configuration: %s", e)
            return False

        # Locate the StatefulSet to derive label selector
        try:
            sts = apps_v1.read_namespaced_stateful_set(name=workload_name, namespace=ns)
        except ApiException as e:
            try:
                actionable = create_kubernetes_error(
                    f"Read StatefulSet {workload_name} in namespace {ns}", e
                )
                logger.error(actionable.message)
            except Exception:
                logger.error(
                    "Failed to read StatefulSet '%s' in namespace '%s': %s",
                    workload_name,
                    ns,
                    e,
                )
            return False

        match_labels = (sts.spec.selector.match_labels or {}) if sts.spec and sts.spec.selector else {}
        if match_labels:
            label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
        else:
            # Fallback: common label used by the classic chart
            label_selector = f"app={workload_name}"

        pods = core_v1.list_namespaced_pod(namespace=ns, label_selector=label_selector)
        if not pods.items:
            logger.error(
                "No pods found for registry workload '%s' in namespace '%s' (selector: %s)",
                workload_name,
                ns,
                label_selector,
            )
            return False

        # Prefer a Running pod
        pod = next((p for p in pods.items if (p.status and p.status.phase == "Running")), pods.items[0])
        pod_name = pod.metadata.name
        container_name = pod.spec.containers[0].name if pod.spec and pod.spec.containers else None

        cmd = [
            "registry",
            "garbage-collect",
            "--delete-untagged",
            "/etc/docker/registry/config.yml",
        ]

        logger.info(
            "Executing registry garbage collection in pod '%s' (container: %s): %s",
            pod_name,
            container_name or "<default>",
            " ".join(cmd),
        )

        resp = core_v1.connect_get_namespaced_pod_exec(
            name=pod_name,
            namespace=ns,
            command=cmd,
            container=container_name,
            stderr=True,
            stdout=True,
            stdin=False,
            tty=False,
        )

        if resp:
            logger.info("Registry garbage-collect output:\n%s", resp)

        logger.info("Docker registry garbage collection completed.")
        return True

    except ImportError:
        logger.error(
            "Kubernetes client library is not installed; cannot run registry garbage collection."
        )
        return False
    except Exception as e:
        try:
            actionable_error = create_kubernetes_error(
                f"Run registry garbage collection in StatefulSet {workload_name} (namespace {ns})",
                e,
            )
            logger.error(actionable_error.message)
        except Exception:
            logger.error("Unexpected error running registry garbage collection: %s", e)
        return False

