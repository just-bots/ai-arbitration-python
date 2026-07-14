# AI Arbitration Platform — Python Edition

> A full Python/FastAPI port of the [n8n AI Arbitration Platform](https://github.com/just-bots/ai-arbitration-n8n).  
> Automated contract arbitration backed by PostgreSQL, LangChain AI agents, and Ethereum escrow.

---

## Table of Contents

1. [What This Is](#what-this-is)
2. [How It Works — The Full Lifecycle](#how-it-works--the-full-lifecycle)
3. [Architecture](#architecture)
4. [Prerequisites](#prerequisites)
5. [Installation & Setup](#installation--setup)
6. [Running the Server](#running-the-server)
7. [Using the Platform — Step by Step](#using-the-platform--step-by-step)
   - [Phase 1 — Case Creation](#phase-1--case-creation)
   - [Phase 2 — Contract Acceptance & Wallet Registration](#phase-2--contract-acceptance--wallet-registration)
   - [Phase 3 — Escrow Deposit](#phase-3--escrow-deposit)
   - [Phase 4 — Payment or Refund Request](#phase-4--payment-or-refund-request)
   - [Phase 5 — Dispute & Evidence](#phase-5--dispute--evidence)
   - [Phase 6 — AI Adjudication](#phase-6--ai-adjudication)
   - [Phase 7 — Objection & Award Distribution](#phase-7--objection--award-distribution)
8. [API Reference](#api-reference)
9. [Environment Variables](#environment-variables)
10. [Status Model](#status-model)
11. [Financial Model](#financial-model)
12. [Project Structure](#project-structure)
13. [Production Deployment Notes](#production-deployment-notes)

---

## What This Is

The AI Arbitration Platform is a smart-contract-style legal arbitration system. Two parties — a **Seller** (provider) and a **Buyer** (payer) — enter a binding contract. The Buyer deposits ETH into escrow. If they cannot resolve a dispute themselves, a two-stage AI system (Magistrate Judge + Final Judge) investigates the case and issues a legally binding award. Funds are distributed automatically on-chain after a 7-day objection window.

This repository is a **Python/FastAPI** implementation that mirrors the logic of the original n8n workflows exactly, replacing Google Sheets with PostgreSQL and Google Drive with local file storage.

---

## How It Works — The Full Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ARBITRATION LIFECYCLE                        │
├──────────┬──────────────────────────────────────────────────────────┤
│ Phase 1  │ Contract submitted → Case ID generated → Emails sent     │
│ Phase 2  │ Both parties accept contract + register ETH wallets       │
│ Phase 3  │ Buyer deposits escrow to on-chain wallet → Verified       │
│ Phase 4  │ Seller requests payment OR Buyer requests refund          │
│ Phase 5  │ Opposing party disputes → Evidence window opens (7 days)  │
│ Phase 6  │ AI Magistrate investigates → AI Final Judge rules         │
│ Phase 7  │ 7-day objection window → Award distributed on-chain       │
└──────────┴──────────────────────────────────────────────────────────┘
```

Each phase maps to one of the five workflow modules:

| Module | Phase | n8n Equivalent |
|--------|-------|----------------|
| `initialization.py` | 1–2 | `Initialization.json` |
| `transactions.py` | 3–4 | `Transactions.json` |
| `prosecution.py` | 4–5 | `Prosecution.json` |
| `adjudication.py` | 6 | `Adjudication.json` |
| `objection.py` | 7 | `Objection.json` |
| `exceptions.py` | Global | `Exceptions.json` |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     FastAPI Application                  │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │  Routes  │  │ Templates│  │  Static  │             │
│  │ (6 mods) │  │ (Jinja2) │  │  (CSS)   │             │
│  └────┬─────┘  └──────────┘  └──────────┘             │
│       │                                                 │
│  ┌────▼──────────────────────────────────────────┐     │
│  │           SQLAlchemy ORM (models.py)           │     │
│  └────┬──────────────────────────────────────────┘     │
│       │                                                 │
└───────┼─────────────────────────────────────────────────┘
        │
┌───────▼──────────┐    ┌──────────────────────────────┐
│   PostgreSQL DB   │    │    External Services (mock)   │
│  (Docker :5433)  │    │  Etherscan · Tatum · OpenAI  │
└──────────────────┘    └──────────────────────────────┘
```

**Key design choices:**
- **PostgreSQL** via Docker replaces Google Sheets as the case ledger
- **Local `uploads/`** replaces Google Drive for file uploads
- **LangChain + OpenAI / Gemini / DeepSeek** powers the two-stage AI adjudication, complete with `with_fallbacks()` logic for multi-provider resilience.
- **Agentic Evidence Tooling**: The AI Magistrate runs as a LangChain ReAct agent equipped with a `read_evidence_file` tool to directly parse PDF/TXT evidence.
- **Gmail IMAP Ingestion**: A built-in IMAP client automatically polls for email replies, extracts Case IDs, hashes attachments, and saves them to the case ledger.
- **APScheduler**: Manages automated cron jobs for timeouts, evidence window expirations, and Gmail polling.
- **Blockchain verified**: Actual Etherscan API validation replaces mock deposits.
- **Email is sent via SMTP**: Powered by `email_service.py` using standard SMTP credentials.

---

## Prerequisites

Before you start, make sure you have:

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.9+ | Runtime |
| Docker Desktop | Latest | PostgreSQL container |
| Git | Any | Cloning the repo |
| OpenAI API Key | — | AI adjudication (Phase 6 only) |

> **Docker Desktop must be running** before you start the database.

---

## Installation & Setup

### Step 1 — Clone the repository

```bash
git clone https://github.com/just-bots/ai-arbitration-python.git
cd ai-arbitration-python
```

### Step 2 — Create your environment file

```bash
cp .env.example .env
```

Open `.env` and fill in your values. At minimum, set `OPENAI_API_KEY` for AI adjudication to work. Everything else has safe defaults for local development.

```env
# Minimum required for local dev:
OPENAI_API_KEY=sk-...your-key-here...

# Optional — defaults are fine for local testing:
BASE_URL=http://localhost:8000/
ADMIN_EMAIL=admin@example.com
ESCROW_WALLET=0x0000000000000000000000000000000000000000
PROCESSING_FEE=1000000000000000        # 0.001 ETH in Wei
DATABASE_URL=postgresql://postgres:password@localhost:5433/arbitration
```

See [Environment Variables](#environment-variables) for the full list.

### Step 3 — Start the database

```bash
docker-compose up -d
```

This starts a PostgreSQL 15 container named `arbitration-db` on port **5433**.  
To verify it's running:

```bash
docker ps
# You should see: arbitration-db   postgres:15   Up ...   0.0.0.0:5433->5432/tcp
```

### Step 4 — Install Python dependencies

```bash
pip install -r requirements.txt
```

> If you're on macOS and get a `psycopg2` error, try: `pip install psycopg2-binary`

### Step 5 — Initialize the database schema

```bash
python create_tables.py
```

Expected output:
```
Database schema created successfully.
```

This creates three tables: `cases`, `messages`, and `files`.

### Step 6 — (Optional) Inspect the database

Connect with any SQL client using:

| Setting | Value |
|---------|-------|
| Host | `127.0.0.1` |
| Port | `5433` |
| Database | `arbitration` |
| Username | `postgres` |
| Password | `password` |

Recommended clients: [TablePlus](https://tableplus.com/), [DBeaver](https://dbeaver.io/), [pgAdmin](https://www.pgadmin.org/)

---

## Running the Server

```bash
python -m uvicorn main:app --reload --port 8000
```

The server is now live at **http://localhost:8000**

| URL | Description |
|-----|-------------|
| http://localhost:8000/ | Case creation form (start here) |
| http://localhost:8000/docs | Auto-generated Swagger API docs |
| http://localhost:8000/redoc | ReDoc API docs |

The `--reload` flag makes the server automatically restart when you edit any Python file.

---

## Using the Platform — Step by Step

> **Important:** In local development, emails are not actually sent.  
> All email links are printed to the **terminal/console** where the server is running.  
> Watch the terminal output — every link you need for the next step will appear there.

---

### Phase 1 — Case Creation

**Who does this:** The platform operator or one of the parties

1. Open **http://localhost:8000/**
2. Fill in the registration form:
   - **Seller name + email** — the service provider (e.g., contractor, freelancer, landlord)
   - **Buyer name + email** — the payer (e.g., client, employer, renter)
   - **Seller ETH wallet** — Ethereum address for receiving awards
   - **Buyer ETH wallet** — Ethereum address for receiving refunds
   - **Required Escrow Fund (ETH)** — the amount the Buyer must deposit (e.g., `0.5`)
   - **Contract file** — upload the PDF of the contract
3. Click **Submit**

**What happens:**
- A unique `Case ID` is generated (e.g., `CASE-3F7A2B1C`)
- Party tokens are created for secure link authentication
- The case is saved to the database with `status = PENDING`
- Escrow fund is stored in Wei (`ETH × 10¹⁸`)
- Two simulated emails are printed to the terminal:
  ```
  --- EMAIL SIMULATION ---
  To: seller@example.com
  Subject: Action Required: Contract Registered (CASE-3F7A2B1C)
  Link: http://localhost:8000/response?caseId=CASE-3F7A2B1C&party=Seller&action=accept&token=...
  Wallet: http://localhost:8000/wallet?caseId=CASE-3F7A2B1C&party=Seller&token=...

  To: buyer@example.com
  Subject: Action Required: Contract Registered (CASE-3F7A2B1C)
  Link: http://localhost:8000/response?caseId=CASE-3F7A2B1C&party=Buyer&action=accept&token=...
  ```

---

### Phase 2 — Contract Acceptance & Wallet Registration

**Who does this:** Both Seller and Buyer independently

#### 2a — Accept the contract

Each party uses their unique link from the terminal:

```
http://localhost:8000/response?caseId=CASE-...&party=Seller&action=accept&token=...
http://localhost:8000/response?caseId=CASE-...&party=Buyer&action=accept&token=...
```

To **decline**, change `action=accept` to `action=decline` in the URL.

**What happens:**
- The party's response is recorded in the database
- When **both** parties accept → `status = SIGNED`
- If either party declines → `status = DECLINED`

#### 2b — Register a wallet (if not provided at creation)

Parties can submit or update their Ethereum wallet address:

```
http://localhost:8000/wallet?caseId=CASE-...&party=Seller&token=...
http://localhost:8000/wallet?caseId=CASE-...&party=Buyer&token=...
```

The address is validated against the Ethereum format (`0x` + 40 hex characters).

---

### Phase 3 — Escrow Deposit

**Who does this:** The Buyer

In production, the Buyer sends ETH to the `ESCROW_WALLET` address with the Case ID encoded in the transaction data field. In local development, this is mocked.

**To simulate a deposit locally:**

```
http://localhost:8000/transactions/verify?caseId=CASE-...
```

**What happens:**
- The system checks if the deposited amount meets `Escrow Fund + Processing Fee`
- If sufficient: `deposited_fund` is recorded, `status` stays `SIGNED`
- The terminal prints the next-step links for both parties:
  ```
  --- EMAIL SIMULATION: FUNDING CONFIRMED ---
  To: seller@example.com
  Action: Release Payment -> http://localhost:8000/transactions/action?caseId=...&token=...&actionType=release_payment

  To: buyer@example.com
  Action: Request Refund -> http://localhost:8000/transactions/action?caseId=...&token=...&actionType=request_refund
  ```

---

### Phase 4 — Payment or Refund Request

**Who does this:** Either party, depending on the outcome

Once funded, the two parties have a chance to settle without a dispute:

#### Option A — Buyer releases payment to Seller

The Buyer uses their token link with `actionType=release_payment`:

```
http://localhost:8000/transactions/action?caseId=CASE-...&token={buyer_token}&actionType=release_payment
```

**What happens:**
- The remaining escrow balance is calculated
- `payment_to_seller` is incremented by the remittance amount
- If the full escrow is paid: `status = CLOSED`
- A simulated blockchain transfer is logged to the terminal

#### Option B — Buyer requests a refund (initiates dispute)

```
http://localhost:8000/transactions/action?caseId=CASE-...&token={buyer_token}&actionType=request_refund
```

**What happens:**
- `status = DISPUTED`
- `dispute_time` and `refund_request_time` are both recorded
- A `Dispute` message is logged in the messages table
- This triggers the **7-day evidence window** (Phase 5)

---

### Phase 5 — Dispute & Evidence Collection

**Who does this:** Both parties submit arguments and supporting files

#### 5a — Submit evidence

Both Seller and Buyer can submit arguments and evidence files:

```
http://localhost:8000/prosecution/evidence?caseId=CASE-...&token={party_token}
```

On this form:
- Write your argument in the text area
- Attach supporting files (PDFs, images, documents)
- Click **Submit Evidence**

**What happens:**
- The argument is saved as a `Message` record with `label = Dispute`
- Each file is SHA-256 hashed, renamed securely, and saved to `storage/evidence/`
- File metadata is recorded in the `files` table linked to the message

#### 5b — Escalate to adjudication

When the 7-day evidence window closes (or you want to manually escalate for testing):

```bash
# POST request to escalate
curl -X POST http://localhost:8000/prosecution/escalate \
  -d "caseId=CASE-..."
```

Or visit `/docs` and use the Swagger UI.

**What happens:**
- `status = DISPUTED` (confirming ready for AI pickup)
- `dispute_time` is recorded (if not already set)
- The AI adjudication scheduler will pick this up in Phase 6

---

### Phase 6 — AI Adjudication

**Who does this:** The AI (triggered by the operator / admin)

> ⚠️ **Requires `OPENAI_API_KEY` to be set in `.env`**

The AI adjudication is a two-stage process:

#### Stage 1: Magistrate Judge
Investigates the case impartially — reads all messages, evidence files, and the contract. Produces a structured report with:
- Verified facts (each citing its source)
- Contradictions between parties
- Unsubstantiated claims
- Recommended payout split (in Wei)

#### Stage 2: Final Judge
Reviews the Magistrate's report and issues a **legally binding ruling** with:
- Final decision text
- `buyer_award` and `seller_award` in Wei
- Detailed rationale
- Confidence score (0.0 – 1.0)

**Math validation:** The system enforces `buyer_award + seller_award == escrow_balance` exactly. If the AI fails this check, the admin is alerted and the ruling is rejected.

#### How to trigger adjudication:

```bash
curl -X POST http://localhost:8000/adjudication/run \
  -d "caseId=CASE-..."
```

Or use the **Swagger UI** at http://localhost:8000/docs → `POST /adjudication/run`

**What happens:**
1. Case is **locked** immediately: `status = PROCESSING`, `adjudication_time` recorded
2. All messages and files are loaded from the database
3. A structured case packet is built and sent to the Magistrate LLM
4. Magistrate report is validated (math + format)
5. Final Judge receives the Magistrate report and issues a ruling
6. Ruling is validated: `buyer_award + seller_award` must equal `escrow_balance`
7. On success:
   - `status = DECIDED`
   - `determination_time`, `decision`, `seller_award`, `buyer_award` stored
   - Simulated determination emails printed to terminal with 7-day appeal link

**What the terminal prints:**
```
--- EMAIL SIMULATION: FINAL DETERMINATION ---
To: seller@example.com | To: buyer@example.com
Subject: Final Determination for Case CASE-...
Decision: [AI ruling text]
Seller Award: X ETH | Buyer Award: Y ETH
Appeal Link (valid 7 days): http://localhost:8000/objection/appeal?caseId=...&token=...
```

---

### Phase 7 — Objection & Award Distribution

**Who does this:** Either party (objection), then the Admin (review), then the system (distribution)

#### 7a — File a procedural objection (optional)

Either party has **7 days** from `determination_time` to file a procedural objection:

```
http://localhost:8000/objection/appeal?caseId=CASE-...&token={party_token}
```

On the form:
- Describe the procedural or logical error in the ruling
- Check **"I confirm this objection contains no new argument"**
- Click **Submit**

> ⚠️ **Objections are limited to procedural/logical errors. No new evidence or arguments are permitted.**

**What happens:**
- Objection text is saved as an `APPEAL` message
- `appeal_time` is recorded
- A simulated admin alert is printed to the terminal with the review portal link

#### 7b — Admin HITL Review (if objection filed)

The admin reviews the objection at:

```
http://localhost:8000/objection/review?caseId=CASE-...
```

The admin sees the full case, the AI ruling, and the objection text.  
The admin selects one of two decisions:

| Decision | Action | n8n equivalent |
|----------|--------|----------------|
| **Uphold** | Original ruling stands; case returns to `DECIDED` for distribution | `Uphold Determination` |
| **Reverse** | Ruling wiped; case returns to `DISPUTED` for re-adjudication | `Revert Determination` |

Submit via:
```bash
# Uphold
curl -X POST http://localhost:8000/objection/review \
  -d "caseId=CASE-...&action=uphold"

# Reverse
curl -X POST http://localhost:8000/objection/review \
  -d "caseId=CASE-...&action=reverse"
```

#### 7c — Award Distribution

In n8n, the `Objection.json` workflow runs on a **hourly schedule** and automatically distributes ETH after 7 days. In this Python implementation, distribution is triggered via the review endpoint (uphold path) or can be called directly.

When distribution runs:
- `seller_award` ETH is transferred to `seller_wallet`
- `buyer_award` ETH is transferred to `buyer_wallet`
- `status = CLOSED`
- Both parties receive a confirmation email with the Etherscan transaction link

> In local dev, blockchain transfers are mocked (logged to terminal). In production, set `PRIVATE_KEY` and `TATUM_API_KEY` for real on-chain transfers.

---

## API Reference

All endpoints are also browsable at **http://localhost:8000/docs**

### Initialization

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/` | None | Case creation form |
| `POST` | `/create-case` | None | Submit new contract |
| `GET` | `/success/{case_id}` | None | Case created confirmation |
| `GET` | `/response` | `token` | Accept or decline contract |
| `GET` | `/wallet` | `token` | Wallet submission form |
| `POST` | `/wallet-submit` | `token` | Save Ethereum wallet address |

### Transactions

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/transactions/verify` | `caseId` | Verify escrow deposit (mock) |
| `GET` | `/transactions/action` | `token` | Release payment or request refund |

**`/transactions/action` query parameters:**

| Parameter | Values | Who |
|-----------|--------|-----|
| `caseId` | Case ID string | — |
| `token` | Party's unique token | — |
| `actionType` | `release_payment` | Buyer only |
| `actionType` | `request_refund` | Buyer only |

### Prosecution

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/prosecution/evidence` | `token` | Evidence submission form |
| `POST` | `/prosecution/evidence` | `token` | Submit argument + files |
| `POST` | `/prosecution/escalate` | None | Escalate case to DISPUTED |

### Adjudication

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `POST` | `/adjudication/run` | None | Run two-stage AI adjudication |

### Objection

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/objection/appeal` | `token` | Procedural objection form |
| `POST` | `/objection/appeal` | `token` | Submit objection |
| `GET` | `/objection/review` | Admin | HITL review dashboard |
| `POST` | `/objection/review` | Admin | Uphold or reverse ruling |

---

## Environment Variables

Copy `.env.example` to `.env` and configure these variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | `postgresql://postgres:password@localhost:5433/arbitration` | PostgreSQL connection string |
| `BASE_URL` | Yes | `http://localhost:8000/` | Public base URL for tokenized email links |
| `ADMIN_EMAIL` | Yes | `admin@example.com` | Recipient of all system error alerts |
| `ESCROW_WALLET` | Yes | `0x000...000` | Ethereum wallet receiving escrow deposits |
| `PRIVATE_KEY` | Prod only | — | Private key for signing payout transactions (**never commit**) |
| `PROCESSING_FEE` | Yes | `1000000000000000` | Platform fee in Wei (default = 0.001 ETH) |
| `OPENAI_API_KEY` | Phase 6 | — | OpenAI API key for LangChain AI agents |
| `ETHERSCAN_API_KEY` | Prod only | — | For real deposit verification |
| `TATUM_API_KEY` | Prod only | — | For broadcasting ETH payout transactions |
| `SMTP_HOST` | Prod only | — | SMTP server for email fallback |
| `SMTP_PORT` | Prod only | `587` | SMTP port |
| `SMTP_USER` | Prod only | — | SMTP login email |
| `SMTP_PASS` | Prod only | — | SMTP password |

> ⚠️ `PRIVATE_KEY` controls the escrow wallet. Store it in a secrets manager (AWS Secrets Manager, HashiCorp Vault, etc.) — **never in `.env` files committed to version control**.

---

## Status Model

| Status | Meaning | Next transition |
|--------|---------|----------------|
| `PENDING` | Case created; awaiting both signatures | → `SIGNED` or `DECLINED` |
| `SIGNED` | Both accepted; awaiting escrow deposit | → `DISPUTED` |
| `DECLINED` | At least one party declined | Terminal |
| `DISPUTED` | Dispute filed; evidence window open | → `PROCESSING` |
| `PROCESSING` | AI adjudication in progress (case locked) | → `DECIDED` |
| `DECIDED` | AI ruling issued; 7-day appeal window | → `CLOSED` or → `DISPUTED` |
| `CLOSED` | Funds distributed; case complete | Terminal |

---

## Financial Model

All monetary values are stored as **Wei integers** in `Numeric(38, 0)` columns.

```
1 ETH = 1,000,000,000,000,000,000 Wei  (10¹⁸)
```

**Escrow balance formula:**
```
escrow_balance = max(0, Escrow Fund − Payment to Seller − Refund to Buyer)
```

**Liquid balance formula (available funds in the deposit pot):**
```
liquid_balance = Deposited Fund − Fee − Tip to Seller − Buyer Withdrawal
               − Payment to Seller − Refund to Buyer
```

**AI award validation (strictly enforced):**
```
buyer_award + seller_award == escrow_balance   ← must be exact
buyer_award  >= 0
seller_award >= 0
```

---

## Project Structure

```
ai-arbitration-python/
│
├── main.py                    # FastAPI app entry point
├── models.py                  # SQLAlchemy ORM models (Case, Message, File)
├── database.py                # DB session factory
├── create_tables.py           # One-time schema initializer
├── docker-compose.yml         # PostgreSQL container
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
│
├── routers/
│   ├── initialization.py      # Phase 1–2: Case creation, signatures, wallets
│   ├── transactions.py        # Phase 3–4: Deposit verification, payments
│   ├── prosecution.py         # Phase 4–5: Evidence upload, dispute escalation
│   ├── adjudication.py        # Phase 6:   AI Magistrate + Final Judge
│   ├── objection.py           # Phase 7:   Appeals, HITL review, distribution
│   └── exceptions.py          # Global:    Error handler, admin alerts
│
├── templates/                 # Jinja2 HTML templates
│   ├── layout.html
│   ├── create_case.html
│   ├── success.html
│   ├── response_status.html
│   ├── wallet_form.html
│   ├── deposit_status.html
│   ├── transaction_action.html
│   ├── evidence_form.html
│   ├── evidence_success.html
│   ├── adjudication_result.html
│   ├── objection_form.html
│   └── objection_review.html
│
├── static/
│   └── css/style.css          # Platform stylesheet
│
├── storage/
│   └── evidence/              # Uploaded evidence files (SHA-256 hashed)
│
└── uploads/                   # Contract PDF uploads
```

---

## Production Deployment Notes

### 1. Real email sending
Replace the `print()` statements in each router with an SMTP client or transactional email provider (SendGrid, Mailgun, Postmark). The `exceptions.py` module already has an SMTP stub ready to uncomment.

### 2. Real blockchain integration
- Set `ETHERSCAN_API_KEY` to enable real deposit scanning via the Etherscan API
- Set `TATUM_API_KEY` + `PRIVATE_KEY` to enable real ETH payout broadcasting via Tatum v3

### 3. Real file storage
Replace `storage/evidence/` with Google Drive, AWS S3, or equivalent. Update the file upload/download logic in `prosecution.py` and `adjudication.py`.

### 4. Scheduled adjudication
The n8n workflows run adjudication on a **12-hour schedule** and distribution on an **hourly schedule**. In production, set up cron jobs or a task queue (Celery, APScheduler) to call:
- `POST /adjudication/run` every 12 hours (picks up DISPUTED cases ≥ 7 days old)
- Award distribution logic every hour (picks up DECIDED cases ≥ 7 days old)

### 5. Security hardening
- Serve over HTTPS (use a reverse proxy: Nginx, Caddy, or Traefik)
- Move `PRIVATE_KEY` to a secrets manager
- Rate-limit token-authenticated endpoints
- Add authentication to `/adjudication/run` and `/objection/review`

### 6. AI model configuration
The adjudication router uses `gpt-4o` via LangChain by default. To match the original n8n setup:
- Magistrate Judge → Gemini 2.5 Pro (multimodal) with Google Drive tool access
- Final Judge → DeepSeek Reasoner (text-only, JSON output mode)

Update the model initialization in `adjudication.py` to use your preferred provider.
