#!/usr/bin/env bash
# MAK-169: enable versioning + tiering lifecycle on the S3 backup bucket.
#
# Versioning gives us undelete-on-bucket-corruption (a misconfigured CLI or
# compromised IAM credential can't silently overwrite history). Lifecycle
# transitions the daily/weekly snapshots to Glacier IR at 30d and expires
# them at 1y, so retention cost stays bounded as the dataset grows.
#
# Idempotent: re-running re-applies the same versioning + lifecycle JSON
# (S3 PutBucketVersioning / PutBucketLifecycleConfiguration are upserts).
#
# Usage:
#   AWS credentials in env (AWS_ACCESS_KEY_ID/SECRET) or ~/.aws/credentials.
#   ./deploy/configure-backups-bucket.sh                # uses defaults
#   BUCKET=foo REGION=us-west-2 ./deploy/configure-backups-bucket.sh
set -euo pipefail

BUCKET="${BUCKET:-pingback-backups-prod}"
REGION="${REGION:-us-east-1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Bucket: $BUCKET ($REGION)"

# Sanity: bucket must exist + we must be able to read its config.
aws s3api head-bucket --bucket "$BUCKET" --region "$REGION"

echo "==> Enabling versioning"
aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --region "$REGION" \
  --versioning-configuration "file://$HERE/s3-versioning.json"

echo "==> Applying lifecycle (Standard 30d → Glacier IR → expire 365d)"
aws s3api put-bucket-lifecycle-configuration \
  --bucket "$BUCKET" \
  --region "$REGION" \
  --lifecycle-configuration "file://$HERE/s3-lifecycle.json"

echo "==> Verifying"
aws s3api get-bucket-versioning --bucket "$BUCKET" --region "$REGION"
aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION"

echo "==> Done."
