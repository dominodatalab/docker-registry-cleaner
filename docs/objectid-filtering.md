# ObjectID Filtering

`delete_image` supports restricting which images are processed to a specific set of Domino ObjectIDs using `--input`.

> **Note:** Other deletion commands (`delete_archived_tags`, `delete_unused_environments`, etc.) also accept an `--input` flag, but their format is different — they take a **pre-generated JSON report** from a previous dry-run, not a plain ObjectID list. See each command's documentation for details.

## File Format

Create a plain-text file with one ObjectID per line. Each line may optionally include a type prefix:

```
environment:6286a3c76d4fd0362f8ba3ec
environmentRevision:6286a3c76d4fd0362f8ba3ed
model:627d94043035a63be6140e93
modelVersion:627d94043035a63be6140e94
```

Lines starting with `#` are treated as comments and ignored. A type prefix is required on every ID — bare ObjectIDs without a prefix are rejected to avoid ambiguous matches across collections.

## Supported Prefixes

| Prefix | Targets |
|--------|---------|
| `environment:` | `environments_v2` documents and their revisions |
| `environmentRevision:` | Specific `environment_revisions` documents |
| `model:` | `models` documents and their versions |
| `modelVersion:` | Specific `model_versions` documents |

## Usage

```bash
# Create the file
cat > my-ids.txt <<EOF
environment:6286a3c76d4fd0362f8ba3ec
environmentRevision:6286a3c76d4fd0362f8ba3ed
EOF

# Use with delete_image
docker-registry-cleaner delete_image --input my-ids.txt --apply
```

When using the web UI, paste ObjectIDs directly into the **Input IDs** text box on the `delete_image` operation — one per line, with optional type prefixes.

## ObjectID Format

A valid MongoDB ObjectId is a 24-character hexadecimal string:

```
# Valid
62798b9bee0eb12322fc97e8
environment:62798b9bee0eb12322fc97e8

# Invalid (23 characters)
62798b9bee0eb12322fc97e
```
