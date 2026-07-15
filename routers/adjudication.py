from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import os

from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from pydantic import BaseModel, Field
import PyPDF2

from database import get_db, SessionLocal
from models import Case, StatusEnum, Message, File
from dependencies import verify_admin_token
import email_service

# Pydantic models for LangChain structured output
class MagistrateReport(BaseModel):
    summary: str = Field(description="Neutral overview of dispute and timeline")
    facts: list[str] = Field(description="Array of verified facts (each with evidence citation)")
    contradictions: list[str] = Field(description="Array of conflicts between claims/evidence or between parties")
    unsubstantiated_claims: list[str] = Field(description="Array of claims lacking evidence")
    reasoning: str = Field(description="Explanation of how facts support your payout recommendation")
    recommended_buyer_payout: str = Field(description="Wei string (must sum with seller payout to escrow_balance)")
    recommended_seller_payout: str = Field(description="Wei string (must sum with buyer payout to escrow_balance)")

class FinalRuling(BaseModel):
    decision: str = Field(description="The final binding ruling description")
    escrow_balance: str = Field(description="The total escrow balance in Wei")
    buyer_award: str = Field(description="Amount awarded to the buyer in Wei (string format)")
    seller_award: str = Field(description="Amount awarded to the seller in Wei (string format)")
    rationale: str = Field(description="Detailed rationale for the final ruling")
    confidence: float = Field(description="AI Confidence score between 0.0 and 1.0")


router = APIRouter(prefix="/adjudication", tags=["Adjudication"])
templates = Jinja2Templates(directory="templates")

def read_evidence_files(case_id: str, db: Session):
    """Summarizes uploaded evidence files for the AI context."""
    files = db.query(File).filter(File.case_id == case_id).all()
    if not files:
        return "_No user evidence files submitted._"
        
    file_table = "| Source | Filename | File Hash |\n|--------|----------|---------|\n"
    for f in files:
        submitter = f.submitter.value if f.submitter else 'SYSTEM'
        file_table += f"| {submitter} | {f.original_name} | {f.hash} |\n"
    return file_table

UPLOAD_DIR = "uploads"

@tool
def read_evidence_file(file_hash: str) -> str:
    """Reads the text content of an uploaded evidence file by its hash.
    Use this to read the details of PDF or TXT files submitted by parties."""
    db = SessionLocal()
    try:
        db_file = db.query(File).filter(File.hash == file_hash).first()
        if not db_file:
            return "File not found."
            
        file_path = os.path.join(UPLOAD_DIR, db_file.secure_name)
        if not os.path.exists(file_path):
            return "File missing from disk."
            
        if db_file.original_name.lower().endswith('.pdf'):
            try:
                with open(file_path, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    text = "\\n".join([page.extract_text() or "" for page in reader.pages])
                    return text[:5000] # Limit length
            except Exception as e:
                return f"Could not read PDF: {e}"
        else:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read()[:5000]
            except Exception as e:
                return f"Could not read text file: {e}"
    finally:
        db.close()

import ast
import operator
import httpx

@tool
def calculator(expression: str) -> str:
    """Evaluates a mathematical expression (e.g. '1000 * 0.15' or '(500 + 200) / 2').
    Use this to calculate exact Wei amounts for fractional damage awards."""
    try:
        # Safe math evaluation using ast
        def eval_expr(node):
            if isinstance(node, ast.Num): return node.n
            elif isinstance(node, ast.BinOp):
                op_map = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
                return op_map[type(node.op)](eval_expr(node.left), eval_expr(node.right))
            elif isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.USub): return -eval_expr(node.operand)
                elif isinstance(node.op, ast.UAdd): return eval_expr(node.operand)
            raise ValueError("Unsupported operation")
        
        result = eval_expr(ast.parse(expression, mode='eval').body)
        return str(result)
    except Exception as e:
        return f"Calculation error: {e}"

