import argparse
import os
import json
from typing import List
from pymongo import MongoClient
from bson import json_util
from logging_utils import setup_logging, get_logger
from config_manager import config_manager
from report_utils import save_json
from mongo_utils import get_mongo_client, bson_to_jsonable

logger = get_logger(__name__)


def model_env_usage_pipeline() -> List[dict]:
	# Converted from mongo_queries/model_env_usage.js
	return [
		{"$match": {"isArchived": False}},
		{"$project": {
			"model_id": "$_id",
			"model_name": "$name",
			"model_created_id": "$metadata.createdBy",
			"model_last_change": "$metadata.lastModified",
			"model_environment_id": "$environmentId",
			"collaboratorSettings": {"$first": {"$filter": {"input": "$collaboratorSettings", "as": "item", "cond": {"$eq": ["$$item.role", "Owner"]}}}}
		}},
		{"$lookup": {"from": "users", "localField": "collaboratorSettings.collaboratorId", "foreignField": "_id", "as": "user_id"}},
		{"$lookup": {"from": "users", "localField": "model_created_id", "foreignField": "_id", "as": "created_id"}},
		{"$lookup": {"from": "environments_v2", "localField": "model_environment_id", "foreignField": "_id", "as": "environment_id"}},
		{"$lookup": {"from": "model_versions", "localField": "_id", "foreignField": "modelId.value", "as": "model_versions_id"}},
		{"$unwind": "$model_versions_id"},
		{"$lookup": {"from": "sagas", "localField": "model_versions_id._id", "foreignField": "parameters.modelVersionId", "as": "sagas_id"}},
		{"$unwind": "$sagas_id"},
		{"$sort": {"sagas_id.started": -1}},
		{"$match": {"sagas_id.sagaName": {"$in": ["ModelVersionDeployment", "StartModelVersion", "StopModelVersion"]}, "sagas_id.state": {"$in": ["slug-build-succeeded", "succeeded"]}, "sagas_id.isCompleted": {"$eq": True}}},
		{"$lookup": {"from": "environment_revisions", "localField": "model_versions_id.environmentRevisionId", "foreignField": "_id", "as": "environment_revisions_id"}},
		{"$project": {
			"model_id": "$model_id",
			"model_name": 1,
			"model_last_change": 1,
			"model_owner": {"$first": "$user_id.fullName"},
			"model_created_by": {"$first": "$created_id.fullName"},
			"environment_name": {"$first": "$environment_id.name"},
			"environment_id": "$model_environment_id",
			"model_version_id": "$model_versions_id._id",
			"model_environment_repository": {"$first": "$model_versions_id.metadata.builds.slug.image.repository"},
			"model_environment_tag": {"$first": "$model_versions_id.metadata.builds.slug.image.tag"},
			"base_environment_repository": {"$first": "$environment_revisions_id.metadata.dockerImageName.repository"},
			"base_environment_tag": {"$first": "$environment_revisions_id.metadata.dockerImageName.tag"},
			"environment_revision_id": "$model_versions_id.environmentRevisionId",
			"saga_name": "$sagas_id.sagaName"
		}},
		{"$group": {
			"_id": "$model_version_id",
			"saga_status": {"$push": "$saga_name"},
			"model_id": {"$first": "$model_id"},
			"model_name": {"$first": "$model_name"},
			"model_last_change": {"$first": "$model_last_change"},
			"model_owner": {"$first": "$model_owner"},
			"model_created_by": {"$first": "$model_created_by"},
			"environment_name": {"$first": "$environment_name"},
			"environment_id": {"$first": "$environment_id"},
			"environment_revision_id": {"$first": "$environment_revision_id"},
			"model_version_id": {"$first": "$model_version_id"},
			"model_environment_repository": {"$first": "$model_environment_repository"},
			"model_environment_tag": {"$first": "$model_environment_tag"},
			"base_environment_repository": {"$first": "$base_environment_repository"},
			"base_environment_tag": {"$first": "$base_environment_tag"}
		}},
		{"$match": {"saga_status.0": {"$eq": "StartModelVersion"}}},
		{"$project": {"saga_status": 0}},
		{"$group": {
			"_id": "$model_id",
			"model_id": {"$first": "$model_id"},
			"model_name": {"$first": "$model_name"},
			"model_last_change": {"$first": "$model_last_change"},
			"model_owner": {"$first": "$model_owner"},
			"model_created_by": {"$first": "$model_created_by"},
			"environment_name": {"$first": "$environment_name"},
			"environment_id": {"$first": "$environment_id"},
			"model_active_versions": {"$push": {
				"environment_revision_id": "$environment_revision_id",
				"model_version_id": "$model_version_id",
				"model_environment_repository": "$model_environment_repository",
				"model_environment_tag": "$model_environment_tag",
				"base_environment_repository": "$base_environment_repository",
				"base_environment_tag": "$base_environment_tag"
			}}
		}}
	]


