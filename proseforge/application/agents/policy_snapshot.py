"""策略快照的构建与 HMAC 签名校验。

run 创建时把角色目录的规范快照连同签名落库；控制动作与执行器加载 run 时
重新校验签名，任何对目录或快照的篡改都会导致校验失败（fail-closed）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json

from proseforge.domain.agents.policy import POLICY_VERSION
from proseforge.domain.agents.roles import CATALOG, AgentRole


def canonical_json(snapshot: dict[str, object]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_snapshot(catalog: dict[AgentRole, object] | None = None) -> dict[str, object]:
    selected = CATALOG if catalog is None else catalog
    roles: dict[str, object] = {}
    for role in sorted(selected, key=lambda item: item.value):
        policy = selected[role]
        roles[role.value] = {
            "artifact_types": sorted(policy.artifact_types),
            "tools": sorted(policy.tools),
            "max_tokens": policy.max_tokens,
            "max_children": policy.max_children,
            "can_activate_facts": policy.can_activate_facts,
            "can_create_revision": policy.can_create_revision,
            "can_create_chapter_version": policy.can_create_chapter_version,
        }
    return {"policy_version": POLICY_VERSION, "roles": roles}


def _key_bytes(master_key: object) -> bytes:
    # 与 workflows/tasks.py 的 master_key 解码保持一致：base64 优先，回退 sha256 到 32 字节。
    raw_value = master_key.get_secret_value() if hasattr(master_key, "get_secret_value") else str(master_key)
    try:
        raw = base64.b64decode(raw_value, validate=True)
    except (ValueError, binascii.Error):
        raw = b""
    if len(raw) != 32:
        raw = hashlib.sha256(raw_value.encode()).digest()
    return raw


def sign(snapshot: dict[str, object], master_key: object) -> str:
    message = f"{snapshot.get('policy_version', '')}|{canonical_json(snapshot)}".encode()
    return hmac.new(_key_bytes(master_key), message, hashlib.sha256).hexdigest()


def verify(snapshot: object, signature: object, master_key: object) -> bool:
    if not isinstance(snapshot, dict) or not isinstance(signature, str) or not signature:
        return False
    try:
        expected = sign(snapshot, master_key)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)
