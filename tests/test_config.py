import json
import os
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zhiqiu import config
from zhiqiu.cli import app
from zhiqiu.local_env import ALLOWED_KEYS, load_local_env

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-config-file-secret-0123456789"


@pytest.fixture
def clean_env(monkeypatch, tmp_path_factory):
    # Set then delete, so the monkeypatch undoes whatever config.load writes into os.environ.
    for name in config.ENV_NAMES:
        monkeypatch.setenv(name, "unset")
        monkeypatch.delenv(name)
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: [])
    monkeypatch.setattr("zhiqiu.cli.PROJECT_DB", tmp_path_factory.getbasetemp() / "no-project-db.sqlite")
    # The voice question of config init depends on whether this machine has the model; take it as there.
    monkeypatch.setattr("zhiqiu.voice.status", lambda directory=None: {
        "available": True, "reason": "ok", "model": "x", "language": "auto", "max_seconds": 60})
    return monkeypatch


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_examples_are_generated_from_the_settings_table():
    assert (ROOT / "config.example.toml").read_text(encoding="utf-8") == config.render()
    assert (ROOT / ".env.example").read_text(encoding="utf-8") == config.render_env()


def test_example_parses_and_every_key_is_known(tmp_path):
    path = write(tmp_path, config.render())
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert {f"{section}.{key}" for section, table in data.items() for key in table} <= set(config.BY_NAME)
    values = config.read(path)
    # Empty strings leave variables unset; the example holds no secret.
    assert "ZHIQIU_LLM_API_KEY" not in values and values["ZHIQIU_WEB_PORT"] == "8765"


def test_env_example_lists_exactly_the_allowed_variables():
    names = {line.split("=", 1)[0] for line in config.render_env().splitlines()
             if line and not line.startswith("#")}
    assert names == set(ALLOWED_KEYS) - {"EXA_API_KEY"}
    assert "ZHIQIU_LONG_ANSWER_SEGMENTS" not in names


def test_values_fill_only_unset_variables_and_sources_are_recorded(tmp_path, clean_env):
    path = write(tmp_path, f'''[model]
base_url = "https://provider.example/v1"
model = "file-model"
api_key = "{KEY}"
budgets = {{ "file-model" = {{ window = 4096, output_reserve = 512 }} }}
[roots]
read = ["D:/notes/a", "D:/notes/b"]
[materials]
extra_suffixes = [".KT", ".lua"]
[web]
open = false
''')
    clean_env.setenv("ZHIQIU_LLM_MODEL", "process-model")
    config.mark_environment()
    assert config.load(path) == path
    assert os.environ["ZHIQIU_LLM_MODEL"] == "process-model"
    assert os.environ["ZHIQIU_LLM_API_KEY"] == KEY
    assert json.loads(os.environ["ZHIQIU_MODEL_BUDGETS"]) == {"file-model": {"window": 4096, "output_reserve": 512}}
    assert os.environ["ZHIQIU_READ_ROOTS"] == os.pathsep.join(["D:/notes/a", "D:/notes/b"])
    assert os.environ["ZHIQIU_MATERIALS_EXTRA_SUFFIXES"] == ".kt,.lua"
    assert os.environ["ZHIQIU_WEB_OPEN"] == "0"
    rows = {row["key"]: row for row in config.rows()}
    assert rows["model.model"]["source"] == "环境变量"
    assert rows["model.base_url"]["source"] == "config.toml"
    assert rows["model.api_key"]["value"] == "已设置（…6789）"
    assert rows["web.open"]["value"] is False
    assert rows["web.port"] == {"key": "web.port", "env": "ZHIQIU_WEB_PORT", "value": 8765, "source": "默认"}


def test_dotenv_wins_over_the_file(tmp_path, clean_env):
    dotenv = tmp_path / ".env"
    dotenv.write_text("ZHIQIU_LLM_MODEL=dotenv-model\n", encoding="utf-8")
    write(tmp_path, '[model]\nmodel = "file-model"\nbase_url = "https://provider.example/v1"\n')
    clean_env.setenv("ZHIQIU_CONFIG", str(tmp_path / "config.toml"))
    clean_env.setattr("zhiqiu.cli.load_local_env", lambda: load_local_env(dotenv))
    result = CliRunner().invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["key"]: row for row in json.loads(result.stdout)["settings"]}
    assert rows["model.model"]["value"] == "dotenv-model" and rows["model.model"]["source"] == ".env"
    assert rows["model.base_url"]["source"] == "config.toml"


