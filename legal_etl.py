"""
legal_etl.py

ETL pipeline for statutes, regulations, legislation, and judicial precedents into pgvector.

Environment:
    DATABASE_URL=postgresql://postgres:password@localhost:5433/arbitration
    LEGAL_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
    LEGAL_CHUNK_SIZE=1000
    LEGAL_CHUNK_OVERLAP=150

Usage:
    python legal_etl.py legal_sources.json
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from pypdf import PdfReader
except ImportError:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        PdfReader = None

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError:
    from langchain.text_splitter import RecursiveCharacterTextSplitter

try:
    from langchain_huggingface import HuggingFaceEmbeddings
except ImportError:
    HuggingFaceEmbeddings = None

try:
    import psycopg
    from pgvector.psycopg import register_vector
    from psycopg.types.json import Jsonb
except ImportError:
    psycopg = None
    register_vector = None
    Jsonb = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@127.0.0.1:5433/arbitration",
)

EMBEDDING_MODEL = os.getenv(
    "LEGAL_EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)

EMBEDDING_DIM = int(os.getenv("LEGAL_EMBEDDING_DIM", "384"))

# MiniLM token limit is 256 tokens (~1000 characters). Keeping chunk size aligned prevents silent truncation.
CHUNK_SIZE = int(os.getenv("LEGAL_CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.getenv("LEGAL_CHUNK_OVERLAP", "150"))

HTTP_TIMEOUT = 60

ALLOWED_SOURCE_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("ALLOWED_LEGAL_DOMAINS", "").split(",")
    if d.strip()
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger("legal-etl")


# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------

def get_embedding_model() -> Any:
    if HuggingFaceEmbeddings is None:
        raise ImportError(
            "langchain-huggingface or sentence-transformers is not installed. "
            "Please run `pip install langchain-huggingface sentence-transformers`"
        )
    return HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={
            "normalize_embeddings": True,
            "batch_size": 64,
        },
    )


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS legal_knowledge (
    id                  BIGSERIAL PRIMARY KEY,

    -- Stable authority identifier
    document_id         TEXT NOT NULL,

    -- Versioning
    version_hash        TEXT NOT NULL,
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,
    superseded_at       TIMESTAMPTZ,

    -- Authority metadata
    authority_type      TEXT NOT NULL,
    title               TEXT NOT NULL,
    citation            TEXT,
    jurisdiction        TEXT NOT NULL,
    court               TEXT,
    publisher           TEXT,

    precedential        BOOLEAN,
    status              TEXT NOT NULL DEFAULT 'active',

    decision_date       DATE,
    effective_from      DATE,
    effective_to        DATE,

    -- Source provenance
    source_url          TEXT,
    source_file         TEXT,
    source_hash         TEXT NOT NULL,
    retrieved_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Chunk provenance
    chunk_index         INTEGER NOT NULL,
    start_index         INTEGER,
    section_label       TEXT,

    content             TEXT NOT NULL,

    -- Arbitrary additional metadata
    metadata            JSONB NOT NULL DEFAULT '{{}}'::jsonb,

    -- Embedding
    embedding_model     TEXT NOT NULL,
    embedding           VECTOR({EMBEDDING_DIM}) NOT NULL,

    -- Lexical retrieval
    search_tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(title, '') || ' ' ||
            coalesce(citation, '') || ' ' ||
            coalesce(section_label, '') || ' ' ||
            coalesce(content, '')
        )
    ) STORED,

    UNIQUE(document_id, version_hash, chunk_index)
);

CREATE INDEX IF NOT EXISTS legal_knowledge_fts_idx
ON legal_knowledge
USING GIN(search_tsv);

CREATE INDEX IF NOT EXISTS legal_knowledge_embedding_hnsw_idx
ON legal_knowledge
USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS legal_knowledge_document_idx
ON legal_knowledge(document_id);

CREATE INDEX IF NOT EXISTS legal_knowledge_jurisdiction_idx
ON legal_knowledge(jurisdiction);

CREATE INDEX IF NOT EXISTS legal_knowledge_authority_type_idx
ON legal_knowledge(authority_type);

CREATE INDEX IF NOT EXISTS legal_knowledge_citation_idx
ON legal_knowledge(citation);

CREATE INDEX IF NOT EXISTS legal_knowledge_dates_idx
ON legal_knowledge(effective_from, effective_to);

CREATE INDEX IF NOT EXISTS legal_knowledge_current_idx
ON legal_knowledge(is_current);
"""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Preserve paragraph structure while cleaning extraction artifacts."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def validate_url(url: str) -> None:
    if not ALLOWED_SOURCE_DOMAINS:
        return
    hostname = (urlparse(url).hostname or "").lower()
    permitted = any(
        hostname == allowed or hostname.endswith("." + allowed)
        for allowed in ALLOWED_SOURCE_DOMAINS
    )
    if not permitted:
        raise ValueError(f"Source domain not approved: {hostname}")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_pdf_bytes(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        pages.append(f"\n[PAGE {page_number}]\n{page_text}")
    return normalize_text("\n".join(pages))


def extract_html(data: bytes) -> str:
    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return normalize_text(text)


def extract_local_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Source file does not exist: {path}")

    extension = path.suffix.lower()
    if extension == ".pdf":
        return extract_pdf_bytes(path.read_bytes())
    if extension in {".html", ".htm"}:
        return extract_html(path.read_bytes())
    if extension in {".txt", ".md"}:
        return normalize_text(path.read_text(encoding="utf-8"))
    raise ValueError(f"Unsupported local file format: {path}")


def fetch_and_extract(source: dict[str, Any]) -> str:
    if source.get("source_url"):
        url = source["source_url"]
        validate_url(url)
        headers = {"User-Agent": "LegalKnowledgeETL/1.0"}
        response = httpx.get(
            url,
            headers=headers,
            follow_redirects=True,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "application/pdf" in content_type or url.lower().endswith(".pdf"):
            return extract_pdf_bytes(response.content)
        if "html" in content_type or url.lower().endswith((".html", ".htm")):
            return extract_html(response.content)
        return normalize_text(response.text)

    if source.get("source_file"):
        return extract_local_file(Path(source["source_file"]))

    raise ValueError("Each source requires source_url or source_file.")


# ---------------------------------------------------------------------------
# Legal-aware chunking
# ---------------------------------------------------------------------------

LEGAL_SEPARATORS = [
    "\n§ ",
    "\nSECTION ",
    "\nSection ",
    "\nARTICLE ",
    "\nArticle ",
    "\nCHAPTER ",
    "\nChapter ",
    "\n[PAGE ",
    "\n\n",
    "\n",
    ". ",
    " ",
    "",
]

splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=LEGAL_SEPARATORS,
    add_start_index=True,
)

SECTION_PATTERNS = [
    r"(?im)^§\s*[^\n]+",
    r"(?im)^Section\s+[^\n]+",
    r"(?im)^ARTICLE\s+[^\n]+",
    r"(?im)^Chapter\s+[^\n]+",
    r"(?im)^\[PAGE\s+\d+\]",
]


def infer_section_label(chunk: str) -> str | None:
    for pattern in SECTION_PATTERNS:
        match = re.search(pattern, chunk)
        if match:
            return match.group(0).strip()[:300]
    return None


def make_chunks(
    text: str,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    docs = splitter.create_documents(
        [text],
        metadatas=[{"document_id": source["document_id"]}],
    )

    chunks = []
    for index, doc in enumerate(docs):
        chunks.append({
            "chunk_index": index,
            "start_index": doc.metadata.get("start_index"),
            "section_label": infer_section_label(doc.page_content),
            "content": doc.page_content.strip(),
        })
    return chunks


# ---------------------------------------------------------------------------
# Database Management & Schema
# ---------------------------------------------------------------------------

def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def current_version(
    conn: psycopg.Connection,
    document_id: str,
) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT version_hash
            FROM legal_knowledge
            WHERE document_id = %s
              AND is_current = TRUE
            LIMIT 1
            """,
            (document_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def build_embedding_text(
    source: dict[str, Any],
    chunk: dict[str, Any],
) -> str:
    """
    Including citation/title improves semantic retrieval without modifying
    the actual authoritative chunk stored in content.
    """
    return "\n".join(
        value
        for value in [
            source.get("title"),
            source.get("citation"),
            source.get("jurisdiction"),
            chunk.get("section_label"),
            chunk["content"],
        ]
        if value
    )


def ingest_source(
    conn: psycopg.Connection,
    source: dict[str, Any],
    embeddings: HuggingFaceEmbeddings,
) -> None:
    required = {
        "document_id",
        "authority_type",
        "title",
        "jurisdiction",
    }
    missing = required - source.keys()
    if missing:
        raise ValueError(f"Missing metadata fields: {sorted(missing)}")

    logger.info("Extracting %s", source["document_id"])
    text = fetch_and_extract(source)
    if not text:
        raise ValueError("Document produced no text.")

    version_hash = sha256_text(text)
    source_hash = version_hash

    existing = current_version(conn, source["document_id"])
    if existing == version_hash:
        logger.info("Unchanged (already up to date): %s", source["document_id"])
        return

    chunks = make_chunks(text, source)
    embedding_inputs = [
        build_embedding_text(source, chunk)
        for chunk in chunks
    ]

    logger.info(
        "Generating embeddings for %d chunks of %s...",
        len(chunks),
        source["document_id"],
    )
    vectors = embeddings.embed_documents(embedding_inputs)

    metadata_exclusions = {
        "document_id",
        "authority_type",
        "title",
        "citation",
        "jurisdiction",
        "court",
        "publisher",
        "precedential",
        "status",
        "decision_date",
        "effective_from",
        "effective_to",
        "source_url",
        "source_file",
    }

    additional_metadata = {
        k: v
        for k, v in source.items()
        if k not in metadata_exclusions
    }

    insert_sql = """
    INSERT INTO legal_knowledge (
        document_id,
        version_hash,
        authority_type,
        title,
        citation,
        jurisdiction,
        court,
        publisher,
        precedential,
        status,
        decision_date,
        effective_from,
        effective_to,
        source_url,
        source_file,
        source_hash,
        chunk_index,
        start_index,
        section_label,
        content,
        metadata,
        embedding_model,
        embedding
    )
    VALUES (
        %(document_id)s,
        %(version_hash)s,
        %(authority_type)s,
        %(title)s,
        %(citation)s,
        %(jurisdiction)s,
        %(court)s,
        %(publisher)s,
        %(precedential)s,
        %(status)s,
        %(decision_date)s,
        %(effective_from)s,
        %(effective_to)s,
        %(source_url)s,
        %(source_file)s,
        %(source_hash)s,
        %(chunk_index)s,
        %(start_index)s,
        %(section_label)s,
        %(content)s,
        %(metadata)s,
        %(embedding_model)s,
        %(embedding)s
    )
    """

    rows = []
    for chunk, vector in zip(chunks, vectors):
        rows.append({
            "document_id": source["document_id"],
            "version_hash": version_hash,
            "authority_type": source["authority_type"],
            "title": source["title"],
            "citation": source.get("citation"),
            "jurisdiction": source["jurisdiction"],
            "court": source.get("court"),
            "publisher": source.get("publisher"),
            "precedential": source.get("precedential"),
            "status": source.get("status", "active"),
            "decision_date": parse_date(source.get("decision_date")),
            "effective_from": parse_date(source.get("effective_from")),
            "effective_to": parse_date(source.get("effective_to")),
            "source_url": source.get("source_url"),
            "source_file": source.get("source_file"),
            "source_hash": source_hash,
            "chunk_index": chunk["chunk_index"],
            "start_index": chunk["start_index"],
            "section_label": chunk["section_label"],
            "content": chunk["content"],
            "metadata": Jsonb(additional_metadata),
            "embedding_model": EMBEDDING_MODEL,
            "embedding": np.asarray(vector, dtype=np.float32),
        })

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE legal_knowledge
                SET is_current = FALSE,
                    superseded_at = now()
                WHERE document_id = %s
                  AND is_current = TRUE
                """,
                (source["document_id"],),
            )
            cur.executemany(insert_sql, rows)

    logger.info("Successfully inserted %d chunks for: %s", len(rows), source["document_id"])


def run_etl(manifest_path: str) -> None:
    manifest_file = Path(manifest_path)
    if not manifest_file.exists():
        logger.error("Manifest file not found: %s", manifest_path)
        sys.exit(1)

    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    sources = manifest.get("sources", [])
    if not sources:
        logger.warning("No sources found in manifest.")
        return

    if psycopg is None:
        logger.error(
            "psycopg / pgvector is not installed. Please run `pip install -r requirements.txt`"
        )
        sys.exit(1)

    logger.info("Connecting to database at: %s", DATABASE_URL.split("@")[-1])
    with psycopg.connect(DATABASE_URL) as conn:
        register_vector(conn)
        ensure_schema(conn)

        logger.info("Loading embedding model '%s'...", EMBEDDING_MODEL)
        embeddings = get_embedding_model()

        for source in sources:
            doc_id = source.get("document_id", "unknown")
            try:
                ingest_source(conn, source, embeddings)
            except Exception:
                logger.exception("Failed ingestion for source: %s", doc_id)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python legal_etl.py <path_to_manifest.json>")
        sys.exit(1)

    run_etl(sys.argv[1])
