import pytest
from unittest.mock import MagicMock, patch
from models import Case, StatusEnum
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

def test_escrow_clamping_logic():
    case = Case(
        case_id="test_clamp_1",
        escrow_fund=1000,
        payment_to_seller=200,
        refund_to_buyer=0,
        requested_payment_amount=1500, # Attempting to drain 1500 when only 800 left
        seller_wallet="0xabc",
        payment_request_time=datetime.now(timezone.utc) - timedelta(days=8),
        status=StatusEnum.DISPUTED
    )
    
    from scheduler import check_transaction_timeouts
    
    mock_db = MagicMock(spec=Session)
    mock_db.query().filter().all.return_value = [case]
    
    with patch("scheduler.SessionLocal", return_value=mock_db), \
         patch("scheduler.transfer_funds") as mock_transfer, \
         patch("email_service.send_payment_released"):
        
        check_transaction_timeouts()
        
        # 1000 - 200 = 800 available. Requested 1500. Should clamp to 800.
        mock_transfer.assert_called_once_with("0xabc", 800, "test_clamp_1")
        assert case.payment_to_seller == 1000
        assert case.status == StatusEnum.CLOSED
