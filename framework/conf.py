# -*- coding: utf-8 -*-
"""配置层：默认配置 -> 环境配置 -> 环境变量覆盖，全局单例。

优先级（后者覆盖前者）：
    config/config.yaml  <  config/env/<env>.yaml  <  环境变量（ATF__HTTP__TIMEOUT=3）

环境选择顺序：显式入参 / pytest 的 --env 参数 / ATF_ENV 环境变量 / config.yaml 的 env 字段。
环境变量用 YAML 解析值，因此 ATF__SUT__AUTO_START=false 会得到布尔值 False。
"""
import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "config" / "config.yaml"
ENV_DIR = ROOT / "config" / "env"
PREFIX = "ATF__"

_cache = {}


def root_path(*parts):
    """相对仓库根目录拼接绝对路径。"""
    return ROOT.joinpath(*parts)


def resolve(path):
    """相对路径按仓库根目录解析，绝对路径原样返回。"""
    if path in (None, ""):
        return None
    node = Path(path)
    return node if node.is_absolute() else ROOT / node


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _env_overrides(environ):
    """ATF__HTTP__TIMEOUT=5 -> {"http": {"timeout": 5}}。"""
    data = {}
    for key, raw in environ.items():
        if not key.startswith(PREFIX) or key == PREFIX + "ENV":
            continue
        parts = [p.lower() for p in key[len(PREFIX):].split("__") if p]
        if not parts:
            continue
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        try:
            node[parts[-1]] = yaml.safe_load(raw)
        except yaml.YAMLError:
            node[parts[-1]] = raw
    return data


class Config:
    """只读配置对象，支持点号取值：cfg.get("http.timeout", 10)。"""

    def __init__(self, data, env, sources):
        self._data = data
        self.env = env
        self.sources = sources

    def get(self, path, default=None):
        node = self._data
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name):
        return copy.deepcopy(self._data.get(name, {}))

    def as_dict(self):
        return copy.deepcopy(self._data)

    def __repr__(self):
        return "<Config env=%s sources=%s>" % (self.env, self.sources)


def _read_yaml(path):
    node = Path(path)
    if not node.exists():
        return {}
    with open(node, "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def load(env=None, config_file=None, environ=None):
    """按优先级合并出最终配置。"""
    environ = os.environ if environ is None else environ
    base = _read_yaml(config_file or DEFAULT_FILE)
    env = env or environ.get("ATF_ENV") or base.get("env") or "dev"
    env_file = ENV_DIR / ("%s.yaml" % env)
    merged = _deep_merge(base, _read_yaml(env_file))
    merged = _deep_merge(merged, _env_overrides(environ))
    merged["env"] = env
    sources = [str(config_file or DEFAULT_FILE)]
    if env_file.exists():
        sources.append(str(env_file))
    sources.append("env:ATF__*")
    return Config(merged, env, sources)


def get_config(env=None):
    """带缓存的单例入口（同一个 env 只解析一次）。"""
    key = env or os.environ.get("ATF_ENV", "")
    if key not in _cache:
        _cache[key] = load(env)
    return _cache[key]


def reset_cache():
    """切换环境或做配置用例时清空缓存。"""
    _cache.clear()
