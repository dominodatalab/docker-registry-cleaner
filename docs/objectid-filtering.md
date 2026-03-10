# ObjectID Filtering

Some commands (`delete_image`, `delete_archived_tags`) support targeting specific environments or models by ObjectID using `--input`.

## File Format

Create a plain-text file with one ObjectID per line. Prefixes are required:

```
environment:6286a3c76d4fd0362f8ba3ec
environmentRevision:6286a3c76d4fd0362f8ba3ed
model:627d94043035a63be6140e93
modelVersion:627d94043035a63be6140e94
```

Lines starting with `#` are treated as comments and ignored.

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

# Use with delete_archived_tags
docker-registry-cleaner delete_archived_tags --environment --input reports/archived-tags.json --apply
```

## ObjectID Format

A valid MongoDB ObjectId is a 24-character hexadecimal string:

```
# Valid
62798b9bee0eb12322fc97e8
environment:62798b9bee0eb12322fc97e8

# Invalid (23 characters)
62798b9bee0eb12322fc97e
```
