from fastapi import APIRouter, Request, Depends, Form, UploadFile, File as FastAPIFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import hashlib
import uuid
import os
import secrets

from database import get_db
from models import Case, StatusEnum, RoleEnum, Message, LabelEnum, File

router = APIRouter(prefix="/prosecution", tags=["Prosecution"])
templates = Jinja2Templates(directory="templates")

# Ensure the storage directory exists
STORAGE_DIR = "storage/evidence"
os.makedirs(STORAGE_DIR, exist_ok=True)

@router.get("/evidence", response_class=HTMLResponse)
async def get_evidence_form(request: Request, caseId: str, token: str, db: Session = Depends(get_db)):
    """Renders the secure portal for evidence submission."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    is_buyer = secrets.compare_digest(token, case.buyer_token)
    is_seller = secrets.compare_digest(token, case.seller_token)
    
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid secure token", status_code=403)
        
    role = "Buyer" if is_buyer else "Seller"
    
    return templates.TemplateResponse("evidence_form.html", {
        "request": request, 
        "case": case,
        "role": role,
        "token": token
    })

@router.post("/evidence", response_class=HTMLResponse)
async def post_evidence(
    request: Request, 
    caseId: str = Form(...), 
    token: str = Form(...), 
    argument: str = Form(...),
    files: list[UploadFile] = FastAPIFile(default=[]),
    db: Session = Depends(get_db)
):
    """Processes evidence submission, hashes files, and links to the case."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)
        
    is_buyer = secrets.compare_digest(token, case.buyer_token)
    is_seller = secrets.compare_digest(token, case.seller_token)
    
    if not is_buyer and not is_seller:
        return HTMLResponse("Invalid secure token", status_code=403)
        
    role_enum = RoleEnum.BUYER if is_buyer else RoleEnum.SELLER
    email = case.buyer_email if is_buyer else case.seller_email
    
    # 1. Save the text argument
    new_message = Message(
        case_id=case.case_id,
        time=datetime.now(timezone.utc),
        sender=role_enum,
        email=email,
        content=argument,
        label=LabelEnum.GENERAL
    )
    db.add(new_message)
    db.flush() # To get the new_message.id
    
    # 2. Process and hash uploaded files
    uploaded_files_info = []
    
    for upload in files:
        if not upload.filename:
            continue
            
        secure_filename = f"{uuid.uuid4()}_{upload.filename}"
        file_path = os.path.join(STORAGE_DIR, secure_filename)
        
        # Read file, write to disk, and calculate hash
        sha256_hash = hashlib.sha256()
        with open(file_path, "wb") as buffer:
            while chunk := upload.file.read(8192):
                sha256_hash.update(chunk)
                buffer.write(chunk)
                
        file_hash = sha256_hash.hexdigest()
        
        # Save file metadata to DB
        new_file = File(
            file_id=str(uuid.uuid4()),
            case_id=case.case_id,
            message_id=new_message.id,
            time=datetime.now(timezone.utc),
            submitter=role_enum,
            email=email,
            original_name=upload.filename,
            secure_name=secure_filename,
            hash=file_hash
        )
        db.add(new_file)
        
        uploaded_files_info.append({
            "filename": upload.filename,
            "hash": file_hash
        })
        
    db.commit()
    
    return templates.TemplateResponse("evidence_success.html", {
        "request": request,
        "case": case,
        "files_info": uploaded_files_info
    })

@router.post("/escalate", response_class=HTMLResponse)
async def escalate_to_adjudication(
    request: Request,
    caseId: str = Form(...),
    token: str = Form(...),
    db: Session = Depends(get_db)
):
    """Ends the Prosecution phase and moves case to DISPUTED status.

    NOTE: This mirrors the n8n Prosecution workflow. Setting DISPUTED + dispute_time
    here signals the Adjudication scheduler to pick this case up. The adjudication
    workflow will then separately set PROCESSING + adjudication_time when it begins work.
    """
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)

    if case.status not in [StatusEnum.PENDING, StatusEnum.SIGNED, StatusEnum.EFFECTIVE]:
        return HTMLResponse(f"Cannot escalate. Case is currently in {case.status.value} status.", status_code=400)

    if not (secrets.compare_digest(token, case.buyer_token) or secrets.compare_digest(token, case.seller_token)):
        return HTMLResponse("Unauthorized token.", status_code=403)

    # Matches n8n "Record Dispute1" node: Status=DISPUTED, Dispute Time=now
    case.status = StatusEnum.DISPUTED
    case.dispute_time = datetime.now(timezone.utc)
    db.commit()

    return HTMLResponse(f"""
        <div style='font-family: sans-serif; text-align: center; margin-top: 50px;'>
            <h1>Case Escalated to Dispute</h1>
            <p>Case {caseId} is now <strong>DISPUTED</strong>. The AI Adjudication scheduler
            will pick this case up after the 7-day evidence window closes.</p>
        </div>
    """)
