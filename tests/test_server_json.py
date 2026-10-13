import json
import tomllib
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[1]


def test_server_json_matches_the_official_registry_schema():
    # tests/data/server.schema.json is the published 2025-12-11 schema, kept locally so the check runs offline.
    schema = json.loads((ROOT / "tests" / "data" / "server.schema.json").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)
    validator.check_schema(schema)
    errors = [error.message for error in validator(schema, format_checker=validator.FORMAT_CHECKER).iter_errors(server)]
    assert errors == []
    assert server["$schema"] == schema["$id"]


def test_server_json_version_and_command_match_the_package():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    package = server["packages"][0]
    assert server["version"] == package["version"] == project["version"]
    assert package["identifier"] == project["name"]
    assert package["runtimeArguments"][0]["value"] == f"{project['name']}[mcp]=={project['version']}"
    assert [arg.get("name") or arg.get("value") for arg in package["packageArguments"]] == ["--db", "mcp"]
