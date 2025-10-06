import os
import httpx

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://10.97.176.83:9090")

async def query_prometheus(query: str):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
        resp.raise_for_status()
        return resp.json()