@tool
def external_verification(url: str) -> str:
    """Fetches the text content of a public URL (HTTP GET).
    Use this to verify tracking numbers, public pricing, or reference data."""
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True)
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code}"
        # Return first 2000 chars of text to avoid context overload
        return resp.text[:2000]
    except Exception as e:
        return f"Fetch error: {e}"

@router.post("/run", response_class=HTMLResponse)
async def run_adjudication(request: Request, caseId: str = Form(...), db: Session = Depends(get_db), admin: str = Depends(verify_admin_token)):
    """Executes the two-stage AI adjudication process."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)

    # LOCK the case immediately before running agents (n8n: Record Status node fires in parallel
    # at the same time as Get Messages / Get Files — before Prepare Case Packet)
    case.status = StatusEnum.PROCESSING_LOCKED
    case.adjudication_time = datetime.now(timezone.utc)
    db.commit()
    db.refresh(case)

    messages = db.query(Message).filter(Message.case_id == caseId).order_by(Message.time).all()
    # 1. Prepare Case Packet
    escrow_fund = int(case.escrow_fund or 0)
    refund_to_buyer = int(case.refund_to_buyer or 0)
    payment_to_seller = int(case.payment_to_seller or 0)
    
    raw_available = escrow_fund - refund_to_buyer - payment_to_seller
    escrow_balance = raw_available if raw_available > 0 else 0
    
    msg_log = "\n\n".join([f"**{m.time.strftime('%Y-%m-%d %H:%M UTC') if m.time else 'Unknown Time'} [{m.sender.value if m.sender else 'Unknown'}]**: {m.content}" for m in messages])
    
    file_table = read_evidence_files(caseId, db)
    
    signed_time = case.created_at.strftime('%Y-%m-%d %H:%M UTC') if case.created_at else 'No Signing Time'
    time_now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    
    user_prompt = f"""
# Case #{case.case_id}  

**Current Time:** {time_now}  
**Contract Signed:** {signed_time}  

## Parties: 
- **Buyer:** {case.buyer} ({case.buyer_email})
- **Seller:** {case.seller} ({case.seller_email})

## Financials: 
- **Total Escrow Fund:** {escrow_fund} Wei
- **Refunded to Buyer:** {refund_to_buyer} Wei
- **Paid to Seller:** {payment_to_seller} Wei
- **Escrow Balance:** {escrow_balance} Wei

## Evidence Files: 
{file_table}

## Messages:
{msg_log if msg_log else "_No messages submitted._"}

