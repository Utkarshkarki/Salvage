"""Reclaim — AI Revenue Recovery Agent.

Deterministic state machine with two narrow LLM workers (Diagnose, Decide).

Pipeline: Ingest -> Diagnose -> Decide -> Act (bounded) -> Log.
The LLM *proposes*; code *disposes* (stopping rules are authoritative).
"""

__version__ = "0.1.0"