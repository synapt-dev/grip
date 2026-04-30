# zep Reference Overlay

Config overlay for analyzing the zep reference repository (getzep/zep).

## Purpose

This overlay configures a synapt agent workspace for structured analysis
of the zep codebase. zep is a memory platform for AI assistants that
scores J=65.99 on the LOCOMO benchmark.

## Architecture Context

zep is a cloud-first memory platform with a thin Python SDK wrapping
a hosted GraphQL API. Key differences from local-first systems:

- `integrations/python/` -- Python SDK for the hosted Zep Cloud service
- `ontology/` -- Default ontology for knowledge graph extraction
- `zep-eval-harness/` -- Evaluation scripts for LOCOMO and LongMemEval
- `benchmarks/` -- Benchmark dataset configs (LOCOMO, LongMemEval)
- `legacy/` -- Self-hosted CE edition (Docker Compose, deprecated)

Key patterns to analyze:
- Cloud-first: memory storage and retrieval are API calls, not local operations
- Knowledge graph: entities and relationships extracted via ontology
- Dual benchmark: evaluates on both LOCOMO and LongMemEval
- SDK-thin pattern: most logic lives server-side, SDK is a transport layer

## Evaluation Baseline

LOCOMO J-score: 65.99 (cloud API, graph-enhanced retrieval).
LongMemEval: evaluated but scores vary by category.
