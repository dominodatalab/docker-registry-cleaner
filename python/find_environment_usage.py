#!/usr/bin/env python3
"""
Find usage of a given environment (or its revisions) across Domino.

This script/function inspects:
  - MongoDB: environments_v2, environment_revisions, projects, scheduler_jobs, app_versions
  - Pre-generated consolidated MongoDB usage report (if present): mongodb_usage_report.json
"""

import argparse
import logging
import sys
from typing import Dict, List, Set

from bson import ObjectId

from config_manager import config_manager
from logging_utils import setup_logging
from mongo_utils import get_mongo_client
from object_id_utils import validate_object_id
from image_usage import ImageUsageService


def find_environment_usage(env_id: str) -> None:
    """
    Find usage of a given environment (or its revisions) across Domino.

    This inspects:
      - MongoDB: environments_v2, environment_revisions, projects, scheduler_jobs, app_versions
      - Pre-generated consolidated MongoDB usage report (if present): mongodb_usage_report.json
    """
    setup_logging()
    logging.info(f"Finding usage for environment ID: {env_id}")

    try:
        env_obj_id = validate_object_id(env_id, field_name="Environment ID")
    except ValueError as e:
        logging.error(str(e))
        sys.exit(1)

    mongo_client = get_mongo_client()
    db = mongo_client[config_manager.get_mongo_db()]

    try:
        # Base environment document
        envs_coll = db["environments_v2"]
        revs_coll = db["environment_revisions"]

        environment = envs_coll.find_one({"_id": env_obj_id})
        if not environment:
            logging.warning(
                f"Environment with ID {env_id} not found in environments_v2 (it may be archived or deleted)."
            )

        # All revisions belonging to this environment
        revision_ids: Set[str] = set()
        for rev in revs_coll.find({"environmentId": env_obj_id}, {"_id": 1}):
            revision_ids.add(str(rev["_id"]))
        logging.info(f"Found {len(revision_ids)} environment revisions for environment {env_id}")

        all_ids: Set[str] = {env_id} | revision_ids

        # Other environments / revisions that depend on these revisions via cloning
        cloned_from_revs = list(
            revs_coll.find(
                {"clonedEnvironmentRevisionId": {"$in": [ObjectId(r) for r in revision_ids]}},
                {"_id": 1, "environmentId": 1, "clonedEnvironmentRevisionId": 1},
            )
        )

        # Load auxiliary JSON reports using service
        service = ImageUsageService()
        
        # Find direct environment ID usage in MongoDB collections (projects, scheduler_jobs, etc.)
        direct_usage = service.find_direct_environment_id_usage(all_ids)
        
        # Aggregate direct usage results
        projects: List[Dict] = []
        scheduler_jobs: List[Dict] = []
        organizations: List[Dict] = []
        app_versions: List[Dict] = []
        
        for env_id, usage_info in direct_usage.items():
            projects.extend(usage_info['projects'])
            scheduler_jobs.extend(usage_info['scheduler_jobs'])
            organizations.extend(usage_info['organizations'])
            app_versions.extend(usage_info['app_versions'])
        
        # Load Docker tag usage reports
        mongodb_reports = service.load_mongodb_usage_reports()
        
        workspace_usages = mongodb_reports.get('workspaces', [])
        runs_usages = mongodb_reports.get('runs', [])

        # Use service to find usage for all environment/revision IDs
        usage_by_id = service.find_usage_for_environment_ids(
            all_ids,
            mongodb_reports=mongodb_reports
        )
        
        # Aggregate results across all IDs
        all_matching_tags: Set[str] = set()
        matching_workspaces: List[Dict] = []
        matching_runs: List[Dict] = []
        seen_workspace_ids: Set[str] = set()
        seen_run_ids: Set[str] = set()
        
        for env_id, usage_info in usage_by_id.items():
            # Collect matching tags
            all_matching_tags.update(usage_info['matching_tags'])
            
            # Collect workspaces (deduplicate by workspace_id)
            for ws in usage_info['workspaces']:
                ws_id = str(ws.get('workspace_id') or ws.get('_id') or ws.get('workspaceId') or '')
                if ws_id and ws_id not in seen_workspace_ids:
                    matching_workspaces.append(ws)
                    seen_workspace_ids.add(ws_id)
            
            # Collect runs (deduplicate by run_id)
            for run in usage_info['runs']:
                run_id = str(run.get('run_id') or run.get('_id') or run.get('runId') or '')
                if run_id and run_id not in seen_run_ids:
                    matching_runs.append(run)
                    seen_run_ids.add(run_id)

        # ------- Summary output -------
        logging.info("\n===== Environment Metadata =====")
        if environment:
            logging.info(f"Environment name: {environment.get('name', '')}")
            logging.info(f"Visibility: {environment.get('visibility', 'unknown')}")
            logging.info(f"OwnerId: {environment.get('ownerId')}")
            logging.info(f"isArchived: {environment.get('isArchived')}")
        else:
            logging.info("No active environments_v2 document found for this ID.")

        logging.info("\n===== Revisions =====")
        if revision_ids:
            logging.info(f"Revision IDs ({len(revision_ids)}): {sorted(revision_ids)}")
        else:
            logging.info("No environment_revisions found for this environment.")

        logging.info("\n===== Projects Using as Default Environment =====")
        if projects:
            for p in projects:
                logging.info(
                    f"Project _id={p.get('_id')} name={p.get('name', '')} ownerId={p.get('ownerId')}"
                )
        else:
            logging.info("No projects found using this environment as overrideV2EnvironmentId.")

        logging.info("\n===== Scheduler Jobs Using Environment Override =====")
        if scheduler_jobs:
            for j in scheduler_jobs:
                logging.info(
                    f"SchedulerJob _id={j.get('_id')} name={j.get('jobName', '')} projectId={j.get('projectId')}"
                )
        else:
            logging.info("No scheduler_jobs found with overrideEnvironmentId pointing to this environment.")

        logging.info("\n===== Organizations Using as Default Environment =====")
        if organizations:
            for org in organizations:
                logging.info(
                    f"Organization _id={org.get('_id')} name={org.get('name', '')} "
                    f"defaultV2EnvironmentId={env_id}"
                )
        else:
            logging.info("No organizations found using this environment as defaultV2EnvironmentId.")

        logging.info("\n===== App Versions Referencing Environment =====")
        if app_versions:
            for av in app_versions:
                logging.info(
                    f"AppVersion _id={av.get('_id')} appId={av.get('appId')} "
                    f"versionNumber={av.get('versionNumber')}"
                )
        else:
            logging.info("No app_versions found referencing this environment (or collection missing).")

        logging.info("\n===== Other Environments / Revisions Cloned From These Revisions =====")
        if cloned_from_revs:
            for r in cloned_from_revs:
                logging.info(
                    "Revision _id=%s environmentId=%s clonedEnvironmentRevisionId=%s",
                    r.get("_id"),
                    r.get("environmentId"),
                    r.get("clonedEnvironmentRevisionId"),
                )
        else:
            logging.info("No environment_revisions found that clone from this environment's revisions.")

        logging.info("\n===== Workspace Usage =====")
        if matching_workspaces:
            logging.info(f"Found {len(matching_workspaces)} workspace records referencing this environment.")
            # Print a concise line per record if possible
            for rec in matching_workspaces:
                workspace_id = rec.get("workspace_id") or rec.get("workspaceId")
                project_id = rec.get("project_id") or rec.get("projectId")
                owner = rec.get("owner_username") or rec.get("user_name")
                logging.info(
                    f"Workspace workspace_id={workspace_id} project_id={project_id} owner={owner}"
                )
        else:
            logging.info("No workspace usage records found for this environment.")

        logging.info("\n===== Run / Execution Usage =====")
        if matching_runs:
            logging.info(f"Found {len(matching_runs)} run records referencing this environment.")
            for rec in matching_runs[:50]:
                run_id = rec.get("run_id") or rec.get("runId")
                project_id = rec.get("project_id") or rec.get("projectId")
                last_used = rec.get("last_used") or rec.get("completed") or rec.get("started")
                logging.info(
                    f"Run run_id={run_id} project_id={project_id} last_used={last_used}"
                )
            if len(matching_runs) > 50:
                logging.info(
                    "Additional %d run records omitted from log (use JSON files for full details).",
                    len(matching_runs) - 50,
                )
        else:
            logging.info("No run usage records found for this environment.")

        logging.info("\n✅ Environment usage lookup completed.")
    finally:
        mongo_client.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find usage of a specific environment ID across Domino.",
    )
    parser.add_argument(
        "--environment-id",
        required=True,
        help="Environment ObjectId (24-char hex) to inspect",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    find_environment_usage(args.environment_id)


if __name__ == "__main__":
    main()

