"""
validators.py — Centralized validation logic for wallet addresses and party tokens.
"""

import re
import secrets
from typing import Optional, Tuple
from models import Case


# Ethereum address validation pattern (40 hex characters after 0x prefix)
WALLET_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

# File upload constraints
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_MIME_TYPES = {"application/pdf", "text/plain", "application/msword", 
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def validate_ethereum_address(address: str) -> Tuple[bool, Optional[str]]:
    """
    Validates an Ethereum address format (0x + 40 hex chars).
    Returns: (is_valid, error_message)
    """
    if not address:
        return False, "Wallet address cannot be empty"
    
    if not WALLET_PATTERN.match(address):
        return False, "Invalid wallet address format. Must be an Ethereum address (0x + 40 hex characters)."
    
    return True, None


def validate_party_token(case: Case, party: str, token: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validates party identity and token.
    Returns: (is_valid, error_message, party_normalized)
    """
    party_lower = party.lower()
    
    # Validate party value
    if party_lower not in ["seller", "buyer"]:
        return False, "Invalid party. Must be 'seller' or 'buyer'.", None
    
    # Get expected token for the party
    expected_token = case.seller_token if party_lower == "seller" else case.buyer_token
    
    # Timing-safe comparison
    if not secrets.compare_digest(token, expected_token):
        return False, "Invalid or expired token.", party_lower
    
    return True, None, party_lower


def check_party_already_responded(case: Case, party: str) -> Tuple[bool, Optional[str]]:
    """
    Checks if a party has already submitted a response.
    Returns: (has_responded, response_value)
    """
    party_lower = party.lower()
    
    if party_lower == "seller" and case.seller_response:
        return True, case.seller_response.value
    elif party_lower == "buyer" and case.buyer_response:
        return True, case.buyer_response.value
    
    return False, None


def validate_file_upload(filename: Optional[str], file_size: Optional[int], mime_type: Optional[str]) -> Tuple[bool, Optional[str]]:
    """
    Validates uploaded file constraints.
    Returns: (is_valid, error_message)
    """
    if not filename:
        return True, None  # File is optional
    
    # Check file size
    if file_size and file_size > MAX_FILE_SIZE:
        return False, f"File is too large. Maximum size is {MAX_FILE_SIZE / 1024 / 1024:.1f}MB."
    
    # Check MIME type
    if mime_type and mime_type not in ALLOWED_MIME_TYPES:
        return False, f"File type not allowed. Accepted types: {', '.join(ALLOWED_MIME_TYPES)}"
    
    return True, None
