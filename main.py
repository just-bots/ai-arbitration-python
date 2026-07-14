from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Load environment variables from .env file (must be before any other imports that read os.environ)
from dotenv import load_dotenv
load_dotenv()

# Import modular routers
from routers import initialization, transactions, prosecution, adjudication, objection
from routers.exceptions import register_exception_handlers

app = FastAPI(title="AI Arbitration Platform")

# Mount static files (CSS, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Connect the routers
app.include_router(initialization.router)
app.include_router(transactions.router)
app.include_router(prosecution.router)
app.include_router(adjudication.router)
app.include_router(objection.router)

# Register the global exception handler (mirrors Exceptions.json)
# Must be called AFTER app is created and BEFORE routes are added
register_exception_handlers(app)
