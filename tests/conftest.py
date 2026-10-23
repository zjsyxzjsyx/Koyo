import pytest


@pytest.fixture(autouse=True)
def _no_user_skills(tmp_path_factory, monkeypatch):
    # The real user Skill directory never leaks into a test; tests that need one set ZHIQIU_SKILLS_DIR.
    monkeypatch.setenv("ZHIQIU_SKILLS_DIR", str(tmp_path_factory.getbasetemp() / "no-user-skills"))


@pytest.fixture(autouse=True)
def _no_user_config(tmp_path_factory, monkeypatch):
    # The real %APPDATA%\zhiqiu\config.toml never leaks into a test; tests that need one write their own.
    monkeypatch.setenv("ZHIQIU_CONFIG", str(tmp_path_factory.getbasetemp() / "no-user-config" / "config.toml"))


@pytest.fixture(autouse=True)
def _no_prices(tmp_path_factory, monkeypatch):
    # A CLI test that reads the real .env leaves its prices in os.environ; tests that price set their own.
    for name in ("ZHIQIU_PRICE_INPUT", "ZHIQIU_PRICE_OUTPUT", "ZHIQIU_PRICE_CACHED_INPUT", "ZHIQIU_PRICE_CURRENCY",
                 "ZHIQIU_PRICE_MODELS", "ZHIQIU_LLM_MODELS"):
        monkeypatch.delenv(name, raising=False)
    # Nor do the prices read from a vendor's page into %LOCALAPPDATA%, and no test reads that page.
    monkeypatch.setenv("ZHIQIU_PRICES_FILE", str(tmp_path_factory.getbasetemp() / "no-prices" / "prices.json"))
    monkeypatch.setenv("ZHIQIU_PRICE_AUTO_UPDATE", "0")


@pytest.fixture(autouse=True)
def _no_streaming(monkeypatch):
    # Existing tests drive the 7.2 non-streamed requests (their fake clients only post); the
    # streaming tests turn it back on with ZHIQIU_LLM_STREAM=1.
    monkeypatch.setenv("ZHIQIU_LLM_STREAM", "0")
