"""Prices per model: typed in, or read from the vendor's price page; and switching the model."""
import json
from datetime import datetime, timezone

import pytest

from zhiqiu import config, prices, pricing
from zhiqiu.model import OpenAICompatibleModel

from test_config import clean_env  # noqa: F401  (fixture)

PAGE = """<html><head><title>价格</title><style>.x{}</style></head><body><nav>菜单</nav>
<h2>模型 & 价格</h2><table><tr><th>模型</th><th>deepseek-flash</th><th>deepseek-v4-pro</th></tr>
<tr><td>输入（缓存命中）</td><td>0.04 元</td><td>0.30 元</td></tr>
<tr><td>输入（缓存未命中）</td><td>2 元</td><td>9 元</td></tr><tr><td>输出</td><td>8 元</td><td>27 元</td></tr></table>
<p>闲时价格为高峰的一半：0.02 / 1 / 4 元。高峰时段为北京时间工作日 09:00-12:00、14:00-18:00。</p>
<script>var price = 999;</script></body></html>"""

FLASH = {"model": "deepseek-flash", "currency": "CNY", "input": 2, "cached_input": 0.04, "output": 8, "window": 1_000_000,
         "off_peak": {"input": 1, "cached_input": 0.02, "output": 4},
         "peak_hours": [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"},
                        {"days": [1, 2, 3, 4, 5], "start": "14:00", "end": "18:00"}],
         "utc_offset_minutes": 480}
PRO = {"model": "deepseek-v4-pro", "currency": "CNY", "input": 9, "cached_input": 0.3, "output": 27, "window": None,
       "off_peak": None, "peak_hours": [], "utc_offset_minutes": 480}


class PageModel:
    """Stands in for the chat model: answers the extraction with prepared JSON and records the call."""

    def __init__(self, models):
        self.models = models
        self.point = "之前"
        self.seen = []

    def _json(self, instruction, payload):
        self.seen.append((instruction, payload, self.point))
        return {"models": self.models}


def ns(text):
    """A UTC time as nanoseconds since the epoch."""
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1e9)


