# main.py
import os
import json
import gzip
from io import BytesIO
from datetime import datetime, timedelta
from typing import List, Dict, Any

import boto3
import httpx
import asyncio
import asyncpg
from fastapi import FastAPI, Request, HTTPException

from sentence_transformers import SentenceTransformer

app = FastAPI(title="Alert Intelligence Agent")

# ---------- CONFIG (env) ----------
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://10.101.183.248:9000")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "vector-logs")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin123")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-service.monitoring.svc.cluster.local:9090")

PG_HOST = os.getenv("PG_HOST", "10.102.23.183")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASSWORD", "postgres123")
PG_DB = os.getenv("PG_DB", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))

# Vector filename prefix used by Vector config (update if different)
VECTOR_KEY_PREFIX_FMT = "k8s/logs/%Y/%m/%d/"  # same as your vector.toml

# ---------- MinIO client ----------
s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
)

# ---------- Embedding model (sentence-transformers) ----------
# all-MiniLM-L6-v2 -> 384-d embeddings (fast)
model = SentenceTransformer("all-MiniLM-L6-v2")

# ---------- DB pool ----------
DB_URL = f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}"
db_pool: asyncpg.pool.Pool | None = None

@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(dsn=DB_URL, min_size=1, max_size=5)

@app.on_event("shutdown")
async def shutdown():
    global db_pool
    if db_pool:
        await db_pool.close()

# ---------- Helpers: MinIO listing, download, parsing ----------
def list_objects_for_date(date: datetime) -> List[Dict[str, Any]]:
    """List objects in MinIO for the given date prefix."""
    prefix = date.strftime(VECTOR_KEY_PREFIX_FMT)
    paginator = s3.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=MINIO_BUCKET, Prefix=prefix)
    objs = []
    for page in page_iterator:
        for c in page.get("Contents", []):
            objs.append(c)
    # sort by LastModified descending (most recent first)
    objs.sort(key=lambda o: o.get("LastModified"), reverse=True)
    return objs

def download_and_decompress(key: str) -> str:
    """Download S3 object and decompress gzip content, return text."""
    resp = s3.get_object(Bucket=MINIO_BUCKET, Key=key)
    compressed = resp["Body"].read()
    buf = BytesIO(compressed)
    with gzip.GzipFile(fileobj=buf) as gf:
        data = gf.read().decode("utf-8", errors="replace")
    return data

def extract_matching_lines_from_text(text: str, pod_name: str = None, namespace: str = None, max_lines=500) -> List[str]:
    """
    Vector writes JSON lines. We'll parse lines as JSON where possible
    and check nested kubernetes.pod_name or message content for pod_name.
    Returns a list of matching raw lines (string).
    """
    matches = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        matched = False
        # try parse JSON
        try:
            j = json.loads(raw_line)
            # Vector pattern: j.get("kubernetes", {}).get("pod_name")
            k = j.get("kubernetes") or j.get("kubernetes", {})
            pod_in_line = None
            if isinstance(k, dict):
                pod_in_line = k.get("pod_name") or j.get("kubernetes", {}).get("pod_name")
            # fallback check message field
            msg = j.get("message") or j.get("msg") or ""
            if pod_name and pod_in_line == pod_name:
                matched = True
            elif pod_name and pod_name in (msg or ""):
                matched = True
            elif namespace and isinstance(k, dict) and k.get("pod_namespace") == namespace:
                matched = True
        except Exception:
            # not json: fallback to substring check
            if pod_name and pod_name in raw_line:
                matched = True
            elif namespace and namespace in raw_line:
                matched = True

        if matched:
            matches.append(raw_line)
            if len(matches) >= max_lines:
                break
    return matches

