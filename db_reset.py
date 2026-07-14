import os
from sqlalchemy import create_engine
from models import Base
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("No DATABASE_URL set. Exiting.")
    exit(1)

print(f"Connecting to {DATABASE_URL}...")
engine = create_engine(DATABASE_URL)

print("Dropping all tables...")
Base.metadata.drop_all(bind=engine)

print("Creating all tables...")
Base.metadata.create_all(bind=engine)

print("Database schema successfully recreated!")
