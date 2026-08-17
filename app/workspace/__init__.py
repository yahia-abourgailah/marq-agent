"""
[claude] The workspace — user-uploaded files the agent can read and compare
against CRM data.

The CRM side of this application is read-only and schema-bound: the catalogue
describes exactly which tables exist and the guard permits nothing else. The
workspace is the opposite kind of surface. Its content arrives at runtime,
its shape is whatever the user uploaded, and none of it is trusted.

Three ideas hold it together:

1.  **A workspace is an isolation boundary.** Every file, every chunk and
    every search is scoped to a `workspace_id`. Nothing crosses.

2.  **Vectors find, readers compute.** Retrieval locates the relevant page or
    rows; exact values come from the parsed content the reader kept. A number
    the agent reports about a spreadsheet is never a number it remembered
    from a retrieved passage.

3.  **File content is data, never instructions.** An uploaded PDF can contain
    text addressed at the model. Tools label it as untrusted and the agent's
    prompt refuses to act on it.
"""

from __future__ import annotations

__all__: list[str] = []
