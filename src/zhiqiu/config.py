"""User-level settings: one config.toml for the model key, the default start-up options and the rest.

The file is optional. Each value becomes the ZHIQIU_* environment variable the rest of the code
already reads, and only when that variable is not set yet: the process environment wins, then the
project .env, then config.toml. So nothing downstream changes, and the secret redaction in
redact.py, which works on those variables, covers values from the file too.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ENV = "ZHIQIU_CONFIG"
FILE_NAME = "config.toml"
KINDS = {"str": "字符串", "choice": "字符串", "bool": "true 或 false", "int": "非负整数", "float": "非负数",
         "paths": "字符串数组", "suffixes": "以 . 开头的字符串数组", "names": "字符串数组", "table": "表"}


@dataclass(frozen=True)
class Setting:
    section: str
    key: str
    env: str
    kind: str
    comment: str
    # What `config init` and config.example.toml write; None leaves the line commented out with `example`.
    template: Any = None
    example: str = ""
    # The value in effect when nobody sets it, as `config show` reports it.
    default: Any = None
    choices: tuple[str, ...] = ()
    secret: bool = False

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"


SECTIONS = {
    "model": "模型端点（OpenAI 风格的 Chat Completions）",
    "web": "启动：不带参数运行 zhiqiu 与 zhiqiu web 的默认值",
    "learn": "学习",
    "materials": "学习资料的范围与上限",
    "roots": "对话中可读、可写的目录（与工作台“目录白名单”页添加的合并）",
    "voice": "本地语音输入",
    "services": "其他服务",
    "price": "花费统计：每百万 token 的价格（不填只显示 token）",
    "trace": "观测",
}

SETTINGS: tuple[Setting, ...] = (
    Setting("model", "base_url", "ZHIQIU_LLM_BASE_URL", "str", "API 根路径，例如 https://api.deepseek.com/v1", ""),
    Setting("model", "model", "ZHIQIU_LLM_MODEL", "str", "模型名", ""),
    Setting("model", "api_key", "ZHIQIU_LLM_API_KEY", "str", "必填；也可以只写在 .env 或环境变量里", "", secret=True),
    Setting("model", "choices", "ZHIQIU_LLM_MODELS", "names",
            "输入框模型按钮里可选的模型；不填用端点的模型列表和价格页上的模型", example='["deepseek-flash", "deepseek-v4-pro"]'),
    Setting("model", "stream", "ZHIQIU_LLM_STREAM", "bool", "流式输出", True, default=True),
    Setting("model", "window", "ZHIQIU_LLM_WINDOW", "int", "上下文窗口（token），不填不做超限判断", example="131072"),
    Setting("model", "output_reserve", "ZHIQIU_LLM_OUTPUT_RESERVE", "int", "输出预留，也是请求的 max_tokens",
            example="8192"),
    Setting("model", "margin", "ZHIQIU_LLM_MARGIN", "int", "余量", example="2048"),
    Setting("model", "window_verified", "ZHIQIU_LLM_WINDOW_VERIFIED", "bool", "窗口已实测（只记入审计）",
            default=False, example="false"),
    Setting("model", "budgets", "ZHIQIU_MODEL_BUDGETS", "table", "按模型配置预算，键为模型名或“端点|模型名”",
            example='{ "deepseek-chat" = { window = 131072, output_reserve = 8192, margin = 2048 } }'),
    Setting("web", "port", "ZHIQIU_WEB_PORT", "int", "工作台端口", 8765, default=8765),
    Setting("web", "open", "ZHIQIU_WEB_OPEN", "bool", "启动后打开浏览器", True, default=False),
    Setting("web", "db", "ZHIQIU_DB", "str", r"数据库文件；留空用 %LOCALAPPDATA%\zhiqiu\state.sqlite", ""),
    Setting("web", "default_command", "ZHIQIU_DEFAULT_COMMAND", "choice",
            "不带参数运行 zhiqiu 时：web 打开工作台，status 只看状态", "web", default="status",
            choices=("web", "status")),
    Setting("learn", "offline", "ZHIQIU_OFFLINE", "bool", "禁止联网搜索，相当于每次都加 --offline", False, default=False),
    Setting("materials", "max_chars", "ZHIQIU_MATERIALS_MAX_CHARS", "int", "一次学习的资料合计字数上限",
            500_000, default=500_000),
    Setting("materials", "max_file_bytes", "ZHIQIU_MATERIALS_MAX_FILE_BYTES", "int", "单个文件的大小上限（字节）",
            2_000_000, default=2_000_000),
    Setting("materials", "max_files", "ZHIQIU_MATERIALS_MAX_FILES", "int", "一个目录最多读多少个文件", 500, default=500),
    Setting("materials", "code_files", "ZHIQIU_MATERIALS_CODE_FILES", "bool",
            "读代码、配置等文本文件；false 时只读 .md 和 .txt", True, default=True),
    Setting("materials", "extra_suffixes", "ZHIQIU_MATERIALS_EXTRA_SUFFIXES", "suffixes",
            "另外要读的扩展名，例如 [\".kt\", \".lua\"]", [], default=[]),
    Setting("materials", "respect_gitignore", "ZHIQIU_MATERIALS_RESPECT_GITIGNORE", "bool",
            "扫描目录时跳过 .gitignore 里的文件", True, default=True),
    Setting("roots", "read", "ZHIQIU_READ_ROOTS", "paths", "对话中可以直接读取的目录", [], default=[]),
    Setting("roots", "write", "ZHIQIU_WRITE_ROOTS", "paths", "对话中可以写笔记的目录（每次写入仍要批准）", [], default=[]),
    Setting("roots", "skills", "ZHIQIU_SKILLS_DIR", "str",
            r"自己的教学 Skill 目录；留空用 %LOCALAPPDATA%\zhiqiu\skills", ""),
    Setting("voice", "auto_download", "ZHIQIU_VOICE_AUTO_DOWNLOAD", "choice",
            "缺少语音模型时：ask 先问再下载，always 直接下载，never 不下载", "ask", default="ask",
            choices=("ask", "always", "never")),
    Setting("voice", "mirror_url", "ZHIQIU_VOICE_MIRROR_URL", "str", "语音模型的下载地址；留空从 GitHub 下载", ""),
    Setting("voice", "model", "ZHIQIU_VOICE_MODEL", "str", r"语音模型目录；留空用 %LOCALAPPDATA%\zhiqiu\voice 下的默认目录", ""),
    Setting("voice", "language", "ZHIQIU_VOICE_LANGUAGE", "choice", "识别语言", "auto", default="auto",
            choices=("auto", "zh", "en", "ja", "ko", "yue")),
    Setting("voice", "threads", "ZHIQIU_VOICE_THREADS", "int", "识别用的 CPU 线程数，不填为 min(4, CPU 核数)", example="4"),
    Setting("services", "exa_api_key", "ZHIQIU_EXA_API_KEY", "str", "Exa 联网搜索", "", secret=True),
    Setting("services", "search", "ZHIQIU_SEARCH", "choice",
            "联网搜索来源：auto 有 Exa 密钥用 Exa，否则用模型端点自带的搜索（如 DeepSeek，按次计费）；"
            "exa、provider 只用其中一个；off 不搜索", "auto", default="auto", choices=("auto", "exa", "provider", "off")),
    Setting("services", "notion_token", "ZHIQIU_NOTION_TOKEN", "str", "Notion 内部集成令牌（learn --notion）", "",
            secret=True),
    Setting("services", "mcp_config", "ZHIQIU_MCP_CONFIG", "str", "外部 MCP 工具配置文件", ""),
    Setting("services", "approval_config", "ZHIQIU_APPROVAL_CONFIG", "str", "对话审批策略文件", ""),
    Setting("price", "input", "ZHIQIU_PRICE_INPUT", "float", "输入", example="2"),
    Setting("price", "cached_input", "ZHIQIU_PRICE_CACHED_INPUT", "float", "缓存命中的输入，不填按输入价", example="0.5"),
    Setting("price", "output", "ZHIQIU_PRICE_OUTPUT", "float", "输出", example="8"),
    Setting("price", "currency", "ZHIQIU_PRICE_CURRENCY", "str", "金额前缀，例如 ¥ 或 $", ""),
    Setting("price", "models", "ZHIQIU_PRICE_MODELS", "table", "按模型手填价格，优先于价格页",
            example='{ "deepseek-v4-pro" = { input = 9, cached_input = 0.3, output = 27, currency = "¥" } }'),
    Setting("price", "auto_update", "ZHIQIU_PRICE_AUTO_UPDATE", "bool",
            "工作台启动时，若价格超过 7 天未更新，就读一次厂商价格页（一次小的模型调用）", True, default=True),
    Setting("trace", "content", "ZHIQIU_TRACE_CONTENT", "bool", "保存每次模型调用的请求与回复正文（脱敏、截断）",
            False, default=False),
)
BY_NAME = {setting.name: setting for setting in SETTINGS}
ENV_NAMES = tuple(setting.env for setting in SETTINGS)

# Where each variable's value came from in this process: 环境变量, .env or config.toml.
SOURCES: dict[str, str] = {}


def default_path() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "zhiqiu" / FILE_NAME
    xdg = os.getenv("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "zhiqiu" / FILE_NAME


def config_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.getenv(ENV, "").strip()
    return Path(configured).expanduser() if configured else default_path()


def mark_environment() -> None:
    """Start a fresh record of sources: what is set now came from the process environment."""
    SOURCES.clear()
    for name in ENV_NAMES:
        if os.environ.get(name):
            SOURCES[name] = "环境变量"


def mark(names: Iterable[str], source: str) -> None:
    for name in names:
        SOURCES.setdefault(name, source)


def _invalid(path: Path, setting: Setting) -> ValueError:
    return ValueError(f"{path}：{setting.name} 应为{KINDS[setting.kind]}"
                      + (f"（{'、'.join(setting.choices)}）" if setting.choices else ""))


def to_env(setting: Setting, value: Any, path: Path) -> str | None:
    """The environment value for one setting from the file; None when it leaves the variable unset."""
    kind = setting.kind
    if kind in ("str", "choice"):
        if not isinstance(value, str):
            raise _invalid(path, setting)
        value = value.strip()
        if kind == "choice" and value and value not in setting.choices:
            raise _invalid(path, setting)
        return value or None
    if kind == "bool":
        if not isinstance(value, bool):
            raise _invalid(path, setting)
        return "1" if value else "0"
    if kind == "int":
        if type(value) is not int or value < 0:
            raise _invalid(path, setting)
        return str(value)
    if kind == "float":
        if type(value) not in (int, float) or value < 0:
            raise _invalid(path, setting)
        return str(value)
    if kind == "names":
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() and "," not in item
                                                  for item in value):
            raise _invalid(path, setting)
        return ",".join(item.strip() for item in value) or None
    if kind in ("paths", "suffixes"):
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise _invalid(path, setting)
        items = [item.strip() for item in value]
        if kind == "suffixes":
            if not all(item.startswith(".") and len(item) > 1 for item in items):
                raise _invalid(path, setting)
            return ",".join(item.lower() for item in items) or None
        return os.pathsep.join(items) or None
    if kind == "table":
        if not isinstance(value, dict):
            raise _invalid(path, setting)
        return json.dumps(value, ensure_ascii=False) if value else None
    raise AssertionError(kind)


def read(path: Path) -> dict[str, str]:
    """The environment values the file sets, after checking every section and key. Raises ValueError
    naming the file and the key; values never appear in a message (some are secrets)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError(f"无法读取配置文件：{path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"配置文件格式有误：{path}（{exc}）") from None
    values: dict[str, str] = {}
    for section, table in data.items():
        if section not in SECTIONS:
            raise ValueError(f"{path}：未知的配置节 [{section}]（可用：{'、'.join(SECTIONS)}）")
        if not isinstance(table, dict):
            raise ValueError(f"{path}：{section} 应为配置节 [{section}]")
        for key, value in table.items():
            setting = BY_NAME.get(f"{section}.{key}")
            if setting is None:
                raise ValueError(f"{path}：未知的配置项 {section}.{key}")
            converted = to_env(setting, value, path)
            if converted is not None:
                values[setting.env] = converted
    return values


def load(explicit: Path | None = None) -> Path | None:
    """Fill unset variables from config.toml. A missing file is fine unless it was named with --config.
    Returns the file that was read, if any."""
    path = config_path(explicit)
    if not path.exists():
        if explicit is not None:
            raise ValueError(f"配置文件不存在：{path}")
        return None
    values = read(path)
    for name, value in values.items():
        if not os.environ.get(name):
            os.environ[name] = value
            SOURCES[name] = FILE_NAME
    return path


def source(setting: Setting) -> str:
    if os.environ.get(setting.env):
        return SOURCES.get(setting.env, "环境变量")
    return "默认"


def masked(value: str) -> str:
    return f"已设置（…{value[-4:]}）" if len(value) >= 12 else "已设置"


def typed(setting: Setting, raw: str) -> Any:
    """An environment value read back as the setting's type (for show and get)."""
    if setting.kind == "bool":
        return raw.strip() == "1"
    if setting.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return raw
    if setting.kind == "paths":
        return [item for item in raw.split(os.pathsep) if item.strip()]
    if setting.kind in ("suffixes", "names"):
        return [item for item in raw.split(",") if item.strip()]
    return raw


def shown(setting: Setting) -> Any:
    """The value in effect for `config show`: secrets only as 已设置 and their last four characters."""
    raw = os.environ.get(setting.env, "")
    if not raw:
        return setting.default if setting.default not in (None, "", []) else None
    if setting.secret:
        return masked(raw)
    return typed(setting, raw)


def rows() -> list[dict[str, Any]]:
    return [{"key": setting.name, "env": setting.env, "value": shown(setting), "source": source(setting)}
            for setting in SETTINGS]


def get(name: str) -> Any:
    """One effective value for scripts (`zhiqiu config get web.port`); secrets are refused."""
    setting = BY_NAME.get(name)
    if setting is None:
        raise ValueError(f"未知的配置项 {name}")
    if setting.secret:
        raise ValueError(f"{name} 是密钥，不输出；用 zhiqiu config show 查看是否已设置")
    raw = os.environ.get(setting.env, "")
    return typed(setting, raw) if raw else setting.default


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip()
    return default if not raw else raw == "1"


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


# ----- writing (config init, config.example.toml) ------------------------------------------------

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a valid TOML basic string (same escapes).
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(str(key), ensure_ascii=False)} = {_toml_value(item)}"
                                for key, item in value.items()) + " }"
    raise TypeError(type(value).__name__)


