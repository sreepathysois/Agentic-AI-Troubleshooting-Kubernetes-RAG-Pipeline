import os
import boto3
from io import BytesIO

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://10.101.183.248:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin123")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "vector-logs")

s3_client = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY
)

def fetch_logs(object_name: str) -> str:
    """Fetch log from MinIO"""
    response = s3_client.get_object(Bucket=MINIO_BUCKET, Key=object_name)
    data = response['Body'].read().decode("utf-8")
    return data

