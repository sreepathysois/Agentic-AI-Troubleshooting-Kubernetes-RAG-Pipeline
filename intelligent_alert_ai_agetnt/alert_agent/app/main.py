import os
import json
import gzip
from io import BytesIO
from datetime import datetime
from typing import List, Dict, Any

import boto3
import httpx
import asyncio
import asyncpg
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sentence_transformers import SentenceTransformer
from pathlib import Path


# ------------------------------------------------------------------
# FastAPI App Setup
# ------------------------------------------------------------------
app = FastAPI(title="🧠 Alert Intelligence Agent")


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
#templates = Jinja2Templates(directory="templates")
#app.mount("/static", StaticFiles(directory="static"), name="static")

EVENT_LOG = []  # in-memory console log for /dashboard UI

def add_event(message: str):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    EVENT_LOG.insert(0, f"[{ts}] {message}")
    if len(EVENT_LOG) > 100:
        EVENT_LOG.pop()


# ------------------------------------------------------------------
# Configuration (from env or defaults)
# ------------------------------------------------------------------
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio.observability.svc.cluster.local:9000")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "vector-logs")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin123")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090")

PG_HOST = os.getenv("PG_HOST", "postgres-service.observability.svc.cluster.local")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASSWORD", "postgres123")
PG_DB = os.getenv("PG_DB", "postgres")
PG_PORT = int(os.getenv("PG_PORT", "5432"))

RAG_AGENT_URL = os.getenv(
    "RAG_AGENT_URL",
    "http://rag-agent.observability.svc.cluster.local:8001/analyze"
)

VECTOR_KEY_PREFIX_FMT = "k8s/logs/%Y/%m/%d/"

# ------------------------------------------------------------------
# Clients: MinIO, Model, PostgreSQL
# ------------------------------------------------------------------
s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
)

model = SentenceTransformer("all-MiniLM-L6-v2")
db_pool: asyncpg.pool.Pool | None = None
DB_URL = f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}"


@app.on_event("startup")
async def startup():
    global db_pool
    add_event("🚀 Starting Alert Intelligence Agent...")
    db_pool = await asyncpg.create_pool(dsn=DB_URL, min_size=1, max_size=5)
    add_event("✅ Connected to PostgreSQL.")


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()
        add_event("🔌 PostgreSQL connection closed.")


# ------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------
def list_objects_for_date(date: datetime) -> List[Dict[str, Any]]:
    prefix = date.strftime(VECTOR_KEY_PREFIX_FMT)
    paginator = s3.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=MINIO_BUCKET, Prefix=prefix)
    objs = []
    for page in page_iterator:
        for c in page.get("Contents", []):
            objs.append(c)
    objs.sort(key=lambda o: o.get("LastModified"), reverse=True)
    return objs


def download_and_decompress(key: str) -> str:
    resp = s3.get_object(Bucket=MINIO_BUCKET, Key=key)
    buf = BytesIO(resp["Body"].read())
    with gzip.GzipFile(fileobj=buf) as gf:
        return gf.read().decode("utf-8", errors="replace")


def extract_matching_lines(text: str, pod_name: str, namespace: str) -> List[str]:
    matches = []
    for line in text.splitlines():
        try:
            j = json.loads(line)
            pod = j.get("kubernetes", {}).get("pod_name")
            msg = j.get("message", "")
            if pod == pod_name or pod_name in msg:
                matches.append(line)
        except Exception:
            if pod_name in line:
                matches.append(line)
    return matches[:300]


def fetch_logs(namespace: str, pod_name: str, alert_time: datetime):
    add_event(f"📦 Searching logs for pod: {pod_name}")
    snippets = []
    objs = list_objects_for_date(alert_time)
    for obj in objs[:5]:  # last few log files
        try:
            text = download_and_decompress(obj["Key"])
            matches = extract_matching_lines(text, pod_name, namespace)
            if matches:
                snippets.extend(matches)
                break
        except Exception:
            continue
    if not snippets:
        add_event(f"⚠️ No logs found for {pod_name}")
    else:
        add_event(f"✅ Found {len(snippets)} log lines for {pod_name}")
    return "\n".join(snippets)


async def fetch_metrics(instance: str):
    q = f'container_cpu_usage_seconds_total{{pod="{instance}"}}'
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": q})
        if r.status_code == 200:
            add_event(f"📊 Metrics fetched for {instance}")
            return r.json()
        add_event(f"⚠️ Failed to fetch metrics for {instance}")
        return {}


async def embed_text_async(text: str) -> List[float]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, model.encode, text)


async def store_alert(alert_name, severity, instance, namespace, desc, logs, metrics, embedding):
    if db_pool is None:
        raise RuntimeError("DB not connected")

    emb_text = "[" + ",".join(map(lambda x: f"{float(x):.6f}", embedding)) + "]"
    sql = """
    INSERT INTO alert_enriched (alert_name, severity, instance, namespace, description, logs, metrics, embedding)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector)
    """
    async with db_pool.acquire() as conn:
        await conn.execute(sql, alert_name, severity, instance, namespace, desc, logs, json.dumps(metrics), emb_text)


# ------------------------------------------------------------------
# Trigger RAG Agent
# ------------------------------------------------------------------
async def trigger_rag_agent(alert_name, instance, namespace, severity):
    payload = {
        "alert_name": alert_name,
        "instance": instance,
        "namespace": namespace,
        "severity": severity,
        "timestamp": datetime.utcnow().isoformat()
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(RAG_AGENT_URL, json=payload)
            if r.status_code == 200:
                add_event(f"🤖 Triggered RAG agent successfully for {alert_name}.")
            else:
                add_event(f"⚠️ RAG agent responded with {r.status_code}: {r.text}")
    except Exception as e:
        add_event(f"❌ Failed to trigger RAG agent: {e}")


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Visual dashboard feed"""
    return templates.TemplateResponse("dashboard.html", {"request": request, "events": EVENT_LOG})


@app.post("/alert")
async def receive_alert(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    alerts = payload.get("alerts", [])
    if not alerts:
        raise HTTPException(status_code=400, detail="No alerts found.")

    for a in alerts:
        labels = a.get("labels", {})
        annotations = a.get("annotations", {})

        alert_name = labels.get("alertname", "unknown")
        namespace = labels.get("namespace", "default")
        instance = labels.get("instance") or labels.get("pod")
        severity = labels.get("severity", "unknown")
        description = annotations.get("description", "")

        add_event(f"🚨 Alert triggered: {alert_name} (Pod: {instance}) in {namespace}")

        alert_time = datetime.utcnow()
        logs_text = fetch_logs(namespace, instance, alert_time)
        metrics = await fetch_metrics(instance)

        context = f"Alert: {alert_name}\nSeverity: {severity}\nNamespace: {namespace}\nPod: {instance}\nDescription: {description}\n\nLogs:\n{logs_text[:10000]}"
        embedding = await embed_text_async(context)
        add_event(f"🧠 Generated embedding ({len(embedding)} dims)")

        try:
            await store_alert(alert_name, severity, instance, namespace, description, logs_text, metrics, embedding)
            add_event(f"💾 Inserted alert {alert_name} into pgvector successfully.")
        except Exception as e:
            add_event(f"❌ Failed to store alert: {e}")

        # Trigger next AI Agent (RAG Agent)
        background_tasks.add_task(trigger_rag_agent, alert_name, instance, namespace, severity)

    return {"status": "ok", "message": "Alerts processed successfully."}

