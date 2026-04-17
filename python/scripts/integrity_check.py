#!/usr/bin/env python3
"""
MongoDB referential integrity check for Docker registry cleaner.

Verifies that cross-collection references are valid:
  - environment_revisions.environmentId             → environments_v2._id
  - environment_revisions.clonedEnvironmentRevisionId → environment_revisions._id
  - model_versions.modelId.value                   → models._id
  - runs.environmentId                             → environments_v2._id
  - runs.environmentRevisionId                     → environment_revisions._id
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

_parent_dir = Path(__file__).parent.parent.absolute()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from utils.config_manager import config_manager
from utils.logging_utils import get_logger, setup_logging
from utils.mongo_utils import get_mongo_client
from utils.report_utils import get_reports_dir, save_json

logger = get_logger(__name__)


class IntegrityChecker:
    """Checks referential integrity across MongoDB collections used by the registry cleaner."""

    def run(self) -> Dict:
        mongo_client = get_mongo_client()
        try:
            db = mongo_client[config_manager.get_mongo_db()]
            issues: List[Dict] = []

            env_ids, rev_ids, rev_count = self._check_environments(db, issues)
            model_count, version_count = self._check_models(db, issues)
            runs_count = self._check_runs(db, env_ids, rev_ids, issues)

            summary: Dict = {
                "environments_checked": len(env_ids),
                "revisions_checked": rev_count,
                "models_checked": model_count,
                "versions_checked": version_count,
                "runs_checked": runs_count,
                "total_issues": len(issues),
                "issues_by_type": {},
                "generated_at": datetime.now().isoformat(),
            }
            for issue in issues:
                t = issue["issue_type"]
                summary["issues_by_type"][t] = summary["issues_by_type"].get(t, 0) + 1

            return {"summary": summary, "issues": issues}
        finally:
            mongo_client.close()

    def _check_environments(self, db, issues: List[Dict]) -> Tuple[Set[str], Set[str], int]:
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

        return env_ids, rev_ids, len(rev_docs)

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

    def _check_runs(self, db, env_ids: Set[str], rev_ids: Set[str], issues: List[Dict]) -> int:
        """Check runs that reference environments or revisions for broken links."""
        logger.info("Checking runs for broken environment references...")
        runs_checked = 0
        query = {"$or": [{"environmentId": {"$exists": True}}, {"environmentRevisionId": {"$exists": True}}]}
        for doc in db["runs"].find(query, {"_id": 1, "environmentId": 1, "environmentRevisionId": 1}):
            runs_checked += 1
            run_id = str(doc["_id"])

            env_id = doc.get("environmentId")
            if env_id is not None and str(env_id) not in env_ids:
                issues.append(
                    {
                        "collection": "runs",
                        "document_id": run_id,
                        "issue_type": "run_missing_environment",
                        "referenced_id": str(env_id),
                        "description": f"environmentId {env_id} not found in environments_v2",
                    }
                )

            rev_id = doc.get("environmentRevisionId")
            if rev_id is not None and str(rev_id) not in rev_ids:
                issues.append(
                    {
                        "collection": "runs",
                        "document_id": run_id,
                        "issue_type": "run_missing_revision",
                        "referenced_id": str(rev_id),
                        "description": f"environmentRevisionId {rev_id} not found in environment_revisions",
                    }
                )

        logger.info(f"  {runs_checked} runs checked")
        return runs_checked


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
    logger.info(f"Runs checked:              {summary['runs_checked']}")
    logger.info(f"Total issues found:        {summary['total_issues']}")
    if summary["issues_by_type"]:
        for issue_type, count in summary["issues_by_type"].items():
            logger.info(f"  {issue_type}: {count}")

    output_path = args.output or str(get_reports_dir() / "integrity-check.json")
    saved_path = save_json(output_path, report, timestamp=True)
    logger.info(f"\nReport saved to: {saved_path}")


if __name__ == "__main__":
    main()
