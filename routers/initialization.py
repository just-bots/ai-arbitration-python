import hashlib
import os
import secrets
import uuid

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db
from models import Case, Message, File as DBFile, StatusEnum, LabelEnum, RoleEnum, ResponseEnum
import email_service
import validators

# Platform constants from environment (mirrors n8n $vars)
ESCROW_WALLET    = os.environ.get("ESCROW_WALLET", "0xdd2Be83773B37564581c2C3Cd2282d34A3E4e584")
PROCESSING_FEE   = int(os.environ.get("PROCESSING_FEE", "1000000000000000"))  # Wei; default 0.001 ETH
BASE_URL         = os.environ.get("BASE_URL", "http://localhost:8000/")

router = APIRouter()
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@router.get("/", response_class=HTMLResponse)
async def create_case_form(request: Request):
    """Renders the HTML form to register a new contract."""
    return templates.TemplateResponse("create_case.html", {"request": request})

@router.post("/create-case", response_class=HTMLResponse)
async def create_case(
    request: Request,
    seller_name: str = Form(...),
    seller_email: str = Form(...),
    seller_wallet: str = Form(default=""),   # Optional at creation — submitted via wallet form later
    buyer_name: str = Form(...),
    buyer_email: str = Form(...),
    buyer_wallet: str = Form(default=""),    # Optional at creation — submitted via wallet form later
    escrow_fund_eth: float = Form(...),      # Required — n8n: EscrowFundETH field
    contract_text: str = Form(...),          # Required — full contract text for AI adjudication
    contract_file: Optional[UploadFile] = File(default=None),  # Optional PDF attachment
    db: Session = Depends(get_db)
):
    """Handles the form submission, saves the file (if any), and inserts into DB."""

    if escrow_fund_eth <= 0:
        return HTMLResponse("Escrow fund must be strictly positive.", status_code=400)
    
    escrow_fund_wei = int(Decimal(str(escrow_fund_eth)) * Decimal(10**18))
    # Regex validation for wallet
    if seller_wallet:
        is_valid, err = validators.validate_ethereum_address(seller_wallet)
        if not is_valid: return HTMLResponse(err, status_code=400)
    if buyer_wallet:
        is_valid, err = validators.validate_ethereum_address(buyer_wallet)
        if not is_valid: return HTMLResponse(err, status_code=400)

    # 1. Generate IDs and Tokens
    case_id      = secrets.token_hex(4).upper()
    seller_token = secrets.token_urlsafe(16)
    buyer_token  = secrets.token_urlsafe(16)
    now          = datetime.now(timezone.utc)

    # 2. Save uploaded file (if provided)
    secure_filename = None
    file_hash       = None
    original_name   = None
    file_path       = None

    if contract_file and contract_file.filename:
        is_valid, err = validators.validate_file_upload(contract_file.filename, contract_file.size, contract_file.content_type)
        if not is_valid: return HTMLResponse(err, status_code=400)
        
        file_content    = await contract_file.read()
        file_hash       = hashlib.sha256(file_content).hexdigest()
        original_name   = contract_file.filename
        secure_filename = f"{uuid.uuid4()}_{original_name}"
        file_path       = os.path.join(UPLOAD_DIR, secure_filename)
        with open(file_path, "wb") as f:
            f.write(file_content)

    folder_link = f"storage/evidence/{case_id}"
    os.makedirs(folder_link, exist_ok=True)

    # 3. Create Case Record
    new_case = Case(
        case_id=case_id,
        created_at=now,
        seller=seller_name,
        buyer=buyer_name,
        seller_email=seller_email,
        buyer_email=buyer_email,
        seller_token=seller_token,
        buyer_token=buyer_token,
        seller_wallet=seller_wallet or None,
        buyer_wallet=buyer_wallet or None,
        contract_text=contract_text,
        folder_link=folder_link,
        # Financials in Wei (n8n: EscrowFund in wei, Fee = $vars.PROCESSING_FEE in wei)
        escrow_fund=escrow_fund_wei,
        fee=PROCESSING_FEE,
        escrow_address=ESCROW_WALLET,
        # Accumulators start at 0
        payment_to_seller=0,
        refund_to_buyer=0,
        tip_to_seller=0,
        buyer_withdrawal=0,
        status=StatusEnum.PENDING
    )
    db.add(new_case)
    db.commit()
    db.refresh(new_case)
    
    # 4. Create Initial Setup Message — stores contract text for AI adjudication
    setup_message = Message(
        case_id=case_id,
        time=now,
        sender=RoleEnum.SYSTEM,
        email="system@ai-arbitration.local",
        content=f"CONTRACT TEXT:\n\n{contract_text}",
        label=LabelEnum.SETUP
    )
    db.add(setup_message)
    db.commit()
    db.refresh(setup_message)

    # 5. Create File Record (only if a file was uploaded)
    if secure_filename:
        db_file = DBFile(
            file_id=str(uuid.uuid4()),
            case_id=case_id,
            message_id=setup_message.id,
            time=now,
            submitter=RoleEnum.SYSTEM,
            email="system@ai-arbitration.local",
            original_name=original_name,
            secure_name=secure_filename,
            hash=file_hash
        )
        db.add(db_file)
        db.commit()

    # 6. Send registration emails to both parties
    escrow_eth   = escrow_fund_eth
    preview_text = contract_text

    email_service.send_case_registered(
        case_id=case_id, party="Seller", name=seller_name, email=seller_email,
        token=seller_token, counterpart_name=buyer_name,
        escrow_eth=escrow_eth, contract_text_preview=preview_text,
        attachment_path=file_path
    )
    email_service.send_case_registered(
        case_id=case_id, party="Buyer", name=buyer_name, email=buyer_email,
        token=buyer_token, counterpart_name=seller_name,
        escrow_eth=escrow_eth, contract_text_preview=preview_text,
        attachment_path=file_path
    )

    # Redirect to success page
    return RedirectResponse(url=f"/success/{case_id}", status_code=303)

