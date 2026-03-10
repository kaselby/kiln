"""
Embedding-based semantic search over the agent's memory system.

Uses nomic-ai/modernbert-embed-base (8192 token context, 768-dim).
Indexes: latent notes, session summaries, facts, buffer entries,
project memory, people, preferences.

Storage: sqlite database at <agent_home>/cache/memory-index.db
"""

import sqlite3
import hashlib
import os
import sys
import glob
import json
import time
import numpy as np
from pathlib import Path
from datetime import datetime

AGENT_HOME = os.environ.get("AGENT_HOME", os.path.expanduser("~/.agent"))
MEMORY_DIR = os.path.join(AGENT_HOME, "memory")
CACHE_DIR = os.path.join(AGENT_HOME, "cache")
DB_PATH = os.path.join(CACHE_DIR, "memory-index.db")
MODEL_NAME = "nomic-ai/modernbert-embed-base"

# Lazy-loaded model singleton
_model = None


def _get_model():
    global _model
    if _model is None:
        import logging
        # Suppress model loading noise
        logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
        logging.getLogger("transformers").setLevel(logging.WARNING)
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        # Redirect tqdm progress bars to devnull during load
        import io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME, device="cpu")
        finally:
            sys.stderr = old_stderr
    return _model


def _get_db():
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            chunk_key TEXT UNIQUE NOT NULL,
            content_hash TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            title TEXT,
            snippet TEXT,
            embedding BLOB NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(doc_type)")
    return conn


def _content_hash(content):
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _extract_title(content, fallback=""):
    """Extract title from markdown content (first # heading)."""
    for line in content.split("\n"):
        if line.startswith("# ") and not line.startswith("# ---"):
            return line[2:].strip()
    return fallback


def _collect_documents():
    """Collect all embeddable memory documents with metadata."""
    docs = []

    # --- Latent notes (one doc per file) ---
    notes_dir = os.path.join(MEMORY_DIR, "latent/notes")
    if os.path.isdir(notes_dir):
        for f in sorted(glob.glob(os.path.join(notes_dir, "*.md"))):
            with open(f) as fh:
                content = fh.read()
            name = os.path.basename(f)
            fallback = name.replace(".md", "").replace("-", " ").title()
            docs.append({
                "source": f"latent/notes/{name}",
                "chunk_key": f"latent/notes/{name}",
                "content": content,
                "type": "latent_note",
                "title": _extract_title(content, fallback),
            })

    # --- Session summaries (one doc per file) ---
    sessions_dir = os.path.join(MEMORY_DIR, "sessions")
    if os.path.isdir(sessions_dir):
        for f in sorted(glob.glob(os.path.join(sessions_dir, "*.md"))):
            with open(f) as fh:
                content = fh.read()
            name = os.path.basename(f)
            docs.append({
                "source": f"sessions/{name}",
                "chunk_key": f"sessions/{name}",
                "content": content,
                "type": "session",
                "title": _extract_title(content, name.replace(".md", "")),
            })

    # --- Facts (one doc per entry) ---
    facts_file = os.path.join(MEMORY_DIR, "facts.md")
    if os.path.exists(facts_file):
        with open(facts_file) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("- ["):
                    h = _content_hash(line)
                    docs.append({
                        "source": "facts.md",
                        "chunk_key": f"facts.md:{h}",
                        "content": line,
                        "type": "fact",
                        "title": line[2:82] if len(line) > 82 else line[2:],
                    })

    # --- Buffer entries (one doc per entry) ---
    buffer_file = os.path.join(MEMORY_DIR, "buffer.md")
    if os.path.exists(buffer_file):
        with open(buffer_file) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("- ["):
                    h = _content_hash(line)
                    docs.append({
                        "source": "buffer.md",
                        "chunk_key": f"buffer.md:{h}",
                        "content": line,
                        "type": "buffer",
                        "title": line[2:82] if len(line) > 82 else line[2:],
                    })

    # --- Project memory (one doc per file) ---
    projects_dir = os.path.join(AGENT_HOME, "projects")
    if os.path.isdir(projects_dir):
        for f in glob.glob(os.path.join(projects_dir, "*/memory.md")):
            with open(f) as fh:
                content = fh.read()
            rel = os.path.relpath(f, AGENT_HOME)
            project = os.path.basename(os.path.dirname(f))
            docs.append({
                "source": rel,
                "chunk_key": rel,
                "content": content,
                "type": "project_memory",
                "title": f"Project: {project}",
            })

    # --- People ---
    people_file = os.path.join(MEMORY_DIR, "people.md")
    if os.path.exists(people_file):
        with open(people_file) as fh:
            content = fh.read()
        docs.append({
            "source": "memory/people.md",
            "chunk_key": "memory/people.md",
            "content": content,
            "type": "people",
            "title": "People",
        })

    # --- Preferences ---
    prefs_file = os.path.join(MEMORY_DIR, "latent/preferences.md")
    if os.path.exists(prefs_file):
        with open(prefs_file) as fh:
            content = fh.read()
        docs.append({
            "source": "memory/latent/preferences.md",
            "chunk_key": "memory/latent/preferences.md",
            "content": content,
            "type": "preferences",
            "title": "Preferences",
        })

    return docs