## Contract Text:
```
{case.contract_text or "No contract text provided."}
```
"""

    # 2. Setup LangChain Models with Fallbacks
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    
    if not openai_api_key:
        return HTMLResponse(
            "<h1>AI Setup Required</h1><p>Error: <code>OPENAI_API_KEY</code> environment variable is not set. Please set it in your terminal to run the AI Adjudicator.</p>", 
            status_code=500
        )
    
    primary_llm = ChatOpenAI(model="gpt-4o", temperature=0)
    # If Gemini is configured, use it as fallback. Otherwise, fallback to a smaller OpenAI model.
    if gemini_api_key:
        fallback_llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0)
    else:
        fallback_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
        
    resilient_llm = primary_llm.with_fallbacks([fallback_llm])
    
    # Create the agent
    magistrate_prompt = ChatPromptTemplate.from_messages([
        ("system", "You are the AI Magistrate Judge. Investigate the case details, read the contract, use the calculator tool for damage math, and use the external_verification tool if a URL or tracking number needs checking. Summarize your factual findings, including a precise mathematical breakdown of the proposed award."),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}")
    ])
    
    agent = create_tool_calling_agent(resilient_llm, [read_evidence_file, calculator, external_verification], magistrate_prompt)
    agent_executor = AgentExecutor(agent=agent, tools=[read_evidence_file, calculator, external_verification], verbose=True)
    
    raw_report = agent_executor.invoke({"input": user_prompt})["output"]
    # Extract the structured JSON from the raw report text using the resilient LLM
    magistrate_report = resilient_llm.with_structured_output(MagistrateReport).invoke(
        f"Extract the magistrate report strictly matching the JSON schema from this text:\n\n{raw_report}"
    )
        
    # 4. Final Judge Stage
    final_judge_llm = resilient_llm.with_structured_output(FinalRuling)
    
    judge_system_prompt = (
        "You are the Final Judge issuing legally binding rulings in an arbitration system.\n\n"
        "## AUTHORITY & CONSTRAINTS\n\n"
        "You may approve, modify, or reject the Magistrate's recommendation. You must:\n"
        "- Distribute exactly the escrow balance (no more, no less)\n"
        "- Use non-negative Wei integers only (as strings)\n"
        "- Justify any deviation from the Magistrate's recommendation\n"
        "- Base decisions solely on the provided report (no new evidence, no invented facts)\n\n"
        "## LEGAL PRINCIPLES\n\n"
        "- Both parties bear equal burden of proof.\n"
        "- Apply contract law; honor explicit obligations.\n"
        "- When evidence conflicts, favor documented proof over verbal claims.\n"
        "- Remain strictly neutral.\n\n"
        "## REVIEW CHECKLIST\n\n"
        "1. **Math:** Ensure buyer_payout + seller_payout == escrow_balance.\n"
        "2. **Evidence:** Confirm reasoning aligns with verified facts and contract terms; weigh contradictions.\n"
        "3. **Fairness:** Ensure split reflects evidence and contract terms.\n"
        "4. **Finality:** This is irreversible; your decision executes immediately.\n\n"
        "## CRITICAL RULES\n\n"
        "- All payouts must be non-negative Wei integers as strings.\n"
        "- Sum of buyer_award and seller_award MUST equal escrow_balance exactly.\n"
        "- All amounts >= 0, no decimals, no rounding.\n"
        "- Use ONLY the escrow balance provided (no fund creation/destruction).\n"
        "- No new investigation—rely solely on Magistrate's report.\n"
        "- Output ONLY valid JSON matching the schema (no extra commentary).\n"
    )
    
    final_ruling = final_judge_llm.invoke([
        {"role": "system", "content": judge_system_prompt},
        {"role": "user", "content": f"Escrow Balance Requirement: {escrow_balance} Wei\n\nMagistrate Report:\n{magistrate_report.model_dump_json()}"}
    ])
        
    # 5. Math Validation & Database Update
    buyer_award_int = int(final_ruling.buyer_award)
    seller_award_int = int(final_ruling.seller_award)
    if buyer_award_int + seller_award_int != escrow_balance:
        raise ValueError(f"Math Error: Awards ({buyer_award_int} + {seller_award_int}) != Escrow Balance ({escrow_balance})")
        
    # Commit final ruling to DB (n8n: Record Determination node)
    # Status becomes DECIDED, Determination Time recorded
    # NOTE: adjudication_time was already set above when the case was locked
    case.status = StatusEnum.DECIDED_LOCKED
    case.determination_time = datetime.now(timezone.utc)
    case.decision = final_ruling.decision
    case.buyer_award = buyer_award_int
    case.seller_award = seller_award_int

    db.commit()

    # Send determination emails to both parties
    email_service.send_determination(
        case_id=case.case_id,
        seller_name=case.seller, seller_email=case.seller_email, seller_token=case.seller_token,
        buyer_name=case.buyer,   buyer_email=case.buyer_email,   buyer_token=case.buyer_token,
        decision=final_ruling.decision,
        seller_award_eth=seller_award_int / 1e18,
        buyer_award_eth=buyer_award_int  / 1e18,
    )

    return templates.TemplateResponse("adjudication_result.html", {
        "request": request,
        "case": case,
        "magistrate": magistrate_report,
        "ruling": final_ruling,
        "timestamp": case.determination_time.strftime('%Y-%m-%d %H:%M UTC')
    })
