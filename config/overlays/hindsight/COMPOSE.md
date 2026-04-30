# hindsight Reference Overlay

Config overlay for analyzing the hindsight reference repository (vectorize-io/hindsight).

## Purpose

This overlay configures a synapt agent workspace for structured analysis
of the hindsight codebase. Hindsight is an agent memory system that
achieves state-of-the-art performance on both LOCOMO (J=72.0) and
LongMemEval benchmarks.

## Architecture Context

Hindsight is a monorepo with Python API, TypeScript control plane, and
embedding service. Key differences from vector-only memory systems:

- `hindsight-api-slim/hindsight_api/engine/` -- Core memory engine with reflect, consolidation, and query analysis
- `hindsight-api-slim/hindsight_api/engine/reflect/` -- Reflect pipeline: extracts structured knowledge from conversations
- `hindsight-api-slim/hindsight_api/engine/consolidation/` -- Memory consolidation and deduplication
- `hindsight-embed/` -- Embedding service (local MLX reranker + provider abstraction)
- `hindsight-control-plane/` -- TypeScript admin UI and management
- `hindsight-clients/python/` -- Python SDK

Key patterns to analyze:
- Reflect pipeline: conversations are processed through LLM-based extraction to produce structured "reflections" (facts, preferences, relationships)
- Memory consolidation: periodic deduplication and merging of overlapping memories
- Cross-encoder reranking: two-stage retrieval with embedding search followed by cross-encoder reranking
- Docker-first: PostgreSQL-backed, runs as a containerized service
- Dual benchmark leader: SOTA on both LOCOMO and LongMemEval

## Evaluation Baseline

LOCOMO J-score: 72.0 (SOTA as of 2026-01, independently reproduced by Virginia Tech).
LongMemEval: SOTA across all categories (information-extraction, temporal-reasoning, knowledge-update, abstention).
