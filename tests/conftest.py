import pytest


@pytest.fixture(autouse=True)
def _no_user_skills(tmp_path_factory, monkeypatch):
    # The real user Skill directory never leaks into a test; tests that need one set ZHIQIU_SKILLS_DIR.
    monkeypatch.setenv("ZHIQIU_SKILLS_DIR", str(tmp_path_factory.getbasetemp() / "no-user-skills"))
