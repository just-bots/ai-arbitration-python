import os
import secrets
from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

ADMIN_KEY = os.environ.get("ADMIN_KEY", "default_insecure_admin_key")

api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)
api_key_query = APIKeyQuery(name="admin_key", auto_error=False)

async def verify_admin_token(
    header_key: str = Security(api_key_header),
    query_key: str = Security(api_key_query)
):
    """
    Validates that the provided admin key (either via header or query param)
    matches the securely stored ADMIN_KEY environment variable.
    """
    if header_key and secrets.compare_digest(header_key, ADMIN_KEY):
        return header_key
    if query_key and secrets.compare_digest(query_key, ADMIN_KEY):
        return query_key
        
    raise HTTPException(
        status_code=403,
        detail="Unauthorized. Invalid or missing Admin Key."
    )
