"""taskwarden: approvals and audit for AI agents that act.

Modules: loop (one task's run), gate (who may run what), tools (Tool, risk tiers, running a
call), decisions (the owner's approve / edit / decline), sweeper (background runs), replies
(a customer's reply wakes the task), triggers (a schedule, an event or a
teammate's finished work wakes an agent), folders (agents as AGENT.md + skills), models (table
columns), host (what the app supplies). See README.md.
"""

__version__ = "0.7.0"
