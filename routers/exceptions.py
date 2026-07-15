"""
exceptions.py — Global Error Handler Router

Mirrors Exceptions.json (the 6th n8n workflow).

In n8n, the global error workflow is triggered automatically whenever any other
workflow fails without catching its own error. It packages the execution metadata
(failing node, error message, raw technical context, and execution URL) into a
structured admin alert email, with an SMTP fallback if Gmail fails.

In this FastAPI implementation we register a global exception handler on the app
that:
  1. Logs the full traceback.
  2. Sends a structured admin alert email (simulated via console print for local dev).
  3. Returns a clean JSON error response to the caller.

Register this handler in main.py via:
    from routers.exceptions import register_exception_handlers
    register_exception_handlers(app)
"""

import os
import traceback
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
BASE_URL    = os.environ.get("BASE_URL", "http://localhost:8000/")


def _format_admin_alert(request: Request, exc: Exception, tb: str) -> str:
    """Builds the structured admin alert body that mirrors Exceptions.json."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"\n{'='*60}\n"
        f"🔴 UNHANDLED SYSTEM EXCEPTION\n"
        f"{'='*60}\n"
        f"Time         : {timestamp}\n"
        f"Method       : {request.method}\n"
        f"URL          : {request.url}\n"
        f"Error Type   : {type(exc).__name__}\n"
        f"Error Message: {str(exc)}\n"
        f"{'─'*60}\n"
        f"Traceback:\n{tb}\n"
        f"{'='*60}\n"
        f"→ To investigate: {BASE_URL}docs\n"
    )


import email_service

def _send_admin_alert(alert_body: str) -> None:
    """
    Send admin alert via SMTP using the existing email service.
    Mirrors n8n's Gmail node with SMTP fallback (Exceptions.json).
    """
    print(f"\n--- ADMIN ALERT → {ADMIN_EMAIL} ---")
    print(alert_body)
    print("--- END ALERT ---\n")
    
    try:
        html_body = alert_body.replace('\n', '<br>')
        email_service.send_email(
            to_email=ADMIN_EMAIL,
            subject="🔴 AI Arbitration — Unhandled Exception",
            html_body=f"<div style='font-family:monospace'>{html_body}</div>",
            text_body=alert_body
        )
    except Exception as e:
        print(f"Failed to send admin alert email: {e}")


def register_exception_handlers(app: FastAPI) -> None:
    """
    Registers the global exception handler on the FastAPI application.
    Call this once in main.py after app is created.
    """

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        Catches any unhandled exception across all routers.
        Mirrors Exceptions.json — the n8n global error workflow that fires on
        any uncaught workflow execution failure.
        """
        tb = traceback.format_exc()
        alert_body = _format_admin_alert(request, exc, tb)
        _send_admin_alert(alert_body)

        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal Server Error",
                "type": type(exc).__name__,
                "message": str(exc),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "path": str(request.url.path),
            },
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": "Not Found",
                "message": f"The requested path '{request.url.path}' does not exist.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
