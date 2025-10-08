# app/main.py
import os
import json
import shlex
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from kubernetes import client, config
import boto3
from sqlalchemy import create_engine, text
from botocore.exceptions import ClientError

# ----------------------------
# Configuration (via env)
# ----------------------------
PG_HOST = os.getenv("PG_HOST", "postgres-service.observability.svc.cluster.local")
#PG_HOST = os.getenv("PG_HOST", "10.102.23.183")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "postgres")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres123")


POSTGRES_URL = f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"



#POSTGRES_URL = os.getenv("POSTGRES_URL")  # e.g. postgresql://user:pass@host:5432/db
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio.observability.svc.cluster.local:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin123")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "remediation-logs")

ALLOWED_OPS = set(os.getenv("ALLOWED_OPS", "describe,get,delete,logs,restart").split(","))
ALLOWED_KINDS = set(os.getenv("ALLOWED_KINDS", "pod,deployment,configmap,hpa").split(","))
ALLOWED_NAMESPACES = set(os.getenv("ALLOWED_NAMESPACES", "default,observability,monitoring").split(","))

# ----------------------------
# Init clients
# ----------------------------
if not POSTGRES_URL:
    raise RuntimeError("POSTGRES_URL env var is required")

engine = create_engine(POSTGRES_URL, pool_pre_ping=True)

# MinIO (S3) client
s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    region_name="us-east-1",
)

# Kubernetes client
try:
    config.load_incluster_config()
except Exception:
    config.load_kube_config()
k8s_core = client.CoreV1Api()
k8s_apps = client.AppsV1Api()

app = FastAPI(title="Remediation Agent")

# ----------------------------
# Models
# ----------------------------
class ExecuteRequest(BaseModel):
    request_id: Optional[str] = None
    alert_id: Optional[str] = None
    alert_labels: Dict[str, Any]
    llm_suggestion: str
    approval_required: Optional[bool] = False
    source: Optional[str] = None

# ----------------------------
# Helpers
# ----------------------------
def ensure_bucket(bucket: str):
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        # try to create
        try:
            s3.create_bucket(Bucket=bucket)
        except Exception as e:
            # Some MinIO setups require no region or special handling; ignore if already exists
            raise

def minio_put_json(bucket: str, key: str, obj: dict):
    ensure_bucket(bucket)
    body = json.dumps(obj, default=str, indent=2)
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))
    return f"s3://{bucket}/{key}"


