import os
import json
import asyncio
import httpx
from datetime import datetime
from typing import List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from pgvector.sqlalchemy import Vector
#from pgvector.sqlalchemy import register_vector

import numpy as np
import openai
from sentence_transformers import SentenceTransformer
from typing import List
from sqlalchemy import text
from typing import List, Dict, Any

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
PG_HOST = os.getenv("PG_HOST", "postgres-service.observability.svc.cluster.local")
#PG_HOST = os.getenv("PG_HOST", "10.102.23.183")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "postgres")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres123")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "playbooks")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio.observability.svc.cluster.local:9000")

DB_URL = f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"

engine = create_engine(DB_URL)
#register_vector(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

hf_model = SentenceTransformer("all-MiniLM-L6-v2")

openai.api_key = OPENAI_API_KEY
#openai.api_key = "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"

# -------------------------------------------------------------------
# FastAPI app setup
# -------------------------------------------------------------------
app = FastAPI(title="RAG AI Agent", description="Retrieves related alerts and playbooks")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

events: List[str] = []


def add_event(message: str):
    """Append event messages for dashboard display."""
    ts = datetime.now().strftime("[%H:%M:%S]")
    msg = f"{ts} {message}"
    print(msg)
    events.append(msg)
    if len(events) > 200:
        events.pop(0)


# -------------------------------------------------------------------
# Utility functions
# -------------------------------------------------------------------
#def get_embedding(text: str) -> List[float]:
#    """Generate text embedding using OpenAI model."""
#    if not text:
#        return [0.0] * 384
#    try:
#        resp = openai.embeddings.create(model="text-embedding-3-small", input=text)
#        return resp.data[0].embedding
#    except Exception as e:
#        add_event(f"❌ Error generating embedding: {e}")
#        return [0.0] * 384

def get_embedding(text: str) -> List[float]:
    """Generate text embedding using local SentenceTransformer (MiniLM)."""
    if not text:
        return [0.0] * 384

    try:
        vector = hf_model.encode(text)
        if isinstance(vector, list):
            return vector
        return vector.tolist()
    except Exception as e:
        add_event(f"❌ Error generating embedding (HF model): {e}")
        return [0.0] * 384



def fetch_playbooks_from_minio() -> List[Dict[str, str]]:
    """Mock or actual fetch of playbooks from MinIO."""
    # For now we simulate with dummy playbooks
    playbooks = [
        {
            "key": "PodImagePullError",
            "content": "When a Pod fails to pull image, verify image name, credentials, and registry connectivity. Run `kubectl describe pod <name>`."
        },
        {
            "key": "CrashLoopBackOff",
            "content": "Pod restarting repeatedly: Check `kubectl logs` and validate readiness/liveness probes and configmaps."
        }
    ]
    add_event(f"📘 Loaded {len(playbooks)} playbooks.")
    return playbooks


