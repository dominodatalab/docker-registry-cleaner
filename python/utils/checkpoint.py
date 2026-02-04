"""
Checkpoint and resume functionality for long-running operations.

This module provides utilities to save progress checkpoints and resume
interrupted operations, preventing duplicate work and data loss.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logging_utils import get_logger
from utils.report_utils import get_reports_dir

logger = get_logger(__name__)


@dataclass
class Checkpoint:
    """Represents a checkpoint for resuming operations."""

    operation_type: str  # e.g., 'delete_archived_tags', 'backup_images'
    started_at: str  # ISO format timestamp
    last_updated: str  # ISO format timestamp
    total_items: int
    completed_items: List[str]  # List of completed item identifiers
    failed_items: List[str]  # List of failed item identifiers
    skipped_items: List[str]  # List of skipped item identifiers
    metadata: Dict[str, Any]  # Additional operation-specific data


class CheckpointManager:
    """Manages checkpoints for resuming interrupted operations."""

    def __init__(self, checkpoint_dir: Optional[Path] = None):
        """
        Initialize checkpoint manager.

        Args:
            checkpoint_dir: Directory to store checkpoints (defaults to reports directory)
        """
        if checkpoint_dir is None:
            checkpoint_dir = get_reports_dir() / "checkpoints"
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def get_checkpoint_path(self, operation_type: str, operation_id: Optional[str] = None) -> Path:
        """
        Get the path for a checkpoint file.

        Args:
            operation_type: Type of operation (e.g., 'delete_archived_tags')
            operation_id: Optional unique identifier for this specific operation run

        Returns:
            Path to checkpoint file
        """
        if operation_id:
            filename = f"{operation_type}-{operation_id}.checkpoint.json"
        else:
            filename = f"{operation_type}.checkpoint.json"
        return self.checkpoint_dir / filename

    def save_checkpoint(
        self,
        operation_type: str,
        completed_items: List[str],
        total_items: int,
        failed_items: Optional[List[str]] = None,
        skipped_items: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        operation_id: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> Path:
        """
        Save a checkpoint for an operation.

        Args:
            operation_type: Type of operation
            completed_items: List of completed item identifiers
            total_items: Total number of items to process
            failed_items: List of failed item identifiers
            skipped_items: List of skipped item identifiers
            metadata: Additional operation-specific data
            operation_id: Optional unique identifier for this operation run
            started_at: Optional start timestamp (defaults to current time if creating new checkpoint)

        Returns:
            Path to saved checkpoint file
        """
        checkpoint_path = self.get_checkpoint_path(operation_type, operation_id)

        # Load existing checkpoint if it exists
        if checkpoint_path.exists():
            existing = self.load_checkpoint(operation_type, operation_id)
            if existing:
                started_at = existing.started_at
                # Merge with existing data
                completed_items = list(set(existing.completed_items + completed_items))
                failed_items = list(set((existing.failed_items or []) + (failed_items or [])))
                skipped_items = list(set((existing.skipped_items or []) + (skipped_items or [])))
                if metadata and existing.metadata:
                    metadata = {**existing.metadata, **metadata}
                elif existing.metadata:
                    metadata = existing.metadata

        if started_at is None:
            started_at = datetime.now().isoformat()

        checkpoint = Checkpoint(
            operation_type=operation_type,
            started_at=started_at,
            last_updated=datetime.now().isoformat(),
            total_items=total_items,
            completed_items=completed_items,
            failed_items=failed_items or [],
            skipped_items=skipped_items or [],
            metadata=metadata or {},
        )

        # Save checkpoint
        with open(checkpoint_path, "w") as f:
            json.dump(asdict(checkpoint), f, indent=2)

        logger.debug(f"Checkpoint saved: {checkpoint_path}")
        return checkpoint_path

    def load_checkpoint(self, operation_type: str, operation_id: Optional[str] = None) -> Optional[Checkpoint]:
        """
        Load a checkpoint for an operation.

        Args:
            operation_type: Type of operation
            operation_id: Optional unique identifier for this operation run

        Returns:
            Checkpoint object if found, None otherwise
        """
        checkpoint_path = self.get_checkpoint_path(operation_type, operation_id)

        if not checkpoint_path.exists():
            return None

        try:
            with open(checkpoint_path, "r") as f:
                data = json.load(f)
            return Checkpoint(**data)
        except Exception as e:
            logger.error(f"Failed to load checkpoint from {checkpoint_path}: {e}")
            return None

    def get_remaining_items(
        self, operation_type: str, all_items: List[str], operation_id: Optional[str] = None
    ) -> List[str]:
        """
        Get list of items that haven't been completed yet.

        Args:
            operation_type: Type of operation
            all_items: Complete list of items to process
            operation_id: Optional unique identifier for this operation run

        Returns:
            List of items that still need to be processed
        """
        checkpoint = self.load_checkpoint(operation_type, operation_id)
        if not checkpoint:
            return all_items

        completed_set = set(checkpoint.completed_items)
        failed_set = set(checkpoint.failed_items)
        skipped_set = set(checkpoint.skipped_items)

        # Return items that haven't been completed, failed, or skipped
        return [
            item
            for item in all_items
            if item not in completed_set and item not in failed_set and item not in skipped_set
        ]

    def is_resumable(self, operation_type: str, operation_id: Optional[str] = None) -> bool:
        """
        Check if an operation can be resumed.

        Args:
            operation_type: Type of operation
            operation_id: Optional unique identifier for this operation run

        Returns:
            True if a valid checkpoint exists
        """
        checkpoint = self.load_checkpoint(operation_type, operation_id)
        if not checkpoint:
            return False

        # Check if operation is complete
        total_completed = len(checkpoint.completed_items) + len(checkpoint.failed_items) + len(checkpoint.skipped_items)
        return total_completed < checkpoint.total_items

    def delete_checkpoint(self, operation_type: str, operation_id: Optional[str] = None) -> bool:
        """
        Delete a checkpoint file.

        Args:
            operation_type: Type of operation
            operation_id: Optional unique identifier for this operation run

        Returns:
            True if checkpoint was deleted, False if it didn't exist
        """
        checkpoint_path = self.get_checkpoint_path(operation_type, operation_id)
        if checkpoint_path.exists():
            checkpoint_path.unlink()
            logger.info(f"Deleted checkpoint: {checkpoint_path}")
            return True
        return False

    def list_checkpoints(self, operation_type: Optional[str] = None) -> List[Path]:
        """
        List all checkpoint files.

        Args:
            operation_type: Optional filter by operation type

        Returns:
            List of checkpoint file paths
        """
        if operation_type:
            pattern = f"{operation_type}*.checkpoint.json"
        else:
            pattern = "*.checkpoint.json"

        return list(self.checkpoint_dir.glob(pattern))
