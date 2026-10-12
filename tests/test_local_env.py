import json
import os

import pytest
from typer.testing import CliRunner

from zhiqiu.cli import app
from zhiqiu.local_env import load_local_env


def test_local_env_loads_known_values_and_process_env_wins(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('''# 本地配置
ZHIQIU_LLM_BASE_URL=https://provider.example/v1
ZHIQIU_LLM_MODEL=file-model
ZHIQIU_LLM_API_KEY='private-key'
ZHIQIU_MODEL_BUDGETS='{"file-model":{"window":4096,"output_reserve":512,"margin":256}}'
''', encoding="utf-8")
    monkeypatch.delenv("ZHIQIU_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ZHIQIU_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ZHIQIU_MODEL_BUDGETS", raising=False)
    monkeypatch.setenv("ZHIQIU_LLM_MODEL", "process-model")
    load_local_env(path)
    assert json.loads(os.environ["ZHIQIU_MODEL_BUDGETS"])["file-model"]["window"] == 4096
    assert os.environ["ZHIQIU_LLM_MODEL"] == "process-model"
    assert os.environ["ZHIQIU_LLM_API_KEY"] == "private-key"


def test_invalid_local_env_does_not_load_partial_credentials(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("ZHIQIU_LLM_API_KEY=private-key\nprivate-key\n", encoding="utf-8")
    monkeypatch.delenv("ZHIQIU_LLM_API_KEY", raising=False)
    with pytest.raises(ValueError) as error:
        load_local_env(path)
    assert "private-key" not in str(error.value)
    assert "ZHIQIU_LLM_API_KEY" not in os.environ


def test_cli_reads_local_env_before_model_check(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("ZHIQIU_LLM_BASE_URL=https://provider.example/v1\n"
                    "ZHIQIU_LLM_MODEL=file-model\nZHIQIU_LLM_API_KEY=private-key\n", encoding="utf-8")
    for name in ("BASE_URL", "MODEL", "API_KEY"):
        monkeypatch.delenv(f"ZHIQIU_LLM_{name}", raising=False)
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: load_local_env(path))
    monkeypatch.setattr("zhiqiu.cli.check_model", lambda model, calibrate: {
        "connection_verified": True, "requested_model": model.model})
    result = CliRunner().invoke(app, ["check-model"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["requested_model"] == "file-model"
    assert "private-key" not in result.stdout
