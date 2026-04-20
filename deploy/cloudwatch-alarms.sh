#!/usr/bin/env bash
# CloudWatch alarms for Pingback (MAK-62).
#
# Idempotent. Creates the SNS topic + 5 alarms. Depends on the metric filters
# published by deploy/cloudwatch-setup.sh (MAK-60) and on an EC2 instance to
# page on (AWS/EC2 + CWAgent metrics).
#
# Budget: 10 CloudWatch alarms are included in the AWS free tier. This script
# creates 5. Do not duplicate without updating docs/OPERATIONS.md.
#
# Required:
#   PINGBACK_INSTANCE_ID   — EC2 instance id (i-xxxx). The CPU / status /
#                            disk alarms are scoped to this instance.
#   ALERT_EMAILS           — comma-separated list of email addresses to
#                            subscribe to the SNS topic. Each subscriber
#                            must click the confirmation link AWS emails
#                            them before alarms actually deliver.
#
# Optional:
#   AWS_REGION                         (default us-east-1)
#   PINGBACK_ALARM_TOPIC               (default pingback-alarms)
#   PINGBACK_METRIC_NAMESPACE          (default Pingback/Logs)
#   PINGBACK_CWAGENT_NAMESPACE         (default CWAgent)
#   PINGBACK_DISK_DEVICE               (default xvda1)
#   PINGBACK_DISK_FSTYPE               (default xfs)
#   PINGBACK_DISK_PATH                 (default /)
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
TOPIC_NAME="${PINGBACK_ALARM_TOPIC:-pingback-alarms}"
LOG_NS="${PINGBACK_METRIC_NAMESPACE:-Pingback/Logs}"
CWA_NS="${PINGBACK_CWAGENT_NAMESPACE:-CWAgent}"
DISK_DEVICE="${PINGBACK_DISK_DEVICE:-xvda1}"
DISK_FSTYPE="${PINGBACK_DISK_FSTYPE:-xfs}"
DISK_PATH="${PINGBACK_DISK_PATH:-/}"

: "${PINGBACK_INSTANCE_ID:?set PINGBACK_INSTANCE_ID to the EC2 instance id (i-xxxx)}"
: "${ALERT_EMAILS:?set ALERT_EMAILS to a comma-separated list of destinations}"

aws_sns()   { aws sns --region "$REGION" "$@"; }
aws_cw()    { aws cloudwatch --region "$REGION" "$@"; }

echo "=== CloudWatch alarms: region=$REGION topic=$TOPIC_NAME instance=$PINGBACK_INSTANCE_ID ==="

# 1. SNS topic (create-topic is idempotent — returns the existing ARN).
TOPIC_ARN=$(aws_sns create-topic --name "$TOPIC_NAME" --query TopicArn --output text)
echo ">>> topic: $TOPIC_ARN"

# 2. Subscribe every requested email. list-subscriptions-by-topic lets us
#    dedupe so re-running does not blast AWS with duplicate confirmations.
existing_subs=$(aws_sns list-subscriptions-by-topic \
    --topic-arn "$TOPIC_ARN" \
    --query "Subscriptions[?Protocol=='email'].Endpoint" \
    --output text)

IFS=',' read -r -a EMAILS <<<"$ALERT_EMAILS"
for raw in "${EMAILS[@]}"; do
    email="$(echo "$raw" | xargs)"      # trim
    [ -z "$email" ] && continue
    if echo "$existing_subs" | tr '\t' '\n' | grep -qxF "$email"; then
        echo ">>> subscription already present: $email"
    else
        aws_sns subscribe \
            --topic-arn "$TOPIC_ARN" \
            --protocol email \
            --notification-endpoint "$email" >/dev/null
        echo ">>> subscribed (pending confirmation): $email"
    fi
done

# 3. Alarms. put-metric-alarm is a full upsert — safe to re-run.

