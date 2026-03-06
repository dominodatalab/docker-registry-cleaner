# find_environment_usage

Shows all the places a specific environment (and its revisions) is used across Domino.

## How It Works

Inspects both live MongoDB collections and pre-generated usage reports to find references to the given environment ID in:

- Runs (`runs` collection)
- Workspaces (`workspace` collection)
- Models / model versions (`models`, `model_versions`)
- Project defaults (`projects.overrideV2EnvironmentId`)
- Scheduled jobs (`scheduler_jobs.jobDataPlain.overrideEnvironmentId`)
- Organizations (`organizations.defaultV2EnvironmentId`)
- App versions (`app_versions.environmentId`)
- User preferences (`userPreferences.defaultEnvironmentId`)
- Environments that were cloned from this environment's revisions

## Usage

```bash
docker-registry-cleaner find_environment_usage --environment-id <objectId>
```

Example:

```bash
docker-registry-cleaner find_environment_usage --environment-id 6286a3c76d4fd0362f8ba3ec
```

## Options

| Option | Description |
|--------|-------------|
| `--environment-id ID` | The ObjectId of the environment to look up (required) |

## Notes

- This is a read-only command — it never modifies the registry or MongoDB.
- Useful before deleting an environment to verify it is truly unused.
