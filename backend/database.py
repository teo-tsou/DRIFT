"""
Database Configuration and Session Management
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from typing import Generator
import os

from models import Base

# Database URL from environment variable
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://drift:drift@localhost:5432/drift"
)
# Name the driver explicitly: SQLAlchemy 2.1 made psycopg (v3) the default for a
# bare "postgresql://" URL, while this image installs psycopg2-binary.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgresql://"):]

# Create SQLAlchemy engine
engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,  # For async compatibility
    echo=os.getenv("SQL_ECHO", "false").lower() == "true"  # Log SQL queries in dev
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    Dependency function to get database session.
    Use with FastAPI Depends.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Initialize database - create all tables.
    Run this during application startup.
    """
    Base.metadata.create_all(bind=engine)


def drop_db():
    """
    Drop all tables - use with caution!
    """
    Base.metadata.drop_all(bind=engine)
