# api-autotest-framework · 接口自动化测试框架

一个面向「秒杀 / 电商交易类接口」的分层接口自动化测试框架：**YAML 数据驱动 + 分层设计 + 断言结构化 + 接口与数据库双端校验 + 并发一致性验证 + 性能门禁 + 自研 HTML 报告 + GitHub Actions 持续集成**。

自带一个可独立运行的被测服务（SUT），`git clone` 后一条命令即可跑通全链路，不依赖 MySQL / Redis 等外部中间件，方便本地自测和 CI 落地。

```
用例总数 38 ｜ 通过 37 ｜ 失败 0 ｜ 跳过 1 ｜ 通过率 100.00%
接口调用 257 次 ｜ 平均 8.3 ms ｜ P95 26.9 ms ｜ 最大 34.2 ms
200 并发抢 10 件库存：成功 10 ｜ 售罄 190 ｜ 其它异常 0 ｜ QPS≈137 ｜ 无超卖、无负数库存
1000 请求并发压测：QPS≈483 ｜ 错误率 0.00%
```

> 报告预览（自研 HTML 报告，含用例明细、断言明细、请求 / 响应流水）：[docs/report-preview.png](docs/report-preview.png)

---

## 一、它解决什么问题

接口测试最容易写成「一堆脚本 + 一堆 if」：用例散落在代码里、断言失败只报 `AssertionError`、
环境切换要改代码、数据被上一条用例污染、只校验了返回码却没校验数据库、并发问题只能靠人工猜。

这个框架把这些痛点拆成 7 个层次，每层只做一件事：

| 层次 | 文件 | 职责 |
| --- | --- | --- |
| 配置层 | `framework/conf.py` | 默认配置 → 环境配置 → 环境变量三级覆盖，切环境零改代码 |
| 日志层 | `framework/logger.py` | 控制台 + 滚动文件日志，请求 / 响应 / 耗时全程可追溯 |
| 协议层 | `framework/http_client.py` | Session 复用、超时、指数退避重试、统一 token、耗时统计、请求快照 |
| 用例数据层 | `framework/dataloader.py` `framework/runner.py` | YAML 用例解析 + 通用执行器（前置步骤 / 变量传递 / 期望断言） |
| 断言层 | `framework/assertions.py` | 结构化断言：失败信息带「断言项 + 期望 + 实际 + 响应体」，上下文自动收集 |
| 数据层 | `framework/db.py` | SQLite / MySQL 统一访问，做「接口 + 数据库」双端校验与最终一致轮询 |
| 报告层 | `framework/report.py` | pytest 插件，收集断言 / 请求流水 / 耗时，产出 HTML + JSON 报告 |

```
api-autotest-framework/
├─ config/            配置：config.yaml（默认）+ env/{dev,ci}.yaml（环境覆盖）
├─ data/              YAML 用例：seckill_cases.yaml、order_flow_cases.yaml
├─ framework/         框架层（与业务无关，可复用到其它项目）
├─ service/           被测服务 SUT：Flask + SQLite 的简化版秒杀服务
├─ testcases/         pytest 用例：夹具 + 冒烟 / 鉴权 / 下单 / 状态机 / 并发 / 性能
├─ scripts/run.py     一键入口：起服务 → 跑用例 → 出报告
├─ docs/              测试方案、用例设计、缺陷记录、报告预览、实测证据
└─ reports/           运行产物：report.html、summary.json、atf.log
```

## 二、快速开始

```bash
git clone https://github.com/renbohao888/api-autotest-framework.git
cd api-autotest-framework
pip install -r requirements.txt

python scripts/run.py smoke        # 冒烟：提交前自检（核心链路）
python scripts/run.py              # 默认：全量用例，不含性能门禁
python scripts/run.py concurrency  # 并发与数据一致性（200 并发抢购）
python scripts/run.py perf         # 性能门禁：P95 / QPS / 错误率
python scripts/run.py all          # 全量 + 性能
python scripts/run.py serve        # 只启动被测服务，供 Postman / JMeter 手工验证
```

执行结束后终端会打印汇总，并在 `reports/` 生成：

- `report.html`：可视化报告（用例通过率、断言明细、请求 / 响应流水、接口 P95）
- `summary.json`：机器可读摘要（可被 CI 或测试平台消费）
- `atf.log`：框架运行日志（按 5MB 滚动）

