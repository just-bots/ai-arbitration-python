from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import os

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from database import get_db
from models import Case, StatusEnum, Message, File

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

@router.post("/run", response_class=HTMLResponse)
async def run_adjudication(request: Request, caseId: str = Form(...), db: Session = Depends(get_db)):
    """Executes the two-stage AI adjudication process."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)

    # LOCK the case immediately before running agents (n8n: Record Status node fires in parallel
    # at the same time as Get Messages / Get Files — before Prepare Case Packet)
    case.status = StatusEnum.PROCESSING
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

    # 2. Setup LangChain Models
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return HTMLResponse(
            "<h1>AI Setup Required</h1><p>Error: <code>OPENAI_API_KEY</code> environment variable is not set. Please set it in your terminal to run the AI Adjudicator.</p>", 
            status_code=500
        )
    
    # Initialize the LLM (temperature=0 for deterministic rulings)
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    
    magistrate_llm = llm.with_structured_output(MagistrateReport)
    final_judge_llm = llm.with_structured_output(FinalRuling)
    
    # 3. Magistrate Judge Stage
    magistrate_system_prompt = (
        "You are a Magistrate Judge conducting impartial case investigation for an automated arbitration system. "
        "Your role is to analyze evidence and prepare a neutral investigation report—not to issue final rulings.\n\n"
        "---\n\n"
        "## INVESTIGATION PROTOCOL\n\n"
        "**1. Evidence Review**\n"
        "- Inspect all files listed in the Evidence Files table (refer to filenames and SHA-256 hashes)\n"
        "- For damage/condition claims, review all associated file metadata\n"
        "- If a file is inaccessible or missing, mark it 'NOT REVIEWED' in your reasoning\n\n"
        "**2. Financial Calculations**\n"
        "- All currency values are in Wei (1 ETH = 1,000,000,000,000,000,000 Wei)\n"
        "- Verify: recommended_buyer_payout + recommended_seller_payout = escrow_balance\n"
        "- Both payouts must be non-negative Wei integers (as strings, no leading zeros)\n\n"
        "**3. Burden of Proof**\n"
        "- Both parties bear equal burden of proof\n"
        "- Document all verified facts in the `facts` array\n"
        "- Flag conflicts in the `contradictions` array (e.g., 'Buyer claims delivery on Feb 5, but tracking shows Feb 7')\n"
        "- Add unsupported claims to `unsubstantiated_claims` array\n"
        "- Do not fill evidence gaps with assumptions\n\n"
        "**4. Analysis Requirements**\n"
        "- Build chronological timeline from messages and files\n"
        "- Compare contract terms against party claims\n"
        "- Cross-reference file metadata with verbal statements\n"
        "- Identify contradictions between evidence sources (messages vs files, buyer vs seller claims)\n"
        "- In the `reasoning` field, explain how the evidence supports your payout recommendation\n"
        "- Every fact must cite its source: file name (e.g., 'file: receipt.pdf'), "
        "message timestamp (e.g., 'Buyer, 2026-02-05 14:32'), or external verification\n\n"
        "---\n\n"
        "## OUTPUT STRUCTURE\n\n"
        "Your response will be validated against this JSON schema:\n"
        "- `summary`: Neutral overview of dispute and timeline\n"
        "- `facts`: Array of verified facts (each with evidence citation)\n"
        "- `contradictions`: Array of conflicts between claims/evidence or between parties\n"
        "- `unsubstantiated_claims`: Array of claims lacking evidence\n"
        "- `reasoning`: Explanation of how facts support your payout recommendation\n"
        "- `recommended_buyer_payout`: Wei string (must sum with seller payout to escrow_balance)\n"
        "- `recommended_seller_payout`: Wei string (must sum with buyer payout to escrow_balance)\n"
    )
    
    try:
        magistrate_report = magistrate_llm.invoke([
            {"role": "system", "content": magistrate_system_prompt},
            {"role": "user", "content": user_prompt}
        ])
    except Exception as e:
        return HTMLResponse(f"Magistrate Agent Error: {str(e)}", status_code=500)
        
    # 4. Final Judge Stage
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
    
    try:
        final_ruling = final_judge_llm.invoke([
            {"role": "system", "content": judge_system_prompt},
            {"role": "user", "content": f"Escrow Balance Requirement: {escrow_balance} Wei\n\nMagistrate Report:\n{magistrate_report.model_dump_json()}"}
        ])
    except Exception as e:
        return HTMLResponse(f"Final Judge Agent Error: {str(e)}", status_code=500)
        
    # 5. Math Validation & Database Update
    try:
        buyer_award_int = int(final_ruling.buyer_award)
        seller_award_int = int(final_ruling.seller_award)
        if buyer_award_int + seller_award_int != escrow_balance:
            raise ValueError(f"Math Error: Awards ({buyer_award_int} + {seller_award_int}) != Escrow Balance ({escrow_balance})")
    except ValueError as ve:
        return HTMLResponse(f"Validation Error: {str(ve)}", status_code=500)
        
    # Commit final ruling to DB (n8n: Record Determination node)
    # Status becomes DECIDED, Determination Time recorded
    # NOTE: adjudication_time was already set above when the case was locked
    case.status = StatusEnum.DECIDED
    case.determination_time = datetime.now(timezone.utc)
    case.decision = final_ruling.decision
    case.buyer_award = buyer_award_int
    case.seller_award = seller_award_int
    
    db.commit()
    
    return templates.TemplateResponse("adjudication_result.html", {
        "request": request,
        "case": case,
        "magistrate": magistrate_report,
        "ruling": final_ruling,
        "timestamp": case.determination_time.strftime('%Y-%m-%d %H:%M UTC')
    })