def workspace_env_usage_pipeline() -> List[dict]:
	# Converted from mongo_queries/workspace_env_usage.js
	return [
		{"$match": {"state": {"$in": ["Stopped", "Deleted"]}}},
		{"$lookup": {"from": "users", "localField": "ownerId", "foreignField": "_id", "as": "user_id"}},
		{"$lookup": {"from": "projects", "localField": "projectId", "foreignField": "_id", "as": "project_id"}},
		{"$lookup": {"from": "workspace_session", "localField": "_id", "foreignField": "workspaceId", "as": "workspace_id"}},
		{"$lookup": {"from": "environments_v2", "localField": "configTemplate.environmentId", "foreignField": "_id", "as": "environment_id"}},
		{"$lookup": {"from": "environments_v2", "localField": "project_id.overrideV2EnvironmentId", "foreignField": "_id", "as": "default_project_environment_id"}},
		{"$addFields": {"running_environments": {"$filter": {"input": "$workspace_id", "as": "item", "cond": {"$ne": ["$$item.rawExecutionDisplayStatus", "Running"]}}}}},
		{"$project": {
			"workspace_name": "$name",
			"workspace_last_change": "$stateUpdatedAt",
			"project_name": {"$first": "$project_id.name"},
			"user_name": {"$first": "$user_id.fullName"},
			"environment_name": {"$first": "$environment_id.name"},
			"project_default_environment_name": {"$first": "$default_project_environment_id.name"},
			"project_default_environment_id": {"$first": "$project_id.overrideV2EnvironmentId"},
			"project_active_revision_spec": {"$first": "$project_id.defaultEnvironmentRevisionSpec"},
			"user_active_revision_id": {"$switch": {"branches": [
				{"case": {"$gt": [{"$size": "$running_environments"}, 0]}, "then": {"$first": "$running_environments.environmentRevisionId"}},
				{"case": {"$eq": ["$configTemplate.environmentRevisionSpec", "ActiveRevision"]}, "then": {"$first": "$environment_id.activeRevisionId"}},
				{"case": {"$eq": [True, {"$regexMatch": {"input": "$configTemplate.environmentRevisionSpec", "regex": "SomeRevision", "options": "i"}}]}, "then": {"$toObjectId": {"$replaceOne": {"input": {"$replaceOne": {"input": "$configTemplate.environmentRevisionSpec", "find": "SomeRevision(", "replacement": ""}}, "find": ")", "replacement": ""}}}}
			], "default": None}}}},
		{"$lookup": {"from": "environment_revisions", "localField": "user_active_revision_id", "foreignField": "_id", "as": "environment_revision_id"}},
		{"$lookup": {"from": "environments_v2", "localField": "project_default_environment_id", "foreignField": "_id", "as": "default_environment_active_revision"}},
		{"$project": {
			"workspace_name": "$workspace_name",
			"workspace_last_change": "$workspace_last_change",
			"project_name": "$project_name",
			"user_name": "$user_name",
			"environment_name": "$environment_name",
			"environment_docker_repo": {"$first": "$environment_revision_id.metadata.dockerImageName.repository"},
			"environment_docker_tag": {"$first": "$environment_revision_id.metadata.dockerImageName.tag"},
			"user_active_revision_id": "$user_active_revision_id",
			"project_default_environment_name": "$project_default_environment_name",
			"project_default_environment_id": "$project_default_environment_id",
			"project_default_active_revision_id": {"$first": "$default_environment_active_revision.activeRevisionId"},
			"project_active_revision_spec": "$project_active_revision_spec",
			"default_active_revision_id": {"$switch": {"branches": [
				{"case": {"$eq": ["$project_active_revision_spec", "ActiveRevision"]}, "then": {"$first": "$default_environment_active_revision.activeRevisionId"}},
				{"case": {"$eq": [True, {"$regexMatch": {"input": "$project_active_revision_spec", "regex": "SomeRevision", "options": "i"}}]}, "then": {"$toObjectId": {"$replaceOne": {"input": {"$replaceOne": {"input": "$project_active_revision_spec", "find": "SomeRevision(", "replacement": ""}}, "find": ")", "replacement": ""}}}}
			], "default": None}}
		}},
		{"$lookup": {"from": "environment_revisions", "localField": "default_active_revision_id", "foreignField": "_id", "as": "default_environment_revision_id"}},
		{"$project": {
			"workspace_name": "$workspace_name",
			"workspace_last_change": "$workspace_last_change",
			"project_name": "$project_name",
			"user_name": "$user_name",
			"environment_name": "$environment_name",
			"environment_docker_repo": "$environment_docker_repo",
			"environment_docker_tag": "$environment_docker_tag",
			"project_default_environment_name": "$project_default_environment_name",
			"project_default_environment_docker_repo": {"$first": "$default_environment_revision_id.metadata.dockerImageName.repository"},
			"project_default_environment_docker_tag": {"$first": "$default_environment_revision_id.metadata.dockerImageName.tag"}
		}}
	]


def run(target: str) -> None:
	mongo_uri = config_manager.get_mongo_connection_string()
	mongo_db = config_manager.get_mongo_db()
	output_dir = config_manager.get_output_dir()

	client = get_mongo_client()
	try:
		db = client[mongo_db]
		if target in ("model", "both"):
			logger.info("Running model environment usage aggregation...")
			model_results = list(db.models.aggregate(model_env_usage_pipeline()))
			save_json(os.path.join(output_dir, "model_env_usage_output.json"), bson_to_jsonable(model_results))
		if target in ("workspace", "both"):
			logger.info("Running workspace environment usage aggregation...")
			workspace_results = list(db.workspace.aggregate(workspace_env_usage_pipeline()))
			save_json(os.path.join(output_dir, "workspace_env_usage_output.json"), bson_to_jsonable(workspace_results))
	finally:
		client.close()


def main():
	setup_logging()
	parser = argparse.ArgumentParser(description='Extract metadata from MongoDB using PyMongo')
	parser.add_argument('--target', choices=['model', 'workspace', 'both'], default='both', help='Which aggregation(s) to run')
	args = parser.parse_args()
	run(args.target)


if __name__ == "__main__":
	main()