等价的原生 pytest 用法：

```bash
pytest -m smoke                       # 只跑冒烟
pytest -m "not perf"                  # 全量（排除性能门禁）
pytest testcases/test_40_consistency.py -k same_user
pytest --env ci --report-dir reports  # 切换环境 + 指定报告目录
```

## 三、核心设计

### 1. 数据驱动：新增覆盖只改 YAML

用例的「请求 + 期望结果」全部写在 `data/*.yaml`，执行由 `framework/runner.py` 统一完成：

```yaml
- id: SEC-002
  name: 边界值：库存 1 时首单成功且库存归零
  tags: [regression]                       # 标签 → pytest 标记，可按需筛选用例
  setup:                                   # 前置步骤：造数据 / 造状态
    - {method: POST, path: /api/admin/reset, auth: false, json: {stock: 1}}
  method: POST
  path: /api/seckill/order
  json: {activityId: 1}
  expect_status: 200                       # 协议层断言
  expect_code: 0                           # 业务层断言
  expect_data: {status: WAIT_PAY, remainStock: 0}   # 字段子集深度匹配
  max_elapsed_ms: 1000                     # 耗时阈值断言
```

用例之间可以传递变量（状态机用例必备）：

```yaml
  setup:
    - {method: POST, path: /api/seckill/order, json: {activityId: 1},
       extract: {order_no: data.orderNo}}          # 从响应里提取变量
    - {method: POST, path: "/api/order/${order_no}/pay"}   # 变量传递
```

测试代码只剩下「取用例 → 执行 → 追加数据库校验」：

```python
CASES = params("data/seckill_cases.yaml")

@pytest.mark.parametrize("case", CASES)
def test_seckill_order_cases(api, db, case):
    resp = run_case(api, case)
    if case["id"] == "SEC-001":
        assert_equal("接口返回的剩余库存与库内一致", resp.get("data.remainStock"),
                     db.scalar("SELECT stock FROM activity WHERE activity_id=1"))
```

### 2. 断言结构化：失败就能定位

断言层把每条断言记录成 `{断言名, 结果, 详情}` 写入用例上下文，报告层直接消费；
失败时抛出的信息包含期望 / 实际 / 响应体，不需要再去翻脚本：

```
AssertionError: [响应字段子集] POST /api/order/SO2026091221155256CEFF/cancel -> 200 (4.01ms)：
以下字段不符合预期（data）：
    - remainStock 期望 5，实际 4
```

内置断言：状态码、业务码、字段子集、单字段、非空、类型、数值范围、耗时阈值、
文本包含、数据库取值 / 计数、最终一致轮询（`assert_eventually` / `assert_db_eventually`）。

### 3. 接口 + 数据库双端校验

只看 HTTP 返回码会漏掉「接口说成功、库里没落数」的问题。框架用 `framework/db.py` 做第二视角：

```python
assert_equal("成功下单数 = 库存数（不超卖）", success, stock)          # 接口侧
assert_db_value(db, "SELECT stock FROM activity WHERE activity_id=1", (), 0)   # 数据库侧
assert_db_count(db, "orders", "activity_id=1 AND status='WAIT_PAY'", (), stock)
assert_eventually(lambda: (...), name="订单数与库存最终一致")            # 最终一致轮询
```

并发用例还会校验不变量：**当前库存 + 有效订单数 = 初始库存**（取消 / 超时关单必须回补库存）。

### 4. 自研报告：把证据留在报告里

`framework/report.py` 是标准 pytest 插件（`conftest.py` 里 `pytest_plugins` 注册），
收集每条用例的断言明细与全部请求 / 响应流水，生成单文件 HTML：

- 顶部汇总卡片：用例数、通过率、接口调用次数、平均 / P95 / 最大耗时
- 失败用例优先展示，自动展开
- 每条用例可展开查看：断言明细（PASS / FAIL 逐条）+ 请求方法 / 路径 / 请求头 / 请求体 / 响应体 / 耗时
- 同时落盘 `summary.json`，便于接入 CI 或测试平台


### 5. 性能门禁

性能不再只靠 JMeter 手工看数，而是沉淀成可回归的门禁用例（阈值写在配置里，CI 可放宽避免误报）：

