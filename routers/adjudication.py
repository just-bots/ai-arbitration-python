from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import os

from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
import PyPDF2
import asyncio

from database import get_db, SessionLocal
from models import Case, StatusEnum, Message, File
from dependencies import verify_admin_token
import email_service

# Pydantic models for LangChain structured output
class MagistrateReport(BaseModel):
    summary: str = Field(description="Neutral overview of dispute and timeline")
    facts: list[str] = Field(description="Array of verified facts (each with evidence citation)")
    contradictions: list[str] = Field(description="Array of conflicts between claims/evidence or between parties")
    unsubstantiated_claims: list[str] = Field(description="Array of claims lacking evidence")
    applicable_legal_authorities: list[str] = Field(default_factory=list, description="Array of cited statutory sections, precedents, or legal rules applied (from retrieve_legal_authorities tool)")
    reasoning: str = Field(description="Explanation of how facts and legal principles support your payout recommendation")
    recommended_buyer_payout: str = Field(description="Wei string (must sum with seller payout to escrow_balance)")
    recommended_seller_payout: str = Field(description="Wei string (must sum with buyer payout to escrow_balance)")

class FinalRuling(BaseModel):
    decision: str = Field(description="The final binding ruling description")
    escrow_balance: str = Field(description="The total escrow balance in Wei")
    buyer_award: str = Field(description="Amount awarded to the buyer in Wei (string format)")
    seller_award: str = Field(description="Amount awarded to the seller in Wei (string format)")
    rationale: str = Field(description="Detailed rationale for the final ruling")
    confidence: float = Field(description="AI Confidence score between 0.0 and 1.0")


router = APIRouter(prefix="/adjudication", tags=["Adjudication"])
templates = Jinja2Templates(directory="templates")

def read_evidence_files(case_id: str, db: Session):
    """Summarizes uploaded evidence files for the AI context."""
    files = db.query(File).filter(File.case_id == case_id).all()
    if not files:
        return "_No user evidence files submitted._"
        
    file_table = "| Source | Filename | File Hash |\n|--------|----------|---------|\n"
    for f in files:
        submitter = f.submitter.value if f.submitter else 'SYSTEM'
        file_table += f"| {submitter} | {f.original_name} | {f.hash} |\n"
    return file_table

UPLOAD_DIR = "uploads"

@tool
def read_evidence_file(file_hash: str) -> str:
    """Reads the text content of an uploaded evidence file by its hash.
    Use this to read the details of PDF or TXT files submitted by parties."""
    db = SessionLocal()
    try:
        db_file = db.query(File).filter(File.hash == file_hash).first()
        if not db_file:
            return "File not found."
            
        file_path = os.path.join(UPLOAD_DIR, db_file.secure_name)
        if not os.path.exists(file_path):
            return "File missing from disk."
            
        if db_file.original_name.lower().endswith('.pdf'):
            try:
                with open(file_path, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    text = "\\n".join([page.extract_text() or "" for page in reader.pages])
                    return text[:5000] # Limit length
            except Exception as e:
                return f"Could not read PDF: {e}"
        else:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read()[:5000]
            except Exception as e:
                return f"Could not read text file: {e}"
    finally:
        db.close()

import ast
import operator
import httpx

@tool
def calculator(expression: str) -> str:
    """Evaluates a mathematical expression (e.g. '1000 * 0.15' or '(500 + 200) / 2').
    Use this to calculate exact Wei amounts for fractional damage awards."""
    try:
        # Safe math evaluation using ast
        def eval_expr(node):
            if isinstance(node, ast.Num): return node.n
            elif isinstance(node, ast.BinOp):
                op_map = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
                return op_map[type(node.op)](eval_expr(node.left), eval_expr(node.right))
            elif isinstance(node, ast.UnaryOp):
                if isinstance(node.op, ast.USub): return -eval_expr(node.operand)
                elif isinstance(node.op, ast.UAdd): return eval_expr(node.operand)
            raise ValueError("Unsupported operation")
        
        result = eval_expr(ast.parse(expression, mode='eval').body)
        return str(result)
    except Exception as e:
        return f"Calculation error: {e}"

@tool
def external_verification(url: str) -> str:
    """Fetches the text content of a public URL (HTTP GET).
    Use this to verify tracking numbers, public pricing, or reference data."""
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True)
        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code}"
        # Return first 2000 chars of text to avoid context overload
        return resp.text[:2000]
    except Exception as e:
        return f"Fetch error: {e}"