def build_context(similar_rows: List[Dict[str, Any]], playbooks: List[Dict[str, str]], current_alert: Dict[str, Any]) -> str:
    """Build combined context for LLM from alert, past alerts, and playbooks."""

    def serialize(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        try:
            json.dumps(obj)
            return obj
        except TypeError:
            return str(obj)

    parts = []
    parts.append("CURRENT ALERT:")
    if current_alert:
        safe_alert = {k: serialize(v) for k, v in current_alert.items()}
        parts.append(json.dumps(safe_alert, indent=2))
    else:
        parts.append("N/A")

    parts.append("\nTOP-K SIMILAR PAST ALERTS:")
    if not similar_rows:
        parts.append("No similar past alerts found.")
    else:
        for r in similar_rows:
            brief = {
                "id": r.get("id"),
                "alert_name": r.get("alert_name"),
                "instance": r.get("instance"),
                "namespace": r.get("namespace"),
                "created_at": serialize(r.get("created_at")),
                "distance": float(r.get("distance") or 0.0),
            }
            logs = r.get("logs") or ""
            brief["logs_snippet"] = (logs[:500] + "...") if len(logs) > 500 else logs
            parts.append(json.dumps(brief, indent=2))

    parts.append("\nPLAYBOOKS / RUNBOOKS:")
    if not playbooks:
        parts.append("No playbooks found.")
    else:
        for p in playbooks:
            parts.append(f"=== {p['key']} ===\n{p['content'][:2000]}\n")

    return "\n\n".join(parts)


#async def query_similar_alerts(embedding: List[float], top_k: int = 3) -> List[Dict[str, Any]]:
#    """Query PGVector DB for similar alerts."""
#    try:
#        with engine.connect() as conn:
#            q = text("""
#                SELECT id, alert_name, instance, namespace, created_at, logs,
#                       1 - (embedding <=> (:embed)::vector) AS similarity,
#                       (embedding <=> (:embed)::vector) AS distance
#                FROM alert_enriched
#                ORDER BY embedding <=> (:embed)::vector
#                LIMIT :k
#            """)
            #q = text("""
            #    SELECT id, alert_name, instance, namespace, created_at, logs,
            #           1 - (embedding <=> :embed) AS similarity,
            #           (embedding <=> :embed) AS distance
            #    FROM alert_enriched
            #    ORDER BY embedding <=> :embed
            #    LIMIT :k
            #""")
#            res = conn.execute(q, {"embed": embedding, "k": top_k})
#            rows = [dict(r._mapping) for r in res]
#            add_event(f"🔍 Found {len(rows)} similar alerts.")
#            return rows
#    except Exception as e:
#        add_event(f"❌ DB similarity query failed: {e}")
#        return []



async def query_similar_alerts(embedding: List[float], top_k: int = 3) -> List[Dict[str, Any]]:
    """Query PGVector DB for similar alerts."""
    try:
        # Convert embedding to Postgres vector literal string
        embed_str = "[" + ",".join(map(str, embedding)) + "]"

        with engine.connect() as conn:
            q = text("""
                SELECT id, alert_name, instance, namespace, created_at, logs,
                       1 - (embedding <=> (:embed)::vector) AS similarity,
                       (embedding <=> (:embed)::vector) AS distance
                FROM alert_enriched
                ORDER BY embedding <=> (:embed)::vector
                LIMIT :k
            """)
            res = conn.execute(q, {"embed": embed_str, "k": top_k})
            rows = [dict(r._mapping) for r in res]
            add_event(f"🔍 Found {len(rows)} similar alerts.")
            return rows

    except Exception as e:
        add_event(f"❌ DB similarity query failed: {e}")
        return []


async def ask_llm(prompt: str) -> str:
    """Send context to LLM and get recommendation."""
    try:
        add_event("🤖 Querying LLM for root cause and fix suggestion...")
        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a Kubernetes troubleshooting assistant."},
                {"role": "user", "content": prompt},
            ],
        )
        reply = resp.choices[0].message.content
        add_event("✅ LLM response received.")
        return reply
    except Exception as e:
        add_event(f"❌ LLM call failed: {e}")
        return "LLM unavailable."


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "events": events})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "events": events})


@app.get("/events")
async def get_events():
    return JSONResponse({"events": events[-50:]})


@app.post("/analyze")
async def analyze(payload: Dict[str, Any]):
    """Receive alert payload and perform RAG analysis."""
    add_event("🚨 RAG analysis triggered.")
    add_event(f"Alert payload: {payload}")

    alert_name = payload.get("alert_name")
    instance = payload.get("instance")
    namespace = payload.get("namespace")
    severity = payload.get("severity")

    current_alert = {
        "alert_name": alert_name,
        "instance": instance,
        "namespace": namespace,
        "severity": severity,
        "timestamp": datetime.utcnow()
    }

    # Step 1: Create embedding
    embed = get_embedding(f"{alert_name} {instance} {namespace} {severity}")

    # Step 2: Query similar past alerts
    similar_rows = await query_similar_alerts(embed)

    # Step 3: Load playbooks / runbooks
    playbooks = fetch_playbooks_from_minio()

    # Step 4: Build context and query LLM
    context = build_context(similar_rows, playbooks, current_alert)
    #add_event("🧠 Context built for LLM:")
    #add_event(context[:1000] + "..." if len(context) > 1000 else context)

    # Display context (formatted and limited)
    context_preview = context if len(context) < 5000 else context[:5000] + "\n\n...[truncated]..."

    print("\n========================= 🧠 CONTEXT BUILT =========================")
    print(context_preview)
    print("==================================================================\n")

    add_event("🧩 Context prepared for LLM:")
    add_event("<pre style='font-size:12px;white-space:pre-wrap;max-height:400px;overflow:auto;'>" +
           context_preview + "</pre>")

    llm_reply = await ask_llm(context)

    add_event("💡 Suggested Fix / Explanation:")
    add_event(llm_reply[:500])  # display summary in dashboard

    return {"status": "ok", "suggestion": llm_reply}

@app.post("/clear")
async def clear_events():
    global events
    events = []
    return {"status": "cleared", "message": "Dashboard events reset"}


# -------------------------------------------------------------------
# Start server
# -------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)

