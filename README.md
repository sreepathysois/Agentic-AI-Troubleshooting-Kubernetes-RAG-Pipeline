# Agentic-AI-Troubleshooting-Kubernetes-RAG-Pipeline
Agentic-AI Troubleshooting Kubernetes RAG Pipeline  


Kubernetes Observability Stack Setup
Overview

We will deploy an observability stack for your cluster using Helm:

Prometheus → Metrics collection

Grafana → Dashboards + visualization

Loki → Centralized log storage

Promtail → Ship container logs to Loki

Persistent Loki storage → ensures logs survive pod restarts

Alerts → High CPU and Pod crash-looping

Prerequisites

Kubernetes cluster up and running (3 nodes in your case)

Helm installed (helm version)

kubectl configured

Step 1: Add Helm Repositories
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

Step 2: Create Monitoring Namespace
kubectl create namespace monitoring

Step 3: Install Prometheus Stack
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring


This installs Prometheus + Alertmanager + Grafana (with default dashboards)

Check pods:

kubectl get pods -n monitoring

Step 4: Create Persistent Storage for Loki
4a. Create PV and PVC
# loki-pv-pvc.yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: loki-pv
spec:
  storageClassName: loki-storage
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  hostPath:
    path: /mnt/data/loki   # Create this directory on all nodes
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: loki-pvc
  namespace: monitoring
spec:
  storageClassName: loki-storage
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi


Create directories and set permissions on all nodes:

sudo mkdir -p /mnt/data/loki
sudo chown -R 10001:10001 /mnt/data/loki   # Loki runs as non-root
sudo chmod 775 /mnt/data/loki


Apply PV and PVC:

kubectl apply -f loki-pv-pvc.yaml

Step 5: Install Loki (Persistent) and Promtail
helm install loki grafana/loki-stack \
  --namespace monitoring \
  --set grafana.enabled=false \
  --set prometheus.enabled=false \
  --set promtail.enabled=false \
  --set loki.persistence.enabled=true \
  --set loki.persistence.storageClassName=loki-storage \
  --set loki.persistence.size=10Gi \
  --set loki.persistence.existingClaim=loki-pvc  




  helm install promtail grafana/promtail
--namespace loki --create-namespace
--set "loki.serviceName=loki"
--set "loki.servicePort=3100"
--set "config.clients[0].url=http://loki:3100/loki/api/v1/push"

kubectl logs -n loki -l app.kubernetes.io/name=promtail

Promtail deployed as DaemonSet to tail pod logs 



#### Agenbt POST Requet to tes


curl -X POST http://localhost:8001/analyze \
  -H "Content-Type: application/json" \
  -d '{"alert_name":"PodImagePullError","instance":"pathy-7f884d8546-n69rs","namespace":"default","severity":"warning","timestamp":"2025-10-06T10:15:00Z"}'


curl -X POST http://localhost:8000/alert      -H "Content-Type: application/json"      -d @sample_alert.json

kubectl run badpod --image=nginx:notfound -n default
kubectl run crashpod --image=busybox --restart=Always -- sh -c "exit 1"


kubectl create secret generic openai-secret -n observability --from-literal=api-key="sk-your-real-api-key"  


### PG Vector Embedding

🧱 alert_enriched Table Schema (PostgreSQL + pgvector)
CREATE TABLE IF NOT EXISTS alert_enriched (
    id SERIAL PRIMARY KEY,
    alert_name TEXT,
    severity TEXT,
    instance TEXT,
    namespace TEXT,
    description TEXT,
    logs TEXT,
    metrics JSONB,
    embedding vector(1536),  -- <-- pgvector column
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);

✅ Notes:

The vector(1536) requires the pgvector extension to be enabled:

CREATE EXTENSION IF NOT EXISTS vector;


metrics is stored as a JSONB field so you can run structured queries like:

SELECT metrics->>'cpu_usage' AS cpu FROM alert_enriched;


logs stores the enriched text blob fetched from MinIO.

embedding holds the 1536-dimension float array from your LLM embedding.

🧠 Example queries

Insert (manual example):

INSERT INTO alert_enriched (
  alert_name, severity, instance, namespace, description, logs, metrics, embedding
)
VALUES (
  'PodImagePullError',
  'warning',
  'sree-5765c8475f-dqvwn',
  'default',
  'Pod failed to pull image',
  'Log snippet here...',
  '{"cpu": 0.75, "memory": 512}',
  '[0.1, 0.2, 0.3, ...]'::vector
);


Check latest alerts:

SELECT id, alert_name, instance, namespace, created_at
FROM alert_enriched
ORDER BY created_at DESC
LIMIT 10;


Vector similarity query (for RAG agent):

SELECT id, alert_name, instance, 1 - (embedding <=> '[0.1, 0.2, 0.3, ...]'::vector) AS similarity
FROM alert_enriched
ORDER BY embedding <=> '[0.1, 0.2, 0.3, ...]'::vector
LIMIT 3;



💡 Example output in your dashboard

When an alert triggers, you’ll now see something like:

🧩 Context prepared for LLM:
Alert: PodImagePullError
Severity: warning
Namespace: default
Pod: sree-5765c8475f-dqvwn
Description: Pod failed to pull image due to invalid tag.

Logs:
[INFO] Trying to pull image...
[ERROR] Failed to pull image: not found

Top-3 Similar Alerts:
1. CrashLoopBackOff in pod xyz (similarity 0.89)
2. ImagePullBackOff in pod abc (similarity 0.82)

Playbook Excerpt:
"Verify Docker credentials and image registry availability."
...