_legal_embeddings = None

def get_legal_embeddings():
    global _legal_embeddings
    if _legal_embeddings is None:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            raise ImportError(
                "langchain-huggingface or sentence-transformers is not installed. "
                "Please run `pip install -r requirements.txt`"
            )
        model_name = os.getenv("LEGAL_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        _legal_embeddings = HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True, "batch_size": 32}
        )
    return _legal_embeddings

def hybrid_search_legal_knowledge(
    query: str,
    jurisdiction: str = None,
    authority_types: list[str] = None,
    top_k: int = 5,
    candidate_k: int = 20,
    rrf_k: int = 60,
) -> list[dict]:
    """Executes Reciprocal Rank Fusion (RRF) combining vector cosine distance and TSVector keyword search."""
    import psycopg
    from pgvector.psycopg import register_vector
    import numpy as np

    embeddings = get_legal_embeddings()
    embedding_model_name = os.getenv("LEGAL_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    query_embedding = np.asarray(embeddings.embed_query(query), dtype=np.float32)

    conditions = [
        "is_current = TRUE",
        "embedding_model = %(embedding_model)s",
    ]
    params = {
        "query": query,
        "embedding": query_embedding,
        "embedding_model": embedding_model_name,
        "candidate_k": candidate_k,
        "top_k": top_k,
        "rrf_k": rrf_k,
    }

    if jurisdiction and jurisdiction.strip():
        conditions.append("LOWER(jurisdiction) = LOWER(%(jurisdiction)s)")
        params["jurisdiction"] = jurisdiction.strip()

    if authority_types:
        conditions.append("authority_type = ANY(%(authority_types)s::text[])")
        params["authority_types"] = authority_types

    where_clause = " AND ".join(conditions)

    sql = f"""
    WITH semantic_search AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> %(embedding)s) AS semantic_rank
        FROM legal_knowledge
        WHERE {where_clause}
        ORDER BY embedding <=> %(embedding)s
        LIMIT %(candidate_k)s
    ),
    keyword_search AS (
        SELECT id, ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(search_tsv, websearch_to_tsquery('english', %(query)s)) DESC
        ) AS keyword_rank
        FROM legal_knowledge
        WHERE {where_clause}
          AND search_tsv @@ websearch_to_tsquery('english', %(query)s)
        ORDER BY ts_rank_cd(search_tsv, websearch_to_tsquery('english', %(query)s)) DESC
        LIMIT %(candidate_k)s
    ),
    fused AS (
        SELECT
            COALESCE(s.id, k.id) AS id,
            COALESCE(1.0 / (%(rrf_k)s + s.semantic_rank), 0.0) +
            COALESCE(1.0 / (%(rrf_k)s + k.keyword_rank), 0.0) AS rrf_score
        FROM semantic_search s
        FULL OUTER JOIN keyword_search k ON s.id = k.id
    )
    SELECT
        lk.id,
        lk.document_id,
        lk.authority_type,
        lk.title,
        lk.citation,
        lk.jurisdiction,
        lk.court,
        lk.precedential,
        lk.status,
        lk.decision_date,
        lk.effective_from,
        lk.effective_to,
        lk.section_label,
        lk.source_url,
        lk.chunk_index,
        lk.content,
        f.rrf_score
    FROM fused f
    JOIN legal_knowledge lk ON lk.id = f.id
    ORDER BY f.rrf_score DESC
    LIMIT %(top_k)s;
    """

    db_url = os.getenv("DATABASE_URL", "postgresql://postgres:password@127.0.0.1:5433/arbitration")
    with psycopg.connect(db_url) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc.name for desc in cur.description]
            rows = cur.fetchall()

    return [dict(zip(columns, row)) for row in rows]