def build_index(force=False, quiet=False):
    """Build or incrementally update the embedding index."""
    conn = _get_db()

    docs = _collect_documents()

    # What's already indexed?
    existing = {}
    for row in conn.execute("SELECT chunk_key, content_hash FROM chunks"):
        existing[row[0]] = row[1]

    # Find what needs embedding
    to_embed = []
    current_keys = set()
    for doc in docs:
        current_keys.add(doc["chunk_key"])
        h = _content_hash(doc["content"])
        if force or doc["chunk_key"] not in existing or existing[doc["chunk_key"]] != h:
            doc["hash"] = h
            to_embed.append(doc)

    # Remove stale entries
    stale = set(existing.keys()) - current_keys
    if stale:
        for key in stale:
            conn.execute("DELETE FROM chunks WHERE chunk_key = ?", (key,))

    if not to_embed and not stale:
        if not quiet:
            print(f"Index up to date ({len(existing)} documents)")
        conn.close()
        return

    # Embed new/changed documents
    if to_embed:
        model = _get_model()
        texts = ["search_document: " + d["content"] for d in to_embed]

        t0 = time.time()
        embeddings = model.encode(texts, show_progress_bar=False, batch_size=8)
        elapsed = time.time() - t0

        now = datetime.now().isoformat()
        for doc, emb in zip(to_embed, embeddings):
            snippet = doc["content"][:300].replace("\n", " ").strip()
            conn.execute("""
                INSERT OR REPLACE INTO chunks
                    (source, chunk_key, content_hash, doc_type, title, snippet, embedding, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                doc["source"], doc["chunk_key"], doc["hash"],
                doc["type"], doc["title"], snippet,
                emb.astype(np.float32).tobytes(), now
            ))

        conn.commit()
        if not quiet:
            print(f"Embedded {len(to_embed)} documents in {elapsed:.1f}s")

    if stale and not quiet:
        print(f"Removed {len(stale)} stale entries")

    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if not quiet:
        print(f"Index total: {total} documents")

    conn.close()


def _needs_update():
    """Quick check: are there documents that aren't in the index or have changed?"""
    try:
        conn = _get_db()
        count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if count == 0:
            conn.close()
            return True

        existing = {}
        for row in conn.execute("SELECT chunk_key, content_hash FROM chunks"):
            existing[row[0]] = row[1]
        conn.close()

        docs = _collect_documents()
        current_keys = set()
        for doc in docs:
            current_keys.add(doc["chunk_key"])
            h = _content_hash(doc["content"])
            if doc["chunk_key"] not in existing or existing[doc["chunk_key"]] != h:
                return True

        # Check for deleted docs
        if set(existing.keys()) - current_keys:
            return True

        return False
    except Exception:
        return True


def search(query, top_k=10, doc_types=None, min_score=0.0):
    """Semantic search across the memory index.

    Auto-updates the index if any source files have changed.
    Returns list of {source, type, title, snippet, score}.
    """
    # Auto-update if stale
    if _needs_update():
        build_index(quiet=True)

    conn = _get_db()

    count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if count == 0:
        conn.close()
        return []

    model = _get_model()
    query_emb = model.encode(
        ["search_query: " + query], show_progress_bar=False
    )[0].astype(np.float32)

    # Load embeddings
    if doc_types:
        placeholders = ",".join("?" * len(doc_types))
        rows = conn.execute(
            f"SELECT source, doc_type, title, snippet, embedding FROM chunks WHERE doc_type IN ({placeholders})",
            doc_types
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source, doc_type, title, snippet, embedding FROM chunks"
        ).fetchall()

    conn.close()

    # Compute similarities
    results = []
    q_norm = np.linalg.norm(query_emb)
    for source, dtype, title, snippet, emb_bytes in rows:
        emb = np.frombuffer(emb_bytes, dtype=np.float32)
        sim = float(np.dot(query_emb, emb) / (q_norm * np.linalg.norm(emb)))
        if sim >= min_score:
            results.append({
                "source": source,
                "type": dtype,
                "title": title,
                "snippet": snippet,
                "score": sim,
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def index_status():
    """Print index statistics."""
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if total == 0:
        print("Index empty. Run 'memory index' to build.")
        conn.close()
        return

    by_type = conn.execute(
        "SELECT doc_type, COUNT(*) FROM chunks GROUP BY doc_type ORDER BY doc_type"
    ).fetchall()
    last_update = conn.execute("SELECT MAX(updated_at) FROM chunks").fetchone()[0]
    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    conn.close()

    print(f"Memory index: {total} documents ({db_size/1024:.0f} KB)")
    for dtype, count in by_type:
        print(f"  {dtype}: {count}")
    if last_update:
        print(f"Last updated: {last_update[:19]}")


# --- CLI entry point ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Memory embedding index")
    sub = parser.add_subparsers(dest="command")

    # index
    idx = sub.add_parser("index", help="Build/update the embedding index")
    idx.add_argument("--force", action="store_true", help="Force full rebuild")
    idx.add_argument("--quiet", action="store_true")

    # search
    srch = sub.add_parser("search", help="Semantic search")
    srch.add_argument("query", nargs="+")
    srch.add_argument("-n", "--top", type=int, default=10)
    srch.add_argument("--type", action="append", dest="types", help="Filter by doc type")
    srch.add_argument("--min-score", type=float, default=0.0)
    srch.add_argument("--json", action="store_true", help="Output as JSON")

    # status
    sub.add_parser("status", help="Show index statistics")

    args = parser.parse_args()

    if args.command == "index":
        build_index(force=args.force, quiet=args.quiet)
    elif args.command == "search":
        query = " ".join(args.query)
        results = search(query, top_k=args.top, doc_types=args.types, min_score=args.min_score)
        if args.json:
            print(json.dumps(results, indent=2))
        elif not results:
            print("No results. Is the index built? Run 'memory index' first.")
        else:
            for r in results:
                score_bar = "█" * int(r["score"] * 20) + "░" * (20 - int(r["score"] * 20))
                print(f"  {r['score']:.3f} {score_bar}  {r['source']}")
                print(f"         {r['title']}")
                if r["snippet"] and len(r["snippet"]) > 10:
                    snip = r["snippet"][:120] + ("..." if len(r["snippet"]) > 120 else "")
                    print(f"         {snip}")
                print()
    elif args.command == "status":
        index_status()
    else:
        parser.print_help()
