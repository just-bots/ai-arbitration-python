# AI Arbitration Platform with Python

This repository contains the Python backend migration of the AI Arbitration Platform, transitioning from an n8n + Google Sheets architecture to a pure Python stack backed by PostgreSQL.

## Architecture

The system uses a highly normalized relational database to track arbitration cases. All schema generation is defined using **SQLAlchemy** to interface natively with the LangChain logic used by the AI Magistrate. 

### Database Schema
The database tracks three primary entities:
- **`cases`**: The ledger of arbitration instances, tracking escrow states, wallet addresses, and final AI decisions.
- **`messages`**: Log of interactions linked to a specific case, stamped by Sender (Admin, System, Seller, Buyer).
- **`files`**: Google Drive files attached to a case, strictly linked to the specific `messages` entry that originally submitted them via a Foreign Key constraint. 

## Local Setup

To streamline local development without installing PostgreSQL directly onto your machine, this project includes a Docker environment configuration. 

### Prerequisites
- [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/) (Make sure it is running!)
- Python 3.9+

### 1. Start the Database

Open a terminal in the root of this project and run the following command to spin up the local PostgreSQL container:

```bash
docker-compose up -d
```

*(Note: The database container explicitly binds to port **`5433`** instead of `5432` to avoid conflicting with any pre-existing PostgreSQL instances on your local machine).*

### 2. Install Dependencies

Install the database ORM and driver dependencies:

```bash
pip install -r requirements.txt
```

### 3. Initialize the Schema

Once the container is running and packages are installed, execute the initialization script. This uses SQLAlchemy to automatically build out all tables, enums, and foreign key constraints on the database.

```bash
python init_db.py
```

If successful, you will see `Database schema created successfully.` 

### 4. Connect to the Database (Visual Inspection)

You can connect any standard database visualization tool (like DBeaver, PGAdmin, or VS Code's Database Client) using the following credentials:

- **Host:** `127.0.0.1` (or `localhost`)
- **Port:** `5433`
- **Database:** `arbitration`
- **Username:** `postgres`
- **Password:** `password`