@tool
def search_legal_authorities(query: str, jurisdiction: str = "", top_k: int = 5) -> str:
    """Searches statutes, UCC provisions, commercial regulations, contract law rules, and legal precedents using Hybrid RRF (Vector + Keyword).
    Use this to research applicable legal standards, breach of contract remedies, perfect tender rules, and warranty claims.
    Do not invent legal citations. Cite only authorities returned by this tool or fetch_legal_authority."""
    try:
        try:
            import psycopg
            from pgvector.psycopg import register_vector
        except ImportError:
            return "Legal database vector search unavailable: pgvector/psycopg not installed."

        results = hybrid_search_legal_knowledge(
            query=query,
            jurisdiction=jurisdiction if jurisdiction and jurisdiction.strip() else None,
            top_k=top_k
        )

        if not results:
            return "No matching legal authorities or statutes found in database."

        formatted = []
        for item in results:
            cite_str = f" ({item.get('citation')})" if item.get('citation') else ""
            sec_str = f" [{item.get('section_label')}]" if item.get('section_label') else ""
            formatted.append(
                f"### {item.get('title')}{cite_str}{sec_str}\n"
                f"**Document ID:** `{item.get('document_id')}` | **Jurisdiction:** {item.get('jurisdiction')} | **Authority:** {item.get('authority_type')} | **RRF Score:** {float(item.get('rrf_score', 0)):.4f}\n"
                f"**Statute / Case Excerpt:**\n{item.get('content')}"
            )
        return "\n\n---\n\n".join(formatted)
    except Exception as e:
        return f"Legal search error: {e}"


@tool
def fetch_legal_authority(document_id: str) -> str:
    """Fetches the complete statutory or judicial text across all chunks for a specific document_id returned by search_legal_authorities.
    Use this when a retrieved passage is insufficient and you need to inspect full surrounding context, exceptions, or definitions."""
    db_url = os.getenv("DATABASE_URL", "postgresql://postgres:password@127.0.0.1:5433/arbitration")
    try:
        try:
            import psycopg
        except ImportError:
            return "Legal database search unavailable: psycopg not installed."

        sql = """
        SELECT chunk_index, section_label, content, title, citation, jurisdiction, court, source_url
        FROM legal_knowledge
        WHERE document_id = %(document_id)s
          AND is_current = TRUE
        ORDER BY chunk_index ASC;
        """
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"document_id": document_id.strip()})
                rows = cur.fetchall()

        if not rows:
            return f"Legal authority '{document_id}' not found in database."

        first = rows[0]
        title, citation, jurisdiction, court, source_url = first[3], first[4], first[5], first[6], first[7]
        cite_str = f" ({citation})" if citation else ""
        header = f"# {title}{cite_str}\n**Document ID:** `{document_id}` | **Jurisdiction:** {jurisdiction} | **Court/Source:** {court or source_url or 'Authoritative'}\n"
        
        chunks_text = []
        for r in rows:
            sec_header = f"### Section: {r[1]}" if r[1] else f"### Chunk [{r[0]}]"
            chunks_text.append(f"{sec_header}\n{r[2]}")

        return header + "\n\n" + "\n\n".join(chunks_text)
    except Exception as e:
        return f"Error fetching legal authority: {e}"

