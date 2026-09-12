# -*- coding: utf-8 -*-
"""报告层：pytest 插件 —— 收集请求 / 响应 / 断言 / 耗时，产出 HTML + JSON 报告。

启用方式：根目录 conftest.py 里 pytest_plugins = ["framework.report"]，命令行可覆盖：
    pytest --report-dir reports --env ci
"""
import html
import json
import os
import time
from pathlib import Path

import pytest

from framework.conf import get_config, reset_cache, resolve
from framework.utils import percentile

SUCCESS = "passed"
STATUS_TEXT = {"passed": "通过", "failed": "失败", "error": "异常", "skipped": "跳过"}
STATUS_CLASS = {"passed": "ok", "failed": "bad", "error": "bad", "skipped": "skip"}

MARKERS = (
    ("smoke", "冒烟用例：核心链路，提交前必跑"),
    ("regression", "回归用例：全量功能验证"),
    ("concurrency", "并发与数据一致性用例"),
    ("perf", "性能门禁用例（默认不跑，用 -m perf 触发）"),
    ("slow", "耗时较长的用例"),
)


class ReportCollector:
    """收集每条用例的执行结果（含断言明细与请求流水）。"""

    def __init__(self, report_dir, title="接口自动化测试报告"):
        self.report_dir = Path(report_dir)
        self.title = title
        self.env = ""
        self.cases = []
        self.started = time.time()
        self.finished = None

    def add(self, nodeid, name, outcome, duration_ms, context=None, markers=(), error=""):
        short = name.split("::")[-1]
        case = {
            "nodeid": nodeid,
            "id": short.split("[")[-1].rstrip("]") if "[" in short else short,
            "name": short,
            "status": outcome,
            "duration_ms": round(duration_ms, 2),
            "markers": [m for m in markers if m != "parametrize"],
            "assertions": [],
            "exchanges": [],
            "error": error,
        }
        if context is not None:
            case["assertions"] = [record.as_dict() for record in context.records]
            case["exchanges"] = context.exchanges
        self.cases.append(case)
        return case

    # ------------------------------------------------------------ 统计
    def stats(self):
        total = len(self.cases)
        passed = sum(1 for c in self.cases if c["status"] == SUCCESS)
        failed = sum(1 for c in self.cases if c["status"] in ("failed", "error"))
        skipped = sum(1 for c in self.cases if c["status"] == "skipped")
        durations = [c["duration_ms"] for c in self.cases]
        api_ms = [e["elapsed_ms"] for c in self.cases for e in c["exchanges"]]
        rate = 100.0
        if (passed + failed) > 0:
            rate = passed / float(passed + failed) * 100
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": round(rate, 2),
            "duration_ms": round(((self.finished or time.time()) - self.started) * 1000, 2),
            "avg_case_ms": round(sum(durations) / total, 2) if total else 0,
            "api_calls": len(api_ms),
            "api_avg_ms": round(sum(api_ms) / len(api_ms), 2) if api_ms else 0,
            "api_p95_ms": round(percentile(api_ms, 0.95), 2) if api_ms else 0,
            "api_max_ms": round(max(api_ms), 2) if api_ms else 0,
        }

    # ------------------------------------------------------------ 输出
    def write_json(self):
        target = self.report_dir / get_config().get("report.json", "summary.json")
        payload = {"title": self.title, "env": self.env,
                   "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
                   "stats": self.stats(), "cases": self.cases}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def write_html(self):
        target = self.report_dir / get_config().get("report.html", "report.html")
        target.write_text(self.render_html(), encoding="utf-8")
        return target

    # ------------------------------------------------------------ HTML
    def render_html(self):
        stats = self.stats()
        rows = "\n".join(self._case_html(case) for case in self.cases)
        failed = [c for c in self.cases if c["status"] in ("failed", "error")]
        failed_block = "".join(
            '<li><span class="mono">%s</span> %s</li>' % (html.escape(c["id"]), html.escape(c["name"]))
            for c in failed) or "<li>本次无失败用例</li>"
        return HTML_TEMPLATE.format(
            css=CSS,
            title=html.escape(self.title),
            env=html.escape(self.env),
            generated=time.strftime("%Y-%m-%d %H:%M:%S"),
            total=stats["total"], passed=stats["passed"], failed=stats["failed"],
            skipped=stats["skipped"], rate=stats["pass_rate"],
            duration=round(stats["duration_ms"] / 1000, 2),
            api_calls=stats["api_calls"], api_avg=stats["api_avg_ms"],
            api_p95=stats["api_p95_ms"], api_max=stats["api_max_ms"],
            failed_list=failed_block, rows=rows)

    def _case_html(self, case):
        cls = STATUS_CLASS.get(case["status"], "skip")
        text = STATUS_TEXT.get(case["status"], case["status"])
        marks = " ".join('<span class="mark">%s</span>' % html.escape(m)
                         for m in case["markers"])
        blocks = []
        if case["error"]:
            blocks.append('<div class="sec">失败信息</div><pre class="err">%s</pre>'
                          % html.escape(case["error"]))
        if case["assertions"]:
            items = "".join(
                '<li class="%s"><b>%s</b> %s</li>'
                % ("a-ok" if a["ok"] else "a-bad", "PASS" if a["ok"] else "FAIL",
                   html.escape(a["name"] + ("：" + a["detail"] if a["detail"] else "")))
                for a in case["assertions"])
            blocks.append('<div class="sec">断言明细（%d 条）</div><ul class="asserts">%s</ul>'
                          % (len(case["assertions"]), items))
        if case["exchanges"]:
            blocks.append('<div class="sec">请求 / 响应流水（%d 次）</div>%s'
                          % (len(case["exchanges"]),
                             "".join(self._exchange_html(e) for e in case["exchanges"])))
        return CASE_TEMPLATE.format(
            cls=cls, text=text, cid=html.escape(case["id"]), name=html.escape(case["name"]),
            duration=case["duration_ms"], marks=marks,
            detail="".join(blocks) or '<div class="sec">无请求记录</div>',
            open_attr=" open" if cls == "bad" else "")

    def _exchange_html(self, exchange):
        request = exchange["request"]
        body = json.dumps(request.get("body"), ensure_ascii=False) if request.get("body") else "-"
        return EXCHANGE_TEMPLATE.format(
            method=html.escape(request.get("method") or ""),
            path=html.escape(request.get("path") or ""),
            status=exchange["status"], code=exchange["code"], elapsed=exchange["elapsed_ms"],
            headers=html.escape(json.dumps(request.get("headers") or {}, ensure_ascii=False)),
            body=html.escape(str(body)), response=html.escape(exchange["body"]))


