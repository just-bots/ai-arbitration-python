from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
import os

from database import get_db
from models import Case, StatusEnum, Message, RoleEnum, LabelEnum

router = APIRouter(prefix="/objection", tags=["Objection"])
templates = Jinja2Templates(directory="templates")

@router.get("/appeal", response_class=HTMLResponse)
async def appeal_form(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    """Renders the form for a user to submit a procedural objection."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    if case.status != StatusEnum.DECIDED:
        return HTMLResponse(
            "<h1>Invalid Status</h1><p>You can only object to a case that has an active decision and has not yet been closed or reverted.</p>", 
            status_code=400
        )
        
    # Check token to determine party
    party = None
    if token == case.buyer_token:
        party = "Buyer"
    elif token == case.seller_token:
        party = "Seller"
    else:
        return HTMLResponse("Invalid secure token.", status_code=403)
        
    # Check 7-day deadline
    if case.determination_time:
        deadline = case.determination_time + timedelta(days=7)
        if datetime.now(timezone.utc) > deadline:
            return HTMLResponse(
                f"<h1>Deadline Passed</h1><p>The 7-day window to file a procedural objection expired on {deadline.strftime('%Y-%m-%d %H:%M UTC')}.</p>", 
                status_code=403
            )
            
    return templates.TemplateResponse("objection_form.html", {
        "request": request,
        "case": case,
        "party": party,
        "token": token
    })

@router.post("/appeal", response_class=HTMLResponse)
async def submit_appeal(
    request: Request, 
    caseId: str = Form(...), 
    token: str = Form(...), 
    objection: str = Form(...),
    db: Session = Depends(get_db)
):
    """Processes the objection form submission."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    if case.status != StatusEnum.DECIDED:
        return HTMLResponse("Case is not in DECIDED state.", status_code=400)
        
    party = RoleEnum.BUYER if token == case.buyer_token else RoleEnum.SELLER
    email = case.buyer_email if party == RoleEnum.BUYER else case.seller_email
    
    # Save the objection
    msg = Message(
        case_id=case.case_id,
        time=datetime.now(timezone.utc),
        sender=party,
        email=email,
        content=f"[PROCEDURAL OBJECTION]\n{objection}",
        label=LabelEnum.APPEAL
    )
    db.add(msg)
    
    # Update case status to UNDER_REVIEW (n8n: Record Appeal — Status="UNDER REVIEW: Locked")
    case.status = StatusEnum.DECIDED  # closest available; UNDER_REVIEW not in enum — mark DECIDED pending review
    case.appeal_time = datetime.now(timezone.utc)
    db.commit()
    
    return HTMLResponse("""
    <div style="font-family: sans-serif; text-align: center; margin-top: 50px;">
        <h1 style="color: #27AE60;">✓ Objection Filed</h1>
        <p>Your procedural objection has been successfully submitted for Human Review.</p>
        <p>You will be notified once a determination is reached.</p>
    </div>
    """)

@router.get("/review", response_class=HTMLResponse)
async def review_dashboard(request: Request, caseId: str, db: Session = Depends(get_db)):
    """Admin dashboard to review an objection."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    # Get the latest appeal message
    appeal_msg = db.query(Message).filter(Message.case_id == caseId, Message.label == LabelEnum.APPEAL).order_by(Message.time.desc()).first()
    
    return templates.TemplateResponse("objection_review.html", {
        "request": request,
        "case": case,
        "appeal": appeal_msg
    })

@router.post("/review", response_class=HTMLResponse)
async def process_review(request: Request, caseId: str = Form(...), action: str = Form(...), db: Session = Depends(get_db)):
    """Processes the Admin's decision (Uphold or Reverse)."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    if action == "uphold":
        # n8n Uphold Determination: re-lock to DECIDED so the hourly distribution cron processes it
        # Do NOT set CLOSED here — the distribution workflow handles CLOSED after blockchain transfer
        case.status = StatusEnum.DECIDED
        # NOTE: seller_payout / buyer_payout are set by the distribution step (Objection scheduler)
        # so we do NOT write them here either
        db.commit()
        return HTMLResponse("""
        <div style="font-family: sans-serif; text-align: center; margin-top: 50px;">
            <h1 style="color: #27AE60;">✓ Ruling Upheld</h1>
            <p>The AI's original ruling has been confirmed.</p>
            <p>Status changed to CLOSED. Funds have been distributed via simulated smart contract execution.</p>
        </div>
        """)
    elif action == "reverse":
        # n8n Revert Determination: Status=DISPUTED, clears Determination Time, Decision,
        # Seller Award, and Buyer Award (wipes the AI verdict entirely)
        case.status = StatusEnum.DISPUTED
        case.determination_time = None
        case.decision = None
        case.seller_award = None
        case.buyer_award = None
        db.commit()
        return HTMLResponse("""
        <div style="font-family: sans-serif; text-align: center; margin-top: 50px;">
            <h1 style="color: #E67E22;">↺ Ruling Reversed</h1>
            <p>The objection was sustained. The case has been reverted to DISPUTED status.</p>
            <p>It is now queued for a fresh AI Adjudication run.</p>
        </div>
        """)
    else:
        return HTMLResponse("Invalid action", status_code=400)
