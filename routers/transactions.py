from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from decimal import Decimal
from datetime import datetime, timezone
import secrets
import httpx
import os

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
ESCROW_WALLET = os.environ.get("ESCROW_WALLET", "0x0000000000000000000000000000000000000000")

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
    
    # Use Etherscan if API key is provided
    was_funded_already = case.deposited_fund is not None and case.deposited_fund >= total_required

    if ETHERSCAN_API_KEY and case.buyer_wallet and not was_funded_already:
        try:
            url = f"https://api.etherscan.io/api?module=account&action=txlist&address={ESCROW_WALLET}&startblock=0&endblock=99999999&sort=asc&apikey={ETHERSCAN_API_KEY}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
                data = resp.json()
                if data.get("status") == "1":
                    total_deposited = Decimal(0)
                    for tx in data.get("result", []):
                        # Match buyer to escrow deposits
                        if tx.get("from", "").lower() == case.buyer_wallet.lower() and tx.get("to", "").lower() == ESCROW_WALLET.lower():
                            total_deposited += Decimal(tx.get("value", "0"))
                    case.deposited_fund = total_deposited
        except Exception as e:
            print(f"Etherscan error: {e}")
            # Fall back to existing amount on error
            if case.deposited_fund is None:
                case.deposited_fund = Decimal(0)
    elif not was_funded_already:
        # Mocking the Etherscan deposit for local testing purposes if no API key
        if case.deposited_fund is None or case.deposited_fund < total_required:
            case.deposited_fund = total_required

    # Check state transitions
    if case.deposited_fund >= total_required and not was_funded_already:
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
async def transaction_action_form(
    request: Request, 
    caseId: str, 
    token: str, 
    actionType: str,
    db: Session = Depends(get_db)
):
    """Renders the form to request a payment or refund."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    is_buyer = secrets.compare_digest(token, case.buyer_token)
    is_seller = secrets.compare_digest(token, case.seller_token)
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid secure token", status_code=403)
        
    escrow_fund       = int(case.escrow_fund       or 0)
    payment_to_seller = int(case.payment_to_seller or 0)
    refund_to_buyer   = int(case.refund_to_buyer   or 0)
    remaining_eth = (escrow_fund - payment_to_seller - refund_to_buyer) / 1e18

    party = "Buyer" if is_buyer else "Seller"

    return templates.TemplateResponse("action_form.html", {
        "request": request,
        "case": case,
        "party": party,
        "token": token,
        "actionType": actionType,
        "remaining_eth": remaining_eth
    })

@router.post("/request-action", response_class=HTMLResponse)
async def request_action(
    request: Request,
    caseId: str = Form(...),
    token: str = Form(...),
    actionType: str = Form(...),
    amount_eth: float = Form(...),
    tip_eth: float = Form(0.0),
    withdrawal_eth: float = Form(0.0),
    db: Session = Depends(get_db)
):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    
    is_buyer = secrets.compare_digest(token, case.buyer_token)
    is_seller = secrets.compare_digest(token, case.seller_token)
    if not is_buyer and not is_seller: return HTMLResponse("Invalid token", status_code=403)

    amount_wei = str(int(amount_eth * 1e18))

    if actionType == "request_payment":
        if not is_seller: return HTMLResponse("Only seller can request payment", status_code=403)
        case.payment_request_time = datetime.now(timezone.utc)
        case.requested_payment_amount = amount_wei
        if tip_eth > 0: case.tip_to_seller = int(tip_eth * 1e18)
        db.commit()
        # TODO: send email to buyer with approve/dispute links
        msg = "Payment request submitted to Buyer."
    elif actionType == "request_refund":
        if not is_buyer: return HTMLResponse("Only buyer can request refund", status_code=403)
        case.refund_request_time = datetime.now(timezone.utc)
        case.requested_refund_amount = amount_wei
        if withdrawal_eth > 0: case.buyer_withdrawal = int(withdrawal_eth * 1e18)
        db.commit()
        # TODO: send email to seller with approve/dispute links
        msg = "Refund request submitted to Seller."
    else:
        return HTMLResponse("Invalid action", status_code=400)

    return templates.TemplateResponse("transaction_action.html", {
        "request": request, "case": case, "action_message": msg
    })

@router.get("/approve", response_class=HTMLResponse)
async def approve_transaction(request: Request, caseId: str, token: str, actionType: str, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    is_buyer = secrets.compare_digest(token, case.buyer_token)
    is_seller = secrets.compare_digest(token, case.seller_token)
    if not is_buyer and not is_seller: return HTMLResponse("Invalid token", status_code=403)

    if actionType == "request_payment" and is_buyer:
        # Buyer approves seller's payment
        remittance = int(case.requested_payment_amount or 0)
        case.payment_to_seller = int(case.payment_to_seller or 0) + remittance
        case.payment_request_time = None
        case.requested_payment_amount = None
        if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
            case.status = StatusEnum.TRANSFERRED_TO_SELLER
        db.commit()
        email_service.send_payment_released(
            case_id=case.case_id,
            seller_name=case.seller, seller_email=case.seller_email,
            buyer_name=case.buyer,   buyer_email=case.buyer_email,
            amount_eth=remittance / 1e18, closed=(case.status == StatusEnum.TRANSFERRED_TO_SELLER)
        )
        return HTMLResponse("Payment Approved and Released.")
        
    elif actionType == "request_refund" and is_seller:
        # Seller approves buyer's refund
        remittance = int(case.requested_refund_amount or 0)
        case.refund_to_buyer = int(case.refund_to_buyer or 0) + remittance
        case.refund_request_time = None
        case.requested_refund_amount = None
        if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
            case.status = StatusEnum.CLOSED
        db.commit()
        # email_service.send_refund_released(...) 
        return HTMLResponse("Refund Approved and Released.")
    
    return HTMLResponse("Not authorized to approve this action.", status_code=403)

@router.get("/dispute", response_class=HTMLResponse)
async def dispute_transaction(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    if not (secrets.compare_digest(token, case.buyer_token) or secrets.compare_digest(token, case.seller_token)):
        return HTMLResponse("Invalid token", status_code=403)

    case.status = StatusEnum.DISPUTED
    case.dispute_time = datetime.now(timezone.utc)
    case.payment_request_time = None
    case.refund_request_time = None
    db.commit()
    return HTMLResponse("Transaction Disputed. Case is now in DISPUTED status.")