_collector = ReportCollector(resolve("reports"))


def get_collector():
    return _collector


def _markers_of(item):
    return [mark.name for mark in item.iter_markers()]


# ------------------------------------------------------------------ pytest 钩子
def pytest_addoption(parser):
    group = parser.getgroup("atf", "接口自动化测试框架")
    group.addoption("--report-dir", action="store", default=None,
                    help="报告输出目录（默认取配置 report.dir）")
    group.addoption("--env", action="store", default=None,
                    help="配置环境：dev / ci（对应 config/env/<env>.yaml）")


def pytest_configure(config):
    """初始化报告收集器：环境切换 + 报告目录 + 注册自定义标记。"""
    env = config.getoption("--env", None)
    if env:
        os.environ["ATF_ENV"] = env
        reset_cache()
    cfg = get_config()
    _collector.env = cfg.env
    _collector.title = cfg.get("report.title", _collector.title)
    _collector.report_dir = resolve(config.getoption("--report-dir", None)
                                    or cfg.get("report.dir", "reports"))
    _collector.report_dir.mkdir(parents=True, exist_ok=True)
    config._atf_collector = _collector
    for marker, description in MARKERS:
        config.addinivalue_line("markers", "%s: %s" % (marker, description))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """用例结束（call 阶段）或 setup 阶段失败/跳过时收集结果。"""
    outcome = yield
    report = outcome.get_result()
    if _collector is None:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        context = getattr(item, "_atf_context", None)
        _collector.add(nodeid=item.nodeid, name=item.name, outcome=report.outcome,
                       duration_ms=report.duration * 1000, context=context,
                       markers=_markers_of(item),
                       error="" if report.passed else str(report.longrepr or "").strip())


def pytest_sessionfinish(session, exitstatus):
    """会话结束：落盘 HTML + JSON，并在终端打印汇总（含接口耗时 P95）。"""
    _collector.finished = time.time()
    reporter = session.config.pluginmanager.getplugin("terminalreporter")

    def emit(line):
        if reporter is not None:
            reporter.write_line(line)
        else:
            print(line)

    try:
        json_path = _collector.write_json()
        html_path = _collector.write_html()
    except Exception as exc:                    # noqa: BLE001 - 报告失败不能掩盖用例结果
        emit("报告生成失败：%s" % exc)
        import traceback
        traceback.print_exc()
        return

    stats = _collector.stats()
    for line in [
        "用例总数 %d ｜ 通过 %d ｜ 失败 %d ｜ 跳过 %d ｜ 通过率 %.2f%%"
        % (stats["total"], stats["passed"], stats["failed"], stats["skipped"], stats["pass_rate"]),
        "接口调用 %d 次 ｜ 平均 %.1f ms ｜ P95 %.1f ms ｜ 最大 %.1f ms"
        % (stats["api_calls"], stats["api_avg_ms"], stats["api_p95_ms"], stats["api_max_ms"]),
        "HTML 报告：%s" % html_path,
        "JSON 摘要：%s" % json_path,
    ]:
        emit(line)



# ------------------------------------------------------------------ HTML 模板
CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: #f4f6fa; color: #1f2329;
       font-family: "Microsoft YaHei", "PingFang SC", Segoe UI, sans-serif; font-size: 14px; }
