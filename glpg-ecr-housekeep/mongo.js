// connect to domino database
use domino

// find archived envs output imagename and tag
db.environment_revisions.find({
  environmentId: {
    $in: db.environments_v2.find({
      isArchived: true
    }).map(function(env) { return env._id })
  },
  'metadata.dockerImageName': { $exists: true, $ne: {} }
}, {
  'metadata.dockerImageName.repository': 1,
  'metadata.dockerImageName.tag': 1,
  _id: 1
})


// find archived envs
//db.environments_v2.find({isArchived: true}).forEach(function(env) {
//  var revisions = db.environment_revisions.find({ environmentId: env._id });
//  print("Environment '" + env.name + "' has " + revisions.count() + " revisions:");
//  revisions.forEach(function(rev) {
//    var user = db.users.findOne({_id: rev.metadata.authorId});
//    if (user !== null) {
//      print("  Revision " + rev.metadata.number + " was created by user " + user.fullName);
//    } else {
//      print("  Revision " + rev.metadata.number + " was created by an unknown user");
//    }
//  });
//});