@pytest.mark.parametrize("text, message", [
    ('[modle]\nmodel = "x"\n', "未知的配置节 [modle]"),
    ('[model]\nmodle = "x"\n', "未知的配置项 model.modle"),
    ('[web]\nport = "8765"\n', "web.port 应为非负整数"),
    ('[web]\nopen = 1\n', "web.open 应为true 或 false"),
    ('[web]\ndefault_command = "chat"\n', "web.default_command 应为字符串（web、status）"),
    ('[materials]\nextra_suffixes = ["kt"]\n', "materials.extra_suffixes 应为以 . 开头的字符串数组"),
    ('[model]\napi_key = 12345678901234\n', "model.api_key 应为字符串"),
    ('model = "x"\n', "model 应为配置节 [model]"),
    ('[model\n', "配置文件格式有误"),
])
def test_bad_files_are_refused_with_the_key_and_without_the_value(tmp_path, clean_env, text, message):
    path = write(tmp_path, text)
    with pytest.raises(ValueError) as error:
        config.load(path)
    assert message in str(error.value) and str(path) in str(error.value)
    assert "12345678901234" not in str(error.value)
    assert "ZHIQIU_LLM_API_KEY" not in os.environ


def test_a_missing_file_is_fine_unless_named_explicitly(tmp_path, clean_env):
    clean_env.setenv("ZHIQIU_CONFIG", str(tmp_path / "absent.toml"))
    assert config.load() is None
    with pytest.raises(ValueError, match="配置文件不存在"):
        config.load(tmp_path / "absent.toml")


