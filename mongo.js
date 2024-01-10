// connect to domino database
use domino;

db.environments_v2.aggregate([
  {
    // Match environments that are archived
    $match: {
      isArchived: true
    }
  },
  {

    $lookup: {
      from: "environment_revisions",
      localField: "_id",
      foreignField: "environmentId",
      as: "revisions"
    }
  },
  { $unwind: "$revisions" },
  {
    $match: {
      "revisions.metadata.dockerImageName": { $exists: true, $ne: {} }
    }
  },
  {
    $project: {
      "authorId": "$revisions.metadata.authorId",
      "created": "$revisions.metadata.created",
      "repository": "$revisions.metadata.dockerImageName.repository",
      "tag": "$revisions.metadata.dockerImageName.tag",
      "_id": "$revisions._id"
    }
  }
]);
