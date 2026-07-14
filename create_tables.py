import os
from sqlalchemy import create_engine
from models import Base

if __name__ == "__main__":
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@127.0.0.1:5433/arbitration")
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    print("Database schema created successfully.")
