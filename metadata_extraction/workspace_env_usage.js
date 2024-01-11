db.workspace.aggregate([
    {
        "$match": {
            "state": {"$ne": "Deleted"}
        }
    },
    {
        "$lookup": {
            "from": "users",
            "localField": "ownerId",
            "foreignField": "_id",
            "as": "user_id"
        }
    },
    {
        "$lookup": {
            "from": "projects",
            "localField": "projectId",
            "foreignField": "_id",
            "as": "project_id"
        }
    },
    {
        "$lookup": {
            "from": "workspace_session",
            "localField": "_id",
            "foreignField": "workspaceId",
            "as": "workspace_id"
        }
    },
    {
        "$lookup": {
            "from": "environments_v2",
            "localField": "configTemplate.environmentId",
            "foreignField": "_id",
            "as": "environment_id"
        }
    },
    {
        "$lookup": {
            "from": "environments_v2",
            "localField": "project_id.overrideV2EnvironmentId",
            "foreignField": "_id",
            "as": "default_project_environment_id"
        }
    },
    {
        "$addFields": {
            "running_environments": {
                "$filter": {
                    "input": "$workspace_id",
                    "as": "item",
                    "cond": {
                        "$eq": ["$$item.rawExecutionDisplayStatus", "Running"]
                    }
                }
            }
        }
    },
    {
        "$project": {
            "workspace_name": "$name",
            "workspace_last_change": "$stateUpdatedAt",
            "project_name": {
                "$first": "$project_id.name"
            },
            "user_name": {
                "$first": "$user_id.fullName"
            },
            "environment_name": {
                "$first": "$environment_id.name"
            },
            "project_default_environment_name": {
                "$first": "$default_project_environment_id.name"
            },
            "project_default_environment_id": {
                "$first": "$project_id.overrideV2EnvironmentId"
            },
            "project_active_revision_spec": {
                "$first": "$project_id.defaultEnvironmentRevisionSpec"
            },
            "user_active_revision_id": {
                "$switch": {
                    "branches": [
                        {
                            "case": {
                                "$gt": [
                                    {"$size": "$running_environments"},
                                    0
                                ]
                            },
                            "then": {
                                "$first": "$running_environments.environmentRevisionId"
                            }
                        },
                        { 
                            "case": {
                                "$eq": [
                                    "$configTemplate.environmentRevisionSpec",
                                    "ActiveRevision"
                                ]
                            },
                            "then": {
                                "$first": "$environment_id.activeRevisionId"
                            }
                        },
                        {
                            "case": {
                                "$eq": [
                                    true,
                                    {
                                        "$regexMatch": {
                                            "input": "$configTemplate.environmentRevisionSpec",
                                            "regex": "SomeRevision",
                                            "options": "i"
                                        }
                                    }
                                ]
                            },
                            "then": {
                                "$toObjectId": {
                                    $replaceOne: {
                                        "input": {
                                            "$replaceOne": {
                                                "input": "$configTemplate.environmentRevisionSpec",
                                                "find": "SomeRevision(",
                                                "replacement": ""
                                            }
                                        },
                                        "find": ")",
                                        "replacement": ""
                                    }
                                }
                            }
                        },
                    ],
                    "default": null
                }
            },
        }
    },
    {
        "$lookup": {
            "from": "environment_revisions",
            "localField": "user_active_revision_id",
            "foreignField": "_id",
            "as": "environment_revision_id"
        }
    },
    {
        "$lookup": {
            "from": "environments_v2",
            "localField": "project_default_environment_id",
            "foreignField": "_id",
            "as": "default_environment_active_revision"
        }
    },
    {
        "$project": {
            "workspace_name": "$workspace_name",
            "workspace_last_change": "$workspace_last_change",
            "project_name": "$project_name",
            "user_name": "$user_name",
            "environment_name": "$environment_name",
            "environment_docker_repo": {
                "$first": "$environment_revision_id.metadata.dockerImageName.repository"
            },
            "environment_docker_tag": {
                "$first": "$environment_revision_id.metadata.dockerImageName.tag"
            },
            "user_active_revision_id": "$user_active_revision_id",
            "project_default_environment_name": "$project_default_environment_name",
            "project_default_environment_id": "$project_default_environment_id",
            "project_default_active_revision_id": {
                "$first": "$default_environment_active_revision.activeRevisionId"
            },
            "project_active_revision_spec": "$project_active_revision_spec",
            "default_active_revision_id": {
                "$switch": {
                    "branches": [
                        { 
                            "case": {
                                "$eq": [
                                    "$project_active_revision_spec",
                                    "ActiveRevision"
                                ]
                            },
                            "then": {
                                "$first": "$default_environment_active_revision.activeRevisionId"
                            }
                        },
                        {
                            "case": {
                                "$eq": [
                                    true,
                                    {
                                        "$regexMatch": {
                                            "input": "$project_active_revision_spec",
                                            "regex": "SomeRevision",
                                            "options": "i"
                                        }
                                    }
                                ]
                            },
                            "then": {
                                "$toObjectId": {
                                    $replaceOne: {
                                        "input": {
                                            "$replaceOne": {
                                                "input": "$project_active_revision_spec",
                                                "find": "SomeRevision(",
                                                "replacement": ""
                                            }
                                        },
                                        "find": ")",
                                        "replacement": ""
                                    }
                                }
                            }
                        },
                    ],
                    "default": null
                }
            }
        }
    },
    {
        "$lookup": {
            "from": "environment_revisions",
            "localField": "default_active_revision_id",
            "foreignField": "_id",
            "as": "default_environment_revision_id"
        }
    },
    {
        "$project": {
            "workspace_name": "$workspace_name",
            "workspace_last_change": "$workspace_last_change",
            "project_name": "$project_name",
            "user_name": "$user_name",
            "environment_name": "$environment_name",
            "environment_docker_repo": "$environment_docker_repo",
            "environment_docker_tag": "$environment_docker_tag",
            "project_default_environment_name": "$project_default_environment_name",
            "project_default_environment_docker_repo": {
                "$first": "$default_environment_revision_id.metadata.dockerImageName.repository"
            }, 
            "project_default_environment_docker_tag": {
                "$first": "$default_environment_revision_id.metadata.dockerImageName.tag"
            }, 
        }
    }
]).pretty()