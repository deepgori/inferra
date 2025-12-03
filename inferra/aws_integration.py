"""
aws_integration.py — AWS Integration for Report Storage

Provides S3 upload capability for analysis reports and
CloudWatch log ingestion for real production telemetry.

Usage:
    # Upload report to S3
    from inferra.aws_integration import upload_report_to_s3
    url = upload_report_to_s3("reports/flask_report.html", "my-bucket")

    # Ingest CloudWatch logs
    from inferra.aws_integration import fetch_cloudwatch_logs
    events = fetch_cloudwatch_logs("/aws/lambda/my-function", hours=1)

Environment variables:
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_DEFAULT_REGION (default: us-east-1)
    INFERRA_S3_BUCKET (optional, default bucket name)
"""

import os
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional


def _get_s3_client():
    """Get a boto3 S3 client."""
    try:
        import boto3
        return boto3.client(
            "s3",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    except Exception as e:
        print(f"   ⚠️  AWS S3 unavailable: {e}")
        return None


def _get_logs_client():
    """Get a boto3 CloudWatch Logs client."""
    try:
        import boto3
        return boto3.client(
            "logs",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
    except Exception as e:
        print(f"   ⚠️  AWS CloudWatch unavailable: {e}")
        return None


def upload_report_to_s3(
    local_path: str,
    bucket: Optional[str] = None,
    key_prefix: str = "inferra-reports",
) -> Optional[str]:
    """
    Upload an analysis report to S3.

    Args:
        local_path: Local path to the report file
        bucket: S3 bucket name (defaults to INFERRA_S3_BUCKET env var)
        key_prefix: S3 key prefix for organizing reports

    Returns:
        The S3 URL of the uploaded report, or None on failure
    """
    s3 = _get_s3_client()
    if not s3:
        return None

    bucket = bucket or os.environ.get("INFERRA_S3_BUCKET")
    if not bucket:
        print("   ⚠️  No S3 bucket specified. Set INFERRA_S3_BUCKET env var or pass --s3-bucket")
        return None

    # Build S3 key: inferra-reports/2026-03-06/flask_report.html
    filename = os.path.basename(local_path)
    date_prefix = datetime.now().strftime("%Y-%m-%d")
    s3_key = f"{key_prefix}/{date_prefix}/{filename}"

    # Determine content type
    content_type = "text/html"
    if local_path.endswith(".json"):
        content_type = "application/json"
    elif local_path.endswith(".md"):
        content_type = "text/markdown"

    try:
        s3.upload_file(
            local_path,
            bucket,
            s3_key,
            ExtraArgs={"ContentType": content_type},
        )
        url = f"s3://{bucket}/{s3_key}"
        print(f"   ☁️  Uploaded to S3: {url}")
        return url
    except Exception as e:
        print(f"   ⚠️  S3 upload failed: {e}")
        return None


def list_s3_reports(
    bucket: Optional[str] = None,
    key_prefix: str = "inferra-reports",
) -> List[Dict]:
    """List all reports stored in S3."""
    s3 = _get_s3_client()
    if not s3:
        return []

    bucket = bucket or os.environ.get("INFERRA_S3_BUCKET")
    if not bucket:
        return []

    try:
        response = s3.list_objects_v2(Bucket=bucket, Prefix=key_prefix)
        reports = []
        for obj in response.get("Contents", []):
            reports.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
                "url": f"s3://{bucket}/{obj['Key']}",
            })
        return reports
    except Exception as e:
        print(f"   ⚠️  Failed to list S3 reports: {e}")
        return []


def fetch_cloudwatch_logs(
    log_group: str,
    hours: int = 1,
    filter_pattern: Optional[str] = None,
    limit: int = 100,
) -> List[Dict]:
    """
    Fetch logs from AWS CloudWatch for analysis.

    Args:
        log_group: CloudWatch log group name (e.g. /aws/lambda/my-func)
        hours: How many hours back to search
        filter_pattern: CloudWatch filter pattern (e.g. "ERROR")
        limit: Maximum number of log events to return

    Returns:
        List of log event dicts with timestamp, message, logStream
    """
    client = _get_logs_client()
    if not client:
        return []

    start_time = int((datetime.utcnow() - timedelta(hours=hours)).timestamp() * 1000)
    end_time = int(datetime.utcnow().timestamp() * 1000)

    try:
        kwargs = {
            "logGroupName": log_group,
            "startTime": start_time,
            "endTime": end_time,
            "limit": limit,
            "interleaved": True,
        }
        if filter_pattern:
            kwargs["filterPattern"] = filter_pattern

        response = client.filter_log_events(**kwargs)

        events = []
        for event in response.get("events", []):
            events.append({
                "timestamp": event["timestamp"],
                "message": event["message"].strip(),
                "log_stream": event["logStreamName"],
                "ingestion_time": event.get("ingestionTime"),
            })

        print(f"   ☁️  Fetched {len(events)} log events from CloudWatch: {log_group}")
        return events

    except client.exceptions.ResourceNotFoundException:
        print(f"   ⚠️  Log group not found: {log_group}")
        return []
    except Exception as e:
        print(f"   ⚠️  CloudWatch fetch failed: {e}")
        return []


def cloudwatch_events_to_trace(events: List[Dict]) -> List[Dict]:
    """
    Convert CloudWatch log events into a format compatible with
    the RCA engine for analysis.
    """
    trace_events = []
    for event in events:
        msg = event["message"]

        # Detect errors in log messages
        is_error = any(
            kw in msg.lower()
            for kw in ["error", "exception", "traceback", "failed", "fatal"]
        )

        trace_events.append({
            "source": "cloudwatch",
            "log_group": event.get("log_stream", "unknown"),
            "timestamp": event["timestamp"],
            "message": msg,
            "is_error": is_error,
            "level": "ERROR" if is_error else "INFO",
        })

    return trace_events
