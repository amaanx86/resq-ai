#!/usr/bin/env python3
"""
Test & Inspection script for ResQ-AI running against Floci (AWS Local Cloud Emulator)
Verifies:
1. Floci Healthcheck
2. S3 Bucket Existence & File Storage
3. DynamoDB Table Schema, GSIs, and Record Counts
4. SQS inbound queue + DLQ depth, and per-API-task portal event queues
5. SNS portal-events topic and its subscriptions
"""

import os
import sys
import boto3
from botocore.exceptions import ClientError

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "test")
AWS_SECRET = os.getenv("AWS_SECRET_ACCESS_KEY", "test")

print("=" * 65)
print("  ResQ-AI Local AWS Emulator Inspection (Floci on port 4566)")
print("=" * 65)
print(f"Target Endpoint: {ENDPOINT_URL}")
print(f"AWS Region:      {REGION}")
print("-" * 65)

# 1. Inspect S3
try:
    s3 = boto3.client(
        's3',
        endpoint_url=ENDPOINT_URL,
        region_name=REGION,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET
    )
    buckets = s3.list_buckets().get("Buckets", [])
    bucket_names = [b["Name"] for b in buckets]
    print(f"[OK] S3 Connected! Active Buckets ({len(bucket_names)}): {bucket_names}")
    
    if "resq-raw-audio-vault" in bucket_names:
        objects = s3.list_objects_v2(Bucket="resq-raw-audio-vault").get("Contents", [])
        print(f"     Audio Vault contains {len(objects)} recorded files:")
        for obj in objects[:5]:
            print(f"     - {obj['Key']} ({obj['Size']} bytes)")
except Exception as e:
    print(f"[FAIL] S3 Error: {e}")

print("-" * 65)

# 2. Inspect DynamoDB
try:
    ddb = boto3.client(
        'dynamodb',
        endpoint_url=ENDPOINT_URL,
        region_name=REGION,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET
    )
    tables = ddb.list_tables().get("TableNames", [])
    print(f"[OK] DynamoDB Connected! Active Tables ({len(tables)}): {tables}")

    if "ResQTable" in tables:
        desc = ddb.describe_table(TableName="ResQTable")["Table"]
        gsis = [g["IndexName"] for g in desc.get("GlobalSecondaryIndexes", [])]
        print(f"     Table Status: {desc['TableStatus']}")
        print(f"     Primary Keys: {[k['AttributeName'] for k in desc['KeySchema']]}")
        print(f"     GSIs:         {gsis}")

        # Scan counts by entity
        ddb_resource = boto3.resource(
            'dynamodb',
            endpoint_url=ENDPOINT_URL,
            region_name=REGION,
            aws_access_key_id=AWS_KEY,
            aws_secret_access_key=AWS_SECRET
        )
        table = ddb_resource.Table("ResQTable")
        scan_res = table.scan()
        items = scan_res.get("Items", [])
        print(f"     Total Items in Table: {len(items)}")

        doctors = [i for i in items if i.get("PK", "").startswith("DOCTOR#")]
        cases = [i for i in items if i.get("PK", "").startswith("CASE#") and i.get("SK") == "METADATA"]
        users = [i for i in items if i.get("PK", "").startswith("USER#")]
        messages = [i for i in items if i.get("SK", "").startswith("MSG#")]
        idem = [i for i in items if i.get("PK", "").startswith("IDEMPOTENCY#")]

        print(f"     - Doctors registered: {len(doctors)}")
        print(f"     - Emergency cases:    {len(cases)}")
        print(f"     - Patients profile:   {len(users)}")
        print(f"     - Chat bridge msgs:   {len(messages)}")
        print(f"     - Idempotency keys:   {len(idem)} "
              f"({sum(1 for i in idem if i.get('status') == 'COMPLETE')} complete)")

        if cases:
            print("\n  Recent Emergency Cases in Floci DynamoDB:")
            for c in cases[:3]:
                data = c.get("data", {})
                print(f"     [Case {data.get('case_id')}] ESI-{data.get('esi_level')} {data.get('matched_specialty')} | Lang: {data.get('detected_language')} | Doctor: {data.get('assigned_doctor_name')}")

except Exception as e:
    print(f"[FAIL] DynamoDB Error: {e}")

print("-" * 65)

# 3. Inspect SQS (inbound webhook queue, DLQ, per-API-task event queues)
WEBHOOK_QUEUE = os.getenv("WEBHOOK_QUEUE_NAME", "resq-webhook-queue")
WEBHOOK_DLQ = os.getenv("WEBHOOK_DLQ_NAME", "resq-webhook-dlq")
try:
    sqs = boto3.client(
        'sqs',
        endpoint_url=ENDPOINT_URL,
        region_name=REGION,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET
    )
    urls = sqs.list_queues().get("QueueUrls", [])
    print(f"[OK] SQS Connected! Queues ({len(urls)}):")
    for url in urls:
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "RedrivePolicy"]
        ).get("Attributes", {})
        name = url.rsplit("/", 1)[-1]
        role = ("inbound" if name == WEBHOOK_QUEUE else "DLQ" if name == WEBHOOK_DLQ
                else "API task events" if name.startswith("resq-api-events-") else "other")
        print(f"     - {name} [{role}] visible={attrs.get('ApproximateNumberOfMessages')} "
              f"in-flight={attrs.get('ApproximateNumberOfMessagesNotVisible')}"
              f"{' redrive=' + attrs['RedrivePolicy'] if attrs.get('RedrivePolicy') else ''}")
    if not any(u.endswith("/" + WEBHOOK_QUEUE) for u in urls):
        print(f"     (inbound queue '{WEBHOOK_QUEUE}' not created yet: start the backend or worker)")
except Exception as e:
    print(f"[FAIL] SQS Error: {e}")

print("-" * 65)

# 4. Inspect SNS (portal events fan-out topic)
try:
    sns = boto3.client(
        'sns',
        endpoint_url=ENDPOINT_URL,
        region_name=REGION,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET
    )
    topics = [t["TopicArn"] for t in sns.list_topics().get("Topics", [])]
    print(f"[OK] SNS Connected! Topics ({len(topics)}):")
    for arn in topics:
        subs = sns.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", [])
        print(f"     - {arn} ({len(subs)} subscriptions)")
        for sub in subs:
            print(f"         {sub.get('Protocol')} -> {sub.get('Endpoint')}")
except Exception as e:
    print(f"[FAIL] SNS Error: {e}")

print("=" * 65)