@router.get("/success/{case_id}", response_class=HTMLResponse)
async def success_page(request: Request, case_id: str, db: Session = Depends(get_db)):
    """Renders the success confirmation page."""
    case = db.query(Case).filter(Case.case_id == case_id).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    return templates.TemplateResponse("success.html", {"request": request, "case": case})

@router.get("/terms", response_class=HTMLResponse)
async def view_terms(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    """Displays the full legal agreement text."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case: return HTMLResponse("Case not found", status_code=404)
    is_seller, _, _ = validators.validate_party_token(case, "seller", token)
    is_buyer, _, _ = validators.validate_party_token(case, "buyer", token)
    if not is_seller and not is_buyer:
        return HTMLResponse("Invalid token", status_code=403)
    return templates.TemplateResponse("terms.html", {"request": request, "case": case})

@router.get("/response", response_class=HTMLResponse)
async def signature_confirm(
    request: Request, 
    caseId: str, 
    party: str, 
    action: str, 
    token: str, 
    db: Session = Depends(get_db)
):
    """Renders the 2-step confirmation page showing contract terms before accepting/declining."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    # Validate token
    is_valid, err, party_lower = validators.validate_party_token(case, party, token)
    if not is_valid:
        return HTMLResponse(err, status_code=403 if err and "token" in err else 400)
        
    # Duplicate/Late-Response Guard
    has_responded, response_val = validators.check_party_already_responded(case, party_lower)
    if has_responded:
        return HTMLResponse(f"You have already submitted a response: {response_val}", status_code=400)

    if action.lower() not in ["accept", "decline"]:
        return HTMLResponse("Invalid action parameter.", status_code=400)

    return templates.TemplateResponse("response_confirm.html", {
        "request": request, 
        "case": case, 
        "party": party,
        "action": action.lower(),
        "token": token
    })

@router.post("/response-submit", response_class=HTMLResponse)
async def signature_submit(
    request: Request, 
    caseId: str = Form(...), 
    party: str = Form(...), 
    action: str = Form(...), 
    token: str = Form(...), 
    db: Session = Depends(get_db)
):
    """Processes the form submission from the confirmation page."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    # Validate token
    is_valid, err, party_lower = validators.validate_party_token(case, party, token)
    if not is_valid:
        return HTMLResponse(err, status_code=403 if err and "token" in err else 400)
        
    # Duplicate/Late-Response Guard
    has_responded, _ = validators.check_party_already_responded(case, party_lower)
    if has_responded:
        return HTMLResponse("Already responded.", status_code=400)
        
    response_val = ResponseEnum.ACCEPT if action.lower() == "accept" else ResponseEnum.DECLINE
    
    if party_lower == "seller":
        case.seller_response = response_val
    else:
        case.buyer_response = response_val
        
    # Update Status
    if case.seller_response == ResponseEnum.ACCEPT and case.buyer_response == ResponseEnum.ACCEPT:
        case.status = StatusEnum.SIGNED
    elif case.seller_response == ResponseEnum.DECLINE or case.buyer_response == ResponseEnum.DECLINE:
        case.status = StatusEnum.DECLINED
        
    db.commit()

    # Send status emails
    if case.status == StatusEnum.SIGNED:
        from decimal import Decimal
        total_eth = float((case.escrow_fund or Decimal(0)) + (case.fee or Decimal(0))) / 1e18
        escrow_eth = float(case.escrow_fund or Decimal(0)) / 1e18
        email_service.send_contract_signed(
            case_id=case.case_id,
            seller_name=case.seller, seller_email=case.seller_email, seller_token=case.seller_token,
            buyer_name=case.buyer,   buyer_email=case.buyer_email,   buyer_token=case.buyer_token,
            escrow_address=case.escrow_address or "(not set)",
            escrow_eth=escrow_eth, total_eth=total_eth
        )
    elif case.status == StatusEnum.DECLINED:
        email_service.send_contract_declined(
            case_id=case.case_id, declining_party=party,
            seller_name=case.seller, seller_email=case.seller_email,
            buyer_name=case.buyer,   buyer_email=case.buyer_email
        )

    return templates.TemplateResponse("response_status.html", {"request": request, "case": case, "party": party})

@router.get("/wallet", response_class=HTMLResponse)
async def wallet_form(
    request: Request, 
    caseId: str, 
    party: str, 
    token: str, 
    db: Session = Depends(get_db)
):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    is_valid, err, _ = validators.validate_party_token(case, party, token)
    if not is_valid:
        return HTMLResponse(err, status_code=403)
        
    return templates.TemplateResponse("wallet_form.html", {"request": request, "case": case, "party": party, "token": token})

@router.post("/wallet-submit", response_class=HTMLResponse)
async def wallet_submit(
    request: Request,
    caseId: str = Form(...),
    party: str = Form(...),
    token: str = Form(...),
    wallet_address: str = Form(...),
    db: Session = Depends(get_db)
):
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    is_valid, err, _ = validators.validate_party_token(case, party, token)
    if not is_valid:
        return HTMLResponse(err, status_code=403)

    # Server-side validation for wallet
    if wallet_address:
        is_valid, err = validators.validate_ethereum_address(wallet_address)
        if not is_valid: return HTMLResponse(err, status_code=400)
        
    if party.lower() == "seller":
        case.seller_wallet = wallet_address
    else:
        case.buyer_wallet = wallet_address

    db.commit()

    # Confirm wallet registration via email
    name  = case.seller if party.lower() == "seller" else case.buyer
    email = case.seller_email if party.lower() == "seller" else case.buyer_email
    email_service.send_wallet_confirmed(
        case_id=caseId, party=party, name=name, email=email, wallet=wallet_address
    )

    return templates.TemplateResponse("response_status.html",
        {"request": request, "case": case, "party": party, "wallet_updated": True})
