"""
Backup and restore Docker images to/from S3.

This script integrates with the docker-registry-cleaner project and uses SkopeoClient
for consistent registry operations. It reads from the 'environments' file by default.

The environments file can contain:
  - Typed ObjectIDs (e.g., "environment:507f1f77bcf86cd799439011")
  - Plain image tags (e.g., "my-env-v1-abc123")
  - Comments (lines starting with '#')

Example environments file:
    # Production environments
    environment:507f1f77bcf86cd799439011
    environment:507f191e810c19729de860ea
    # Legacy models
    model:6329334b3e5ab1f3e2e2e2e2

Features:
  - Uses SkopeoClient for registry authentication and image operations
  - Supports both local and in-pod skopeo execution
  - Automatic registry configuration from config.yaml
  - S3 backup/restore with checksum validation

Usage:
    # Backup images from environments file (uses config.yaml for registry/repo)
    python backup_restore.py backup --s3-bucket my-bucket

    # Restore specific tags (explicit repo override)
    python backup_restore.py restore --repo REGISTRY/REPO --s3-bucket my-bucket --tags tag1 tag2

    # Delete old images after backup
    python backup_restore.py backup --s3-bucket my-bucket --delete
"""

import argparse
import boto3
import datetime
import hashlib
import logging
import os
import re
import tempfile

from botocore.config import Config
from botocore.exceptions import ClientError
from typing import List, Optional, Tuple

from config_manager import ConfigManager, SkopeoClient
from logging_utils import get_logger
from object_id_utils import read_typed_object_ids_from_file

logger = get_logger(__name__)

def parse_registry_and_repo(full_repo: str) -> Tuple[str, str]:
    parts = full_repo.split('/')
    registry = parts[0]
    repository = '/'.join(parts[1:])
    return registry, repository

def get_s3_key(repository: str, tag: str) -> str:
    """Generate S3 key for an image tag."""
    return f"{repository.replace('/', '_')}/{tag}.tar"

def cleanup_tmpdir(tmpdir: Optional[str], dry_run: bool) -> None:
    """Clean up any stale temporary files left from previous runs."""
    if not tmpdir or dry_run:
        return
    
    try:
        for fname in os.listdir(tmpdir):
            if fname.startswith("tmp") or fname.startswith("docker-tarfile-blob"):
                fpath = os.path.join(tmpdir, fname)
                if os.path.isfile(fpath):
                    try:
                        os.remove(fpath)
                        logger.debug(f"Removed leftover file {fpath}")
                    except Exception as rm_err:
                        logger.warning(f"Could not remove leftover file {fpath}: {rm_err}")
    except Exception as list_err:
        logger.warning(f"Could not list tmpdir {tmpdir} for cleanup: {list_err}")

def create_temp_tar_file(tmpdir: Optional[str]):
    """Create a temporary tar file with optional custom tmpdir."""
    tmp_kwargs = {"suffix": ".tar", "delete": False}
    if tmpdir:
        tmp_kwargs["dir"] = tmpdir
    return tempfile.NamedTemporaryFile(**tmp_kwargs)

def cleanup_temp_file(filepath: str) -> None:
    """Remove a temporary file, logging any errors."""
    try:
        os.unlink(filepath)
    except Exception as rm_err:
        logger.warning(f"Could not remove temporary file {filepath}: {rm_err}")

def record_failed_tag(tag: str, failed_tags_file: Optional[str]) -> None:
    """Record a failed tag to a file."""
    if not failed_tags_file:
        return
    
    try:
        with open(failed_tags_file, "a") as f:
            f.write(f"{tag}\n")
    except Exception as fw_err:
        logger.warning(f"Could not record failed tag {tag} to {failed_tags_file}: {fw_err}")

def get_ecr_client(region_name):
    return boto3.client("ecr", region_name=region_name)

def get_s3_client(max_pool_connections: int = 20):
    """
    Create an S3 client with a larger connection pool.

    By default, boto3 clients use a small connection pool (10 connections). When
    processing many images concurrently, especially with multiple workers, this pool
    can become exhausted, leading to warnings like "Connection pool is full" from
    urllib3. To mitigate this, the S3 client is created with a configurable
    ``max_pool_connections`` setting.

    Parameters
    ----------
    max_pool_connections : int, optional
        The maximum number of connections to keep in the pool. A higher number
        reduces the likelihood of connection pool warnings at the cost of using
        more resources. The default is 20.

    Returns
    -------
    botocore.client.S3
        An S3 client configured with the specified connection pool size.
    """
    config = Config(max_pool_connections=max_pool_connections)
    return boto3.client("s3", config=config)

