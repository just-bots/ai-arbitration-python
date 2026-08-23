import pytest
from unittest.mock import MagicMock, patch
from datetime import date
from routers.adjudication import (
    MagistrateReport,
    FinalRuling,
    search_legal_authorities,
    fetch_legal_authority,
)
from legal_etl import (
    normalize_text,
    sha256_text,
    infer_section_label,
    parse_date,
    validate_url,
    make_chunks,
)

def test_magistrate_report_schema_with_legal_authorities():
    report = MagistrateReport(
        summary="Buyer ordered goods that failed non-conformity inspection.",
        facts=["Seller delivered 10 units on Aug 1st", "Buyer rejected on Aug 3rd within 48h window"],
        contradictions=[],
        unsubstantiated_claims=[],
        applicable_legal_authorities=[
            "UCC § 2-601 (Perfect Tender Rule)",
            "Commercial Arbitration Rule 45(3) (Full Rescission for Non-Conformity)"
        ],
        reasoning="Under UCC § 2-601, buyer timely exercised right of rejection due to substantial non-conformity.",
        recommended_buyer_payout="1000000000000000000",
        recommended_seller_payout="0"
    )
    assert len(report.applicable_legal_authorities) == 2
    assert "UCC § 2-601" in report.applicable_legal_authorities[0]
    assert report.recommended_buyer_payout == "1000000000000000000"


def test_legal_etl_utilities():
    # Text normalization
    raw_text = "Section 1. \r\n\r\n\r\n  Terms of Sale.\n   Payment in escrow."
    normalized = normalize_text(raw_text)
    assert "\r" not in normalized
    assert "\n\n\n" not in normalized

    # Section labeling
    chunk_text = "\n§ 2-601. Buyer's Rights on Improper Delivery\nDelivery must conform."
    assert infer_section_label(chunk_text) == "§ 2-601. Buyer's Rights on Improper Delivery"

    # Date parsing
    assert parse_date("2026-08-01") == date(2026, 8, 1)
    assert parse_date(None) is None

    # Hashing
    assert sha256_text("sample") == sha256_text("sample")
    assert len(sha256_text("sample")) == 64


def test_make_chunks_and_section_tagging():
    sample_text = """
    # Contract Law Principles
    
    ## § 2-601. Buyer's Rights on Improper Delivery
    If the goods fail in any respect to conform to the contract, the buyer may reject the whole.
    
    ## § 2-714. Buyer's Damages for Breach
    Where the buyer has accepted goods, he may recover damages for non-conformity.
    """
    source = {"document_id": "TEST-UCC"}
    chunks = make_chunks(sample_text, source)
    assert len(chunks) >= 1
    assert all("document_id" not in c for c in chunks) # Metadata placed properly
    assert all("content" in c for c in chunks)


def test_search_legal_authorities_tool_mock():
    import sys
    mock_cursor = MagicMock()
    mock_desc = [
        MagicMock(name="id"),
        MagicMock(name="document_id"),
        MagicMock(name="authority_type"),
        MagicMock(name="title"),
        MagicMock(name="citation"),
        MagicMock(name="jurisdiction"),
        MagicMock(name="court"),
        MagicMock(name="precedential"),
        MagicMock(name="status"),
        MagicMock(name="decision_date"),
        MagicMock(name="effective_from"),
        MagicMock(name="effective_to"),
        MagicMock(name="section_label"),
        MagicMock(name="source_url"),
        MagicMock(name="chunk_index"),
        MagicMock(name="content"),
        MagicMock(name="rrf_score"),
    ]
    # Configure names
    names = [
        "id", "document_id", "authority_type", "title", "citation",
        "jurisdiction", "court", "precedential", "status", "decision_date",
        "effective_from", "effective_to", "section_label", "source_url",
        "chunk_index", "content", "rrf_score"
    ]
    for d, n in zip(mock_desc, names):
        d.name = n

    mock_cursor.description = mock_desc
    mock_cursor.fetchall.return_value = [
        (
            1,
            "UCC-ARTICLE-2-SALES",
            "statute",
            "Uniform Commercial Code - Article 2 (Sales)",
            "UCC § 2-601",
            "US-Commercial",
            None,
            True,
            "active",
            None,
            date(1951, 1, 1),
            None,
            "§ 2-601. Buyer's Rights on Improper Delivery",
            None,
            0,
            "Subject to the provisions of this Article... buyer may reject the whole.",
            0.032
        )
    ]
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_psycopg = MagicMock()
    mock_psycopg.connect.return_value = mock_conn

    mock_pgvector = MagicMock()
    mock_pgvector_psycopg = MagicMock()

    mock_embeddings = MagicMock()
    mock_embeddings.embed_query.return_value = [0.1] * 384

    with patch.dict(sys.modules, {"psycopg": mock_psycopg, "pgvector": mock_pgvector, "pgvector.psycopg": mock_pgvector_psycopg}), \
         patch("routers.adjudication.get_legal_embeddings", return_value=mock_embeddings):
        
        result = search_legal_authorities.invoke({"query": "buyer rejection remedies"})
        assert "Uniform Commercial Code - Article 2 (Sales)" in result
        assert "UCC § 2-601" in result
        assert "UCC-ARTICLE-2-SALES" in result


def test_fetch_legal_authority_tool_mock():
    import sys
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        (
            0,
            "§ 2-601. Buyer's Rights on Improper Delivery",
            "Buyer may reject the whole.",
            "Uniform Commercial Code - Article 2 (Sales)",
            "UCC § 2-601",
            "US-Commercial",
            None,
            None
        ),
        (
            1,
            "§ 2-608. Revocation of Acceptance",
            "Buyer may revoke acceptance if non-conformity substantially impairs value.",
            "Uniform Commercial Code - Article 2 (Sales)",
            "UCC § 2-601",
            "US-Commercial",
            None,
            None
        )
    ]
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_psycopg = MagicMock()
    mock_psycopg.connect.return_value = mock_conn

    with patch.dict(sys.modules, {"psycopg": mock_psycopg}):
        result = fetch_legal_authority.invoke({"document_id": "UCC-ARTICLE-2-SALES"})
        assert "Uniform Commercial Code - Article 2 (Sales)" in result
        assert "§ 2-601" in result
        assert "§ 2-608" in result
        assert "substantially impairs value" in result

