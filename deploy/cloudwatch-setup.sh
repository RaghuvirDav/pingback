#!/usr/bin/env bash
# CloudWatch Logs setup for Pingback (MAK-60).
#
# Idempotent — safe to re-run on every deploy. Configures:
#   1. Log group `pingback` with 14-day retention (free-tier guardrail).
#   2. Metric filters publishing custom metrics to `Pingback/Logs`:
#        - ErrorCount              (level=ERROR)
#        - SchedulerFailureCount   (level=ERROR && logger=pingback.scheduler)
#
# Requires: awscli v2 and credentials with logs:* on the pingback log group
# (the EC2 instance role should carry a minimal inline policy).
set -euo pipefail

LOG_GROUP="${PINGBACK_LOG_GROUP:-pingback}"
REGION="${AWS_REGION:-us-east-1}"
RETENTION_DAYS="${PINGBACK_LOG_RETENTION_DAYS:-14}"
METRIC_NAMESPACE="${PINGBACK_METRIC_NAMESPACE:-Pingback/Logs}"

aws_logs() { aws logs --region "$REGION" "$@"; }

echo "=== CloudWatch setup: group=$LOG_GROUP region=$REGION retention=${RETENTION_DAYS}d ==="

# 1. Create the log group if missing.
if ! aws_logs describe-log-groups \
        --log-group-name-prefix "$LOG_GROUP" \
        --query "logGroups[?logGroupName=='$LOG_GROUP'] | [0].logGroupName" \
        --output text | grep -qx "$LOG_GROUP"; then
    echo ">>> creating log group"
    aws_logs create-log-group --log-group-name "$LOG_GROUP"
fi

# 2. Enforce retention. CEO called 14 days non-negotiable — reset on every run.
echo ">>> setting retention to $RETENTION_DAYS days"
aws_logs put-retention-policy \
    --log-group-name "$LOG_GROUP" \
    --retention-in-days "$RETENTION_DAYS"

# 3. Metric filters. `put-metric-filter` is a full replace — re-running is safe.
echo ">>> metric filter: ErrorCount"
aws_logs put-metric-filter \
    --log-group-name "$LOG_GROUP" \
    --filter-name ErrorCount \
    --filter-pattern '{ $.level = "ERROR" }' \
    --metric-transformations \
        "metricName=ErrorCount,metricNamespace=$METRIC_NAMESPACE,metricValue=1,defaultValue=0,unit=Count"

echo ">>> metric filter: SchedulerFailureCount"
aws_logs put-metric-filter \
    --log-group-name "$LOG_GROUP" \
    --filter-name SchedulerFailureCount \
    --filter-pattern '{ $.level = "ERROR" && $.logger = "pingback.scheduler" }' \
    --metric-transformations \
        "metricName=SchedulerFailureCount,metricNamespace=$METRIC_NAMESPACE,metricValue=1,defaultValue=0,unit=Count"

# 4. Saved Logs Insights queries. `put-query-definition` upserts by name.
save_query() {
    local name="$1"; local body="$2"
    local existing
    existing=$(aws_logs describe-query-definitions \
        --query-definition-name-prefix "$name" \
        --query "queryDefinitions[?name=='$name'] | [0].queryDefinitionId" \
        --output text)
    if [ "$existing" = "None" ] || [ -z "$existing" ]; then
        aws_logs put-query-definition \
            --name "$name" \
            --log-group-names "$LOG_GROUP" \
            --query-string "$body" >/dev/null
    else
        aws_logs put-query-definition \
            --query-definition-id "$existing" \
            --name "$name" \
            --log-group-names "$LOG_GROUP" \
            --query-string "$body" >/dev/null
    fi
    echo ">>> saved query: $name"
}

save_query "Pingback/errors-last-hour" \
'fields @timestamp, level, logger, message, request_id, path, status
| filter level = "ERROR"
| sort @timestamp desc
| limit 200'

save_query "Pingback/5xx-by-path" \
'fields @timestamp, path, status, request_id, duration_ms
| filter status >= 500
| stats count() as count by path
| sort count desc'

save_query "Pingback/scheduler-failures" \
'fields @timestamp, message, request_id
| filter logger = "pingback.scheduler" and level = "ERROR"
| sort @timestamp desc
| limit 200'

# 5. Verify.
actual_retention=$(aws_logs describe-log-groups \
    --log-group-name-prefix "$LOG_GROUP" \
    --query "logGroups[?logGroupName=='$LOG_GROUP'] | [0].retentionInDays" \
    --output text)

if [ "$actual_retention" != "$RETENTION_DAYS" ]; then
    echo "!!! retention mismatch: expected=$RETENTION_DAYS got=$actual_retention" >&2
    exit 1
fi

echo "=== CloudWatch setup OK — retention=${actual_retention}d, metric filters in $METRIC_NAMESPACE ==="