def render(values: dict[str, Any] | None = None) -> str:
    """The whole file with a comment on every key; `values` (by setting name) override the template."""
    values = values or {}
    lines = ["# 知秋的用户配置。环境变量和项目目录的 .env 优先于这里；zhiqiu config show 显示每一项实际从哪里来。",
             "# 密钥只保存在本机这个文件里，不要提交或分享。"]
    for section, title in SECTIONS.items():
        lines += ["", f"# {title}", f"[{section}]"]
        for setting in (item for item in SETTINGS if item.section == section):
            value = values.get(setting.name, setting.template)
            if value is None:
                lines.append(f"# {setting.key} = {setting.example}  # {setting.comment}")
            else:
                lines.append(f"{setting.key} = {_toml_value(value)}  # {setting.comment}")
    return "\n".join(lines) + "\n"


def render_env() -> str:
    """The .env.example: the same settings as variables, all empty (a .env line has no trailing comment)."""
    lines = ["# 项目目录的 .env：与 config.toml 同样的设置，写成环境变量；优先于 config.toml，低于进程环境变量。",
             "# 复制为 .env 后只填需要的行；空值等于不设置。.env 已被 .gitignore 忽略，不要提交密钥。",
             "# 数组（目录）用 ; 分隔，扩展名用 , 分隔，布尔值写 1 或 0，budgets 写 JSON。"]
    for section, title in SECTIONS.items():
        lines += ["", f"# [{section}] {title}"]
        for setting in (item for item in SETTINGS if item.section == section):
            lines += [f"# {setting.name}：{setting.comment}", f"{setting.env}="]
    return "\n".join(lines) + "\n"


