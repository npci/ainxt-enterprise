#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
drop_chromadb.py — ChromaDB removed.

The chroma_db/ directory can be deleted entirely:
    rm -rf chroma_db/

All vector data is in pgvector (document_embeddings table).
Use scripts/cleanup_kb_namespaces.py to clean stale Redis KB namespace entries.
"""
import os, shutil, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CHROMA_DIR = os.path.join(ROOT, "chroma_db")

if os.path.isdir(CHROMA_DIR):
    confirm = input(f"Delete {CHROMA_DIR}? [y/N] ").strip().lower()
    if confirm == "y":
        shutil.rmtree(CHROMA_DIR)
        print(f"Deleted {CHROMA_DIR}")
    else:
        print("Aborted.")
else:
    print(f"{CHROMA_DIR} does not exist — nothing to do.")
