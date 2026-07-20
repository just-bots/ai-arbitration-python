import pytest
from fastapi.testclient import TestClient
from main import app
from database import get_db
from models import Case, Message, File as DBFile, StatusEnum, RoleEnum, LabelEnum, ResponseEnum, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
import uuid
import secrets
from unittest.mock import patch, MagicMock

# Create a test database
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_arbitration.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("./test_arbitration.db"):
        os.remove("./test_arbitration.db")

@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    yield db
    # Cleanup cases after each test
    db.query(DBFile).delete()
    db.query(Message).delete()
    db.query(Case).delete()
    db.commit()
    db.close()

def test_create_case_invalid_wallet():
    response = client.post(
        "/create-case",
        data={
            "seller_name": "Alice",
            "seller_email": "alice@test.com",
            "seller_wallet": "invalid_wallet",
            "buyer_name": "Bob",
            "buyer_email": "bob@test.com",
            "escrow_fund_eth": "1.5",
            "contract_text": "Test contract"
        }
    )
    assert response.status_code == 400
    assert "Invalid wallet address format" in response.text

def test_create_case_negative_escrow():
    response = client.post(
        "/create-case",
        data={
            "seller_name": "Alice",
            "seller_email": "alice@test.com",
            "buyer_name": "Bob",
            "buyer_email": "bob@test.com",
            "escrow_fund_eth": "-5.0",
            "contract_text": "Test contract"
        }
    )
    assert response.status_code == 400
    assert "strictly positive" in response.text

def test_create_case_invalid_file():
    # Attempt to upload a bash script which is not in ALLOWED_MIME_TYPES
    files = {'contract_file': ('malicious.sh', b'#!/bin/bash\nrm -rf /', 'application/x-sh')}
    response = client.post(
        "/create-case",
        data={
            "seller_name": "Alice",
            "seller_email": "alice@test.com",
            "buyer_name": "Bob",
            "buyer_email": "bob@test.com",
            "escrow_fund_eth": "1.5",
            "contract_text": "Test contract"
        },
        files=files
    )
    assert response.status_code == 400
    assert "File type not allowed" in response.text

@patch("email_service.send_case_registered")
def test_create_case_success(mock_send_email, db_session):
    response = client.post(
        "/create-case",
        data={
            "seller_name": "Alice",
            "seller_email": "alice@test.com",
            "seller_wallet": "0x1234567890123456789012345678901234567890",
            "buyer_name": "Bob",
            "buyer_email": "bob@test.com",
            "escrow_fund_eth": "1.5",
            "contract_text": "Test contract"
        }
    )
    assert response.status_code == 200 # Redirects return 200 with TestClient if follow_redirects=True (default is True)
    
    cases = db_session.query(Case).all()
    assert len(cases) == 1
    case = cases[0]
    assert case.seller == "Alice"
    assert case.escrow_fund == 1500000000000000000 # 1.5 ETH in wei
    assert case.status == StatusEnum.PENDING
    
    messages = db_session.query(Message).filter_by(case_id=case.case_id).all()
    assert len(messages) == 1
    assert "Test contract" in messages[0].content
    assert messages[0].label == LabelEnum.SETUP
    
    assert mock_send_email.call_count == 2

@patch("email_service.send_contract_signed")
def test_signature_submit_accept_both(mock_send_signed, db_session):
    # Setup a case
    case = Case(
        case_id="TESTCASE",
        seller="Alice", buyer="Bob",
        seller_email="a@a.com", buyer_email="b@b.com",
        seller_token="seller_tok", buyer_token="buyer_tok",
        status=StatusEnum.PENDING
    )
    db_session.add(case)
    db_session.commit()

    # Seller accepts
    resp1 = client.post(
        "/response-submit",
        data={"caseId": "TESTCASE", "party": "seller", "action": "accept", "token": "seller_tok"}
    )
    assert resp1.status_code == 200
    
    db_session.refresh(case)
    assert case.seller_response == ResponseEnum.ACCEPT
    assert case.status == StatusEnum.PENDING # Still pending until buyer signs
    
    # Buyer accepts
    resp2 = client.post(
        "/response-submit",
        data={"caseId": "TESTCASE", "party": "buyer", "action": "accept", "token": "buyer_tok"}
    )
    assert resp2.status_code == 200
    
    db_session.refresh(case)
    assert case.buyer_response == ResponseEnum.ACCEPT
    assert case.status == StatusEnum.SIGNED
    
    mock_send_signed.assert_called_once()

def test_signature_submit_invalid_token(db_session):
    case = Case(
        case_id="TESTCASE2",
        seller_token="seller_tok", buyer_token="buyer_tok",
        status=StatusEnum.PENDING
    )
    db_session.add(case)
    db_session.commit()

    resp = client.post(
        "/response-submit",
        data={"caseId": "TESTCASE2", "party": "seller", "action": "accept", "token": "wrong_tok"}
    )
    assert resp.status_code == 403
    assert "Invalid or expired token." in resp.text

@patch("email_service.send_wallet_confirmed")
def test_wallet_submit(mock_send_wallet, db_session):
    case = Case(
        case_id="TESTCASE3",
        seller_token="seller_tok", buyer_token="buyer_tok",
        seller_email="a@a.com", buyer_email="b@b.com",
        status=StatusEnum.PENDING
    )
    db_session.add(case)
    db_session.commit()

    resp = client.post(
        "/wallet-submit",
        data={"caseId": "TESTCASE3", "party": "seller", "token": "seller_tok", "wallet_address": "0x1234567890123456789012345678901234567890"}
    )
    assert resp.status_code == 200
    
    db_session.refresh(case)
    assert case.seller_wallet == "0x1234567890123456789012345678901234567890"
    mock_send_wallet.assert_called_once()
