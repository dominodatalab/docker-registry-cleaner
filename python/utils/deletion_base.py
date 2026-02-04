"""
Base class for deletion scripts to reduce code duplication and standardize behavior.

This module provides common functionality for all deletion scripts including:
- Standardized confirmation prompts
- Common deletion workflow
- Backup integration
- Error handling
- Logging consistency
"""

import sys
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from utils.checkpoint import CheckpointManager
from utils.config_manager import SkopeoClient, config_manager
from utils.health_checks import HealthChecker
from utils.logging_utils import get_logger
from utils.report_utils import sizeof_fmt


class BaseDeletionScript(ABC):
    """Base class for deletion scripts with common functionality"""

    def __init__(
        self,
        registry_url: Optional[str] = None,
        repository: Optional[str] = None,
        namespace: Optional[str] = None,
        enable_docker_deletion: bool = False,
        registry_statefulset: Optional[str] = None,
    ):
        """Initialize base deletion script

        Args:
            registry_url: Docker registry URL (default: from config)
            repository: Repository name (default: from config)
            namespace: Kubernetes namespace (default: from config)
            enable_docker_deletion: Enable registry deletion override
            registry_statefulset: Registry StatefulSet name (default: docker-registry)
        """
        self.registry_url = registry_url or config_manager.get_registry_url()
        self.repository = repository or config_manager.get_repository()
        self.namespace = namespace or config_manager.get_domino_platform_namespace()
        self.logger = get_logger(self.__class__.__name__)

        # Initialize Skopeo client
        self.skopeo_client = SkopeoClient(
            config_manager,
            enable_docker_deletion=enable_docker_deletion,
            registry_statefulset=registry_statefulset,
        )

        # Initialize health checker
        self.health_checker = HealthChecker()

        # Initialize checkpoint manager
        self.checkpoint_manager = CheckpointManager()

    def confirm_deletion(self, count: int, item_type: str, force: bool = False) -> bool:
        """Standardized confirmation prompt for deletions

        Args:
            count: Number of items to be deleted
            item_type: Type of items (e.g., "images", "tags", "environments")
            force: If True, skip confirmation and return True

        Returns:
            True if user confirmed, False otherwise
        """
        if force:
            self.logger.warning("⚠️  Force mode enabled - skipping confirmation prompt")
            return True

        print("\n" + "=" * 60)
        print("⚠️  WARNING: You are about to DELETE Docker images from the registry!")
        print("=" * 60)
        print(f"This will delete {count} {item_type}.")
        print("This action cannot be undone.")
        print("Make sure you have reviewed the analysis output above.")
        print("=" * 60)

        while True:
            response = input("Are you sure you want to proceed with deletion? (yes/no): ").lower().strip()
            if response in ["yes", "y"]:
                return True
            elif response in ["no", "n"]:
                return False
            else:
                print("Please enter 'yes' or 'no'.")

    def enable_registry_deletion(self) -> bool:
        """Enable deletion in Docker registry

        Returns:
            True if successful, False otherwise
        """
        self.logger.info("Enabling deletion of Docker images in registry...")
        success = self.skopeo_client.enable_registry_deletion(namespace=self.namespace)
        if success:
            self.logger.info("✓ Deletion enabled in registry")
        else:
            self.logger.warning("Failed to enable registry deletion - continuing anyway")
        return success

    def disable_registry_deletion(self) -> bool:
        """Disable deletion in Docker registry

        Returns:
            True if successful, False otherwise
        """
        self.logger.info("Disabling deletion of Docker images in registry...")
        success = self.skopeo_client.disable_registry_deletion(namespace=self.namespace)
        if success:
            self.logger.info("✓ Deletion disabled in registry")
        else:
            self.logger.warning("Failed to disable registry deletion - continuing anyway")
        return success

    # Note: Subclasses may implement their own methods for finding and deleting items.
    # These abstract methods are optional - subclasses can use their own patterns.
    # They're provided as a suggested interface but not enforced.

    def run_health_checks(self, skip_optional: bool = True) -> bool:
        """Run health checks before deletion operations

        Args:
            skip_optional: If True, skip optional checks (S3, Kubernetes if not needed)

        Returns:
            True if all required checks passed, False otherwise
        """
        self.logger.info("Running health checks...")
        results = self.health_checker.run_all_checks(skip_optional=skip_optional)

        # Check if all required checks passed
        required_checks = ["configuration", "registry_connectivity", "mongodb_connectivity"]
        required_results = [r for r in results if r.name in required_checks]
        all_required_passed = all(r.status for r in required_results)

        if not all_required_passed:
            self.logger.error("Health checks failed - required services are not accessible")
            self.health_checker.print_health_report(results)
            return False

        self.logger.info("✓ All required health checks passed")
        return True

    def log_summary(self, summary: Dict[str, Any], dry_run: bool = False) -> None:
        """Log a standardized deletion summary

        Args:
            summary: Dictionary with summary information
            dry_run: Whether this was a dry run
        """
        mode = "DRY RUN: " if dry_run else ""
        self.logger.info(f"\n📊 {mode}Deletion Summary:")

        if "total" in summary:
            self.logger.info(f"   Total items: {summary['total']}")
        if "deleted" in summary:
            self.logger.info(f"   {'Would delete' if dry_run else 'Successfully deleted'}: {summary['deleted']}")
        if "failed" in summary:
            self.logger.info(f"   Failed deletions: {summary['failed']}")
        if "skipped" in summary:
            self.logger.info(f"   Skipped (in use): {summary['skipped']}")
        if "space_freed_gb" in summary:
            space_freed_bytes = summary.get("space_freed_bytes", summary.get("space_freed_gb", 0) * (1024**3))
            self.logger.info(f"   {'Would save' if dry_run else 'Saved'}: {sizeof_fmt(space_freed_bytes)}")
        if "results_file" in summary:
            self.logger.info(f"   Results saved to: {summary['results_file']}")
