from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from decimal import Decimal
from datetime import datetime, timezone
import secrets

from database import get_db
from models import Case, StatusEnum, RoleEnum, Message, LabelEnum
import email_service

router = APIRouter(prefix="/transactions", tags=["Transactions"])
templates = Jinja2Templates(directory="templates")

@router.get("/verify", response_class=HTMLResponse)
async def verify_deposit(request: Request, caseId: str, db: Session = Depends(get_db)):
    """Mocks Etherscan Verification and confirms funding."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    total_required = (case.escrow_fund or Decimal(0)) + (case.fee or Decimal(0))
    
    # Mocking the Etherscan deposit for local testing purposes
    # We pretend the exact amount was just deposited.
    if case.deposited_fund is None or case.deposited_fund < total_required:
        case.deposited_fund = total_required
        if case.status == StatusEnum.SIGNED:
            case.status = StatusEnum.EFFECTIVE
        db.commit()

        # Send funding confirmed emails
        email_service.send_escrow_confirmed(
            case_id=caseId,
            seller_name=case.seller, seller_email=case.seller_email, seller_token=case.seller_token,
            buyer_name=case.buyer,   buyer_email=case.buyer_email,   buyer_token=case.buyer_token
        )

    is_funded = case.deposited_fund >= total_required

    return templates.TemplateResponse("deposit_status.html", {
        "request": request, 
        "case": case,
        "is_funded": is_funded,
        "total_required": total_required
    })

@router.get("/action", response_class=HTMLResponse)
async def transaction_action(
    request: Request, 
    caseId: str, 
    token: str, 
    actionType: str,
    db: Session = Depends(get_db)
):
    """Handles releasing payment or requesting a refund."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    # Validate tokens
    is_buyer = (token == case.buyer_token)
    is_seller = (token == case.seller_token)
    
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid secure token", status_code=403)
        
    action_message = ""
    
    if actionType == "release_payment":
        if case.status in [StatusEnum.TRANSFERRED_TO_SELLER, StatusEnum.CLOSED]:
            return HTMLResponse("Payment has already been released.", status_code=400)
            
        # Only the Buyer can release payment to the Seller (n8n: Conditions2 → token matches Seller Token)
        if not is_buyer:
            return HTMLResponse("Only the buyer can release payment", status_code=403)

        # Calculate remaining escrow balance (mirrors n8n's Calculate Amount node)
        escrow_fund       = int(case.escrow_fund       or 0)
        payment_to_seller = int(case.payment_to_seller or 0)
        refund_to_buyer   = int(case.refund_to_buyer   or 0)
        tip_to_seller     = int(case.tip_to_seller     or 0)
        buyer_withdrawal  = int(case.buyer_withdrawal  or 0)
        deposited_fund    = int(case.deposited_fund    or 0)
        fee               = int(case.fee               or 0)

        # Available escrow balance (n8n: escrow = Escrow Fund - Payment to Seller - Refund to Buyer)
        remaining_escrow = escrow_fund - payment_to_seller - refund_to_buyer
        # Liquid balance in the actual deposited pot
        liquid_balance = deposited_fund - fee - tip_to_seller - buyer_withdrawal - payment_to_seller - refund_to_buyer

        if remaining_escrow <= 0 or liquid_balance <= 0:
            return HTMLResponse("No escrow balance available to release.", status_code=400)

        # Remittance is the full remaining escrow amount (buyer releases everything)
        remittance = min(remaining_escrow, liquid_balance)

        # Increment Payment to Seller (n8n: Record Payment1)
        case.payment_to_seller = payment_to_seller + remittance

        # Status = CLOSED only if entire escrow fund has been paid out (n8n: TRANSFERRED to Seller)
        new_payment_total = payment_to_seller + remittance
        if new_payment_total >= escrow_fund:
            case.status = StatusEnum.TRANSFERRED_TO_SELLER

        action_message = f"Payment of {remittance / 1e18:.6f} ETH successfully released to the Seller."
        if case.status == StatusEnum.TRANSFERRED_TO_SELLER:
            action_message += " The case is now TRANSFERRED to Seller."

        db.commit()
        email_service.send_payment_released(
            case_id=case.case_id,
            seller_name=case.seller, seller_email=case.seller_email,
            buyer_name=case.buyer,   buyer_email=case.buyer_email,
            amount_eth=remittance / 1e18,
            closed=(case.status == StatusEnum.TRANSFERRED_TO_SELLER)
        )
    elif actionType == "request_refund":
        if case.status == StatusEnum.DISPUTED:
            return HTMLResponse("Refund already requested (case is already disputed).", status_code=400)
        if case.status in [StatusEnum.TRANSFERRED_TO_SELLER, StatusEnum.CLOSED]:
            return HTMLResponse("Cannot request refund, payment has already been released.", status_code=400)
            
        # Only the Buyer can request a refund
        if not is_buyer:
            return HTMLResponse("Only the buyer can request a refund", status_code=403)

        # n8n: Record Dispute1 — Status=DISPUTED, Dispute Time=now
        case.status = StatusEnum.DISPUTED
        case.dispute_time = datetime.now(timezone.utc)
        case.refund_request_time = datetime.now(timezone.utc)
        action_message = "Refund requested. This case has been officially escalated to DISPUTED status."

        # Log this event in the messages table (n8n: Record Message, Label=Dispute)
        dispute_msg = Message(
            case_id=case.case_id,
            time=datetime.now(timezone.utc),
            sender=RoleEnum.BUYER,
            email=case.buyer_email,
            content="Buyer has requested a refund and disputed the contract.",
            label=LabelEnum.DISPUTE
        )
        db.add(dispute_msg)
        db.commit()
        email_service.send_refund_requested(
            case_id=case.case_id,
            seller_name=case.seller, seller_email=case.seller_email,
            buyer_name=case.buyer,   buyer_email=case.buyer_email
        )

    else:
        return HTMLResponse("Unknown action", status_code=400)
        
    db.commit()

    return templates.TemplateResponse("transaction_action.html", {
        "request": request,
        "case": case,
        "action_message": action_message
    })