def list_ecr_images(ecr_client, repository_name):
    paginator = ecr_client.get_paginator('list_images')
    page_iterator = paginator.paginate(repositoryName=repository_name, filter={'tagStatus': 'TAGGED'})
    tags = []
    for page in page_iterator:
        for image in page['imageIds']:
            tags.append(image['imageTag'])
    return tags

def filter_tags(tags, prefix, exclude_latest=True, min_age_days=None):
    versioned_tags = []
    pattern = re.compile(rf"^{re.escape(prefix)}-v(\d+)-.*")
    for tag in tags:
        match = pattern.match(tag)
        if match:
            version = int(match.group(1))
            versioned_tags.append((version, tag))
    versioned_tags.sort()
    if exclude_latest and versioned_tags:
        versioned_tags = versioned_tags[:-1]
    return [tag for _, tag in versioned_tags]

def calculate_checksum(file_path, algo='sha256'):
    h = hashlib.new(algo)
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def upload_to_s3(s3_client, bucket, key, file_path):
    s3_client.upload_file(file_path, bucket, key)

def s3_checksum_exists(s3_client, bucket, key):
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        # Treat 404 and 403 as "does not exist"/not accessible so that backup/restore
        # proceeds.  Many bucket policies require additional permissions for HeadObject.
        code = e.response.get('Error', {}).get('Code')
        if code in ('404', '403', 'NoSuchKey', 'AccessDenied'):
            return False
        logger.error(f"Error checking s3 object {key}: {e}")
        return False

def s3_checksum_matches(s3_client, bucket, key, local_checksum):
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            s3_client.download_file(bucket, key, temp_file.name)
            s3_checksum = calculate_checksum(temp_file.name)
        os.unlink(temp_file.name)
        return s3_checksum == local_checksum
    except ClientError as e:
        logger.error(f"S3 download/checksum failed for {key}: {e}")
        return False

def skopeo_copy_to_tar(
    skopeo_client: SkopeoClient,
    image: str,
    output_path: str,
    tmpdir: Optional[str] = None,
) -> None:
    """Copy a container image from a registry to a local tar file using skopeo.

    Parameters
    ----------
    skopeo_client : SkopeoClient
        SkopeoClient instance for running skopeo commands.
    image : str
        Fully-qualified image reference (``registry/repository:tag``).
    output_path : str
        Path to write the resulting tar archive.
    tmpdir : Optional[str]
        Temporary directory for skopeo to use for blobs. When not provided,
        skopeo defaults to ``/var/tmp``, which may not have sufficient space.
    """
    args = ["--all"]
    if tmpdir:
        args.extend(["--tmpdir", tmpdir])
    args.extend([
        f"docker://{image}",
        f"docker-archive:{output_path}",
    ])
    skopeo_client.run_skopeo_command("copy", args)


