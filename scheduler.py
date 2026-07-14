import os
import httpx
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from database import SessionLocal
from models import Case, StatusEnum
import email_service
from gmail_ingestion import process_inbound_emails

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "default_insecure_admin_key")

def check_disputed_cases():
    print("[Scheduler] Checking for ripe disputed cases (past evidence window)...")
    db = SessionLocal()
    try:
        # User requested 12-hour timeout
        twelve_hours_ago = datetime.now(timezone.utc) - timedelta(hours=12)
        cases = db.query(Case).filter(
            Case.status == StatusEnum.DISPUTED,
            Case.dispute_time != None,
            Case.dispute_time <= twelve_hours_ago
        ).all()
        for case in cases:
            print(f"[Scheduler] Triggering adjudication for {case.case_id}")
            # Trigger via HTTP to offload the heavy agentic process
            try:
                # Use httpx post but don't wait for completion if it's long, or run in thread
                httpx.post(f"{BASE_URL}/adjudication/run", data={"caseId": case.case_id}, headers={"X-Admin-Key": ADMIN_KEY}, timeout=1.0)
            except httpx.ReadTimeout:
                # Expected since adjudication takes minutes
                pass
            except Exception as e:
                print(f"[Scheduler] Error triggering {case.case_id}: {e}")
    finally:
        db.close()

def check_transaction_timeouts():
    print("[Scheduler] Checking for 12-hr transaction timeouts...")
    db = SessionLocal()
    try:
        twelve_hours_ago = datetime.now(timezone.utc) - timedelta(hours=12)
        
        # Check payment requests
        payment_cases = db.query(Case).filter(
            Case.payment_request_time != None,
            Case.payment_request_time <= twelve_hours_ago
        ).all()
        
        for case in payment_cases:
            print(f"[Scheduler] Auto-approving payment for {case.case_id}")
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

        # Check refund requests
        refund_cases = db.query(Case).filter(
            Case.refund_request_time != None,
            Case.refund_request_time <= twelve_hours_ago
        ).all()
        
        for case in refund_cases:
            print(f"[Scheduler] Auto-approving refund for {case.case_id}")
            remittance = int(case.requested_refund_amount or 0)
            case.refund_to_buyer = int(case.refund_to_buyer or 0) + remittance
            case.refund_request_time = None
            case.requested_refund_amount = None
            if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
                case.status = StatusEnum.CLOSED
            db.commit()

    finally:
        db.close()

def start_scheduler():
    scheduler = BackgroundScheduler()
    
    # Run checks every 5 minutes
    scheduler.add_job(check_disputed_cases, 'interval', minutes=5)
    scheduler.add_job(check_transaction_timeouts, 'interval', minutes=5)
    scheduler.add_job(process_inbound_emails, 'interval', minutes=5)
    
    scheduler.start()
    return scheduler
