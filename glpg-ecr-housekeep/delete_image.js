// connect to domino database
use domino

// find archived envs output imagename and tag
db.environment_revisions.deleteOne({
  environmentId: ObjectId("$1"),
  'metadata.dockerImageName.repository': "$2",
  'metadata.dockerImageName.tag': "$3"
})