from fastapi import APIRouter, Request, Depends, Form
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
from models import Case, StatusEnum, RoleEnum
import email_service
import validators

router = APIRouter(prefix="/transactions", tags=["Transactions"])
templates = Jinja2Templates(directory="templates")

async def _verify_deposit_logic(request: Request, caseId: str, db: Session):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    total_required = (case.escrow_fund or Decimal(0)) + (case.fee or Decimal(0))
    
    # Use Etherscan if API key is provided
    was_funded_already = case.deposited_fund is not None and case.deposited_fund >= total_required

    if ETHERSCAN_API_KEY and case.buyer_wallet and not was_funded_already:
        try:
            CHAIN_ID = os.environ.get("CHAIN_ID", "1")
            timestamp = int(case.created_at.timestamp())
            
            # Fetch startblock to optimize scan
            block_url = f"https://api.etherscan.io/v2/api?chainid={CHAIN_ID}&module=block&action=getblocknobytime&timestamp={timestamp}&closest=before&apikey={ETHERSCAN_API_KEY}"
            async with httpx.AsyncClient() as client:
                b_resp = await client.get(block_url)
                b_data = b_resp.json()
                startblock = b_data.get("result", "0") if b_data.get("status") == "1" else "0"

            # Fetch transactions
            url = f"https://api.etherscan.io/v2/api?chainid={CHAIN_ID}&module=account&action=txlist&address={ESCROW_WALLET}&startblock={startblock}&endblock=99999999&sort=asc&apikey={ETHERSCAN_API_KEY}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
                data = resp.json()
                if data.get("status") == "1":
                    total_deposited = Decimal(0)
                    for tx in data.get("result", []):
                        tx_timestamp = int(tx.get("timeStamp", "0"))
                        if tx_timestamp < timestamp:
                            continue
                            
                        # Match buyer to escrow deposits AND match hex input data to Case ID
                        input_hex = tx.get("input", "").lower()
                        expected_hex = "0x" + case.case_id.split("-")[-1].lower() if "-" in case.case_id else "0x" + case.case_id.lower()
                        
                        if tx.get("from", "").lower() == case.buyer_wallet.lower() and tx.get("to", "").lower() == ESCROW_WALLET.lower():
                            if input_hex == expected_hex:
                                total_deposited += Decimal(tx.get("value", "0"))
                    case.deposited_fund = total_deposited
        except Exception as e:
            print(f"Etherscan error: {e}")
            if case.deposited_fund is None:
                case.deposited_fund = Decimal(0)
    elif not ETHERSCAN_API_KEY and not was_funded_already:
        # Mock deposit
        case.deposited_fund = total_required

    current_deposit = case.deposited_fund or Decimal(0)

    # Check state transitions
    if current_deposit >= total_required and not was_funded_already:
        if case.status == StatusEnum.SIGNED:
            case.status = StatusEnum.EFFECTIVE
        db.commit()

        # Calculate excess ETH if any
        excess_wei = max(0, int(current_deposit) - int(total_required))
        excess_eth = excess_wei / 1e18

        # Send funding confirmed emails
        email_service.send_escrow_confirmed(
            case_id=caseId,
            seller_name=case.seller, seller_email=case.seller_email, seller_token=case.seller_token,
            buyer_name=case.buyer,   buyer_email=case.buyer_email,   buyer_token=case.buyer_token,
            excess_eth=excess_eth
        )

    is_funded = current_deposit >= total_required

    return templates.TemplateResponse("deposit_status.html", {
        "request": request, 
        "case": case,
        "is_funded": is_funded,
        "total_required": total_required,
        "token": getattr(request.state, "token", "") # This will be populated from query param or form
    })

