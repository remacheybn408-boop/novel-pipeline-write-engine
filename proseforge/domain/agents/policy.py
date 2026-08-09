from __future__ import annotations

from proseforge.domain.agents.roles import CATALOG, AgentRole

POLICY_VERSION = "v3-policy-1"

class PolicyDenied(PermissionError): pass

def _grants(policy) -> dict[str, bool]:
    # 能力授予表：只有显式映射且为 True 的能力才放行，其余一律拒绝（fail-closed）。
    # create_chapter_version / activate_memory_fact 对所有角色恒为 False —— 仅用户审批可执行。
    return {
        "read_project_facts": True,
        "create_artifact": True,
        "create_revision": policy.can_create_revision,
        "create_revision_proposal": policy.can_create_revision,
        "activate_fact": policy.can_activate_facts,
        "activate_memory_fact": policy.can_activate_facts,
        "create_chapter_version": policy.can_create_chapter_version,
        "call_external_tools": bool(policy.tools),
    }

def authorize(role: AgentRole | str, capability: str, *, policy_version: str = POLICY_VERSION) -> None:
    selected = AgentRole(role); policy = CATALOG[selected]
    if policy.policy_version != policy_version: raise PolicyDenied("policy version mismatch")
    allowed = _grants(policy)
    if not allowed.get(capability, False): raise PolicyDenied(f"{selected.value} cannot {capability}")

def check_children(role: AgentRole | str, child_count: int) -> None:
    policy = CATALOG[AgentRole(role)]
    if child_count > policy.max_children: raise PolicyDenied("maximum child count exceeded")
