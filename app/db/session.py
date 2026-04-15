import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

# 1. Look for Render's live database URL. If it doesn't exist, use your local settings.
DB_URL = os.getenv("DATABASE_URL", settings.DATABASE_URL)

# 2. Safety fix: Cloud providers sometimes use 'postgres://', but SQLAlchemy requires 'postgresql://'
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)