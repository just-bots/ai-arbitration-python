import pytest
from fastapi.testclient import TestClient
from main import app
from database import get_db
from models import Case, StatusEnum, RoleEnum, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from decimal import Decimal

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_transactions.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("./test_transactions.db"):
        os.remove("./test_transactions.db")

@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    yield db
    db.query(Case).delete()
    db.commit()
    db.close()

def test_request_payment_no_tip_inflation(db_session):
    """Ensure request_payment does not increment tip_to_seller."""
    case = Case(
        case_id="TX_CASE_1",
        seller_token="seller_tok", buyer_token="buyer_tok",
        status=StatusEnum.EFFECTIVE,
        escrow_fund=Decimal(10**18),
        tip_to_seller=Decimal(0)
    )
    db_session.add(case)
    db_session.commit()

    resp = client.post(
        "/transactions/request-action",
        data={
            "caseId": "TX_CASE_1",
            "token": "seller_tok",
            "actionType": "request_payment",
            "amount_eth": "0.5",
            "tip_eth": "100"  # Malicious tip attempt
        }
    )
    assert resp.status_code == 200
    
    db_session.refresh(case)
    # The tip should not be incremented!
    assert case.tip_to_seller == 0
    assert str(case.requested_payment_amount) == str(int(0.5 * 10**18))

def test_dispute_invalid_status(db_session):
    """Ensure /dispute fails if the case is not EFFECTIVE or SIGNED."""
    case = Case(
        case_id="TX_CASE_2",
        seller_token="seller_tok", buyer_token="buyer_tok",
        status=StatusEnum.CLOSED
    )
    db_session.add(case)
    db_session.commit()

    resp = client.post(
        "/transactions/dispute",
        data={
            "caseId": "TX_CASE_2",
            "token": "buyer_tok"
        }
    )
    assert resp.status_code == 400
    assert "Cannot dispute" in resp.text

    db_session.refresh(case)
    assert case.status == StatusEnum.CLOSED
