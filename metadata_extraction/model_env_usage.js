db.models.aggregate([
    {
        "$match": {
            "isArchived": false
        }
    },
    {
        "$project": {
            "model_id": "$_id",
            "model_name": "$name",
            "model_created_id": "$metadata.createdBy",
            "model_last_change": "$metadata.lastModified",
            "model_environment_id": "$environmentId",
            "collaboratorSettings": {
                "$first": {
                    "$filter": {
                        "input": "$collaboratorSettings",
                        "as": "item",
                        "cond": {
                            "$eq": [
                            "$$item.role",
                            "Owner"
                            ]
                        }
                    }
                }
            }
        }
    },
    {
        "$lookup": {
            "from": "users",
            "localField": "collaboratorSettings.collaboratorId",
            "foreignField": "_id",
            "as": "user_id"
        }
    },
    {
        "$lookup": {
                "from": "users",
                "localField": "model_created_id",
                "foreignField": "_id",
                "as": "created_id"
        }
    },
    {
        "$lookup": {
            "from": "environments_v2",
            "localField": "model_environment_id",
            "foreignField": "_id",
            "as": "environment_id"
        }
    },
    {
        "$lookup": {
            "from": "model_versions",
            "localField": "_id",
            "foreignField": "modelId.value",
            "as": "model_versions_id",
        }
    },
    {
        "$unwind": "$model_versions_id" 
    },
    {
        "$lookup": {
            "from": "sagas",
            "localField": "model_versions_id._id",
            "foreignField": "parameters.modelVersionId",
            "as": "sagas_id",
        }
    },
    {
        "$unwind": "$sagas_id"
    },
    {
        "$sort": {
            "sagas_id.started": -1
        }
    },
    {
        "$match": {
            "sagas_id.sagaName": {
                "$in": ["ModelVersionDeployment", "StartModelVersion", "StopModelVersion"]
            },
            "sagas_id.state": {
                "$in": ["slug-build-succeeded", "succeeded"]
            },
            "sagas_id.isCompleted": {
                "$eq": true
            }
        }
    },
    {
        "$lookup": {
            "from": "environment_revisions",
            "localField": "model_versions_id.environmentRevisionId",
            "foreignField": "_id",
            "as": "environment_revisions_id",
        }
    },
    {
        "$project": {
            "model_id": "$model_id",
            "model_name": 1,
            "model_last_change": 1,
            "model_owner": {
                "$first": "$user_id.fullName"
            },
            "model_created_by": {
                "$first": "$created_id.fullName"
            },
            "environment_name": {
                "$first": "$environment_id.name"
            },
            "environment_id": "$model_environment_id",
            "model_version_id": "$model_versions_id._id",
            "model_environment_repository": {
                "$first": "$model_versions_id.metadata.builds.slug.image.repository"
            },
            "model_environment_tag": {
                "$first": "$model_versions_id.metadata.builds.slug.image.tag"
            },
            "base_environment_repository": {
                "$first": "$environment_revisions_id.metadata.dockerImageName.repository"
            },
            "base_environment_tag": {
                "$first": "$environment_revisions_id.metadata.dockerImageName.tag"
            },
            "environment_revision_id": "$model_versions_id.environmentRevisionId",
            "saga_name": "$sagas_id.sagaName",
        }
    },
    {
        "$group": {
            "_id": "$model_version_id",
            "saga_status": {
                "$push": "$saga_name"
            }, 
            "model_id": {
                "$first": "$model_id"
            },
            "model_name": {
                "$first": "$model_name"
            },
            "model_last_change": {
                "$first": "$model_last_change"
            },
            "model_owner": {
                "$first": "$model_owner"
            },
            "model_created_by": {
                "$first": "$model_created_by"
            },
            "environment_name": {
                "$first": "$environment_name"
            },
            "environment_id": {
                "$first": "$environment_id"
            },
            "environment_revision_id": {
                "$first": "$environment_revision_id"
            },
            "model_version_id": {
                "$first": "$model_version_id"
            },
            "model_environment_repository": {
                "$first": "$model_environment_repository"
            },
            "model_environment_tag": {
                "$first": "$model_environment_tag"
            },
            "base_environment_repository": {
                "$first": "$base_environment_repository"
            },
            "base_environment_tag": {
                "$first": "$base_environment_tag"
            },
        }
    },
    {
        "$match": {
            "saga_status.0": {"$eq": "StartModelVersion"}
        }
    },
    { 
        "$project": {
            "saga_status": 0,            
        }
    },
    {
        "$group": {
            "_id": "$model_id",
            "model_id": {
                "$first": "$model_id"
            },
            "model_name": {
                "$first": "$model_name"
            },
            "model_last_change": {
                "$first": "$model_last_change"
            },
            "model_owner": {
                "$first": "$model_owner"
            },
            "model_created_by": {
                "$first": "$model_created_by"
            },
            "environment_name": {
                "$first": "$environment_name"
            },
            "environment_id": {
                "$first": "$environment_id"
            },
            "model_active_versions": {
                "$push": {
                    "environment_revision_id": "$environment_revision_id",
                    "model_version_id": "$model_version_id",
                    "model_environment_repository": "$model_environment_repository",
                    "model_environment_tag": "$model_environment_tag",
                    "base_environment_repository": "$base_environment_repository",
                    "base_environment_tag": "$base_environment_tag"
                }
            }
        }
    }           
]).pretty()