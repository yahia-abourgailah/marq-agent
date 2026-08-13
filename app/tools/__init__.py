"""
LangChain tools available to the domain agents.

`sql_query` is the only path to CRM data. Everything else operates on numbers
the agent already holds and never touches the database.

ANALYSIS_TOOLS are domain-neutral and go to every agent. LEADS_TOOLS are
attached to the Leads domain only — see app/graph/agents/domain.py.
"""

from app.tools.analysis import ANALYSIS_TOOLS
from app.tools.leads import LEADS_TOOLS
from app.tools.sql import SQLTool, build_sql_tool

__all__ = [
    "ANALYSIS_TOOLS",
    "LEADS_TOOLS",
    "SQLTool",
    "build_sql_tool",
]