def insert_audit_row(payload: dict, parsed_commands: list, minio_path: str, status: str):
    req_id = payload.get("request_id")
    alert_id = payload.get("alert_id")
    labels = payload.get("alert_labels", {})
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO remediation_audit
                    (request_id, alert_id, alert_name, namespace, pod, severity, llm_suggestion, parsed_commands, overall_status, minio_path, created_at)
                    VALUES (:rid, :aid, :an, :ns, :pod, :sev, :llm, CAST(:pc AS JSONB), :status, :mp, now())
                """),
                {
                    "rid": req_id,
                    "aid": alert_id,
                    "an": labels.get("alertname"),
                    "ns": labels.get("namespace"),
                    "pod": labels.get("pod"),
                    "sev": labels.get("severity"),
                    "llm": payload.get("llm_suggestion"),
                    "pc": json.dumps(parsed_commands),
                    "status": status,
                    "mp": minio_path
                }
            )
    except Exception as e:
        print("❌ Failed to insert audit row:", e)



def parse_commands_from_text(text: str) -> List[Dict[str, Any]]:
    """
    Extract structured kubectl commands from the LLM suggestion text.
    Handles numbered lists (1), bullets (-), and plural resource names.
    """
    import re
    extracted = []

    # Normalize lines
    lines = text.splitlines()
    for ln in lines:
        ln = ln.strip()
        # Remove prefixes like "1)", "2.", "-", "•"
        ln = re.sub(r"^(\d+[\)\.\s-]*)", "", ln)
        if ln.startswith("kubectl"):
            parsed = _parse_kubectl_line(ln)
            if parsed:
                extracted.append({"orig": ln, **parsed})
    return extracted


def _parse_kubectl_line(line: str) -> Optional[Dict[str, Any]]:
    """Improved parser for kubectl commands with plural handling."""
    try:
        parts = shlex.split(line)
    except Exception:
        parts = line.split()

    if not parts or parts[0] != "kubectl":
        return None

    i = 1
    if len(parts) <= i:
        return None

    cmd = parts[i]
    ns = _find_namespace(parts)

    # Handle rollout restart
    if cmd == "rollout" and len(parts) > i + 2 and parts[i + 1] == "restart":
        kind = parts[i + 2].rstrip('s')  # normalize plural
        name = parts[i + 3] if len(parts) > i + 3 else None
        return {"op": "restart", "kind": kind, "name": name, "namespace": ns or "default"}

    # Standard CRUD: describe/get/delete
    if cmd in ("describe", "get", "delete"):
        kind = parts[i + 1].rstrip('s') if len(parts) > i + 1 else None
        name = parts[i + 2] if len(parts) > i + 2 else None
        return {"op": cmd, "kind": kind, "name": name, "namespace": ns or "default"}

    # Logs
    if cmd == "logs":
        name = parts[i + 1] if len(parts) > i + 1 else None
        return {"op": "logs", "kind": "pod", "name": name, "namespace": ns or "default"}

    return None





def _find_namespace(parts: List[str]) -> Optional[str]:
    for idx, p in enumerate(parts):
        if p in ("-n", "--namespace") and len(parts) > idx+1:
            return parts[idx+1]
    return None

def validate_parsed_command(cmd: Dict[str, Any]) -> (bool, str):
    """Return (ok, reason)."""
    op = (cmd.get("op") or "").lower()
    kind = (cmd.get("kind") or "").lower()
    ns = cmd.get("namespace") or "default"
    name = cmd.get("name")
    if op not in ALLOWED_OPS:
        return False, f"op '{op}' not allowed"
    if kind not in ALLOWED_KINDS:
        return False, f"kind '{kind}' not allowed"
    if ns not in ALLOWED_NAMESPACES:
        return False, f"namespace '{ns}' not allowed"
    if not name:
        return False, "resource name missing"
    return True, "ok"

# ----------------------------
# Execution functions (safe)
# ----------------------------
def exec_command(cmd: Dict[str, Any]) -> Dict[str, Any]:
    """Map parsed command to k8s API calls and return result dict."""
    op = (cmd.get("op") or "").lower()
    kind = (cmd.get("kind") or "").lower()
    name = cmd.get("name")
    ns = cmd.get("namespace") or "default"

    res = {"op": op, "kind": kind, "name": name, "namespace": ns, "success": False, "summary": "", "output": None}
    try:
        if op in ("describe", "get"):
            if kind == "pod":
                pod = k8s_core.read_namespaced_pod(name=name, namespace=ns)
                res["success"] = True
                res["summary"] = f"fetched pod {name}"
                res["output"] = pod.to_dict()
            elif kind == "deployment":
                dep = k8s_apps.read_namespaced_deployment(name=name, namespace=ns)
                res["success"] = True
                res["summary"] = f"fetched deployment {name}"
                res["output"] = dep.to_dict()
            else:
                raise Exception("unsupported describe/get kind")
        elif op == "delete":
            if kind == "pod":
                resp = k8s_core.delete_namespaced_pod(name=name, namespace=ns, body=client.V1DeleteOptions())
                res["success"] = True
                res["summary"] = f"deleted pod {name}"
                res["output"] = ({ "status": "deleted", "raw": str(resp.to_dict()) })
            else:
                raise Exception("delete unsupported for this kind")
        elif op == "restart":
            # patch template annotation to trigger rollout
            if kind == "deployment":
                patch = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "remediation.restartedAt": datetime.utcnow().isoformat()
                                }
                            }
                        }
                    }
                }
                resp = k8s_apps.patch_namespaced_deployment(name=name, namespace=ns, body=patch)
                res["success"] = True
                res["summary"] = f"patched deployment {name} (restart)"
                res["output"] = resp.to_dict()
            else:
                raise Exception("restart only supported for deployment")
        elif op == "logs":
            # limit logs to last N lines
            lines = k8s_core.read_namespaced_pod_log(name=name, namespace=ns, tail_lines=100, _return_http_data_only=True)
            res["success"] = True
            res["summary"] = f"fetched logs (tail 100) for {name}"
            res["output"] = {"logs_tail": lines}
        else:
            raise Exception("operation not implemented")
    except Exception as e:
        res["success"] = False
        res["summary"] = f"error: {str(e)}"
        res["output"] = {"err": str(e), "trace": traceback.format_exc()[:4000]}
    return res

# ----------------------------
# Background processing
# ----------------------------
def process_request(payload: dict):
    """Main background worker to parse, validate, execute, store results in minio & postgres."""
    req_id = payload.get("request_id") or f"req-{int(datetime.utcnow().timestamp())}"
    alert_id = payload.get("alert_id") or req_id
    labels = payload.get("alert_labels", {})
    llm = payload.get("llm_suggestion", "")

    # parse
    parsed = parse_commands_from_text(llm)
    command_results = []

    if not parsed:
        overall_status = "no_commands_found"
    else:
        overall_status = "running"
        # validate & execute
        for p in parsed:
            ok, reason = validate_parsed_command(p)
            if not ok:
                command_results.append({
                    "orig": p.get("orig"),
                    "parsed": p,
                    "status": "rejected",
                    "reason": reason
                })
                continue
            # execute
            r = exec_command(p)
            command_results.append({
                "orig": p.get("orig"),
                "parsed": p,
                "status": "success" if r["success"] else "failed",
                "summary": r.get("summary"),
                "output": r.get("output")
            })

        # determine overall
        if any(cr["status"] == "failed" for cr in command_results):
            overall_status = "partial_failed"
        else:
            overall_status = "completed"

    # build final JSON to store in minio
    final_obj = {
        "request_id": req_id,
        "alert_id": alert_id,
        "alert_labels": labels,
        "llm_suggestion": llm,
        "parsed_commands": command_results,
        "overall_status": overall_status,
        "executed_at": datetime.utcnow().isoformat()
    }

    # upload to minio
    key = f"{alert_id}.json"
    try:
        minio_path = minio_put_json(MINIO_BUCKET, key, final_obj)
    except Exception as e:
        minio_path = None
        print("MinIO upload failed:", e)

    # save in Postgres
    try:
        insert_audit_row(payload, command_results, minio_path or "", overall_status)
    except Exception as e:
        print("DB insert failed:", e)

    print(f"[remediation] finished request={req_id} status={overall_status} minio={minio_path}")

# ----------------------------
# Endpoint
# ----------------------------
@app.post("/execute")
async def execute_endpoint(req: ExecuteRequest, background: BackgroundTasks):
    payload = req.dict()
    # quick sanity checks
    if not payload.get("alert_labels") or not payload.get("llm_suggestion"):
        raise HTTPException(status_code=400, detail="alert_labels and llm_suggestion required")
    # run in background
    background.add_task(process_request, payload)
    return {"status": "accepted", "request_id": payload.get("request_id") or f"req-{int(datetime.utcnow().timestamp())}"}

@app.get("/health")
def health():
    return {"status": "ok"}