def write(path: Path, values: dict[str, Any]) -> Path | None:
    """Write a new file; an existing one is kept as config.toml.bak first. Returns the backup, if any."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
    text = render(values)
    tomllib.loads(text)  # never write a file the loader would refuse
    path.write_text(text, encoding="utf-8")
    return backup


def _replace_line(lines: list[str], section: str | None, pattern: re.Pattern[str], line: str) -> list[str]:
    """Put `line` where `pattern` matches (inside [section] when given), or at the section's end."""
    start, end = 0, len(lines)
    if section is not None:
        header = next((i for i, text in enumerate(lines) if text.strip() == f"[{section}]"), None)
        if header is None:
            return lines + ["", f"[{section}]", line]
        start = header + 1
        end = next((i for i in range(start, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(start, end):
        if pattern.match(lines[i]):
            return lines[:i] + [line] + lines[i + 1:]
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[:end] + [line] + lines[end:]


def set_value(name: str, value: Any, explicit: Path | None = None) -> tuple[str, Path]:
    """Save one setting where its value now comes from: the project .env when that set it, otherwise
    config.toml (made from the template when missing; the old file kept as .bak). The process takes the
    new value at once. A value set in the process environment cannot be changed from here."""
    from .local_env import PROJECT_ENV
    setting = BY_NAME.get(name)
    if setting is None:
        raise ValueError(f"未知的配置项 {name}")
    where = source(setting)
    if where == "环境变量":
        raise ValueError(f"{setting.env} 在环境变量里设置，优先于配置文件；请在那里修改")
    if where == ".env":
        target = PROJECT_ENV
        converted = to_env(setting, value, target)
        lines = target.read_text(encoding="utf-8-sig").splitlines()
        pattern = re.compile(rf"^\s*{re.escape(setting.env)}\s*=")
        text = "\n".join(_replace_line(lines, None, pattern, f"{setting.env}={converted or ''}")) + "\n"
    else:
        target = config_path(explicit)
        converted = to_env(setting, value, target)
        if not target.exists():
            write(target, {name: value})
            restrict(target)
            _apply(setting, converted, FILE_NAME)
            return FILE_NAME, target
        lines = target.read_text(encoding="utf-8-sig").splitlines()
        pattern = re.compile(rf"^\s*#?\s*{re.escape(setting.key)}\s*=")
        text = "\n".join(_replace_line(lines, setting.section, pattern,
                                        f"{setting.key} = {_toml_value(value)}  # {setting.comment}")) + "\n"
        read_text(text, target)  # never write a file the loader would refuse
    target.with_name(target.name + ".bak").write_bytes(target.read_bytes())
    target.write_text(text, encoding="utf-8")
    _apply(setting, converted, where if where == ".env" else FILE_NAME)
    return (where if where == ".env" else FILE_NAME), target


def _apply(setting: Setting, converted: str | None, where: str) -> None:
    if converted is None:
        os.environ.pop(setting.env, None)
        SOURCES.pop(setting.env, None)
    else:
        os.environ[setting.env] = converted
        SOURCES[setting.env] = where


def read_text(text: str, path: Path) -> dict[str, str]:
    """read() for text not yet written to `path`."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"配置文件格式有误：{path}（{exc}）") from None
    scratch = path.with_name(path.name + ".check")
    scratch.write_text(text, encoding="utf-8")
    try:
        return read(scratch)
    finally:
        scratch.unlink(missing_ok=True)


def restrict(path: Path) -> bool:
    """Only the current user may read the file (Windows: drop inherited ACLs). False if that failed."""
    if sys.platform != "win32":
        try:
            path.chmod(0o600)
            return True
        except OSError:
            return False
    user = os.getenv("USERNAME")
    if not user:
        return False
    try:
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                                capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