echo ">>> alarm: Pingback/ErrorRateHigh"
aws_cw put-metric-alarm \
    --alarm-name "Pingback/ErrorRateHigh" \
    --alarm-description "ErrorCount > 5 in a 5-minute window. See docs/OPERATIONS.md (MAK-62)." \
    --namespace "$LOG_NS" \
    --metric-name ErrorCount \
    --statistic Sum \
    --period 300 \
    --evaluation-periods 1 \
    --datapoints-to-alarm 1 \
    --threshold 5 \
    --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC_ARN" \
    --ok-actions "$TOPIC_ARN"

echo ">>> alarm: Pingback/SchedulerFailure"
aws_cw put-metric-alarm \
    --alarm-name "Pingback/SchedulerFailure" \
    --alarm-description "One or more scheduler ERROR log lines in 5 min. (MAK-62)" \
    --namespace "$LOG_NS" \
    --metric-name SchedulerFailureCount \
    --statistic Sum \
    --period 300 \
    --evaluation-periods 1 \
    --datapoints-to-alarm 1 \
    --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC_ARN" \
    --ok-actions "$TOPIC_ARN"

echo ">>> alarm: Pingback/HealthCheckMissing (EC2 StatusCheckFailed)"
# We're not behind an ALB (MAK-54 single-instance deploy). StatusCheckFailed
# is the free EC2-built-in replacement for ALB reachability checks;
# UptimeRobot remains the canonical external up/down signal.
aws_cw put-metric-alarm \
    --alarm-name "Pingback/HealthCheckMissing" \
    --alarm-description "EC2 status checks failing — host is unreachable or unhealthy. External confirmation via UptimeRobot. (MAK-62)" \
    --namespace AWS/EC2 \
    --metric-name StatusCheckFailed \
    --dimensions "Name=InstanceId,Value=$PINGBACK_INSTANCE_ID" \
    --statistic Maximum \
    --period 60 \
    --evaluation-periods 3 \
    --datapoints-to-alarm 2 \
    --threshold 1 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data breaching \
    --alarm-actions "$TOPIC_ARN" \
    --ok-actions "$TOPIC_ARN"

echo ">>> alarm: Pingback/DiskSpaceLow (CWAgent disk_used_percent)"
# Requires the CloudWatch agent to be running with a disk metric config that
# publishes disk_used_percent under CWAgent with device/fstype/path dimensions.
# See docs/OPERATIONS.md for the agent config snippet.
aws_cw put-metric-alarm \
    --alarm-name "Pingback/DiskSpaceLow" \
    --alarm-description "Root fs used >80% (free <20%). Requires CloudWatch agent. (MAK-62)" \
    --namespace "$CWA_NS" \
    --metric-name disk_used_percent \
    --dimensions \
        "Name=InstanceId,Value=$PINGBACK_INSTANCE_ID" \
        "Name=device,Value=$DISK_DEVICE" \
        "Name=fstype,Value=$DISK_FSTYPE" \
        "Name=path,Value=$DISK_PATH" \
    --statistic Average \
    --period 300 \
    --evaluation-periods 1 \
    --datapoints-to-alarm 1 \
    --threshold 80 \
    --comparison-operator GreaterThanThreshold \
    --treat-missing-data missing \
    --alarm-actions "$TOPIC_ARN" \
    --ok-actions "$TOPIC_ARN"

echo ">>> alarm: Pingback/CpuHigh"
aws_cw put-metric-alarm \
    --alarm-name "Pingback/CpuHigh" \
    --alarm-description "CPU >80% sustained for 10 min. (MAK-62)" \
    --namespace AWS/EC2 \
    --metric-name CPUUtilization \
    --dimensions "Name=InstanceId,Value=$PINGBACK_INSTANCE_ID" \
    --statistic Average \
    --period 300 \
    --evaluation-periods 2 \
    --datapoints-to-alarm 2 \
    --threshold 80 \
    --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC_ARN" \
    --ok-actions "$TOPIC_ARN"

echo "=== alarms upserted. Verify state with: ==="
echo "    aws cloudwatch describe-alarms --region $REGION \\"
echo "      --alarm-name-prefix Pingback/ --query 'MetricAlarms[].[AlarmName,StateValue]' --output table"