def restore_images(
    skopeo_client: SkopeoClient,
    full_repo: str,
    tags: List[str],
    s3_bucket: str,
    region: str,
    dry_run: bool,
    tmpdir: Optional[str] = None,
    failed_tags_file: Optional[str] = None,
) -> None:
    """
    Restore images from S3 archives back into a container registry.

    For each tag provided, this function downloads the corresponding tar file
    from S3 and uses ``skopeo`` to push it back to the specified registry
    repository. If ``dry_run`` is set, actions are logged but not executed.

    Parameters
    ----------
    skopeo_client : SkopeoClient
        SkopeoClient instance for running skopeo commands.
    full_repo : str
        Full registry/repository identifier where images will be restored (e.g.
        ``ACCOUNT.dkr.ecr.REGION.amazonaws.com/repo``).
    tags : List[str]
        List of image tags to restore. Each tag corresponds to a tar file in S3.
    s3_bucket : str
        S3 bucket where tar archives are stored.
    region : str
        AWS region used for ECR operations.
    dry_run : bool
        When ``True``, log actions without performing any download or push.
    """
    registry, repository = parse_registry_and_repo(full_repo)
    s3_client = get_s3_client()
    ecr_client = get_ecr_client(region)

    cleanup_tmpdir(tmpdir, dry_run)

    # SkopeoClient handles authentication automatically
    
    for tag in tags:
        s3_key = get_s3_key(repository, tag)
        image = f"{registry}/{repository}:{tag}"
        # Skip if the image tag already exists in the target repository
        try:
            resp = ecr_client.describe_images(
                repositoryName=repository,
                imageIds=[{"imageTag": tag}],
            )
            # If ``imageDetails`` is non-empty, the tag exists and we skip restoration
            if resp.get("imageDetails"):
                logger.info(f"🔄 Skipping {image}, already present in ECR")
                continue
        except ClientError as describe_err:
            # When the image is not found, ECR may raise a ClientError with
            # code ``ImageNotFoundException``. In that case, proceed with restore.
            error_code = describe_err.response.get("Error", {}).get("Code")
            if error_code != "ImageNotFoundException":
                # Log unexpected errors but continue with restore
                logger.warning(f"Could not check existence of {image}: {describe_err}")
        logger.info(f"Restoring {image} from S3 key {s3_key}...")
        if dry_run:
            continue
        # Download archive from S3 and push back to the registry
        try:
            tmp_file = create_temp_tar_file(tmpdir)
            try:
                with tmp_file:
                    s3_client.download_file(s3_bucket, s3_key, tmp_file.name)
                    args = ["--all"]
                    if tmpdir:
                        args.extend(["--tmpdir", tmpdir])
                    args.extend([
                        f"docker-archive:{tmp_file.name}",
                        f"docker://{image}",
                    ])
                    skopeo_client.run_skopeo_command("copy", args)
                    logger.info(f"✅ Restored {image}")
            finally:
                cleanup_temp_file(tmp_file.name)
        except Exception as ex:
            logger.error(f"❌ Failed to restore {image}: {ex}")
            record_failed_tag(tag, failed_tags_file)