| 指标 | 阈值（dev / ci） | 实测 |
| --- | --- | --- |
| 查询接口 P95 响应时间 | ≤ 800ms / 1500ms | 23.6 ms |
| 50 并发 × 20 次查询 QPS | ≥ 50 / 30 | ≈ 483 |
| 错误率 | ≤ 1% | 0.00% |

## 四、用例设计（38 条，覆盖 5 类场景）

| 模块 | 文件 | 用例数 | 设计方法 |
| --- | --- | --- | --- |
| 冒烟 | `test_00_smoke.py` | 2 | 核心链路串测（登录 → 下单 → 查单 + 落库校验） |
| 鉴权 | `test_10_auth.py` | 8 | 等价类（正确 / 错误密码、缺参）、越权、伪造 token |
| 秒杀下单 | `test_20_seckill.py` + `data/seckill_cases.yaml` | 13 | 等价类 + 边界值（库存 0/1/N）+ 异常入参 + 幂等 + 限流 |
| 订单状态机 | `test_30_order_state.py` + `data/order_flow_cases.yaml` | 10 | 状态迁移法（5 态、合法 5 条 / 非法 4 条 / 异常 1 条） |
| 并发与一致性 | `test_40_consistency.py` | 3 | 200 并发抢购、单人并发幂等、取消 / 超时回补不变量 |
| 性能门禁 | `test_50_perf.py` | 2 | P95 响应时间、并发吞吐 + 错误率 |

详见 [docs/用例设计.md](docs/用例设计.md)（含等价类 / 边界值表、状态迁移矩阵、用例清单）。

## 五、实测结果与缺陷发现

跑 v1 版本时框架报出 4 条失败，定位到 2 个真实缺陷，修复后全量回归通过
（证据：[docs/evidence/run-v1-before-fix.log](docs/evidence/run-v1-before-fix.log)、
[docs/evidence/report-final.html](docs/evidence/report-final.html)）：

| 缺陷 | 发现用例 | 现象 | 根因 | 修复 |
| --- | --- | --- | --- | --- |
| DEF-001 取消 / 支付超时关单未回补库存 | ODR-004、ODR-005、`test_stock_restore_invariant` | `remainStock` 期望 5 实际 4；不变量校验库存 5、期望 8 | `change_state` 只改订单状态、没有把占用的库存还回去 | 终态（CANCELED / TIMEOUT_CLOSED）在同一事务内回补库存 |
| DEF-002 并发重复下单返回 500 | `test_same_user_concurrent_orders_only_one_success` | 其它异常响应 `[500]`（期望 `[]`），重复下单未返回业务码 1009 | 重复下单校验在事务外（TOCTOU 竞态）+ 唯一索引冲突异常被全局兜底成 500 | 校验移入 `BEGIN IMMEDIATE` 事务内，并捕获 `IntegrityError` 转业务码 |

完整复现步骤、定位过程与回归结果见 [docs/缺陷记录.md](docs/缺陷记录.md)。

## 六、配置与多环境

```yaml
# config/config.yaml（默认值）      config/env/dev.yaml、config/env/ci.yaml（按环境覆盖）
http:
  base_url: http://127.0.0.1:8899
  timeout: 10
  retries: 2
  retry_on_status: [502, 503, 504]
sut:
  auto_start: true        # true：用例自动拉起临时被测服务；false：直接测已有环境
database:
  driver: sqlite          # sqlite | mysql（真实环境用 MySQL 做数据校验）
```

优先级：`config.yaml` < `config/env/<env>.yaml` < 环境变量（`ATF__HTTP__TIMEOUT=5`）。
切换环境：`pytest --env ci` 或 `set ATF_ENV=ci`。

**接入真实服务**：把 `sut.auto_start` 改为 `false`、`http.base_url` 指向真实网关、
`database` 指向真实库，然后在 `data/*.yaml` 里按真实接口补用例即可 —— 框架层（`framework/`）与业务解耦，无需改动。

## 七、持续集成

`.github/workflows/ci.yml`：每次 push / PR 自动执行「冒烟 + 回归 + 并发」，归档 HTML 报告为构建产物，
失败即阻断合并（性能门禁单独触发，避免共享 runner 抖动导致误报）。


## 八、被测服务（SUT）接口清单

自带服务用于承载测试，业务规则与真实秒杀场景一致（库存原子扣减、同一用户限购 1 件、
活动时间窗口、每人每秒限流、订单状态机 + 取消 / 超时回补库存），统一响应结构：

