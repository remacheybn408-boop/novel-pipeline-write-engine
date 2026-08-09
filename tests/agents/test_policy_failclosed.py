import base64

import pytest
from pydantic import SecretStr

from proseforge.application.agents.policy_snapshot import (
    build_snapshot,
    canonical_json,
    sign,
    verify,
)
from proseforge.domain.agents.policy import (
    POLICY_VERSION,
    PolicyDenied,
    authorize,
    check_children,
)
from proseforge.domain.agents.roles import CATALOG, AgentRole


def test_unlisted_capability_is_denied_for_every_role():
    # fail-closed：能力表里没有的能力一律拒绝，而不是默认放行。
    for role in AgentRole:
        with pytest.raises(PolicyDenied):
            authorize(role, "delete_project")
        with pytest.raises(PolicyDenied):
            authorize(role, "shell_exec")


def test_reviewer_cannot_activate_memory_fact():
    with pytest.raises(PolicyDenied):
        authorize("continuity_reviewer", "activate_memory_fact")
    with pytest.raises(PolicyDenied):
        authorize("world_builder", "activate_fact")


def test_no_role_can_create_chapter_version_not_even_chief_editor():
    # ChapterVersion 只能由用户审批产生，任何角色（含 chief_editor）都不能直接创建。
    for role in AgentRole:
        with pytest.raises(PolicyDenied):
            authorize(role, "create_chapter_version")


def test_only_chief_editor_creates_revision_proposal():
    authorize("chief_editor", "create_revision_proposal")
    authorize("chief_editor", "create_revision")
    with pytest.raises(PolicyDenied):
        authorize("merge_editor", "create_revision_proposal")
    with pytest.raises(PolicyDenied):
        authorize("scene_writer", "create_revision")


def test_external_tools_denied_unless_policy_lists_tools():
    # 当前目录所有角色 tools 为空，默认全部拒绝。
    for role in AgentRole:
        with pytest.raises(PolicyDenied):
            authorize(role, "call_external_tools")


def test_read_facts_and_create_artifact_are_granted():
    for role in AgentRole:
        authorize(role, "read_project_facts")
        authorize(role, "create_artifact")


def test_role_cannot_elevate_itself_beyond_max_children():
    check_children("scene_writer", 4)
    with pytest.raises(PolicyDenied):
        check_children("scene_writer", 5)
    with pytest.raises(PolicyDenied):
        check_children("world_builder", 4)  # world_builder max_children=3


def test_policy_version_defaults_to_v3_and_mismatch_is_denied():
    assert POLICY_VERSION == "v3-policy-1"
    assert all(policy.policy_version == "v3-policy-1" for policy in CATALOG.values())
    authorize("chief_editor", "create_revision")
    with pytest.raises(PolicyDenied):
        authorize("chief_editor", "create_revision", policy_version="v1")
    with pytest.raises(PolicyDenied):
        authorize("chief_editor", "create_revision", policy_version="old")


def test_snapshot_is_canonical_versioned_and_signed():
    snapshot = build_snapshot()
    assert snapshot["policy_version"] == "v3-policy-1"
    assert set(snapshot["roles"]) == {role.value for role in AgentRole}
    assert canonical_json(snapshot) == canonical_json(build_snapshot())

    key = SecretStr("test-master-key")
    signature = sign(snapshot, key)
    assert len(signature) == 64
    assert verify(snapshot, signature, key)


def test_snapshot_verify_rejects_tampering_and_wrong_key():
    snapshot = build_snapshot()
    signature = sign(snapshot, "test-master-key")

    tampered = build_snapshot()
    tampered["roles"]["chief_editor"]["can_create_chapter_version"] = True
    assert not verify(tampered, signature, "test-master-key")
    assert not verify(snapshot, signature, "another-key")
    assert not verify(snapshot, "0" * 64, "test-master-key")
    assert not verify(snapshot, None, "test-master-key")
    assert not verify(None, signature, "test-master-key")
    assert not verify("not-a-dict", signature, "test-master-key")


def test_master_key_base64_and_plaintext_fallback_both_verify():
    # 与 tasks.py 相同的解码：合法 base64 的 32 字节 key 直接用，其余 sha256 回退。
    snapshot = build_snapshot()
    b64_key = base64.b64encode(b"k" * 32).decode()
    assert verify(snapshot, sign(snapshot, b64_key), b64_key)
    assert verify(snapshot, sign(snapshot, "plain-key"), "plain-key")
    assert sign(snapshot, b64_key) != sign(snapshot, "plain-key")
