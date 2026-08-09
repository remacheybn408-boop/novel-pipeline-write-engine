import pytest

from proseforge.domain.agents.policy import PolicyDenied, authorize, check_children


def test_roles_cannot_write_versions_or_self_elevate():
    with pytest.raises(PolicyDenied): authorize("scene_writer", "create_chapter_version")
    with pytest.raises(PolicyDenied): authorize("chief_editor", "create_revision", policy_version="old")
    with pytest.raises(PolicyDenied): check_children("chief_planner", 5)
    authorize("chief_editor", "create_revision")
