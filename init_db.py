import enum
import os
from sqlalchemy import Column, String, Integer, Text, Numeric, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import create_engine

Base = declarative_base()

class ResponseEnum(str, enum.Enum):
    ACCEPT = 'accept'
    DECLINE = 'decline'

class RoleEnum(str, enum.Enum):
    ADMIN = 'Admin'
    SYSTEM = 'System'
    SELLER = 'Seller'
    BUYER = 'Buyer'

class StatusEnum(str, enum.Enum):
    PENDING = 'PENDING'
    SIGNED = 'SIGNED'
    DECLINED = 'DECLINED'
    DISPUTED = 'DISPUTED'
    PROCESSING = 'PROCESSING'
    DECIDED = 'DECIDED'
    CLOSED = 'CLOSED'

class LabelEnum(str, enum.Enum):
    SETUP = 'Setup'
    DISPUTE = 'Dispute'
    GENERAL = 'General'
    EMAIL = 'Email'
    APPEAL = 'Appeal'
    REQUEST_PAYMENT = 'Request Payment'
    REQUEST_REFUND = 'Request Refund'

class Case(Base):
    __tablename__ = 'cases'
    case_id = Column(String(50), primary_key=True)
    created_at = Column(DateTime(timezone=True))
    seller = Column(String(255))
    buyer = Column(String(255))
    seller_email = Column(String(255))
    buyer_email = Column(String(255))
    seller_token = Column(String(255))
    buyer_token = Column(String(255))
    seller_wallet = Column(String(255))
    buyer_wallet = Column(String(255))
    contract_text = Column(Text)
    seller_response = Column(SQLEnum(ResponseEnum))
    buyer_response = Column(SQLEnum(ResponseEnum))
    folder_link = Column(Text)
    escrow_address = Column(String(255))
    escrow_fund = Column(Numeric(38, 0))
    fee = Column(Numeric(38, 0))
    deposited_fund = Column(Numeric(38, 0))
    payment_to_seller = Column(Numeric(38, 0))
    refund_to_buyer = Column(Numeric(38, 0))
    tip_to_seller = Column(Numeric(38, 0))
    buyer_withdrawal = Column(Numeric(38, 0))
    status = Column(SQLEnum(StatusEnum))
    payment_request_time = Column(DateTime(timezone=True))
    refund_request_time = Column(DateTime(timezone=True))
    dispute_time = Column(DateTime(timezone=True))
    adjudication_time = Column(DateTime(timezone=True))
    determination_time = Column(DateTime(timezone=True))
    decision = Column(Text)
    seller_award = Column(Numeric(38, 0))
    buyer_award = Column(Numeric(38, 0))
    seller_payout = Column(Numeric(38, 0))
    buyer_payout = Column(Numeric(38, 0))
    appeal_time = Column(DateTime(timezone=True))

    messages = relationship("Message", back_populates="case")
    files = relationship("File", back_populates="case")

class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String(50), ForeignKey('cases.case_id'))
    time = Column(DateTime(timezone=True))
    sender = Column(SQLEnum(RoleEnum))
    email = Column(String(255))
    content = Column(Text)
    label = Column(SQLEnum(LabelEnum))

    case = relationship("Case", back_populates="messages")
    files = relationship("File", back_populates="message")

class File(Base):
    __tablename__ = 'files'
    file_id = Column(String(255), primary_key=True)
    case_id = Column(String(50), ForeignKey('cases.case_id'))
    message_id = Column(Integer, ForeignKey('messages.id'))
    time = Column(DateTime(timezone=True))
    submitter = Column(SQLEnum(RoleEnum))
    email = Column(String(255))
    original_name = Column(String(500))
    secure_name = Column(String(500))
    hash = Column(String(255))

    case = relationship("Case", back_populates="files")
    message = relationship("Message", back_populates="files")

if __name__ == "__main__":
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@127.0.0.1:5433/arbitration")
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    print("Database schema created successfully.")
