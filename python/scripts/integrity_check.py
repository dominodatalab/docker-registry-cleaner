#!/usr/bin/env python3
"""
MongoDB referential integrity check for Docker registry cleaner.

Verifies that cross-collection references are valid:
  - environment_revisions.environmentId             → environments_v2._id
  - environment_revisions.clonedEnvironmentRevisionId → environment_revisions._id
  - model_versions.modelId.value                   → models._id

Runs are intentionally excluded: the --unused-since flag may delete images for
environments that old runs still reference, so dangling run references are
expected after cleanup and would produce false positives here.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from bson import ObjectId

_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import SkopeoClient, config_manager
from utils.image_metadata import extract_model_tag_from_version_doc
from utils.logging_utils import get_logger, setup_logging
from utils.mongo_utils import get_mongo_client
from utils.report_utils import get_reports_dir, save_json
from utils.tag_matching import model_tags_match

logger = get_logger(__name__)


class IntegrityChecker:
    """Checks referential integrity across MongoDB collections used by the registry cleaner."""

    def run(self) -> Dict:
        mongo_client = get_mongo_client()
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            issues: List[Dict] = []

            env_ids, rev_count = self._check_environments(db, issues)
            model_count, version_count = self._check_models(db, issues)
            self._enrich_with_registry_status(db, issues)

            summary: Dict = {
                "environments_checked": len(env_ids),
                "revisions_checked": rev_count,
                "models_checked": model_count,
                "versions_checked": version_count,
                "total_issues": len(issues),
                "issues_by_type": {},
                "generated_at": datetime.now().isoformat(),
            }
            for issue in issues:
                t = issue["issue_type"]
                summary["issues_by_type"][t] = summary["issues_by_type"].get(t, 0) + 1

            orphaned_types = {"orphaned_revision", "orphaned_model_version"}
            with_image = sum(1 for i in issues if i["issue_type"] in orphaned_types and i.get("has_image") is True)
            without_image = sum(1 for i in issues if i["issue_type"] in orphaned_types and i.get("has_image") is False)
            if with_image or without_image:
                summary["orphaned_with_image"] = with_image
                summary["orphaned_without_image"] = without_image

            return {"summary": summary, "issues": issues}
        finally:
            mongo_client.close()

    def _check_environments(self, db, issues: List[Dict]) -> Tuple[Set[str], int]:
        logger.info("Loading environments from environments_v2...")
        env_ids: Set[str] = set()
        for doc in db["environments_v2"].find({}, {"_id": 1}):
            env_ids.add(str(doc["_id"]))
        logger.info(f"  {len(env_ids)} environments loaded")

        logger.info("Loading all environment revisions...")
        rev_docs = list(
            db["environment_revisions"].find({}, {"_id": 1, "environmentId": 1, "clonedEnvironmentRevisionId": 1})
        )
        rev_ids: Set[str] = {str(d["_id"]) for d in rev_docs}
        logger.info(f"  {len(rev_docs)} revisions loaded")

        for doc in rev_docs:
            rev_id = str(doc["_id"])
            env_id = doc.get("environmentId")

            if env_id is None:
                issues.append(
                    {
                        "collection": "environment_revisions",
                        "document_id": rev_id,
                        "issue_type": "missing_environment_id",
                        "referenced_id": None,
                        "description": "Document has no environmentId field",
                    }
                )
            elif str(env_id) not in env_ids:
                issues.append(
                    {
                        "collection": "environment_revisions",
                        "document_id": rev_id,
                        "issue_type": "orphaned_revision",
                        "referenced_id": str(env_id),
                        "description": f"environmentId {env_id} not found in environments_v2",
                    }
                )

            cloned_id = doc.get("clonedEnvironmentRevisionId")
            if cloned_id is not None and str(cloned_id) not in rev_ids:
                issues.append(
                    {
                        "collection": "environment_revisions",
                        "document_id": rev_id,
                        "issue_type": "broken_clone_reference",
                        "referenced_id": str(cloned_id),
                        "description": f"clonedEnvironmentRevisionId {cloned_id} not found in environment_revisions",
                    }
                )

        return env_ids, len(rev_docs)

    def _check_models(self, db, issues: List[Dict]) -> Tuple[int, int]:
        logger.info("Loading models...")
        model_ids: Set[str] = set()
        for doc in db["models"].find({}, {"_id": 1}):
            model_ids.add(str(doc["_id"]))
        logger.info(f"  {len(model_ids)} models loaded")

        logger.info("Loading model versions...")
        versions_checked = 0
        for doc in db["model_versions"].find({}, {"_id": 1, "modelId": 1}):
            versions_checked += 1
            ver_id = str(doc["_id"])
            model_id_val = doc.get("modelId")

            if not isinstance(model_id_val, dict) or model_id_val.get("value") is None:
                issues.append(
                    {
                        "collection": "model_versions",
                        "document_id": ver_id,
                        "issue_type": "missing_model_id",
                        "referenced_id": None,
                        "description": "Document has no modelId.value field",
                    }
                )
            else:
                mid = str(model_id_val["value"])
                if mid not in model_ids:
                    issues.append(
                        {
                            "collection": "model_versions",
                            "document_id": ver_id,
                            "issue_type": "orphaned_model_version",
                            "referenced_id": mid,
                            "description": f"modelId.value {mid} not found in models",
                        }
                    )

        logger.info(f"  {versions_checked} model versions checked")
        return len(model_ids), versions_checked

    def _enrich_with_registry_status(self, db, issues: List[Dict]) -> None:
        orphaned_rev_issues = [i for i in issues if i["issue_type"] == "orphaned_revision"]
        orphaned_ver_issues = [i for i in issues if i["issue_type"] == "orphaned_model_version"]

        if not orphaned_rev_issues and not orphaned_ver_issues:
            return

        logger.info("Cross-referencing orphaned documents with Docker registry...")

        try:
            skopeo = SkopeoClient(config_manager)
            repository = config_manager.get_repository()

            env_registry_tags: Set[str] = set()
            model_registry_tags: Set[str] = set()

            if orphaned_rev_issues:
                env_registry_tags = set(skopeo.list_tags(f"{repository}/environment"))
                logger.info(f"  {len(env_registry_tags)} environment tags in registry")

            if orphaned_ver_issues:
                model_registry_tags = set(skopeo.list_tags(f"{repository}/model"))
                logger.info(f"  {len(model_registry_tags)} model tags in registry")

        except Exception as e:
            logger.warning(f"Could not reach Docker registry for image check: {e}")
            for issue in orphaned_rev_issues + orphaned_ver_issues:
                issue["has_image"] = None
            return

        if orphaned_rev_issues:
            rev_oids = [ObjectId(i["document_id"]) for i in orphaned_rev_issues]
            rev_doc_map: Dict[str, Dict] = {
                str(d["_id"]): d
                for d in db["environment_revisions"].find(
                    {"_id": {"$in": rev_oids}},
                    {"_id": 1, "metadata.isBuilt": 1, "metadata.dockerImageName.tag": 1},
                )
            }
            for issue in orphaned_rev_issues:
                doc = rev_doc_map.get(issue["document_id"], {})
                is_built: Optional[bool] = doc.get("metadata", {}).get("isBuilt", None)
                if is_built is False:
                    issue["has_image"] = False
                else:
                    tag: Optional[str] = doc.get("metadata", {}).get("dockerImageName", {}).get("tag")
                    if tag:
                        issue["image_tag"] = tag
                        issue["has_image"] = tag in env_registry_tags
                    else:
                        issue["has_image"] = False

        if orphaned_ver_issues:
            ver_oids = [ObjectId(i["document_id"]) for i in orphaned_ver_issues]
            ver_doc_map: Dict[str, Dict] = {
                str(d["_id"]): d
                for d in db["model_versions"].find(
                    {"_id": {"$in": ver_oids}},
                    {"_id": 1, "metadata.builds": 1},
                )
            }
            for issue in orphaned_ver_issues:
                doc = ver_doc_map.get(issue["document_id"], {})
                tag = extract_model_tag_from_version_doc(doc)
                if tag:
                    issue["image_tag"] = tag
                    issue["has_image"] = any(model_tags_match(rt, tag) for rt in model_registry_tags)
                else:
                    issue["has_image"] = False


def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="Check MongoDB referential integrity for registry cleaner collections")
    parser.add_argument("--output", help="Output file path (default: integrity-check.json in reports dir)")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("   MongoDB Referential Integrity Check")
    logger.info("=" * 60)

    checker = IntegrityChecker()
    report = checker.run()

    summary = report["summary"]
    logger.info(f"Environments checked:      {summary['environments_checked']}")
    logger.info(f"Revisions checked:         {summary['revisions_checked']}")
    logger.info(f"Models checked (versions): {summary['versions_checked']}")
    logger.info(f"Total issues found:        {summary['total_issues']}")
    if summary["issues_by_type"]:
        for issue_type, count in summary["issues_by_type"].items():
            logger.info(f"  {issue_type}: {count}")
    if "orphaned_with_image" in summary:
        logger.info(f"Orphaned with image:       {summary['orphaned_with_image']} (needs investigation)")
        logger.info(
            f"Orphaned without image:    {summary['orphaned_without_image']} (safe to clean via delete_unused_references)"
        )

    output_path = args.output or str(get_reports_dir() / "integrity-check.json")
    saved_path = save_json(output_path, report, timestamp=True)
    logger.info(f"\nReport saved to: {saved_path}")


if __name__ == "__main__":
    main()