def test_cli_reads_the_file_named_with_config(tmp_path, clean_env):
    path = write(tmp_path, f'[model]\nbase_url = "https://provider.example/v1"\nmodel = "file-model"\napi_key = "{KEY}"\n')
    clean_env.setattr("zhiqiu.cli.check_model", lambda model, calibrate: {
        "connection_verified": True, "requested_model": model.model})
    result = CliRunner().invoke(app, ["--config", str(path), "check-model"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["requested_model"] == "file-model"
    assert KEY not in result.output


def test_show_never_prints_a_secret_and_get_refuses_one(tmp_path, clean_env):
    path = write(tmp_path, f'[model]\napi_key = "{KEY}"\n[services]\nexa_api_key = "exa-0123456789abcdef"\n')
    runner = CliRunner()
    for args in (["config", "show"], ["config", "show", "--json"]):
        result = runner.invoke(app, ["--config", str(path), *args])
        assert result.exit_code == 0, result.output
        assert KEY not in result.output and "exa-0123456789abcdef" not in result.output
    refused = runner.invoke(app, ["--config", str(path), "config", "get", "model.api_key"])
    assert refused.exit_code == 1 and KEY not in refused.output
    port = runner.invoke(app, ["--config", str(path), "config", "get", "web.port"])
    assert port.stdout.strip() == "8765"


def test_init_writes_a_loadable_file_and_keeps_hand_edits(tmp_path, clean_env):
    path = tmp_path / "zhiqiu" / "config.toml"
    path.parent.mkdir()
    path.write_text('[services]\nnotion_token = "ntn-kept-by-init-0000"\n[model]\nmodel = "old"\n', encoding="utf-8")
    clean_env.setattr("zhiqiu.config.restrict", lambda target: True)
    answers = "\n".join(["y", "https://provider.example/v1/", "new-model", KEY, r"D:\notes", "9000",
                         "y", "n", "n"]) + "\n"
    result = CliRunner().invoke(app, ["--config", str(path), "config", "init"], input=answers)
    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    assert (tmp_path / "zhiqiu" / "config.toml.bak").read_text(encoding="utf-8").startswith("[services]")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["model"]["base_url"] == "https://provider.example/v1"
    assert data["model"]["model"] == "new-model" and data["model"]["api_key"] == KEY
    assert data["roots"]["read"] == [r"D:\notes"]
    assert data["web"] == {"port": 9000, "open": False, "db": "", "default_command": "web"}
    assert data["services"]["notion_token"] == "ntn-kept-by-init-0000"
    assert config.read(path)["ZHIQIU_LLM_MODEL"] == "new-model"


def test_init_keeps_an_existing_key_on_enter_and_refuses_plain_http(tmp_path, clean_env):
    path = tmp_path / "config.toml"
    clean_env.setenv("ZHIQIU_LLM_API_KEY", KEY)
    clean_env.setattr("zhiqiu.config.restrict", lambda target: True)
    answers = "\n".join(["http://provider.example/v1", "https://provider.example/v1", "m", "", "", "", "n", "y",
                         "n"]) + "\n"
    result = CliRunner().invoke(app, ["--config", str(path), "config", "init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "远程端点必须用 https://" in result.output
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["model"]["api_key"] == KEY and data["web"]["default_command"] == "status"


def test_config_commands_do_not_open_the_database(tmp_path, clean_env, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.Store", lambda *args, **kwargs: pytest.fail("opened the database"))
    result = CliRunner().invoke(app, ["config", "path"])
    assert result.exit_code == 0 and result.stdout.strip().endswith("config.toml")


class FakeBench:
    conversation_id = "conv"

    def __init__(self, path, make_agent, resume=None, **options):
        self.path = path

    def start(self):
        pass

    def update_prices(self):
        return False

    def close(self):
        pass


@pytest.fixture
def served(clean_env, tmp_path):
    calls = {"opened": []}

    def serve(path, port, ready, workbench=None):
        calls.update(path=path, port=port)
        ready(f"http://127.0.0.1:{port}/")

    clean_env.setattr("zhiqiu.dashboard.serve", serve)
    clean_env.setattr("zhiqiu.webio.Workbench", FakeBench)
    clean_env.setattr("webbrowser.open", lambda url: calls["opened"].append(url))
    clean_env.setenv("ZHIQIU_DB", str(tmp_path / "configured.sqlite"))
    return calls


def test_bare_zhiqiu_opens_the_workbench_when_configured(tmp_path, served):
    path = write(tmp_path, '[web]\ndefault_command = "web"\nport = 9123\nopen = true\n')
    result = CliRunner().invoke(app, ["--config", str(path)])
    assert result.exit_code == 0, result.output
    assert served["port"] == 9123 and served["opened"] == ["http://127.0.0.1:9123/"]
    assert served["path"] == tmp_path / "configured.sqlite"
    assert "到期" not in result.output  # the workbench page shows the status itself


def test_command_line_wins_over_the_web_defaults(tmp_path, served):
    path = write(tmp_path, '[web]\nport = 9123\nopen = true\n')
    result = CliRunner().invoke(app, ["--config", str(path), "web", "--port", "9200", "--no-open"])
    assert result.exit_code == 0, result.output
    assert served["port"] == 9200 and served["opened"] == []


def test_without_a_file_the_defaults_are_those_of_8_0(tmp_path, served):
    result = CliRunner().invoke(app, ["web"])
    assert result.exit_code == 0, result.output
    assert served["port"] == 8765 and served["opened"] == []
    bare = CliRunner().invoke(app, [])
    assert bare.exit_code == 0 and "当前没有到期复习" in bare.output


def test_offline_and_db_come_from_the_file(tmp_path, clean_env):
    db = tmp_path / "from-file.sqlite"
    path = write(tmp_path, f'[web]\ndb = {json.dumps(str(db))}\n[learn]\noffline = true\n')
    seen = {}
    clean_env.setattr("zhiqiu.cli.show_status", lambda store, full=False: seen.update(path=store.path))
    result = CliRunner().invoke(app, ["--config", str(path), "status"])
    assert result.exit_code == 0, result.output
    assert seen["path"] == db and os.environ["ZHIQIU_OFFLINE"] == "1"
