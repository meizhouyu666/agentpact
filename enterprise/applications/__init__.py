"""Application composition roots that wire platform services to concrete Domain Packs.

Modules in this package may depend inward on both generic platform packages and
concrete adapters. Platform and Domain Pack packages must not depend back on them.
"""

from .agent_runs import AgentRunComposition, compose_agent_run_service, mount_agent_run_application

__all__ = ["AgentRunComposition", "compose_agent_run_service", "mount_agent_run_application"]
