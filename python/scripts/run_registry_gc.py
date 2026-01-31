#!/usr/bin/env python3
"""
Run Docker registry garbage collection inside the registry pod.

This is intended for in-cluster Docker Registry deployments. Managed registries
such as ECR perform their own garbage collection and do not need this.
"""

import argparse
import sys

from utils.logging_utils import setup_logging, get_logger
from utils.registry_maintenance import run_registry_garbage_collection
from utils.config_manager import config_manager


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Docker registry garbage collection in the registry pod",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run garbage collection against the default docker-registry StatefulSet
  python run_registry_gc.py

  # Specify a custom registry StatefulSet and namespace
  python run_registry_gc.py --registry-statefulset my-registry --namespace domino-platform
        """,
    )

    parser.add_argument(
        "--registry-statefulset",
        default="docker-registry",
        help="Name of registry StatefulSet/Deployment to exec into (default: docker-registry)",
    )
    parser.add_argument(
        "--namespace",
        help="Kubernetes namespace for the registry workload (default: Domino platform namespace from config)",
    )

    return parser.parse_args()


def main() -> None:
    setup_logging()
    logger = get_logger(__name__)
    args = parse_arguments()

    namespace = args.namespace or config_manager.get_domino_platform_namespace()

    logger.info(
        "Requesting Docker registry garbage collection for StatefulSet '%s' in namespace '%s'...",
        args.registry_statefulset,
        namespace,
    )

    success = run_registry_garbage_collection(
        registry_statefulset=args.registry_statefulset,
        namespace=namespace,
    )

    if not success:
        logger.error("Docker registry garbage collection did not complete successfully.")
        sys.exit(1)


if __name__ == "__main__":
    main()