# ---------- Helper: fetch relevant logs for a pod ----------
def fetch_relevant_logs(namespace: str, pod_name: str, alert_time: datetime, lookback_minutes: int = 10, max_files: int = 6):
    """
    Find and return a concatenated log snippet (string) that best matches the pod_name.
    Strategy:
      - List objects for alert_time date (and also previous day to be safe)
      - Iterate newest objects and search inside content for pod_name match
      - Return concatenated matched lines (bounded by length) or fallback to tail of latest file
    """
    snippets: List[str] = []
    # consider alert day and previous day (edge cases near midnight)
    dates_to_check = [alert_time, alert_time - timedelta(days=1)]
    checked_files = 0

    for dt in dates_to_check:
        objs = list_objects_for_date(dt)
        for obj in objs:
            if checked_files >= max_files:
                break
            key = obj["Key"]
            checked_files += 1
            try:
                text = download_and_decompress(key)
            except Exception as e:
                # skip unreadable object
                continue
            matches = extract_matching_lines_from_text(text, pod_name=pod_name, namespace=namespace, max_lines=400)
            if matches:
                snippets.extend(matches)
            # if we have enough snippet content, stop early
            if len(snippets) >= 200:
                break
        if len(snippets) >= 200:
            break

    # Fallback: if no matches found, take tail of the latest file downloaded (up to N lines)
    if not snippets:
        # try latest file overall (use the most recent object from today list)
        today_objs = list_objects_for_date(alert_time)
        if today_objs:
            try:
                key = today_objs[0]["Key"]
                text = download_and_decompress(key)
                tail = "\n".join(text.splitlines()[-200:])  # last 200 lines
                snippets.append(tail)
            except Exception:
                pass

    # join snippets into a single text (bounded length)
    joined = "\n".join(snippets)[:200_000]  # 200KB cap
    return joined

# ---------- Helper: fetch metrics snapshot (simple example) ----------
async def fetch_metrics_for_instance(instance: str) -> Any:
    # Example: fetch last value of cpu usage or container restarts
    q = f'container_cpu_usage_seconds_total{{pod="{instance}"}}'  # change to your metric
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": q})
        r.raise_for_status()
        return r.json()

# ---------- Embedding helper (run CPU-bound encode in thread pool) ----------
async def embed_text_async(text: str) -> List[float]:
    loop = asyncio.get_event_loop()
    emb = await loop.run_in_executor(None, model.encode, text)
    return emb.tolist()

# ---------- Store to pgvector (asyncpg) ----------
async def store_alert_with_embedding(
    alert_name: str,
    severity: str,
    instance: str,
    namespace: str,
    description: str,
    logs_text: str,
    metrics_json: Any,
    embedding: List[float]
):
    if db_pool is None:
        raise RuntimeError("DB pool not initialized")
    # convert embedding to vector literal like '[0.123, 0.234, ...]'
    emb_text = "[" + ",".join(map(lambda x: f"{float(x):.6f}", embedding)) + "]"
    sql = """
    INSERT INTO alert_enriched
      (alert_name, severity, instance, namespace, description, logs, metrics, embedding)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector)
    """
    async with db_pool.acquire() as conn:
        await conn.execute(sql, alert_name, severity, instance, namespace, description, logs_text, json.dumps(metrics_json), emb_text)

# ---------- Endpoint ----------
@app.post("/alert")
async def receive_alert(request: Request):
    payload = await request.json()
    alerts = payload.get("alerts", [])
    if not alerts:
        raise HTTPException(status_code=400, detail="No alerts in payload")

    processed = []
    for a in alerts:
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})

        alertname = labels.get("alertname", "unknown")
        namespace = labels.get("namespace", "default")
        instance = labels.get("instance") or labels.get("pod") or ""
        severity = labels.get("severity", "unknown")
        description = annotations.get("description", "")

        # choose alert time (use startsAt if provided)
        startsAt = a.get("startsAt")
        if startsAt:
            try:
                alert_time = datetime.fromisoformat(startsAt.replace("Z", "+00:00"))
            except Exception:
                alert_time = datetime.utcnow()
        else:
            alert_time = datetime.utcnow()

        # Fetch logs relevant to pod
        logs_text = ""
        if instance:
            logs_text = fetch_relevant_logs(namespace=namespace, pod_name=instance, alert_time=alert_time)

        # Fetch metrics (async)
        metrics_json = {}
        try:
            metrics_json = await fetch_metrics_for_instance(instance) if instance else {}
        except Exception:
            metrics_json = {}

        # Build context text for embedding (bounded)
        context_text = f"Alert: {alertname}\nSeverity: {severity}\nNamespace: {namespace}\nInstance: {instance}\nDescription: {description}\n\nLogs:\n{logs_text[:150_000]}"

        # Create embedding (async)
        try:
            embedding = await embed_text_async(context_text)
        except Exception as e:
            embedding = []

        # Store record
        try:
            await store_alert_with_embedding(
                alert_name=alertname,
                severity=severity,
                instance=instance,
                namespace=namespace,
                description=description,
                logs_text=logs_text,
                metrics_json=metrics_json,
                embedding=embedding
            )
            processed.append({"alert": alertname, "instance": instance, "stored": True})
        except Exception as e:
            processed.append({"alert": alertname, "instance": instance, "stored": False, "error": str(e)})

    return {"received": len(processed), "details": processed}

