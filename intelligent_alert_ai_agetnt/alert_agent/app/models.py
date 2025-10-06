from sqlalchemy import Column, Integer, Text, JSON, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.declarative import declarative_base
from pgvector.sqlalchemy import Vector

Base = declarative_base()

class AlertEnriched(Base):
    __tablename__ = "alert_enriched"

    id = Column(Integer, primary_key=True)
    alert_name = Column(Text, nullable=True)
    severity = Column(Text, nullable=True)
    instance = Column(Text, nullable=True)
    namespace = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    logs = Column(Text, nullable=True)
    metrics = Column(JSON, nullable=True)
    embedding = Column(Vector(384), nullable=True)  # <-- fixed VECTOR -> Vector
    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

