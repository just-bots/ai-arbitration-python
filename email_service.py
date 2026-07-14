"""
email_service.py — Centralized email sending module

Uses Python's built-in smtplib with STARTTLS.
Reads all config from environment variables.

Compatible with Gmail (App Password) and any standard SMTP provider.

Gmail setup:
  1. Enable 2-Step Verification on your Google account
  2. Go to https://myaccount.google.com/apppasswords
  3. Generate an App Password for "Mail"
  4. Set SMTP_USER=your@gmail.com and SMTP_PASS=<16-char app password>
  5. Set SMTP_HOST=smtp.gmail.com and SMTP_PORT=587
"""

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

# ── Config from environment ─────────────────────────────────────────────────
SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("SMTP_USER", "")
SMTP_PASS  = os.environ.get("SMTP_PASS", "")
BASE_URL   = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")


def _is_configured() -> bool:
    """Returns True if SMTP credentials are set in the environment."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def send_email(to: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """
    Send an email via SMTP with STARTTLS.
    Falls back to console print if SMTP is not configured.
    Returns True on success, False on failure.
    """
    if not _is_configured():
        # Graceful fallback — log to console for local dev without SMTP
        print(f"\n{'─'*60}")
        print(f"📧  EMAIL (not sent — SMTP not configured)")
        print(f"    To      : {to}")
        print(f"    Subject : {subject}")
        print(f"    Body    : {text_body or html_body[:300]}")
        print(f"{'─'*60}\n")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to, msg.as_string())
        print(f"✅  Email sent → {to} | {subject}")
        return True
    except Exception as e:
        print(f"❌  Email FAILED → {to} | {subject} | Error: {e}")
        return False


# ── Email Templates ──────────────────────────────────────────────────────────

def _wrap_html(title: str, body: str) -> str:
    """Wraps email body in a clean, branded HTML shell."""
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f172a;padding:40px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#1e293b;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:32px 40px;">
            <h1 style="color:#fff;margin:0;font-size:22px;letter-spacing:-0.5px;">⚖️ AI Arbitration Platform</h1>
            <p style="color:rgba(255,255,255,0.7);margin:6px 0 0;font-size:13px;">Automated Contract Arbitration</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:36px 40px;color:#e2e8f0;font-size:15px;line-height:1.7;">
            {body}
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:20px 40px;border-top:1px solid rgba(255,255,255,0.06);
                     color:#475569;font-size:12px;text-align:center;">
            This is an automated message from the AI Arbitration Platform.
            Do not reply to this email.
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _btn(url: str, label: str, color: str = "#4f46e5") -> str:
    return (f'<a href="{url}" style="display:inline-block;background:{color};color:#fff;'
            f'text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:600;'
            f'font-size:14px;margin:8px 4px 8px 0;">{label}</a>')


def _case_badge(case_id: str) -> str:
    return (f'<div style="background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.3);'
            f'border-radius:8px;padding:12px 18px;margin:20px 0;font-family:monospace;'
            f'font-size:16px;color:#a5b4fc;letter-spacing:1px;">'
            f'Case ID: <strong>{case_id}</strong></div>')


# ── Initialization emails ────────────────────────────────────────────────────

def send_case_registered(case_id: str, party: str, name: str, email: str,
                          token: str, counterpart_name: str,
                          escrow_eth: float, contract_text_preview: str) -> bool:
    """Sent to each party after a new case is created."""
    accept_url  = f"{BASE_URL}/response?caseId={case_id}&party={party}&action=accept&token={token}"
    decline_url = f"{BASE_URL}/response?caseId={case_id}&party={party}&action=decline&token={token}"
    wallet_url  = f"{BASE_URL}/wallet?caseId={case_id}&party={party}&token={token}"

    preview = contract_text_preview[:300] + ("…" if len(contract_text_preview) > 300 else "")

    body = f"""
    <p>Hello <strong>{name}</strong>,</p>
    <p>A new arbitration contract has been registered between you and <strong>{counterpart_name}</strong>.
       Please review the details below and accept or decline.</p>

    {_case_badge(case_id)}

    <p><strong>Escrow Required:</strong> {escrow_eth} ETH</p>

    <div style="background:rgba(255,255,255,0.04);border-radius:8px;padding:16px;
                margin:16px 0;font-size:13px;color:#94a3b8;border-left:3px solid #4f46e5;
                white-space: pre-wrap; font-family: monospace;">
      <strong style="color:#cbd5e1; font-family: 'Segoe UI',Arial,sans-serif;">Contract Preview:</strong><br><br>{preview}
    </div>
    
    <p style="font-size:13px;color:#64748b;">
      <a href="{BASE_URL}/terms?caseId={case_id}&token={token}" style="color:#a5b4fc;text-decoration:underline;">View Full Legal Agreement</a>
    </p>

    <p><strong>Action required — please respond within 7 days:</strong></p>
    {_btn(accept_url, "✅  Accept Contract", "#16a34a")}
    {_btn(decline_url, "❌  Decline Contract", "#dc2626")}

    <hr style="border:none;border-top:1px solid rgba(255,255,255,0.08);margin:28px 0;">
    <p style="font-size:13px;color:#64748b;">
      You can also register or update your Ethereum wallet address at any time:<br>
      {_btn(wallet_url, "🔑  Submit Wallet Address", "#0891b2")}
    </p>
    """

    return send_email(
        to=email,
        subject=f"Action Required: Contract Registered ({case_id})",
        html_body=_wrap_html(f"Contract Registered — {case_id}", body),
        text_body=(f"Hello {name},\n\nA contract has been registered. Case: {case_id}\n\n"
                   f"Accept:  {accept_url}\nDecline: {decline_url}\nWallet:  {wallet_url}")
    )


def send_wallet_confirmed(case_id: str, party: str, name: str, email: str, wallet: str) -> bool:
    """Sent after a party successfully submits a wallet address."""
    body = f"""
    <p>Hello <strong>{name}</strong>,</p>
    <p>Your Ethereum wallet address has been successfully registered for case <strong>{case_id}</strong>.</p>
    {_case_badge(case_id)}
    <p><strong>Registered Wallet:</strong><br>
       <code style="background:rgba(255,255,255,0.06);padding:8px 12px;border-radius:6px;
                    font-size:14px;color:#a5b4fc;">{wallet}</code></p>
    <p style="color:#94a3b8;font-size:13px;">
      If this was not you, contact the platform administrator immediately.
    </p>
    """
    return send_email(
        to=email,
        subject=f"Wallet Address Confirmed ({case_id})",
        html_body=_wrap_html("Wallet Confirmed", body),
        text_body=f"Hello {name},\n\nYour wallet {wallet} has been registered for case {case_id}."
    )


def send_contract_signed(case_id: str, seller_name: str, seller_email: str,
                          buyer_name: str, buyer_email: str,
                          escrow_address: str, escrow_eth: float, total_eth: float) -> None:
    """Sent to both parties when the contract is fully signed — instructs Buyer to fund escrow."""
    deposit_body = lambda name: f"""
    <p>Hello <strong>{name}</strong>,</p>
    <p>Both parties have accepted the contract. The case is now <strong>SIGNED</strong>.</p>
    {_case_badge(case_id)}
    <p>The Buyer (<strong>{buyer_name}</strong>) must now deposit escrow to activate the agreement.</p>
    <p>
      <strong>Escrow Wallet Address:</strong><br>
      <code style="background:rgba(255,255,255,0.06);padding:8px 12px;border-radius:6px;
                   font-size:14px;color:#a5b4fc;">{escrow_address}</code>
    </p>
    <p><strong>Amount to deposit:</strong> {total_eth:.6f} ETH
       <span style="color:#64748b;font-size:13px;">(includes 0.001 ETH platform fee)</span></p>
    <p style="color:#94a3b8;font-size:13px;">
      Include your Case ID <strong>{case_id}</strong> in the transaction memo/data field.
    </p>
    """
    for name, email in [(seller_name, seller_email), (buyer_name, buyer_email)]:
        send_email(
            to=email,
            subject=f"Contract Signed — Awaiting Escrow Deposit ({case_id})",
            html_body=_wrap_html("Contract Signed", deposit_body(name)),
            text_body=(f"Case {case_id} is SIGNED. Buyer must deposit {total_eth} ETH to {escrow_address}.")
        )


def send_contract_declined(case_id: str, declining_party: str,
                            seller_name: str, seller_email: str,
                            buyer_name: str, buyer_email: str) -> None:
    """Sent to both parties when a contract is declined."""
    body = lambda name: f"""
    <p>Hello <strong>{name}</strong>,</p>
    <p><strong>{declining_party}</strong> has declined the contract. Case <strong>{case_id}</strong>
       is now closed.</p>
    {_case_badge(case_id)}
    <p style="color:#94a3b8;">No further action is required.</p>
    """
    for name, email in [(seller_name, seller_email), (buyer_name, buyer_email)]:
        send_email(
            to=email,
            subject=f"Contract Declined ({case_id})",
            html_body=_wrap_html("Contract Declined", body(name)),
            text_body=f"Case {case_id}: {declining_party} declined the contract."
        )


# ── Transactions emails ──────────────────────────────────────────────────────

def send_escrow_confirmed(case_id: str,
                           seller_name: str, seller_email: str, seller_token: str,
                           buyer_name: str, buyer_email: str, buyer_token: str) -> None:
    """Sent to both parties when escrow deposit is confirmed."""
    release_url = f"{BASE_URL}/transactions/action?caseId={case_id}&token={buyer_token}&actionType=release_payment"
    refund_url  = f"{BASE_URL}/transactions/action?caseId={case_id}&token={buyer_token}&actionType=request_refund"

    seller_body = f"""
    <p>Hello <strong>{seller_name}</strong>,</p>
    <p>The escrow deposit for case <strong>{case_id}</strong> has been confirmed.
       The agreement is now <strong>active</strong>.</p>
    {_case_badge(case_id)}
    <p>The Buyer may now release payment to you once services are delivered, or request a refund.
       You will be notified of any action taken.</p>
    """
    buyer_body = f"""
    <p>Hello <strong>{buyer_name}</strong>,</p>
    <p>Your escrow deposit for case <strong>{case_id}</strong> has been confirmed.
       The agreement is now <strong>active</strong>.</p>
    {_case_badge(case_id)}
    <p>Once you are satisfied with the delivered services, release payment to the Seller.
       If you have a concern, you can request a refund.</p>
    {_btn(release_url, "💰  Release Payment to Seller", "#16a34a")}
    {_btn(refund_url,  "↩️  Request Refund", "#d97706")}
    """

    send_email(seller_name, f"Escrow Confirmed — Agreement Active ({case_id})",
               _wrap_html("Escrow Confirmed", seller_body))
    send_email(buyer_email, f"Escrow Confirmed — Agreement Active ({case_id})",
               _wrap_html("Escrow Confirmed", buyer_body),
               text_body=f"Escrow confirmed for {case_id}.\nRelease: {release_url}\nRefund: {refund_url}")


def send_payment_released(case_id: str, seller_name: str, seller_email: str,
                           buyer_name: str, buyer_email: str,
                           amount_eth: float, closed: bool) -> None:
    """Sent to both parties after a payment is released to the Seller."""
    status_note = "The case is now <strong>CLOSED</strong>." if closed else "Partial payment released — case remains active."
    for name, email in [(seller_name, seller_email), (buyer_name, buyer_email)]:
        body = f"""
        <p>Hello <strong>{name}</strong>,</p>
        <p>A payment of <strong>{amount_eth:.6f} ETH</strong> has been released to the Seller
           for case <strong>{case_id}</strong>.</p>
        {_case_badge(case_id)}
        <p>{status_note}</p>
        """
        send_email(email, f"Payment Released ({case_id})",
                   _wrap_html("Payment Released", body))

def send_refund_released(case_id: str, seller_name: str, seller_email: str,
                           buyer_name: str, buyer_email: str,
                           amount_eth: float, closed: bool) -> None:
    """Sent to both parties after a refund is released to the Buyer."""
    status_note = "The case is now <strong>CLOSED</strong>." if closed else "Partial refund released — case remains active."
    for name, email in [(seller_name, seller_email), (buyer_name, buyer_email)]:
        body = f"""
        <p>Hello <strong>{name}</strong>,</p>
        <p>A refund of <strong>{amount_eth:.6f} ETH</strong> has been released to the Buyer
           for case <strong>{case_id}</strong>.</p>
        {_case_badge(case_id)}
        <p>{status_note}</p>
        """
        send_email(email, f"Refund Released ({case_id})",
                   _wrap_html("Refund Released", body))


def send_refund_requested(case_id: str,
                           seller_name: str, seller_email: str,
                           buyer_name: str, buyer_email: str) -> None:
    """Sent to both parties when the Buyer requests a refund (initiates DISPUTED)."""
    for name, email in [(seller_name, seller_email), (buyer_name, buyer_email)]:
        body = f"""
        <p>Hello <strong>{name}</strong>,</p>
        <p>The Buyer has requested a refund for case <strong>{case_id}</strong>.
           The case is now in <strong>DISPUTED</strong> status.</p>
        {_case_badge(case_id)}
        <p>Both parties now have <strong>7 days</strong> to submit evidence and arguments
           through the Evidence Portal.</p>
        """
        send_email(email, f"Refund Requested — Case Disputed ({case_id})",
                   _wrap_html("Case Disputed", body))

def send_payment_requested(case_id: str,
                           seller_name: str, seller_email: str,
                           buyer_name: str, buyer_email: str, buyer_token: str) -> None:
    """Sent to both parties when the Seller requests a payment."""
    for name, email in [(seller_name, seller_email), (buyer_name, buyer_email)]:
        body = f"""
        <p>Hello <strong>{name}</strong>,</p>
        <p>The Seller has requested a payment for case <strong>{case_id}</strong>.</p>
        {_case_badge(case_id)}
        <p>The Buyer has <strong>7 days</strong> to approve or dispute this request.</p>
        """
        send_email(email, f"Payment Requested ({case_id})",
                   _wrap_html("Payment Requested", body))


# ── Prosecution emails ───────────────────────────────────────────────────────

def send_evidence_received(case_id: str, submitter_name: str,
                            recipient_name: str, recipient_email: str) -> bool:
    """Notifies the opposing party that new evidence has been submitted."""
    body = f"""
    <p>Hello <strong>{recipient_name}</strong>,</p>
    <p><strong>{submitter_name}</strong> has submitted new evidence for case
       <strong>{case_id}</strong>.</p>
    {_case_badge(case_id)}
    <p>The evidence has been recorded and will be reviewed during adjudication.
       You may also submit your own evidence and arguments.</p>
    <p style="color:#94a3b8;font-size:13px;">
      Use the secure link in your original case email to access the Evidence Portal.
    </p>
    """
    return send_email(
        to=recipient_email,
        subject=f"New Evidence Submitted ({case_id})",
        html_body=_wrap_html("New Evidence", body)
    )


# ── Adjudication emails ──────────────────────────────────────────────────────

def send_determination(case_id: str,
                        seller_name: str, seller_email: str, seller_token: str,
                        buyer_name: str, buyer_email: str, buyer_token: str,
                        decision: str, seller_award_eth: float, buyer_award_eth: float) -> None:
    """Sent to both parties after AI Final Judge issues a ruling."""
    for name, email, token in [
        (seller_name, seller_email, seller_token),
        (buyer_name,  buyer_email,  buyer_token)
    ]:
        appeal_url = f"{BASE_URL}/objection/appeal?caseId={case_id}&token={token}"
        body = f"""
        <p>Hello <strong>{name}</strong>,</p>
        <p>The AI Adjudication Panel has issued a final determination for case
           <strong>{case_id}</strong>.</p>
        {_case_badge(case_id)}

        <div style="background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.2);
                    border-radius:10px;padding:20px;margin:20px 0;">
          <p style="margin:0 0 12px;font-weight:600;color:#c7d2fe;">📋 Decision Summary</p>
          <p style="color:#e2e8f0;margin:0;">{decision}</p>
        </div>

        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
          <tr>
            <td style="padding:12px;background:rgba(22,163,74,0.1);border-radius:8px 0 0 8px;
                       color:#86efac;text-align:center;">
              <div style="font-size:12px;color:#4ade80;margin-bottom:4px;">Seller Award</div>
              <strong style="font-size:20px;">{seller_award_eth:.6f} ETH</strong>
            </td>
            <td style="width:8px;"></td>
            <td style="padding:12px;background:rgba(59,130,246,0.1);border-radius:0 8px 8px 0;
                       color:#93c5fd;text-align:center;">
              <div style="font-size:12px;color:#60a5fa;margin-bottom:4px;">Buyer Award</div>
              <strong style="font-size:20px;">{buyer_award_eth:.6f} ETH</strong>
            </td>
          </tr>
        </table>

        <p>You have <strong>7 days</strong> from this notice to file a procedural objection.
           Awards will be distributed automatically after the objection window closes.</p>

        {_btn(appeal_url, "📝  File Procedural Objection", "#7c3aed")}

        <p style="color:#64748b;font-size:12px;margin-top:24px;">
          Objections are limited to procedural or logical errors.
          No new evidence or arguments are permitted at this stage.
        </p>
        """
        send_email(
            to=email,
            subject=f"Final Determination Issued ({case_id})",
            html_body=_wrap_html("Final Determination", body),
            text_body=(f"Determination for {case_id}:\n{decision}\n\n"
                       f"Seller Award: {seller_award_eth} ETH | Buyer Award: {buyer_award_eth} ETH\n"
                       f"File objection (7 days): {appeal_url}")
        )


# ── Objection emails ─────────────────────────────────────────────────────────

def send_objection_received(case_id: str, objecting_party: str,
                             admin_email: str, review_url: str) -> bool:
    """Notifies the admin that an objection has been filed and requires HITL review."""
    body = f"""
    <p>A procedural objection has been filed for case <strong>{case_id}</strong>
       by <strong>{objecting_party}</strong>.</p>
    {_case_badge(case_id)}
    <p>Please review the objection and the AI ruling, then uphold or reverse the determination.</p>
    {_btn(review_url, "🔍  Open Review Portal", "#4f46e5")}
    """
    return send_email(
        to=admin_email,
        subject=f"Objection Filed — Review Required ({case_id})",
        html_body=_wrap_html("Objection Filed", body),
        text_body=f"Objection filed for {case_id} by {objecting_party}.\nReview: {review_url}"
    )


def send_award_distributed(case_id: str,
                            seller_name: str, seller_email: str, seller_award_eth: float,
                            buyer_name: str, buyer_email: str, buyer_award_eth: float) -> None:
    """Sent to both parties after final award distribution."""
    for name, email, award in [
        (seller_name, seller_email, seller_award_eth),
        (buyer_name,  buyer_email,  buyer_award_eth)
    ]:
        body = f"""
        <p>Hello <strong>{name}</strong>,</p>
        <p>The award distribution for case <strong>{case_id}</strong> has been completed.</p>
        {_case_badge(case_id)}
        <p>Your award of <strong>{award:.6f} ETH</strong> has been transferred to your
           registered wallet.</p>
        <p>The case is now <strong>CLOSED</strong>.</p>
        """
        send_email(
            to=email,
            subject=f"Award Distributed — Case Closed ({case_id})",
            html_body=_wrap_html("Award Distributed", body),
            text_body=f"Case {case_id} closed. Your award: {award} ETH."
        )
