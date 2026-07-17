import os
import httpx
from decimal import Decimal

TATUM_API_KEY = os.environ.get("TATUM_API_KEY", "")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")

async def transfer_funds(to_address: str, amount_wei: Decimal, case_id: str = None) -> str:
    """
    Executes an on-chain transfer of funds from the Escrow wallet to the specified address.
    Returns the transaction hash on success, or raises an Exception.
    """
    if not to_address:
        raise ValueError("Destination address is missing.")
    if amount_wei <= 0:
        raise ValueError("Amount must be greater than zero.")
        
    amount_eth = format(Decimal(amount_wei) / Decimal(10**18), 'f')
    
    # In local testing without a Tatum API key, mock the blockchain transfer
    if not TATUM_API_KEY or not PRIVATE_KEY:
        print(f"[Blockchain Mock] Simulating transfer of {amount_eth} ETH to {to_address}")
        return f"mock_tx_{os.urandom(4).hex()}"
        
    url = "https://api.tatum.io/v3/ethereum/transaction"
    headers = {
        "x-api-key": TATUM_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "to": to_address,
        "currency": "ETH",
        "amount": amount_eth,
        "fromPrivateKey": PRIVATE_KEY
    }
    
    if case_id:
        # Include Case ID hex as transaction data
        expected_hex = case_id.split("-")[-1].lower() if "-" in case_id else case_id.lower()
        payload["data"] = "0x" + expected_hex
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise Exception(f"Tatum API Error: {resp.text}")
            
        data = resp.json()
        tx_id = data.get("txId")
        if not tx_id:
            raise Exception(f"Failed to extract txId from Tatum response: {data}")
            
        print(f"[Blockchain] Transferred {amount_eth} ETH to {to_address}. Tx: {tx_id}")
        return tx_id
