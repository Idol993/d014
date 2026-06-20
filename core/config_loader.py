import os
import re
import yaml
from pathlib import Path
from typing import Any, Dict

from . import CONFIG_DIR, logger


def _env_var_constructor(loader, node):
    value = loader.construct_scalar(node)
    pattern = re.compile(r"\$\{(\w+)\}")

    def replace(match):
        env_name = match.group(1)
        return os.environ.get(env_name, match.group(0))

    return pattern.sub(replace, value)


yaml.SafeLoader.add_constructor("!env", _env_var_constructor)


def load_yaml(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        logger.warning(f"配置文件不存在: {file_path}")
        return {}

    content = file_path.read_text(encoding="utf-8")
    pattern = re.compile(r"\$\{(\w+)\}")

    def replace_env(match):
        env_name = match.group(1)
        return os.environ.get(env_name, match.group(0))

    content = pattern.sub(replace_env, content)

    try:
        data = yaml.safe_load(content)
        return data or {}
    except yaml.YAMLError as e:
        logger.error(f"配置文件解析失败 {file_path}: {e}")
        return {}


class ConfigLoader:
    _instance = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_all()
        return cls._instance

    def _load_all(self):
        self._config = {
            "main": load_yaml(CONFIG_DIR / "config.yaml"),
            "thresholds": load_yaml(CONFIG_DIR / "thresholds.yaml"),
            "approval": load_yaml(CONFIG_DIR / "approval_matrix.yaml"),
            "notify": load_yaml(CONFIG_DIR / "notify_config.yaml"),
        }
        logger.info("配置加载完成")

    def reload(self):
        self._load_all()
        logger.info("配置已重新加载")

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    @property
    def main(self) -> Dict[str, Any]:
        return self._config.get("main", {})

    @property
    def thresholds(self) -> Dict[str, Any]:
        return self._config.get("thresholds", {})

    @property
    def approval(self) -> Dict[str, Any]:
        return self._config.get("approval", {})

    @property
    def notify(self) -> Dict[str, Any]:
        return self._config.get("notify", {})


config = ConfigLoader()
