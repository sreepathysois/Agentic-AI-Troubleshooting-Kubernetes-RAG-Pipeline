from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import os

DB_USER = os.environ.get("PG_USER", "postgres")
DB_PASS = os.environ.get("PG_PASSWORD", "postgres123")
DB_HOST = os.environ.get("PG_HOST", "postgres-service.observability.svc.cluster.local")
DB_NAME = os.environ.get("PG_DB", "observability")

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASS}@{DB_HOST}:5432/{DB_NAME}"

engine = create_async_engine(DATABASE_URL, echo=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

