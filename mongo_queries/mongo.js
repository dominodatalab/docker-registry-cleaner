db.environments_v2.aggregate([
  {
    // Match environments that are archived
    $match: {
      isArchived: true
    }
  },
  {
    // Lookup to join with environment revisions
    $lookup: {
      from: "environment_revisions",
      localField: "_id",
      foreignField: "environmentId",
      as: "revisions"
    }
  },
  { 
    $unwind: "$revisions" 
  },
  {
    $match: {
      "revisions.metadata.dockerImageName": { $exists: true, $ne: {} }
    }
  },
  {
    // Lookup to join with projects
    $lookup: {
      from: "projects",
      localField: "_id",
      foreignField: "overrideV2EnvironmentId",
      as: "usingProjects"
    }
  },
  {
    // Add field to handle unused environments
    $addFields: {
      projectName: {
        $ifNull: [{
          $arrayElemAt: ["$usingProjects.name", 0]
        }, "unused in project"]
      }
    }
  },
  {
    $project: {
      "authorId": "$revisions.metadata.authorId",
      "created": "$revisions.metadata.created",
      "repository": "$revisions.metadata.dockerImageName.repository",
      "tag": "$revisions.metadata.dockerImageName.tag",
      "_id": "$revisions._id",
      "projectName": 1 // Include project name in the projection
    }
  }
]).pretty()
