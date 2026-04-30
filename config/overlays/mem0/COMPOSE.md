# mem0 Reference Overlay

Config overlay for analyzing the mem0 reference repository (mem0ai/mem0).

## Purpose

This overlay configures a synapt agent workspace for structured analysis
of the mem0 codebase. mem0 is a memory layer for AI agents that scores
J=66.88 on the LOCOMO benchmark.

## Architecture Context

mem0 uses a vector-store-backed memory system with these core components:

- `mem0/memory/` — Memory class: add, search, update, delete operations
- `mem0/vector_stores/` — Pluggable vector backends (Qdrant default)
- `mem0/llms/` — LLM provider abstraction for memory extraction
- `mem0/embeddings/` — Embedding provider abstraction
- `mem0/configs/` — Pydantic config models for all components

Key patterns to analyze:
- Memory lifecycle: add -> extract facts -> embed -> store -> search -> retrieve
- Graph memory mode (Neo4j) vs vector-only mode
- Client/server split: `mem0/client/` for hosted API, `mem0/memory/` for local

## Evaluation Baseline

LOCOMO J-score: 66.88 (single-session, vector-only mode).
