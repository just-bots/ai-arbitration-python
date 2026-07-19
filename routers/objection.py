from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
import os

from database import get_db
from models import Case, StatusEnum, RoleEnum, Message, LabelEnum
import email_service
import validators
import hashlib
import secrets

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
BASE_URL    = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

router = APIRouter(prefix="/objection", tags=["Objection"])
templates = Jinja2Templates(directory="templates")

@router.get("/appeal", response_class=HTMLResponse)
async def appeal_form(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    """Renders the form for a user to submit a procedural objection."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    if case.status != StatusEnum.DECIDED_LOCKED:
        return HTMLResponse(
            "<h1>Invalid Status</h1><p>You can only object to a case that has an active decision and has not yet been closed or reverted.</p>", 
            status_code=400
        )
        
    # Check token to determine party
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid secure token.", status_code=403)
    party = "Buyer" if is_buyer else "Seller"
        
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
        
    if case.status != StatusEnum.DECIDED_LOCKED:
        return HTMLResponse("Case is not in DECIDED_LOCKED state.", status_code=400)
        
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid token", status_code=403)
        
    party = RoleEnum.BUYER if is_buyer else RoleEnum.SELLER
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
    
    # Update case status pending admin review
    case.status = StatusEnum.UNDER_REVIEW_LOCKED
    case.appeal_time = datetime.now(timezone.utc)
    db.commit()

    # Use secure ADMIN_KEY for admin endpoints
    from dependencies import ADMIN_KEY
    admin_token = ADMIN_KEY
    
    # Notify admin that a review is required
    objecting_name = case.buyer if party == RoleEnum.BUYER else case.seller
    review_url = f"{BASE_URL}/objection/review?caseId={caseId}&token={admin_token}"
    if ADMIN_EMAIL:
        email_service.send_objection_received(
            case_id=caseId, objecting_party=objecting_name,
            admin_email=ADMIN_EMAIL, review_url=review_url
        )

    return HTMLResponse("""
    <div style="font-family: sans-serif; text-align: center; margin-top: 50px;">
        <h1 style="color: #27AE60;">&#x2713; Objection Filed</h1>
        <p>Your procedural objection has been successfully submitted for Human Review.</p>
        <p>You will be notified once a determination is reached.</p>
    </div>
    """)

@router.get("/review", response_class=HTMLResponse)
async def review_dashboard(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    """Admin dashboard to review an objection."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    from dependencies import ADMIN_KEY
    if not secrets.compare_digest(token, ADMIN_KEY):
        return HTMLResponse("Invalid admin key", status_code=403)
        
    # Get the latest appeal message
    appeal_msg = db.query(Message).filter(Message.case_id == caseId, Message.label == LabelEnum.APPEAL).order_by(Message.time.desc()).first()
    
    return templates.TemplateResponse("objection_review.html", {
        "request": request,
        "case": case,
        "appeal": appeal_msg,
        "token": token
    })

@router.post("/review", response_class=HTMLResponse)
async def process_review(request: Request, caseId: str = Form(...), action: str = Form(...), token: str = Form(...), db: Session = Depends(get_db)):
    """Processes the Admin's decision (Uphold or Reverse)."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    from dependencies import ADMIN_KEY
    if not secrets.compare_digest(token, ADMIN_KEY):
        return HTMLResponse("Invalid admin key", status_code=403)
        
    if action == "uphold":
        # Handle seller and buyer payouts independently for idempotency and atomicity
        from blockchain import transfer_funds
        
        seller_award = int(case.seller_award or 0)
        buyer_award = int(case.buyer_award or 0)
        
        if seller_award > 0 and int(case.seller_payout or 0) == 0:
            try:
                if case.seller_wallet:
                    await transfer_funds(case.seller_wallet, seller_award, case.case_id)
                    case.seller_payout = seller_award
                    db.commit()
            except Exception as e:
                print(f"Transfer error for seller: {e}")
                return HTMLResponse(f"Blockchain Transfer Failed for Seller: {e}", status_code=500)
                
        if buyer_award > 0 and int(case.buyer_payout or 0) == 0:
            try:
                if case.buyer_wallet:
                    await transfer_funds(case.buyer_wallet, buyer_award, case.case_id)
                    case.buyer_payout = buyer_award
                    db.commit()
            except Exception as e:
                print(f"Transfer error for buyer: {e}")
                return HTMLResponse(f"Blockchain Transfer Failed for Buyer: {e}", status_code=500)
        
        if int(case.seller_payout or 0) == 0 and int(case.buyer_payout or 0) == 0:
            case.status = StatusEnum.CLOSED_NO_AWARD
        else:
            case.status = StatusEnum.CLOSED
            
        db.commit()

        # Notify both parties the ruling stands and distribute awards
        seller_award_eth = float(case.seller_award or 0) / 1e18
        buyer_award_eth  = float(case.buyer_award  or 0) / 1e18
        email_service.send_award_distributed(
            case_id=caseId,
            seller_name=case.seller, seller_email=case.seller_email, seller_award_eth=seller_award_eth,
            buyer_name=case.buyer,   buyer_email=case.buyer_email,   buyer_award_eth=buyer_award_eth
        )
        return HTMLResponse("""
        <div style="font-family: sans-serif; text-align: center; margin-top: 50px;">
            <h1 style="color: #27AE60;">&#x2713; Ruling Upheld</h1>
            <p>The AI's original ruling has been confirmed. Award distribution emails sent.</p>
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