.wrap { max-width: 1080px; margin: 0 auto; }
.head { background: #1f4e79; color: #fff; padding: 18px 24px; border-radius: 10px 10px 0 0; }
.head h1 { margin: 0 0 6px; font-size: 20px; }
.head .meta { font-size: 12px; opacity: .85; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; background: #fff; padding: 16px 24px; }
.card { flex: 1 1 120px; background: #f8fafc; border: 1px solid #e3e8ef; border-radius: 8px;
        padding: 10px 14px; }
.card .num { font-size: 20px; font-weight: 700; }
.card .lbl { font-size: 12px; color: #667085; margin-top: 2px; }
.card.ok .num { color: #12805c; }
.card.bad .num { color: #d92d20; }
.panel { background: #fff; padding: 16px 24px; border-top: 1px solid #eef1f6; }
.panel h2 { font-size: 15px; margin: 6px 0 10px; color: #1f4e79; }
.panel ul { margin: 0; padding-left: 20px; }
.panel li { line-height: 1.7; }
.mono { font-family: Consolas, monospace; color: #1f4e79; }
.case { background: #fff; margin-top: 10px; border: 1px solid #e3e8ef; border-radius: 8px; }
.case > summary { cursor: pointer; padding: 10px 14px; list-style: none; display: flex;
                  gap: 10px; align-items: center; }
.case > summary::-webkit-details-marker { display: none; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; color: #fff; }
.badge.ok { background: #12805c; }
.badge.bad { background: #d92d20; }
.badge.skip { background: #98a2b3; }
.cid { font-family: Consolas, monospace; color: #1f4e79; font-weight: 600; }
.ms { margin-left: auto; color: #667085; font-size: 12px; }
.mark { background: #eef4fd; color: #1f4e79; border-radius: 4px; padding: 1px 6px; font-size: 11px; }
.body { border-top: 1px solid #eef1f6; padding: 12px 14px; }
.sec { font-weight: 600; color: #1f4e79; margin: 10px 0 6px; }
pre { background: #0f172a; color: #d6e2f0; padding: 10px; border-radius: 6px; overflow: auto;
      font-family: Consolas, monospace; font-size: 12px; }
pre.err { background: #fff5f5; color: #912018; border: 1px solid #fecaca; }
.asserts { list-style: none; padding: 0; margin: 0; }
.asserts li { padding: 4px 8px; border-radius: 4px; margin-bottom: 4px; font-size: 13px; }
.asserts .a-ok { background: #f0fdf4; color: #14532d; }
.asserts .a-bad { background: #fef2f2; color: #991b1b; }
.ex { border: 1px solid #eef1f6; border-radius: 6px; padding: 8px 10px; margin-bottom: 8px; }
.ex .line { font-family: Consolas, monospace; }
.ex .tag { color: #1f4e79; font-weight: 600; }
.foot { text-align: center; color: #98a2b3; font-size: 12px; padding: 14px 0 4px; }
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body><div class="wrap">
<div class="head"><h1>{title}</h1>
<div class="meta">环境：{env} ｜ 生成时间：{generated} ｜ 用例耗时 {duration}s</div></div>
<div class="cards">
<div class="card"><div class="num">{total}</div><div class="lbl">用例总数</div></div>
<div class="card ok"><div class="num">{passed}</div><div class="lbl">通过</div></div>
<div class="card bad"><div class="num">{failed}</div><div class="lbl">失败 / 异常</div></div>
<div class="card"><div class="num">{skipped}</div><div class="lbl">跳过</div></div>
<div class="card ok"><div class="num">{rate}%</div><div class="lbl">通过率</div></div>
<div class="card"><div class="num">{api_calls}</div><div class="lbl">接口调用次数</div></div>
<div class="card"><div class="num">{api_avg}</div><div class="lbl">接口平均耗时 ms</div></div>
<div class="card"><div class="num">{api_p95}</div><div class="lbl">接口 P95 耗时 ms</div></div>
<div class="card"><div class="num">{api_max}</div><div class="lbl">接口最大耗时 ms</div></div>
</div>
<div class="panel"><h2>失败用例</h2><ul>{failed_list}</ul></div>
<div class="panel"><h2>用例明细（点击展开请求 / 响应 / 断言）</h2>{rows}</div>
<div class="foot">api-autotest-framework ｜ YAML 数据驱动 + 分层设计 ｜ 报告由 framework/report.py 生成</div>
</div></body></html>"""

CASE_TEMPLATE = """<details class="case"{open_attr}>
<summary><span class="badge {cls}">{text}</span><span class="cid">{cid}</span>
<span>{name}</span>{marks}<span class="ms">{duration} ms</span></summary>
<div class="body">{detail}</div></details>"""

EXCHANGE_TEMPLATE = """<div class="ex">
<div class="line"><span class="tag">{method}</span> {path} -> <b>{status}</b>
(code={code}, {elapsed} ms)</div>
<div class="line">请求头：{headers}</div>
<div class="line">请求体：{body}</div>
<pre>{response}</pre></div>"""

