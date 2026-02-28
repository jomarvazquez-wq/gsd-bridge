"""
GSD Bridge v3 — Unified state system for GSD ↔ Codex/Superpowers.

Three-part contract:
  Export:     GSD plans → Superpowers markdown + manifest + state files
  Execute:    Codex reads manifest, updates state files
  Reconcile:  Bridge reads state, generates dashboard, detects drift
"""

__version__ = "3.0.0"