@router.get("/verify", response_class=HTMLResponse)
async def verify_deposit_get(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    """Triggers Etherscan Verification immediately when accessed via link."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller: return HTMLResponse("Invalid token", status_code=403)
    request.state.token = token
    return await _verify_deposit_logic(request, caseId, db)

@router.post("/verify", response_class=HTMLResponse)
async def verify_deposit_post(request: Request, caseId: str = Form(...), token: str = Form(...), db: Session = Depends(get_db)):
    """Triggers Etherscan Verification when form button is clicked."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller: return HTMLResponse("Invalid token", status_code=403)
    request.state.token = token
    return await _verify_deposit_logic(request, caseId, db)

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
        
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid secure token", status_code=403)
        
    if actionType in ["withdraw_excess", "tip_excess"]:
        total_required = (case.escrow_fund or 0) + (case.fee or 0)
        excess_wei = max(0, int(case.deposited_fund or 0) - int(total_required) - int(case.buyer_withdrawal or 0) - int(case.tip_to_seller or 0))
        remaining_eth = excess_wei / 1e18
    else:
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
    
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller: return HTMLResponse("Invalid token", status_code=403)

    amount_wei = str(int(Decimal(str(amount_eth)) * Decimal(10**18)))

    if actionType == "request_payment":
        if not is_seller: return HTMLResponse("Only seller can request payment", status_code=403)
        case.payment_request_time = datetime.now(timezone.utc)
        case.requested_payment_amount = amount_wei
        db.commit()
        if hasattr(email_service, "send_payment_requested"):
            email_service.send_payment_requested(case.case_id, case.seller, case.seller_email, case.buyer, case.buyer_email, case.buyer_token)
        msg = "Payment request submitted to Buyer."
    elif actionType == "request_refund":
        if not is_buyer: return HTMLResponse("Only buyer can request refund", status_code=403)
        case.refund_request_time = datetime.now(timezone.utc)
        case.requested_refund_amount = amount_wei
        db.commit()
        if hasattr(email_service, "send_refund_requested"):
            email_service.send_refund_requested(case.case_id, case.seller, case.seller_email, case.buyer, case.buyer_email)
        msg = "Refund request submitted to Seller."
    elif actionType == "send_payment":
        if not is_buyer: return HTMLResponse("Only buyer can send payment", status_code=403)
        if not case.seller_wallet: return HTMLResponse("No Seller Wallet provided", status_code=400)
        
        available = max(0, int(case.escrow_fund or 0) - int(case.payment_to_seller or 0) - int(case.refund_to_buyer or 0))
        remittance = min(int(amount_wei), available)
        
        if remittance > 0:
            from blockchain import transfer_funds
            try:
                if case.seller_wallet:
                    await transfer_funds(case.seller_wallet, remittance, case.case_id)
            except Exception as e:
                return HTMLResponse(f"Blockchain Transfer Failed: {e}", status_code=500)
            
        case.payment_to_seller = int(case.payment_to_seller or 0) + remittance
            
        if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
            case.status = StatusEnum.CLOSED
        db.commit()
        if hasattr(email_service, "send_payment_released"):
            email_service.send_payment_released(case.case_id, case.seller, case.seller_email, case.buyer, case.buyer_email, remittance / 1e18, closed=(case.status == StatusEnum.CLOSED))
        msg = "Payment successfully sent to Seller."
    elif actionType == "send_refund":
        if not is_seller: return HTMLResponse("Only seller can send refund", status_code=403)
        if not case.buyer_wallet: return HTMLResponse("No Buyer Wallet provided", status_code=400)
        
        available = max(0, int(case.escrow_fund or 0) - int(case.payment_to_seller or 0) - int(case.refund_to_buyer or 0))
        remittance = min(int(amount_wei), available)
        
        if remittance > 0:
            from blockchain import transfer_funds
            try:
                if case.buyer_wallet:
                    await transfer_funds(case.buyer_wallet, remittance, case.case_id)
            except Exception as e:
                return HTMLResponse(f"Blockchain Transfer Failed: {e}", status_code=500)
            
        case.refund_to_buyer = int(case.refund_to_buyer or 0) + remittance
            
        if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
            case.status = StatusEnum.CLOSED
        db.commit()
        if hasattr(email_service, "send_refund_released"):
            email_service.send_refund_released(case.case_id, case.seller, case.seller_email, case.buyer, case.buyer_email, remittance / 1e18, closed=(case.status == StatusEnum.CLOSED))
        msg = "Refund successfully sent to Buyer."
    elif actionType == "withdraw_excess":
        if not is_buyer: return HTMLResponse("Only buyer can withdraw excess", status_code=403)
        if not case.buyer_wallet: return HTMLResponse("No Buyer Wallet provided", status_code=400)
        total_required = (case.escrow_fund or 0) + (case.fee or 0)
        excess_wei = max(0, int(case.deposited_fund or 0) - int(total_required) - int(case.buyer_withdrawal or 0) - int(case.tip_to_seller or 0))
        amount_wei_int = int(amount_wei)
        
        if amount_wei_int <= 0 or amount_wei_int > excess_wei:
            return HTMLResponse("Invalid or insufficient excess funds.", status_code=400)
            
        from blockchain import transfer_funds
        try:
            await transfer_funds(case.buyer_wallet, amount_wei_int, case.case_id)
            case.buyer_withdrawal = int(case.buyer_withdrawal or 0) + amount_wei_int
            db.commit()
        except Exception as e:
            return HTMLResponse(f"Blockchain Transfer Failed: {e}", status_code=500)
            
        msg = f"Successfully withdrawn {amount_eth} ETH from excess funds."
    elif actionType == "tip_excess":
        if not is_buyer: return HTMLResponse("Only buyer can tip excess", status_code=403)
        if not case.seller_wallet: return HTMLResponse("No Seller Wallet provided", status_code=400)
        total_required = (case.escrow_fund or 0) + (case.fee or 0)
        excess_wei = max(0, int(case.deposited_fund or 0) - int(total_required) - int(case.buyer_withdrawal or 0) - int(case.tip_to_seller or 0))
        amount_wei_int = int(amount_wei)
        
        if amount_wei_int <= 0 or amount_wei_int > excess_wei:
            return HTMLResponse("Invalid or insufficient excess funds.", status_code=400)
            
        from blockchain import transfer_funds
        try:
            await transfer_funds(case.seller_wallet, amount_wei_int, case.case_id)
            case.tip_to_seller = int(case.tip_to_seller or 0) + amount_wei_int
            db.commit()
        except Exception as e:
            return HTMLResponse(f"Blockchain Transfer Failed: {e}", status_code=500)
            
        msg = f"Successfully sent {amount_eth} ETH as a tip to the Seller."
    else:
        return HTMLResponse("Invalid action", status_code=400)

    return templates.TemplateResponse("transaction_action.html", {
        "request": request, "case": case, "action_message": msg
    })

@router.get("/approve-confirm", response_class=HTMLResponse)
async def approve_transaction_confirm(request: Request, caseId: str, token: str, actionType: str, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    action_title = "Approve Payment to Seller" if actionType == "request_payment" else "Approve Refund to Buyer"
    return templates.TemplateResponse("action_confirm.html", {
        "request": request, "case": case, "action_title": action_title, "post_url": "/transactions/approve", "token": token, "actionType": actionType
    })

@router.post("/approve", response_class=HTMLResponse)
async def approve_transaction(request: Request, caseId: str = Form(...), token: str = Form(...), actionType: str = Form(...), db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller: return HTMLResponse("Invalid token", status_code=403)

    if actionType == "request_payment" and is_buyer:
        # Buyer approves seller's payment
        requested = int(case.requested_payment_amount or 0)
        available = max(0, int(case.escrow_fund or 0) - int(case.payment_to_seller or 0) - int(case.refund_to_buyer or 0))
        remittance = min(requested, available)
        
        if remittance > 0 and not case.seller_wallet:
            return HTMLResponse("No Seller Wallet provided", status_code=400)
        
        if remittance > 0:
            from blockchain import transfer_funds
            try:
                await transfer_funds(case.seller_wallet, remittance, case.case_id)
            except Exception as e:
                return HTMLResponse(f"Blockchain Transfer Failed: {e}", status_code=500)
            
        case.payment_to_seller = int(case.payment_to_seller or 0) + remittance
        case.payment_request_time = None
        case.requested_payment_amount = None
        if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
            case.status = StatusEnum.CLOSED
        db.commit()
        email_service.send_payment_released(
            case_id=case.case_id,
            seller_name=case.seller, seller_email=case.seller_email,
            buyer_name=case.buyer,   buyer_email=case.buyer_email,
            amount_eth=remittance / 1e18, closed=(case.status == StatusEnum.CLOSED)
        )
        return HTMLResponse("Payment Approved and Released.")
        
    elif actionType == "request_refund" and is_seller:
        # Seller approves buyer's refund
        requested = int(case.requested_refund_amount or 0)
        available = max(0, int(case.escrow_fund or 0) - int(case.payment_to_seller or 0) - int(case.refund_to_buyer or 0))
        remittance = min(requested, available)
        
        if remittance > 0 and not case.buyer_wallet:
            return HTMLResponse("No Buyer Wallet provided", status_code=400)
        
        if remittance > 0:
            from blockchain import transfer_funds
            try:
                await transfer_funds(case.buyer_wallet, remittance, case.case_id)
            except Exception as e:
                return HTMLResponse(f"Blockchain Transfer Failed: {e}", status_code=500)
            
        case.refund_to_buyer = int(case.refund_to_buyer or 0) + remittance
        case.refund_request_time = None
        case.requested_refund_amount = None
            
        if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
            case.status = StatusEnum.CLOSED
        db.commit()
        
        if hasattr(email_service, "send_refund_released"):
            email_service.send_refund_released(
                case_id=case.case_id,
                seller_name=case.seller, seller_email=case.seller_email,
                buyer_name=case.buyer,   buyer_email=case.buyer_email,
                amount_eth=remittance / 1e18, closed=(case.status == StatusEnum.CLOSED)
            )
        return HTMLResponse("Refund Approved and Released.")
    
    return HTMLResponse("Not authorized to approve this action.", status_code=403)

@router.get("/dispute-confirm", response_class=HTMLResponse)
async def dispute_transaction_confirm(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    return templates.TemplateResponse("action_confirm.html", {
        "request": request, "case": case, "action_title": "Dispute Transaction", "post_url": "/transactions/dispute", "token": token
    })

@router.post("/dispute", response_class=HTMLResponse)
async def dispute_transaction(request: Request, caseId: str = Form(...), token: str = Form(...), db: Session = Depends(get_db)):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid token", status_code=403)
        
    if case.status not in [StatusEnum.EFFECTIVE, StatusEnum.SIGNED]:
        return HTMLResponse(f"Cannot dispute. Case is currently in {case.status.value} status.", status_code=400)

    case.status = StatusEnum.DISPUTED
    case.dispute_time = datetime.now(timezone.utc)
    case.payment_request_time = None
    case.refund_request_time = None
    db.commit()
    return HTMLResponse("Transaction Disputed. Case is now in DISPUTED status.")

