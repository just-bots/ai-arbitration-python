import os
import httpx
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from database import SessionLocal
from models import Case, StatusEnum
import email_service
from gmail_ingestion import process_inbound_emails

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
ADMIN_KEY = os.environ["ADMIN_KEY"]

def check_disputed_cases():
    print("[Scheduler] Checking for ripe disputed cases (past evidence window)...")
    db = SessionLocal()
    try:
        # User requested 7-day timeout for disputes
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        cases = db.query(Case).filter(
            Case.status == StatusEnum.DISPUTED,
            Case.dispute_time.isnot(None),
            Case.dispute_time <= seven_days_ago
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
    print("[Scheduler] Checking for 7-day transaction timeouts...")
    db = SessionLocal()
    try:
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        
        # Check payment requests
        payment_cases = db.query(Case).filter(
            Case.payment_request_time.isnot(None),
            Case.payment_request_time <= seven_days_ago
        ).all()
        
        for case in payment_cases:
            print(f"[Scheduler] Auto-approving payment for {case.case_id}")
            remittance = int(case.requested_payment_amount or 0)
            
            import asyncio
            from blockchain import transfer_funds
            try:
                if remittance > 0 and case.seller_wallet:
                    asyncio.run(transfer_funds(case.seller_wallet, remittance, case.case_id))
            except Exception as e:
                print(f"[Scheduler] Blockchain Transfer Failed for {case.case_id}: {e}")
                continue
                
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
            Case.refund_request_time.isnot(None),
            Case.refund_request_time <= seven_days_ago
        ).all()
        
        for case in refund_cases:
            print(f"[Scheduler] Auto-approving refund for {case.case_id}")
            remittance = int(case.requested_refund_amount or 0)
            
            import asyncio
            from blockchain import transfer_funds
            try:
                if remittance > 0 and case.buyer_wallet:
                    asyncio.run(transfer_funds(case.buyer_wallet, remittance, case.case_id))
            except Exception as e:
                print(f"[Scheduler] Blockchain Transfer Failed for {case.case_id}: {e}")
                continue
                
            case.refund_to_buyer = int(case.refund_to_buyer or 0) + remittance
            case.refund_request_time = None
            case.requested_refund_amount = None
            if (int(case.payment_to_seller or 0) + int(case.refund_to_buyer or 0)) >= int(case.escrow_fund or 0):
                case.status = StatusEnum.CLOSED
            db.commit()

    finally:
        db.close()

import asyncio
from blockchain import transfer_funds

def hourly_auto_distribute():
    print("[Scheduler] Checking for DISTRIBUTED cases to execute on-chain transfer...")
    db = SessionLocal()
    try:
        # Immediate Terminal States check for DECIDED_LOCKED cases
        immediate_close = db.query(Case).filter(Case.status == StatusEnum.DECIDED_LOCKED).all()
        for case in immediate_close:
            escrow_fund = int(case.escrow_fund or 0)
            payment_to_seller = int(case.payment_to_seller or 0)
            refund_to_buyer = int(case.refund_to_buyer or 0)
            available = escrow_fund - payment_to_seller - refund_to_buyer
            if available <= 0:
                print(f"[Scheduler] Immediate close (No Escrow) for {case.case_id}")
                case.status = StatusEnum.CLOSED_NO_ESCROW
                db.commit()
            elif int(case.seller_award or 0) == 0 and int(case.buyer_award or 0) == 0:
                print(f"[Scheduler] Immediate close (No Award) for {case.case_id}")
                case.status = StatusEnum.CLOSED_NO_AWARD
                db.commit()

        # Also check for DECIDED_LOCKED cases where 7-day appeal window has lapsed
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        lapsed_cases = db.query(Case).filter(
            Case.status == StatusEnum.DECIDED_LOCKED,
            Case.determination_time.isnot(None),
            Case.determination_time <= seven_days_ago
        ).all()
        
        for case in lapsed_cases:
            print(f"[Scheduler] 7-day objection window closed for {case.case_id}. Distributing...")
            case.status = StatusEnum.DISTRIBUTED
            db.commit()
            
        ready_cases = db.query(Case).filter(Case.status == StatusEnum.DISTRIBUTED).all()
        
        for case in ready_cases:
            try:
                seller_award = int(case.seller_award or 0)
                buyer_award = int(case.buyer_award or 0)
                
                # Terminal States: No Escrow vs No Award
                escrow_fund = int(case.escrow_fund or 0)
                payment_to_seller = int(case.payment_to_seller or 0)
                refund_to_buyer = int(case.refund_to_buyer or 0)
                available = escrow_fund - payment_to_seller - refund_to_buyer
                
                if available <= 0:
                    case.status = StatusEnum.CLOSED_NO_ESCROW
                    db.commit()
                    continue
                    
                if seller_award == 0 and buyer_award == 0:
                    case.status = StatusEnum.CLOSED_NO_AWARD
                    db.commit()
                    continue

                # Process Seller Transfer
                if seller_award > 0 and int(case.seller_payout or 0) == 0:
                    if case.seller_wallet:
                        try:
                            asyncio.run(transfer_funds(case.seller_wallet, seller_award, case.case_id))
                            case.seller_payout = seller_award
                            db.commit()
                        except Exception as e:
                            print(f"Seller transfer error for {case.case_id}: {e}")
                    else:
                        print(f"No seller wallet for {case.case_id}. Skipping seller transfer.")
                        
                # Process Buyer Transfer
                if buyer_award > 0 and int(case.buyer_payout or 0) == 0:
                    if case.buyer_wallet:
                        try:
                            asyncio.run(transfer_funds(case.buyer_wallet, buyer_award, case.case_id))
                            case.buyer_payout = buyer_award
                            db.commit()
                        except Exception as e:
                            print(f"Buyer transfer error for {case.case_id}: {e}")
                    else:
                        print(f"No buyer wallet for {case.case_id}. Skipping buyer transfer.")

                # Final Status Update & Notification
                seller_ok = (seller_award == 0) or (int(case.seller_payout or 0) > 0)
                buyer_ok = (buyer_award == 0) or (int(case.buyer_payout or 0) > 0)
                
                if seller_ok and buyer_ok:
                    case.status = StatusEnum.CLOSED
                    db.commit()
                    
                    email_service.send_award_distributed(
                        case_id=case.case_id,
                        seller_name=case.seller, seller_email=case.seller_email, seller_award_eth=seller_award/1e18,
                        buyer_name=case.buyer,   buyer_email=case.buyer_email,   buyer_award_eth=buyer_award/1e18
                    )
            except Exception as e:
                print(f"Error distributing funds for {case.case_id}: {e}")
    finally:
        db.close()

def start_scheduler():
    scheduler = BackgroundScheduler()
    
    # Run checks every 5 minutes
    scheduler.add_job(check_disputed_cases, 'interval', minutes=5)
    scheduler.add_job(check_transaction_timeouts, 'interval', minutes=5)
    scheduler.add_job(process_inbound_emails, 'interval', minutes=5)
    
    # Run distribution hourly
    scheduler.add_job(hourly_auto_distribute, 'interval', minutes=60)
    
    scheduler.start()
    return scheduler