@router.post("/run", response_class=HTMLResponse)
async def run_adjudication(request: Request, caseId: str = Form(...), db: Session = Depends(get_db), admin: str = Depends(verify_admin_token)):
    """Executes the two-stage AI adjudication process."""
    case = db.query(Case).filter(Case.case_id == caseId).first()
    if not case:
        return HTMLResponse("Case not found", status_code=404)

    if case.status != StatusEnum.DISPUTED:
        return HTMLResponse("Case is not in DISPUTED status.", status_code=400)

    # LOCK the case immediately before running agents (n8n: Record Status node fires in parallel
    # at the same time as Get Messages / Get Files — before Prepare Case Packet)
    case.status = StatusEnum.PROCESSING_LOCKED
    case.adjudication_time = datetime.now(timezone.utc)
    db.commit()
    db.refresh(case)

    try:
        messages = db.query(Message).filter(Message.case_id == caseId).order_by(Message.time).all()
        # 1. Prepare Case Packet
        escrow_fund = int(case.escrow_fund or 0)
        refund_to_buyer = int(case.refund_to_buyer or 0)
        payment_to_seller = int(case.payment_to_seller or 0)
    
        raw_available = escrow_fund - refund_to_buyer - payment_to_seller
        escrow_balance = raw_available if raw_available > 0 else 0
    
        msg_log = "\n\n".join([f"**{m.time.strftime('%Y-%m-%d %H:%M UTC') if m.time else 'Unknown Time'} [{m.sender.value if m.sender else 'Unknown'}]**: {m.content}" for m in messages])
    
        file_table = read_evidence_files(caseId, db)
    
        signed_time = case.created_at.strftime('%Y-%m-%d %H:%M UTC') if case.created_at else 'No Signing Time'
        time_now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    
        user_prompt_text = f"""
    # Case #{case.case_id}  

    **Current Time:** {time_now}  
    **Contract Signed:** {signed_time}  
    **Governing Law / Jurisdiction:** {case.governing_law or 'US-Commercial (Default)'}

    ## Parties: 
    - **Buyer:** {case.buyer} ({case.buyer_email})
    - **Seller:** {case.seller} ({case.seller_email})

    ## Financials: 
    - **Total Escrow Fund:** {escrow_fund} Wei
    - **Refunded to Buyer:** {refund_to_buyer} Wei
    - **Paid to Seller:** {payment_to_seller} Wei
    - **Escrow Balance:** {escrow_balance} Wei

    ## Evidence Files: 
    {file_table}

    ## Messages:
    {msg_log if msg_log else "_No messages submitted._"}

    ## Contract Text:
    ```
    {case.contract_text or "No contract text provided."}
    ```
    """
        user_prompt = [{"type": "text", "text": user_prompt_text}]
    
        # Multimodal: append images directly to the user prompt for the Magistrate
        db_files = db.query(File).filter(File.case_id == caseId).all()
        for f in db_files:
            ext = f.original_name.lower().split('.')[-1]
            if ext in ['jpg', 'jpeg', 'png', 'webp']:
                import base64
                file_path = os.path.join(UPLOAD_DIR, f.secure_name)
                try:
                    with open(file_path, 'rb') as img_file:
                        b64 = base64.b64encode(img_file.read()).decode('utf-8')
                        user_prompt.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/{ext};base64,{b64}"}
                        })
                except Exception:
                    pass

        # 2. Setup LangChain Models with Fallbacks
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        gemini_api_key = os.environ.get("GEMINI_API_KEY")
    
        deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY")
    
        if not openai_api_key:
            return HTMLResponse(
                "<h1>AI Setup Required</h1><p>Error: <code>OPENAI_API_KEY</code> environment variable is not set. Please set it in your terminal to run the AI Adjudicator.</p>", 
                status_code=500
            )
    
        # Magistrate LLM (Multimodal, Heavy, Slow)
        magistrate_primary = ChatOpenAI(model="gpt-4o", temperature=0)
        magistrate_fallbacks = []
        if gemini_api_key:
            magistrate_fallbacks.append(ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0))
        magistrate_fallbacks.append(ChatOpenAI(model="gpt-4o-mini", temperature=0))
        magistrate_llm = magistrate_primary.with_fallbacks(magistrate_fallbacks)

        # Final Judge LLM (Text-only, Fast, Efficient)
        judge_primary = ChatOpenAI(model="gpt-4o-mini", temperature=0)
        judge_fallbacks = []
        if deepseek_api_key:
            judge_fallbacks.append(ChatOpenAI(model="deepseek-chat", temperature=0, api_key=deepseek_api_key, base_url="https://api.deepseek.com/v1"))
        if gemini_api_key:
            judge_fallbacks.append(ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0))
        judge_fallbacks.append(ChatOpenAI(model="gpt-3.5-turbo", temperature=0))
        judge_llm = judge_primary.with_fallbacks(judge_fallbacks)
    
        # Create the agent
        magistrate_prompt = ChatPromptTemplate.from_messages([
            ("system", 
             "You are the AI Magistrate Judge in a two-stage arbitration system. Investigate the case details, analyze evidence, and prepare a neutral, evidence-grounded report for the Final Judge.\n\n"
             "LEGAL RESEARCH RULES:\n"
             "- Use 'search_legal_authorities' to research applicable commercial law, UCC statutes, regulations, or precedents.\n"
             "- When an excerpt is insufficient, use 'fetch_legal_authority' to inspect the full statutory text or exceptions.\n"
             "- Do not rely on unverified memory for legal citations; cite only authorities returned by your tools.\n"
             "- Never invent citations, statutes, or holdings.\n\n"
             "CASE ANALYSIS & MATH RULES:\n"
             "- Use 'read_evidence_file' to examine uploaded evidence and 'external_verification' for URLs/tracking.\n"
             "- Use 'calculator' for exact damage award math.\n"
             "- Distinguish agreed facts, disputed facts, and unsupported claims.\n"
             "- Provide a factual summary, cite governing legal authorities, and calculate the exact proposed Wei payouts (must sum to escrow_balance)."),
            MessagesPlaceholder(variable_name="input"),
            ("placeholder", "{agent_scratchpad}")
        ])
    
        magistrate_tools = [
            read_evidence_file,
            calculator,
            external_verification,
            search_legal_authorities,
            fetch_legal_authority,
        ]
        agent = create_tool_calling_agent(magistrate_llm, magistrate_tools, magistrate_prompt)
        agent_executor = AgentExecutor(agent=agent, tools=magistrate_tools, verbose=True)
    
        raw_report = (await asyncio.to_thread(agent_executor.invoke, {"input": [HumanMessage(content=user_prompt)]}))["output"]
        # Extract the structured JSON from the raw report text using the fast judge_llm
        magistrate_report = await asyncio.to_thread(
            judge_llm.with_structured_output(MagistrateReport).invoke,
            f"Extract the magistrate report strictly matching the JSON schema from this text:\n\n{raw_report}"
        )
    
        # Mathematical Validation of Magistrate Report
        try:
            m_buyer_wei = int(magistrate_report.recommended_buyer_payout)
            m_seller_wei = int(magistrate_report.recommended_seller_payout)
            if m_buyer_wei + m_seller_wei != escrow_balance:
                error_msg = f"Magistrate math failure: Awards ({m_buyer_wei} + {m_seller_wei}) != Escrow Balance ({escrow_balance})"
                print(error_msg)
                from routers.exceptions import _send_admin_alert
                _send_admin_alert(error_msg)
                case.status = StatusEnum.PROCESSING_LOCKED
                db.commit()
                return {"status": "error", "message": error_msg}
        except Exception as e:
            error_msg = f"Magistrate Validation Error: {e}"
            print(error_msg)
            from routers.exceptions import _send_admin_alert
            _send_admin_alert(error_msg)
            case.status = StatusEnum.PROCESSING_LOCKED
            db.commit()
            return {"status": "error", "message": error_msg}
        
        # Audit Trail: Persist AI Report
        os.makedirs("storage/evidence", exist_ok=True)
        report_path = f"storage/evidence/{case.case_id}_magistrate_report.json"
        with open(report_path, "w") as rf:
            rf.write(magistrate_report.model_dump_json(indent=2))
        
        # 4. Final Judge Stage
        final_judge_llm = judge_llm.with_structured_output(FinalRuling)
    
        judge_system_prompt = (
            "You are the Final Judge issuing legally binding rulings in an arbitration system.\n\n"
            "## AUTHORITY & CONSTRAINTS\n\n"
            "You may approve, modify, or reject the Magistrate's recommendation. You must:\n"
            "- Distribute exactly the escrow balance (no more, no less)\n"
            "- Use non-negative Wei integers only (as strings)\n"
            "- Justify any deviation from the Magistrate's recommendation\n"
            "- Base decisions solely on the provided report (no new evidence, no invented facts)\n\n"
            "## LEGAL PRINCIPLES\n\n"
            "- Both parties bear equal burden of proof.\n"
            "- Apply contract law; honor explicit obligations.\n"
            "- When evidence conflicts, favor documented proof over verbal claims.\n"
            "- Remain strictly neutral.\n\n"
            "## REVIEW CHECKLIST\n\n"
            "1. **Math:** Ensure buyer_payout + seller_payout == escrow_balance.\n"
            "2. **Evidence:** Confirm reasoning aligns with verified facts and contract terms; weigh contradictions.\n"
            "3. **Fairness:** Ensure split reflects evidence and contract terms.\n"
            "4. **Finality:** This is irreversible; your decision executes immediately.\n\n"
            "## CRITICAL RULES\n\n"
            "- All payouts must be non-negative Wei integers as strings.\n"
            "- Sum of buyer_award and seller_award MUST equal escrow_balance exactly.\n"
            "- All amounts >= 0, no decimals, no rounding.\n"
            "- Use ONLY the escrow balance provided (no fund creation/destruction).\n"
            "- No new investigation—rely solely on Magistrate's report.\n"
            "- Output ONLY valid JSON matching the schema (no extra commentary).\n"
        )
    
        final_ruling = await asyncio.to_thread(
            final_judge_llm.invoke,
            [
                {"role": "system", "content": judge_system_prompt},
                {"role": "user", "content": f"Escrow Balance Requirement: {escrow_balance} Wei\n\nMagistrate Report:\n{magistrate_report.model_dump_json()}"}
            ]
        )
        
        # 5. Math Validation & Database Update
        buyer_award_int = int(final_ruling.buyer_award)
        seller_award_int = int(final_ruling.seller_award)
        if buyer_award_int + seller_award_int != escrow_balance:
            raise ValueError(f"Math Error: Awards ({buyer_award_int} + {seller_award_int}) != Escrow Balance ({escrow_balance})")
        
        # Commit final ruling to DB (n8n: Record Determination node)
        # Status becomes DECIDED, Determination Time recorded
        # NOTE: adjudication_time was already set above when the case was locked
        case.status = StatusEnum.DECIDED_LOCKED
        case.determination_time = datetime.now(timezone.utc)
        case.decision = final_ruling.decision
        case.rationale = final_ruling.rationale
        case.magistrate_report_json = magistrate_report.model_dump_json(indent=2)
        case.buyer_award = buyer_award_int
        case.seller_award = seller_award_int

        db.commit()

        # Audit Trail: Persist Final Ruling
        os.makedirs("storage/evidence", exist_ok=True)
        ruling_path = f"storage/evidence/{case.case_id}_final_ruling.md"
        with open(ruling_path, "w") as rf:
            rf.write(f"# Final Ruling for {case.case_id}\n\n**Decision:** {final_ruling.decision}\n\n**Rationale:** {final_ruling.rationale}\n\n**Buyer Award:** {buyer_award_int} Wei\n**Seller Award:** {seller_award_int} Wei\n\n**Confidence:** {final_ruling.confidence}")
            
    except Exception as e:
        print(f"Adjudication Pipeline Failed: {e}")
        # Revert the lock so it can be retried
        case.status = StatusEnum.DISPUTED
        db.commit()
        return HTMLResponse(f"Internal Adjudication Error: {e}", status_code=500)


    # Send determination emails to both parties
    email_service.send_determination(
        case_id=case.case_id,
        seller_name=case.seller, seller_email=case.seller_email, seller_token=case.seller_token,
        buyer_name=case.buyer,   buyer_email=case.buyer_email,   buyer_token=case.buyer_token,
        decision=final_ruling.decision,
        seller_award_eth=seller_award_int / 1e18,
        buyer_award_eth=buyer_award_int  / 1e18,
    )

    return templates.TemplateResponse("adjudication_result.html", {
        "request": request,
        "case": case,
        "magistrate": magistrate_report,
        "ruling": final_ruling,
        "timestamp": case.determination_time.strftime('%Y-%m-%d %H:%M UTC')
    })
