"""Orchestration boundary: drives planner output through tools, subject to
backend.core.security.PermissionManager, and reconciles results with memory.

No orchestration logic (e.g. a LangGraph graph) is implemented in Phase 0 —
this package exists to establish the boundary between planning, tool
execution and memory, not to execute anything yet.
"""