```json
{"code": 0, "message": "success", "data": {...}, "traceId": "8f2c1a9b3d4e"}
```

| 方法 | 路径 | 说明 | 业务码 |
| --- | --- | --- | --- |
| GET | `/api/health` | 健康检查 | 0 |
| POST | `/api/auth/login` | 登录取 token（`test123`） | 1001 密码错误 / 1003 缺参 |
| GET | `/api/activity/{id}` | 活动与库存 | 1004 活动不存在 |
| POST | `/api/seckill/order` | 秒杀下单（需 token） | 1002 未登录 / 1003 缺参 / 1005 未开始 / 1006 已结束 / 1007 售罄 / 1008 限流 / 1009 重复下单 |
| GET | `/api/order/{no}` | 订单详情 | 1011 订单不存在 |
| POST | `/api/order/{no}/pay` `/cancel` `/complete` | 状态流转 | 1010 非法状态迁移 |
| POST | `/api/admin/expire/{no}` | 模拟支付超时关单（回补库存） | 1010 |
| GET | `/api/admin/stock/{id}` | 内部库存 / 订单数（校验用） | 0 |
| POST | `/api/admin/reset` `/api/admin/window` | 测试夹具：重置数据 / 调整活动时间窗 | 0 |

## 九、常见问答（面试要点）

**Q：为什么这么分层？框架层和用例层怎么划边界？**
框架层（`framework/`）不感知业务：它只会「发请求、断言、记数据、出报告」；业务知识全在 `data/*.yaml`
和 `testcases/` 里的库内校验。所以换一个服务只需要换 YAML 与断言，框架代码不用动。

**Q：用例数据和代码分离后，怎么保证可维护性？**
每条 YAML 用例必须有 `id`（报告里按 ID 定位）、`name`（业务语言描述）；`tags` 决定它属于
冒烟 / 回归 / 并发，回归时用 `-m` 挑选；`setup` 负责造数据或造状态，用例之间不共享状态，
配合用例级数据复位夹具，避免「用例顺序依赖」。

**Q：并发测试怎么保证不是「偶然通过」？**
① 并发数（200）远大于库存（10），保证竞争充分；② 断言不看单条响应，而是看聚合不变量：
成功数 = 库存数、被拦截数 = 总数 - 库存数、无其它异常码；③ 再用数据库二次校验库存为 0、
订单数 = 10、无负数库存，并轮询校验最终一致；④ 单人并发场景单独校验幂等（只成功 1 单）。

**Q：断言失败后你怎么定位问题？**
框架把断言写入上下文，报告里能直接看到「断言项 + 期望 + 实际 + 那一刻的请求头 / 请求体 / 响应体」；
日志里同时有每一跳请求的方法、路径、业务码与耗时；需要时用 `python scripts/run.py serve` 起服务，
把同一请求丢进 Postman 复现。

**Q：怎么和 CI 结合？性能用例为什么默认不跑？**
CI 上是每次 push 跑冒烟 + 回归 + 并发并归档报告；性能门禁对机器敏感，放在独立触发，
并且阈值走配置（`perf.p95_ms` / `perf.min_qps`），共享 runner 用 `ci` 环境的放宽阈值，避免误报。

**Q：这套框架怎么扩展到其它项目？**
① 改 `config/env/*.yaml` 的 `base_url` / `database`；② 把 `sut.auto_start` 置 false；
③ 在 `data/` 下新增用例文件并在 `testcases/` 里 `params()` 引用即可；④ 需要新断言时，
在 `framework/assertions.py` 里加函数（保持「结构化记录 + 失败详情」的约定）。

## 十、目录内其它文件

- `docs/测试方案.md`：测试范围、策略、分层、准入准出、风险与待确认项
- `docs/用例设计.md`：等价类 / 边界值表、状态迁移矩阵、38 条用例清单
- `docs/缺陷记录.md`：2 个缺陷的复现、定位、修复与回归
- `docs/evidence/`：v1（修复前）与最终（修复后）的运行日志、报告与摘要 JSON
- `docs/report-preview.png`：HTML 报告预览图
- `pytest.ini`：用例目录、默认参数、自定义标记、实时日志
- `conftest.py`（根目录）：注册报告插件；`testcases/conftest.py`：服务 / 客户端 / 数据库 / 上下文夹具

## License

MIT