def process_backup(
    skopeo_client: SkopeoClient,
    full_repo: str,
    tags: List[str],
    s3_bucket: str,
    region: str,
    dry_run: bool,
    delete: bool,
    min_age_days: Optional[int] = None,
    workers: int = 1,
    tmpdir: Optional[str] = None,
    failed_tags_file: Optional[str] = None,
) -> None:
    """
    Back up images for the given list of tags.

    This function performs the following actions for each tag:
    1. Optionally skip images younger than ``min_age_days``.
    2. Skip processing if the image has already been archived in S3.
    3. Copy the image to a local tar file via ``skopeo``.
    4. Upload the tar file to S3 and verify its checksum.
    5. Optionally delete the original image from the registry.

    When ``workers`` is greater than 1, multiple tags are processed concurrently
    using a thread pool.
    
    Parameters
    ----------
    skopeo_client : SkopeoClient
        SkopeoClient instance for running skopeo commands.
    """

    registry, repository = parse_registry_and_repo(full_repo)
    ecr_client = get_ecr_client(region)
    s3_client = get_s3_client()

    # Pre-calculate age cutoff if provided
    cutoff_time = None
    if isinstance(min_age_days, int) and min_age_days > 0:
        cutoff_time = datetime.datetime.utcnow() - datetime.timedelta(days=min_age_days)

    cleanup_tmpdir(tmpdir, dry_run)

    # SkopeoClient handles authentication automatically

    def process_single_tag(tag: str) -> None:
        image = f"{registry}/{repository}:{tag}"
        s3_key = get_s3_key(repository, tag)

        # Age check
        if cutoff_time is not None:
            try:
                resp = ecr_client.describe_images(
                    repositoryName=repository,
                    imageIds=[{"imageTag": tag}],
                )
                details = resp.get("imageDetails", [])
                if details:
                    pushed_at = details[0].get("imagePushedAt")
                    if pushed_at and pushed_at > cutoff_time:
                        logger.info(f"⏭️  Skipping {image} due to age filter")
                        return
            except Exception as err:
                logger.warning(f"Could not determine age for {image}: {err}")

        # Skip if already backed up
        if s3_checksum_exists(s3_client, s3_bucket, s3_key):
            logger.info(f"🔄 Skipping {image}, already backed up")
            # If deletion is requested, remove the tag even if it was backed up by a previous run
            if delete:
                try:
                    ecr_client.batch_delete_image(
                        repositoryName=repository,
                        imageIds=[{"imageTag": tag}],
                    )
                    logger.info(f"🗑️  Deleted {image} from registry (pre-archived)")
                except Exception as del_err:
                    logger.error(f"❌ Failed to delete {image} from registry: {del_err}")
            return

        logger.info(f"Processing {image}...")
        if dry_run:
            return

        try:
            tmp_file = create_temp_tar_file(tmpdir)
            try:
                with tmp_file:
                    # Copy image to tar
                    skopeo_copy_to_tar(skopeo_client, image, tmp_file.name, tmpdir=tmpdir)
                    checksum = calculate_checksum(tmp_file.name)
                    # Upload to S3
                    upload_to_s3(s3_client, s3_bucket, s3_key, tmp_file.name)
                    # Verify checksum
                    if s3_checksum_matches(s3_client, s3_bucket, s3_key, checksum):
                        logger.info(f"✅ Backed up {image}")
                        if delete:
                            ecr_client.batch_delete_image(
                                repositoryName=repository,
                                imageIds=[{"imageTag": tag}],
                            )
                            logger.info(f"🗑️  Deleted {image} from registry")
                    else:
                        logger.error(f"❌ Checksum mismatch for {image}")
            finally:
                cleanup_temp_file(tmp_file.name)
        except Exception as ex:
            logger.error(f"❌ Failed to backup image {image}: {ex}")
            record_failed_tag(tag, failed_tags_file)

    # Process tags concurrently or sequentially
    if workers and workers > 1:
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(process_single_tag, t) for t in tags]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as exc:
                        logger.error(f"Thread encountered an error: {exc}")
        except Exception as pool_err:
            logger.warning(f"Concurrency setup failed ({pool_err}); falling back to sequential processing")
            for t in tags:
                process_single_tag(t)
    else:
        for t in tags:
            process_single_tag(t)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["backup", "restore", "delete"])
    parser.add_argument("--repo", help="Registry and repository (e.g. IMAGE_REGISTRY/repo). Defaults to config.yaml values.")
    # ``--prefix`` is required for the backup mode but optional for restore.  We enforce this
    # requirement after parsing based on the selected mode.
    parser.add_argument(
        "--prefix",
        required=False,
        help="Prefix to filter tags (e.g. commit SHA). Required when running in backup mode and ignored in restore mode.",
    )
    parser.add_argument("--s3-bucket", required=True, help="S3 bucket for backups")
    parser.add_argument("--region", default="us-west-2", help="AWS region")
    parser.add_argument("--dry-run", action="store_true", help="Simulate operations")
    parser.add_argument("--min-age-days", type=int, help="Skip tags younger than this")
    parser.add_argument("--delete", action="store_true", help="Delete image from registry after successful backup")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent workers for processing tags (default: 1)")
    parser.add_argument(
        "--tmpdir",
        help=(
            "Temporary directory for skopeo to use when copying images. Useful when the default /var/tmp does not have enough space"
        ),
    )
    parser.add_argument(
        "--log-file",
        help="Write all log output to this file instead of or in addition to the console",
    )
    parser.add_argument(
        "--failed-tags-file",
        help="File to record image tags that failed to backup or restore. Each failed tag will be appended on a new line.",
    )
    parser.add_argument(
        "--file",
        default="environments",
        help=(
            "Path to file containing ObjectIDs or image tags (default: environments). "
            "Supports typed ObjectID format (environment:ID, model:ID) or plain tags. "
            "Lines beginning with '#' are ignored."
        ),
    )
    parser.add_argument(
        "--tags",
        nargs="+",
        help=(
            "One or more image tags to restore (used only with the restore mode). "
            "When omitted in backup mode, all tags matching the prefix are processed."
        ),
    )
    args = parser.parse_args()

    # Initialize ConfigManager and SkopeoClient
    cfg_mgr = ConfigManager()
    skopeo_client = SkopeoClient(cfg_mgr, use_pod=False)

    # Use config_manager values if --repo not provided
    if not args.repo:
        registry_url = cfg_mgr.get_registry_url()
        repository = cfg_mgr.get_repository()
        args.repo = f"{registry_url}/{repository}"
        logger.info(f"Using repository from config: {args.repo}")

    # Configure optional file logging before any processing
    if args.log_file:
        try:
            file_handler = logging.FileHandler(args.log_file)
            file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            logger.logger.addHandler(file_handler)
        except Exception as log_err:
            logger.warning(f"Unable to set up log file {args.log_file}: {log_err}")

    # Consolidate tags from CLI and file, if provided.  The final list of tags
    # is used in both backup and restore modes.  For backup, when tags are
    # provided explicitly, the prefix requirement is relaxed.
    tags_from_file: List[str] = []
    if args.file:
        try:
            # Try to read as typed ObjectIDs first
            object_ids = read_typed_object_ids_from_file(args.file)
            if object_ids:
                # Convert ObjectIDs to tags (format: environment-{objectid} or model-{objectid})
                for oid_type, oid in object_ids:
                    # Use the type prefix that matches this project's convention
                    tags_from_file.append(f"{oid_type}-{oid}")
            else:
                # Fall back to reading plain tags (one per line)
                with open(args.file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        tags_from_file.append(line)
        except FileNotFoundError:
            # If the default 'environments' file doesn't exist, that's OK
            if args.file != "environments":
                parser.error(f"Unable to read tags from {args.file}: file not found")
        except Exception as err:
            parser.error(f"Unable to read tags from {args.file}: {err}")

    if args.mode == "backup":
        registry, repository = parse_registry_and_repo(args.repo)
        ecr_client = get_ecr_client(args.region)
        if args.tags or tags_from_file:
            # Use provided tags (from CLI or file).  Combine both sources.
            tags = []
            if args.tags:
                tags.extend(args.tags)
            tags.extend(tags_from_file)
            # Remove duplicates while preserving order
            seen = set()
            tags = [t for t in tags if not (t in seen or seen.add(t))]
            # Warn if prefix is provided but tags are explicit
            if args.prefix:
                logger.info("Explicit tag list provided; --prefix is ignored in this run")
        else:
            # Require prefix only when auto-discovering tags
            if not args.prefix:
                parser.error("--prefix must be provided when using backup mode without explicit tags")
            tags = list_ecr_images(ecr_client, repository)
            tags = filter_tags(
                tags,
                args.prefix,
                exclude_latest=True,
                min_age_days=args.min_age_days,
            )
        process_backup(
            skopeo_client,
            args.repo,
            tags,
            args.s3_bucket,
            args.region,
            args.dry_run,
            args.delete,
            min_age_days=args.min_age_days,
            workers=args.workers,
            tmpdir=args.tmpdir,
            failed_tags_file=args.failed_tags_file,
        )
    elif args.mode == "restore":
        # Consolidate tags from CLI and file
        restore_tags: List[str] = []
        if args.tags:
            restore_tags.extend(args.tags)
        restore_tags.extend(tags_from_file)
        # Remove duplicates
        seen_restore = set()
        restore_tags = [t for t in restore_tags if not (t in seen_restore or seen_restore.add(t))]
        if not restore_tags:
            parser.error("--tags or --file must be provided when using restore mode")
        restore_images(
            skopeo_client,
            args.repo,
            restore_tags,
            args.s3_bucket,
            args.region,
            args.dry_run,
            tmpdir=args.tmpdir,
            failed_tags_file=args.failed_tags_file,
        )
    elif args.mode == "delete":
        delete_tags = list(dict.fromkeys((args.tags or []) + tags_from_file))
        if not delete_tags:
            if not args.prefix:
                parser.error("--prefix required when no explicit tags for delete")
            _, repo = parse_registry_and_repo(args.repo)
            all_tags = list_ecr_images(get_ecr_client(args.region), repo)
            delete_tags = filter_tags(all_tags, args.prefix, exclude_latest=True)
    
        if not delete_tags:
            logger.warning("No tags to delete. Skipping delete operation.")
            return
    
        ecr = get_ecr_client(args.region)
        for tag in delete_tags:
            image = f"{args.repo}:{tag}"
            try:
                if not args.dry_run:
                    ecr.batch_delete_image(
                        repositoryName=repo,
                        imageIds=[{"imageTag": tag}]
                    )
                logger.info(f"🗑️  {'(dry-run) would delete' if args.dry_run else 'Deleted'} {image}")
            except Exception as e:
                logger.error(f"❌ Failed to delete {image}: {e}")
                record_failed_tag(tag, args.failed_tags_file)


if __name__ == "__main__":
    main()