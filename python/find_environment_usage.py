#!/usr/bin/env python3
"""
Find usage of a given environment (or its revisions) across Domino.

This script/function inspects:
  - MongoDB: environments_v2, environment_revisions, projects, scheduler_jobs, app_versions
  - Pre-generated reports (if present): workspace_env_usage_output.json, runs_env_usage_output.json,
    workload-report.json
"""

import argparse
import json
import logging
import os
import sys
from typing import Dict, List, Set

from bson import ObjectId

from config_manager import config_manager
from logging_utils import setup_logging
from mongo_utils import get_mongo_client
from object_id_utils import validate_object_id


def find_environment_usage(env_id: str) -> None:
    """
    Find usage of a given environment (or its revisions) across Domino.

    This inspects:
      - MongoDB: environments_v2, environment_revisions, projects, scheduler_jobs, app_versions
      - Pre-generated reports (if present): workspace_env_usage_output.json, runs_env_usage_output.json,
        workload-report.json
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
        projects_coll = db["projects"]
        scheduler_coll = db["scheduler_jobs"]

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

        # Projects using this environment as default
        projects = list(
            projects_coll.find(
                {"overrideV2EnvironmentId": env_obj_id},
                {"_id": 1, "name": 1, "ownerId": 1},
            )
        )

        # Scheduler jobs using this environment override
        scheduler_jobs = list(
            scheduler_coll.find(
                {"jobDataPlain.overrideEnvironmentId": env_obj_id},
                {"_id": 1, "jobName": 1, "projectId": 1},
            )
        )

        # Organizations using this environment as default (if collection exists)
        organizations: List[Dict] = []
        if "organizations" in db.list_collection_names():
            orgs_coll = db["organizations"]
            organizations = list(
                orgs_coll.find(
                    {"defaultV2EnvironmentId": env_obj_id},
                    {"_id": 1, "name": 1},
                )
            )

        # App versions that directly reference this environment (if collection exists)
        app_versions: List[Dict] = []
        if "app_versions" in db.list_collection_names():
            app_versions_coll = db["app_versions"]
            app_versions = list(
                app_versions_coll.find(
                    {"environmentId": env_obj_id},
                    {"_id": 1, "appId": 1, "versionNumber": 1},
                )
            )

        # Other environments / revisions that depend on these revisions via cloning
        cloned_from_revs = list(
            revs_coll.find(
                {"clonedEnvironmentRevisionId": {"$in": [ObjectId(r) for r in revision_ids]}},
                {"_id": 1, "environmentId": 1, "clonedEnvironmentRevisionId": 1},
            )
        )

        # Load auxiliary JSON reports (if present)
        output_dir = config_manager.get_output_dir()
        workspace_usage_path = os.path.join(output_dir, "workspace_env_usage_output.json")
        runs_usage_path = config_manager.get_runs_env_usage_path()
        workload_report_path = config_manager.get_workload_report_path()

        workspace_usages: List[Dict] = []
        if os.path.exists(workspace_usage_path):
            try:
                with open(workspace_usage_path, "r") as f:
                    workspace_usages = json.load(f)
            except Exception as e:
                logging.warning(f"Could not parse workspace environment usage file: {e}")

        runs_usages: List[Dict] = []
        if os.path.exists(runs_usage_path):
            try:
                with open(runs_usage_path, "r") as f:
                    runs_usages = json.load(f)
            except Exception as e:
                logging.warning(f"Could not parse runs environment usage file: {e}")

        workload_report: Dict[str, Dict] = {}
        if os.path.exists(workload_report_path):
            try:
                with open(workload_report_path, "r") as f:
                    workload_report = json.load(f)
            except Exception as e:
                logging.warning(f"Could not parse workload report file: {e}")

        # Helper to match tags that embed any of our IDs (env or revision)
        def _tag_matches_ids(tag: str) -> bool:
            if not tag or len(tag) < 24:
                return False
            prefix = tag[:24]
            return prefix in all_ids

        # Find workspace references (from workspace_env_usage_output.json)
        matching_workspaces: List[Dict] = []
        for rec in workspace_usages:
            tags: List[str] = []
            for key in [
                "environment_docker_tag",
                "project_default_environment_docker_tag",
                "compute_environment_docker_tag",
                "session_environment_docker_tag",
                "session_compute_environment_docker_tag",
            ]:
                val = rec.get(key)
                if isinstance(val, str) and val:
                    tags.append(val)
            if any(_tag_matches_ids(tag) for tag in tags):
                matching_workspaces.append(rec)

        # Find execution / runs references (from runs_env_usage_output.json)
        matching_runs: List[Dict] = []
        for rec in runs_usages:
            env_id_val = str(rec.get("environment_id") or "")
            rev_id_val = str(rec.get("environment_revision_id") or "")
            tag = rec.get("environment_docker_tag")
            if env_id_val in all_ids or rev_id_val in all_ids or (
                isinstance(tag, str) and _tag_matches_ids(tag)
            ):
                matching_runs.append(rec)

        # Find running workloads still using this environment (from workload-report.json)
        matching_workloads: Dict[str, Dict] = {}
        for tag, info in workload_report.items():
            if _tag_matches_ids(tag):
                matching_workloads[tag] = info

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

        logging.info("\n===== Workspace Usage (from workspace_env_usage_output.json) =====")
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

        logging.info("\n===== Run / Execution Usage (from runs_env_usage_output.json) =====")
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

        logging.info("\n===== Active Workloads (from workload-report.json) =====")
        if matching_workloads:
            logging.info(f"Found {len(matching_workloads)} workload image tags currently running.")
            for tag, info in matching_workloads.items():
                pods = info.get("pods", [])
                labels = info.get("labels", [])
                logging.info(
                    f"Tag {tag} -> pods={pods} labels={labels}"
                )
        else:
            logging.info("No active workloads found using this environment's image tags.")

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

