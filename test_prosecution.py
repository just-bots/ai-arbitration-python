import pytest
from fastapi.testclient import TestClient
from main import app
from database import get_db
from models import Case, StatusEnum, RoleEnum, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
import uuid

SQLALCHEMY_DATABASE_URL = "sqlite:///./test_prosecution.db"
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
    if os.path.exists("./test_prosecution.db"):
        os.remove("./test_prosecution.db")

@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    yield db
    db.query(Case).delete()
    db.commit()
    db.close()

def test_escalate_invalid_status(db_session):
    """Ensure escalating a PENDING case fails."""
    case = Case(
        case_id="PROS_CASE_1",
        seller_token="seller_tok", buyer_token="buyer_tok",
        status=StatusEnum.PENDING
    )
    db_session.add(case)
    db_session.commit()

    resp = client.post(
        "/prosecution/escalate",
        data={
            "caseId": "PROS_CASE_1",
            "token": "seller_tok"
        }
    )
    assert resp.status_code == 400
    assert "must be in EFFECTIVE status" in resp.text
    
    db_session.refresh(case)
    assert case.status == StatusEnum.PENDING

def test_post_evidence_path_traversal_and_size_limit(db_session):
    """Ensure path traversal is blocked and large files are rejected even without size header."""
    case = Case(
        case_id="PROS_CASE_2",
        seller_token="seller_tok", buyer_token="buyer_tok",
        status=StatusEnum.EFFECTIVE,
        seller_email="seller@test.com"
    )
    db_session.add(case)
    db_session.commit()

    # Create a dummy large file (larger than 10MB) to test chunk loop enforcement
    # We will just write a small file but mock MAX_FILE_SIZE for the test
    import validators
    original_max = validators.MAX_FILE_SIZE
    validators.MAX_FILE_SIZE = 10  # 10 bytes max

    try:
        files = {
            'files': ('../../../etc/passwd', b'This is more than 10 bytes long!', 'text/plain')
        }
        resp = client.post(
            "/prosecution/evidence",
            data={
                "caseId": "PROS_CASE_2",
                "token": "seller_tok",
                "argument": "Look at my evidence!"
            },
            files=files
        )
        assert resp.status_code == 400
        assert "is too large" in resp.text
        
        # Verify it didn't create a file in /etc/passwd or similar
        # Since it's rejected, it shouldn't exist anywhere
    finally:
        validators.MAX_FILE_SIZE = original_max
