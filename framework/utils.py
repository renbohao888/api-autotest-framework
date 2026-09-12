# -*- coding: utf-8 -*-
"""通用工具：路径取值、深度子集比较、最终一致轮询、分位数统计。"""
import time


def _tokens(path):
    """把 "data.items[0].id" 拆成 ["data", "items", 0, "id"]。"""
    tokens = []
    buf = ""
    for ch in path:
        if ch == ".":
            if buf:
                tokens.append(buf)
                buf = ""
        elif ch == "[":
            if buf:
                tokens.append(buf)
                buf = ""
        elif ch == "]":
            if buf:
                tokens.append(int(buf) if buf.isdigit() else buf)
                buf = ""
        else:
            buf += ch
    if buf:
        tokens.append(buf)
    return tokens


def dig(data, path, default=None):
    """按点号/下标取值，取值失败返回 default。"""
    if path in (None, ""):
        return data
    node = data
    for token in _tokens(str(path)):
        try:
            if isinstance(token, int):
                if not isinstance(node, (list, tuple)) or token >= len(node):
                    return default
                node = node[token]
            else:
                if not isinstance(node, dict) or token not in node:
                    return default
                node = node[token]
        except (KeyError, IndexError, TypeError):
            return default
    return node


def deep_contains(actual, expected, path="") :
    """判断 actual 是否「包含」expected（dict 子集 / list 按下标 / 标量相等）。

    返回差异描述列表：空列表表示完全匹配，便于断言失败时直接输出差异。
    """
    diffs = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return ["%s 期望对象，实际为 %r" % (path or "$", actual)]
        for key, value in expected.items():
            child = "%s.%s" % (path, key) if path else str(key)
            if key not in actual:
                diffs.append("%s 缺失" % child)
            else:
                diffs.extend(deep_contains(actual[key], value, child))
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            return ["%s 期望数组，实际为 %r" % (path or "$", actual)]
        if len(actual) < len(expected):
            diffs.append("%s 长度 %d < 期望 %d" % (path or "$", len(actual), len(expected)))
        for index, value in enumerate(expected):
            if index < len(actual):
                diffs.extend(deep_contains(actual[index], value, "%s[%d]" % (path, index)))
    else:
        if actual != expected:
            diffs.append("%s 期望 %r，实际 %r" % (path or "$", expected, actual))
    return diffs


def percentile(values, ratio):
    """最近秩分位数（ratio 取 0~1）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(ratio * len(ordered) + 0.5)) - 1
    index = max(0, min(index, len(ordered) - 1))
    return float(ordered[index])


def eventually(func, timeout=3.0, interval=0.1):
    """轮询等待最终一致：func 返回真值即成功，返回 (是否成功, 最后一次结果)。"""
    deadline = time.time() + timeout
    result = None
    while True:
        try:
            result = func()
        except Exception as exc:                       # noqa: BLE001 - 轮询期间的异常不算失败
            result = exc
        if result:
            return True, result
        if time.time() >= deadline:
            return False, result
        time.sleep(interval)


def now_ms():
    return int(time.time() * 1000)
