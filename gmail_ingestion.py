import imaplib
import email
import email.header
import re
import os
import hashlib
import uuid
import email_service
from datetime import datetime, timezone, timedelta
from database import SessionLocal
from models import Case, Message, File, RoleEnum, LabelEnum, StatusEnum

GMAIL_USERNAME = os.environ.get("GMAIL_USERNAME", "Law.Economist@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
IMAP_SERVER = "imap.gmail.com"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

def process_inbound_emails():
    print("[Gmail Ingestion] Checking for unread emails...")
    if not GMAIL_APP_PASSWORD:
        print("[Gmail Ingestion] Error: GMAIL_APP_PASSWORD not set. Skipping email ingestion.")
        return

    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        # Search for all unread emails
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages[0]:
            print("[Gmail Ingestion] No unread emails found.")
            mail.logout()
            return

        email_ids = messages[0].split()
        
        db = SessionLocal()
        
        for e_id in email_ids:
            # Fetch email
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # Parse Subject
                    subject = ""
                    if "Subject" in msg:
                        subject_header = email.header.decode_header(msg["Subject"])
                        for part, encoding in subject_header:
                            if isinstance(part, bytes):
                                subject += part.decode(encoding or "utf-8")
                            elif isinstance(part, str):
                                subject += part
                                
                    # Parse Sender
                    sender_email = ""
                    if "From" in msg:
                        from_header = email.header.decode_header(msg["From"])[0]
                        sender_raw = from_header[0]
                        if isinstance(sender_raw, bytes):
                            sender_raw = sender_raw.decode(from_header[1] or "utf-8")
                        # Extract just the email address
                        email_match = re.search(r'<(.+?)>', sender_raw)
                        sender_email = email_match.group(1) if email_match else sender_raw
                    
                    # Extract Case ID from Subject (e.g., 1234ABCD)
                    case_match = re.search(r'\b[A-Fa-f0-9]{8}\b', subject)
                    if not case_match:
                        print(f"[Gmail Ingestion] Skipping email: No Case ID in subject '{subject}'")
                        continue
                        
                    case_id = case_match.group(0).upper()
                    case = db.query(Case).filter(Case.case_id == case_id).first()
                    if not case:
                        print(f"[Gmail Ingestion] Skipping email: Case {case_id} not found in DB.")
                        continue
                        
                    # Deadline Enforcement
                    now = datetime.now(timezone.utc)
                    if case.status != StatusEnum.DISPUTED:
                        print(f"[Gmail Ingestion] Skipping email: Case {case_id} is not in DISPUTED state (Status: {case.status}).")
                        if hasattr(email_service, 'send_late_submission_reply'):
                            email_service.send_late_submission_reply(case.case_id, sender_email)
                        continue
                        
                    if case.dispute_time and now > (case.dispute_time + timedelta(days=7)):
                        print(f"[Gmail Ingestion] Skipping email: Case {case_id} 7-day evidence window has expired.")
                        if hasattr(email_service, 'send_late_submission_reply'):
                            email_service.send_late_submission_reply(case.case_id, sender_email)
                        continue
                        
                    # Determine Role
                    sender_role = RoleEnum.SYSTEM
                    if sender_email.lower() == case.seller_email.lower():
                        sender_role = RoleEnum.SELLER
                    elif sender_email.lower() == case.buyer_email.lower():
                        sender_role = RoleEnum.BUYER

                    # Extract Body and Attachments
                    body_text = ""
                    attachments = []
                    
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            content_disposition = str(part.get("Content-Disposition"))
                            
                            if content_type == "text/plain" and "attachment" not in content_disposition:
                                try:
                                    body_text += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
                                except:
                                    pass
                            elif "attachment" in content_disposition or part.get_filename():
                                filename = part.get_filename()
                                if filename:
                                    decoded_filename = email.header.decode_header(filename)[0]
                                    if isinstance(decoded_filename[0], bytes):
                                        filename = decoded_filename[0].decode(decoded_filename[1] or "utf-8")
                                    else:
                                        filename = decoded_filename[0]
                                        
                                    file_data = part.get_payload(decode=True)
                                    attachments.append({"filename": filename, "data": file_data})
                    else:
                        try:
                            body_text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8")
                        except:
                            pass

                    if not body_text.strip() and not attachments:
                        continue

                    # Insert Message
                    now = datetime.now(timezone.utc)
                    db_msg = Message(
                        case_id=case_id,
                        time=now,
                        sender=sender_role,
                        email=sender_email,
                        content=body_text.strip() or "[Attachment Only]",
                        label=LabelEnum.EVIDENCE
                    )
                    db.add(db_msg)
                    db.commit()
                    db.refresh(db_msg)
                    
                    # Process Attachments
                    for att in attachments:
                        file_data = att["data"]
                        original_name = att["filename"]
                        file_hash = hashlib.sha256(file_data).hexdigest()
                        secure_filename = f"{uuid.uuid4()}_{original_name}"
                        file_path = os.path.join(UPLOAD_DIR, secure_filename)
                        
                        with open(file_path, "wb") as f:
                            f.write(file_data)
                            
                        db_file = File(
                            file_id=str(uuid.uuid4()),
                            case_id=case_id,
                            message_id=db_msg.id,
                            time=now,
                            submitter=sender_role,
                            email=sender_email,
                            original_name=original_name,
                            secure_name=secure_filename,
                            hash=file_hash
                        )
                        db.add(db_file)
                        
                    db.commit()
                    print(f"[Gmail Ingestion] Logged email from {sender_email} for {case_id} with {len(attachments)} attachments.")
                    
        db.close()
        mail.logout()
    except Exception as e:
        print(f"[Gmail Ingestion] Error: {e}")