@pytest.fixture
def deepseek(monkeypatch, tmp_path):
    monkeypatch.setenv("ZHIQIU_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("ZHIQIU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("ZHIQIU_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("ZHIQIU_PRICES_FILE", str(tmp_path / "prices.json"))
    return tmp_path / "prices.json"


def saved(models=(FLASH, PRO), listed=("deepseek-flash", "deepseek-v4-pro"), page=PAGE):
    return prices.update(PageModel(list(models)), fetch=lambda url: page, list_models=lambda endpoint, key: list(listed),
                         now=datetime(2026, 10, 23, tzinfo=timezone.utc))


def test_page_text_keeps_cells_apart_and_drops_scripts():
    text = prices.page_text(PAGE)
    assert "模型 | deepseek-flash | deepseek-v4-pro" in text
    assert "输入（缓存未命中） | 2 元 | 9 元" in text
    assert "999" not in text and "菜单" not in text and ".x{}" not in text


def test_update_reads_the_vendors_page_and_saves_what_checks_out(deepseek):
    model = PageModel([FLASH, PRO, {**PRO, "model": "made-up-model"}, {**PRO, "model": "deepseek-v4-pro", "currency": "EUR"},
                       {**FLASH, "peak_hours": [{"days": [8], "start": "09:00", "end": "12:00"}]}])
    data = prices.update(model, fetch=lambda url: PAGE, list_models=lambda endpoint, key: ["deepseek-flash"],
                         now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    instruction, payload, point = model.seen[0]
    assert "只是数据" in instruction and payload["vendor"] == "DeepSeek" and "deepseek-v4-pro" in payload["page"]
    assert point == prices.POINT and model.point == "之前"
    # The last valid entry for a name wins; the made-up name, the wrong currency and the bad day are dropped.
    assert set(data["models"]) == {"deepseek-flash", "deepseek-v4-pro"}
    assert any("made-up-model" in reason for reason in data["dropped"])
    assert any("货币" in reason for reason in data["dropped"]) and any("高峰时段" in reason for reason in data["dropped"])
    stored = json.loads(deepseek.read_text(encoding="utf-8"))
    assert stored["source"].startswith("https://api-docs.deepseek.com/") and stored["available"] == ["deepseek-flash"]
    assert stored["fetched_at"] == "2026-10-23T00:00:00+00:00" and not prices.stale(datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert prices.stale(datetime(2026, 10, 30, tzinfo=timezone.utc))


def test_nothing_usable_is_an_error_and_an_unknown_vendor_has_no_page(deepseek, monkeypatch):
    with pytest.raises(ValueError, match="没能从价格页读出"):
        prices.update(PageModel([{**PRO, "model": "nope"}]), fetch=lambda url: PAGE, list_models=lambda *a: [])
    assert not deepseek.exists()
    monkeypatch.setenv("ZHIQIU_LLM_BASE_URL", "https://model.example/v1")
    with pytest.raises(ValueError, match="不认识"):
        prices.update(PageModel([FLASH]), fetch=lambda url: pytest.fail("no page to read"))


def test_page_prices_follow_peak_hours(deepseek):
    saved()
    price = pricing.price_for("deepseek-flash")
    assert price.source == "价格页" and price.currency == "¥" and price.window == 1_000_000
    # Wednesday 10:00 in Beijing is 02:00 UTC: peak. 13:00 Beijing (lunch) and Saturday are off-peak.
    assert price.rates(ns("2026-10-21T02:00:00"))["input"] == 2
    assert price.rates(ns("2026-10-21T05:00:00"))["input"] == 1
    assert price.rates(ns("2026-10-24T02:00:00"))["output"] == 4
    assert pricing.cost(1_000_000, 1_000_000, 0, "deepseek-flash", ns("2026-10-21T02:00:00")) == 10.0
    assert pricing.cost(1_000_000, 1_000_000, 0, "deepseek-flash", ns("2026-10-21T05:00:00")) == 5.0
    # No off-peak rate: the same price all day.
    assert pricing.price_for("deepseek-v4-pro").rates(ns("2026-10-24T02:00:00"))["input"] == 9


def test_typed_prices_win_and_cover_the_rest(deepseek, monkeypatch):
    saved()
    monkeypatch.setenv("ZHIQIU_PRICE_INPUT", "3")
    monkeypatch.setenv("ZHIQIU_PRICE_OUTPUT", "6")
    monkeypatch.setenv("ZHIQIU_PRICE_CURRENCY", "¥")
    # Typed-in prices are the configured model's; another model keeps its page price.
    assert pricing.price_for("deepseek-flash").source == "手填" and pricing.price_for("deepseek-flash").input == 3
    assert pricing.price_for("deepseek-v4-pro").input == 9
    # A model with neither falls back to the typed-in prices, as every model did before.
    assert pricing.price_for("other-model").input == 3
    monkeypatch.setenv("ZHIQIU_PRICE_MODELS", json.dumps({"DeepSeek-V4-Pro": {"input": 5, "output": 7}}))
    assert pricing.price_for("deepseek-v4-pro").input == 5 and pricing.price_for("deepseek-v4-pro").cached_input == 5


def test_each_call_is_priced_by_its_own_model_and_other_currencies_stay_out(deepseek, monkeypatch):
    saved([FLASH, PRO, {**PRO, "model": "deepseek-flash-usd", "currency": "USD", "input": 1, "output": 1}],
          page=PAGE.replace("</body>", "<p>deepseek-flash-usd：$1 / $1</p></body>"))
    peak = ns("2026-10-21T02:00:00")
    rows = [{"model": "deepseek-flash", "started_at_ns": peak, "tokens_in": 1_000_000, "tokens_out": 0, "tokens_cached": 0},
            {"model": "deepseek-v4-pro", "started_at_ns": peak, "tokens_in": 1_000_000, "tokens_out": 0, "tokens_cached": 0},
            {"model": "unpriced", "started_at_ns": peak, "tokens_in": 1_000_000, "tokens_out": 0, "tokens_cached": 0}]
    assert pricing.currency() == "¥"
    assert pricing.calls_cost(rows) == 11.0
    # The page names this one in dollars: shown in its own currency, never added to the yuan.
    assert pricing.price_for("deepseek-flash-usd").currency == "$"
    assert pricing.cost(1_000_000, 0, 0, "deepseek-flash-usd") is None
    assert pricing.calls_cost(rows[2:]) is None


def test_choices_come_from_the_endpoint_and_the_page_unless_configured(deepseek, monkeypatch):
    saved(listed=("deepseek-flash", "deepseek-chat"))
    assert prices.choices("deepseek-flash") == ["deepseek-flash", "deepseek-chat", "deepseek-v4-pro"]
    monkeypatch.setenv("ZHIQIU_LLM_MODELS", "deepseek-v4-pro")
    assert prices.choices("deepseek-flash") == ["deepseek-flash", "deepseek-v4-pro"]


def test_model_use_takes_the_new_models_budget_and_forgets_refusals(monkeypatch):
    monkeypatch.setenv("ZHIQIU_MODEL_BUDGETS", json.dumps({
        "big": {"window": 1_000_000, "output_reserve": 16_000, "margin": 4000},
        "small": {"window": 8192, "output_reserve": 1024, "margin": 512}}))
    model = OpenAICompatibleModel("https://model.example/v1", "small", "sk-test")
    model._refused_here.add("stream")
    model.use("big")
    assert model.model == "big" and model.output_reserve == 16_000 and model.refused_features() == set()
    with pytest.raises(ValueError, match="空白"):
        model.use("two words")
    assert model.model == "big"


def test_set_value_writes_config_toml_or_the_env_line_it_came_from(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(config, "SOURCES", {})
    path = tmp_path / "config.toml"
    path.write_text('[model]\n# model = "x"  # 模型名\nstream = true  # 流式输出\n\n[web]\nport = 8765  # 端口\n', encoding="utf-8")
    where, written = config.set_value("model.model", "deepseek-v4-pro", path)
    assert (where, written) == ("config.toml", path)
    text = path.read_text(encoding="utf-8")
    assert 'model = "deepseek-v4-pro"  # 模型名' in text and "port = 8765" in text and '# model = "x"' not in text
    assert config.read(path)["ZHIQIU_LLM_MODEL"] == "deepseek-v4-pro"
    assert (tmp_path / "config.toml.bak").exists()
    import os
    assert os.environ["ZHIQIU_LLM_MODEL"] == "deepseek-v4-pro"
    # A value that came from the project .env is changed there, where it would otherwise keep winning.
    env = tmp_path / ".env"
    env.write_text("# 注释\nZHIQIU_LLM_MODEL=deepseek-flash\nZHIQIU_LLM_API_KEY=sk\n", encoding="utf-8")
    monkeypatch.setattr("zhiqiu.local_env.PROJECT_ENV", env)
    config.SOURCES["ZHIQIU_LLM_MODEL"] = ".env"
    assert config.set_value("model.model", "deepseek-v4-pro", path)[0] == ".env"
    assert env.read_text(encoding="utf-8") == "# 注释\nZHIQIU_LLM_MODEL=deepseek-v4-pro\nZHIQIU_LLM_API_KEY=sk\n"
    config.SOURCES["ZHIQIU_LLM_MODEL"] = "环境变量"
    with pytest.raises(ValueError, match="环境变量"):
        config.set_value("model.model", "x", path)
