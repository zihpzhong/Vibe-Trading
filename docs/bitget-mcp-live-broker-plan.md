# Bitget MCP Live Broker 集成方案

> **状态**: ✅ Phase 1–3 已完成 · ✅ Phase 5 接线（agent.json + bridge 增强）· ⏳ Phase 4 手动冒烟
> **分支**: `dev`
> **最后更新**: 2026-05-30

## Context

上游 `agent/src/live/` 是有界自治交易通道，当前仅支持 Robinhood 券商。该架构是 broker-agnostic 的，已内置 `InstrumentType.CRYPTO` 和 `AssetClass.CRYPTO`。我们 fork 拥有成熟的 `BitgetExchange` 封装层（ccxt 驱动，支持永续合约全功能）。目标是通过 MCP 通道将 Bitget 注册为第二个 live broker，使上游系统可以直接通过 `place_order` → `order_guard` → mandate 检查 → Bitget 执行 的完整链路交易加密货币。

## 扩展原则

遵循 `docs/extension-guide.md` 的**不修改上游文件**原则：
- **不修改** `agent/src/` 下任何原有文件（冲突隐患）
- 唯一例外：`agent/src/tools/ext_bridge.py`（扩展指南允许）
- 新增 broker 注册通过 **运行时桥接（bridge）** 模式完成：`extensions/live/bitget_bridge.py` 在启动时 monkey-patch 上游数据结构和函数
- 分类映射、提取器、MCP 服务器全部放在 `extensions/live/` 下

## 交易全流程图

```
┌──────────────────────────────────────────────────────────────────┐
│                       用户交互层                                  │
│  vibe-trading TUI  (/live, /halt, /resume 命令)                  │
│  或: vibe-trading live start  (CLI 启动 runner)                  │
└──────────┬───────────────────────────────────────────────────────┘
           │ 提交 Mandate（委托契约）
           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     Mandate（有界权限契约）                        │
│  HardCaps:                                                      │
│    account_funding_usd      ← 券商侧资金隔离（硬上限）           │
│    max_order_notional_usd   ← 单笔金额上限                      │
│    max_total_exposure_usd   ← 总敞口上限                        │
│    max_leverage             ← 杠杆上限                          │
│    allowed_instruments=[CRYPTO]                                  │
│    max_trades_per_day                                           │
│  UniverseConstraint:                                            │
│    asset_classes=[CRYPTO]                                        │
│    exclude_symbols=[...]   ← 拒绝列表                           │
│  ↓ 写入 ~/.vibe-trading/live/bitget/mandate.json (不可变)       │
└──────────┬───────────────────────────────────────────────────────┘
           │ agent 自主交易循环（LiveRunner.run_once）
           ▼
┌──────────────────────────────────────────────────────────────────┐
│                   交易 Tick（每周期循环）                          │
│                                                                  │
│  ① HALT？→ 检查 kill switch 文件                                │
│     └── touch ~/.vibe-trading/live/bitget/HALT = 立即熔断        │
│                                                                  │
│  ② Mandate 过期？→ expires_at 已过则停止                        │
│                                                                  │
│  ③ Reconcile → 对账交易所 ↔ 本地状态                            │
│     └── 发现不一致 → 中止 tick (永不自动重试)                    │
│                                                                  │
│  ④ Agent 调用 → 注入 mandate 到 prompt                          │
│     └── agent 决定: place_order("BTCUSDT", "buy", 100)           │
│                                                                  │
│  ⑤ Audit → 记录到 audit.jsonl                                   │
└──────────┬───────────────────────────────────────────────────────┘
           │ place_order() 被调用
           ▼
┌──────────────────────────────────────────────────────────────────┐
│               Order Guard（前置门控）                             │
│                                                                  │
│  ① load_mandate() → 重新读取委托，拒绝 = DENY                   │
│  ② _is_expired() → 检查过期                                     │
│  ③ halt_flag_set("bitget") → 检查熔断                           │
│  ④ extract_order_intent() → 解析为 OrderIntent                  │
│     └── symbol=BTCUSDT, side=buy, notional_usd=100               │
│  ⑤ _normalize_intent_notional() → 标准化名义价值                 │
│     └── 只有 quantity? 调用 get_quotes 获取最新价                │
│  ⑥ 读取持仓 + 余额快照                                          │
│  ⑦ check_mandate() → 逐一检查 9 项                              │
│     └── exclude → instrument → asset → notional → exposure      │
│         → leverage → daily_count → funding → universe_floor      │
│     ├── None → ALLOW → 转发到 Broker                            │
│     ├── BREACH_KIND_UNIVERSE → DENY                              │
│     └── BREACH_KIND_QUANTITATIVE → PAUSE_FOR_REAUTH              │
└──────────┬───────────────────────────────────────────────────────┘
           │ ALLOW
           ▼
┌──────────────────────────────────────────────────────────────────┐
│               Bitget MCP Server (stdio 子进程)                    │
│                                                                  │
│  extensions/live/bitget_mcp_server.py                            │
│                                                                  │
│  place_order(symbol="BTCUSDT", side="buy", notional_usd=100)     │
│    → 计算 qty = 100 / last_price                                 │
│    → BitgetExchange.create_market_order("BTCUSDT", "buy", qty)  │
│    → ccxt.create_order() → Bitget API                            │
│    → 返回 order result                                           │
│                                                                  │
│  失败? → 返回 error → order_guard 记录 audit                     │
└──────────┬───────────────────────────────────────────────────────┘
           ▼
┌──────────────────────────────────────────────────────────────────┐
│               Bitget 交易所（永续合约）                           │
│                                                                  │
│  开仓 → 持仓出现在 get_positions()                               │
│  TPSL 条件单 → 可另由 TPSLMonitor 管理                           │
│  （但我们 fork 的 TPSLMonitor 是独立系统，不在本计划范围内）      │
└──────────────────────────────────────────────────────────────────┘
```

## 与 fork 实盘引擎的区别

| 路径 | 入口 | 适用场景 | 典型部署 |
|------|------|----------|----------|
| **本文档（agent + mandate）** | `vibe-trading live start bitget` + MCP | Agent 在 mandate 边界内自主下单 | 本机 / `vibe-trading` 容器 |
| **fork 调度引擎** | `run_live_trading.py --exchange bitget` | Docker 定时扫描 / Phase1+Gate（无 mandate 环） | `docker compose` 服务 `live-trading` |

二者共用 `BitgetExchange` 类，但**进程、风控、配置相互独立**。

### 推荐：双 API Key / 双子账户

生产环境建议 **两套 Bitget API Key（或两个子账户）**，避免资金与持仓耦合：

| 系统 | 进程 | 环境变量来源 | 说明 |
|------|------|--------------|------|
| **fork 实盘** | `live-trading` 容器 | 服务器 `agent/.env`（compose `env_file`） | 跑 Phase1 扫描、Gate、TPSL；**不加载** `ext_bridge` / MCP |
| **agent + mandate** | `vibe-trading serve` 或本机 TUI | **单独**一套 `BITGET_*`（勿与实盘容器共用同一 `.env` 若 key 需隔离） | MCP stdio 子进程继承**启动 serve 时**的环境变量 |

要点：

- **不同 API Key → 交易所侧账户隔离**，不存在「两套程序抢同一持仓」的问题。
- **mandate 里的 `account_funding_usd` 等**只约束 agent 所用 key 对应的钱包；**不限制** `live-trading` 子账户。
- 若策略上交易同一标的，仍可能**方向相反**（策略风险），与是否同 key 无关。

### 对原交易系统（`live-trading`）的影响

| 项 | 是否影响 fork 实盘 |
|----|-------------------|
| `run_live_trading.py` / scheduler / Gate / TPSL | **否**（不 import `extensions/live/`） |
| Docker `live-trading` 启动命令 | **否** |
| `BitgetExchange.get_account_balance()` / 下单逻辑 | **否**（行为未改；仅新增可选方法供 MCP 用） |
| 同机同时跑 `vibe-trading serve` + `live-trading` | **否**（独立进程；注意 **env 里 BITGET_* 指向哪把 key**） |

仅在**主动执行** `setup_bitget_live.py` 时会写入 `~/.vibe-trading/agent.json`，**不修改** `extensions/config/config.json` 或 `trading.db`。

## 用户使用方法（agent + mandate on Bitget）

### 0. 配置 API Key（agent 专用）

与服务器 `live-trading` **分开**配置 agent 使用的 Bitget 凭据，例如：

```bash
# 本机 agent 路径：extensions/config/.env.local（gitignore，推荐）
# 或启动 serve 前 export（勿与实盘容器共用同一 key，除非刻意共用子账户）

# extensions/config/.env.local 示例：
# BITGET_API_KEY=<agent-子账户-key>
# BITGET_SECRET=...
# BITGET_PASSPHRASE=...
```

`bitget_mcp_server.py` 以 stdio 子进程启动时读取**父进程**环境变量；`vibe-trading serve` 需能加载上述 `.env`（与项目现有 `agent/.env` 链一致时，注意**不要**在实盘服务器上把 agent key 写进仅供 `live-trading` 使用的那份 env，除非两容器刻意分离 env 文件）。

### 1. 前置条件

```bash
# 确认 agent 侧 Bitget 凭据（与 live-trading 容器 env 无关）
export BITGET_API_KEY=...   # agent 子账户
export BITGET_SECRET=...
export BITGET_PASSPHRASE=...

pip install -e ".[dev]"

# 冒烟 MCP（mock，无需密钥）
python3.12 extensions/ext_cli/setup_bitget_live.py --status-only
```

### 2. 写入 MCP 配置（必需）

```bash
# 合并 bitget stdio MCP 到 ~/.vibe-trading/agent.json（仅 READ 工具）
python3.12 extensions/ext_cli/setup_bitget_live.py

# 查看就绪状态
python3.12 extensions/ext_cli/setup_bitget_live.py --status-only
```

等价的手动片段（路径按本机仓库调整）见 `extensions/live/bitget_mcp_seed.py` 的 `build_bitget_mcp_server_seed()`。

### 3. 启动服务并加载 bridge

```bash
# 推荐（确保 bridge 在 api_server 进程内生效）
PYTHONPATH=agent:. python3.12 -m uvicorn extensions.live.serve_with_bridge:app \
  --host 127.0.0.1 --port 8899

# 或已安装 CLI 时（需确认会加载 ext_bridge）
vibe-trading serve --port 8899
export VIBE_TRADING_API_URL=http://127.0.0.1:8899
```

**Bitget 不需要** `vibe-trading live authorize bitget`（无 OAuth；凭据为环境变量）。

### 4. 提交 Mandate

在 chat 中让 agent 调用 `propose_mandate_profiles`，参数 `broker: "bitget"`，并由用户在界面/API 完成 commit：

```bash
# 只读查看已提交 mandate
vibe-trading live mandate bitget

# API commit（示例，需已有 proposal_id）
# POST /mandate/commit  { "broker": "bitget", "proposal_id": "...", "profile_ordinal": 1 }
```

`allowed_instruments` 应包含 `CRYPTO`；caps 按 USDT 名义填写。

### 5. 启用 WRITE 工具（mandate 提交之后）

编辑 `~/.vibe-trading/agent.json`，在 `mcp_servers.bitget.enabled_tools` 中加入：

```json
"place_order",
"cancel_order"
```

或一步写入：

```bash
python3.12 extensions/ext_cli/setup_bitget_live.py --with-write-tools
```

**然后重启** `vibe-trading serve` / TUI，WRITE 工具才会挂载且经 `LiveOrderGuardTool` 门控。

### 6. 启动自主 Runner

```bash
vibe-trading live status bitget
vibe-trading live start bitget

# API（需 API_AUTH_KEY）
curl -s "http://localhost:8899/live/status?broker=bitget" -H "Authorization: Bearer $API_AUTH_KEY"
curl -s -X POST http://localhost:8899/live/runner/start \
  -H "Authorization: Bearer $API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"broker":"bitget"}'
```

### 7. 熔断 / 恢复

```bash
vibe-trading live halt bitget    # 或 touch ~/.vibe-trading/live/bitget/HALT
vibe-trading live resume bitget  # 或 rm ~/.vibe-trading/live/bitget/HALT
```

TUI：`/halt`、`/resume`、`/live status bitget`、`/live start bitget`（broker 参数需显式带 `bitget`）。

## 架构概览

```
用户提交 mandate（chat propose_mandate_profiles + commit）
  → ~/.vibe-trading/live/bitget/mandate.json（不可变）
  → agent 调用 place_order("BTCUSDT", "buy", notional=100)
  → order_guard（mandate / halt / intent / enforce）
  → Bitget MCP Server（stdio，extensions/live/bitget_mcp_server.py）
  → BitgetExchange → ccxt → Bitget API（agent 专用 API Key）
```

MCP server 以 stdio 子进程运行（非 HTTP/OAuth），从**启动 vibe-trading 时的环境变量**读取 `BITGET_*`（与 `live-trading` 容器内的 env **可完全不同**）。

### Bridge 加载流程

```
vibe-trading 启动（serve / TUI；live-trading 容器不经过此路径）
  → ext_bridge.py 被上游工具注册器自动加载
  → bitget_bridge.patch_upstream()
  → patch: is_live_broker / 分类表 / 提取器 / is_live_broker_entry
  → patch: api_server._known_live_brokers、_oauth_token_present（若已加载）
  → bitget MCP 工具经 LiveOrderGuardTool 包装（WRITE 需在 agent.json 显式启用）
```

## 已创建文件

### 1. `extensions/live/bitget_mcp_server.py` (~260 行)

FastMCP 服务器，包装 BitgetExchange 方法为 6 个工具：

| 工具 | 对应 exchange 方法 | 分类 |
|------|-------------------|------|
| `get_account` | `get_balance_snapshot()` → 返回 equity+available（单次 API 调用） | READ |
| `get_positions` | `get_positions()` → 返回持仓列表 | READ |
| `get_quotes(symbol)` | `get_ticker(symbol)` → 返回 ticker | READ |
| `list_orders` | `fetch_open_orders()` → 返回未成交订单列表 | READ |
| `place_order(symbol, side, notional_usd?, quantity?, order_type?, price?, reduce_only?)` | `create_market_order()` 或 `create_limit_order()` | WRITE |
| `cancel_order(order_id, symbol)` | `cancel_order()` | WRITE |

关键逻辑：
- `place_order` 接受 `notional_usd` 和 `quantity` 二选一，server 内部做 notional→quantity 转换（`qty = notional / last_price`）
- `sys.path` 自动设置项目根路径，确保 `extensions.trading.crypto.live` 模块可导入
- `--mock` 参数支持 MockExchange 模式用于测试
- 每个工具独立 `try/except` + `json.dumps` 序列化

### 2. `extensions/live/bitget_classification.py` (~28 行)

```python
BITGET_TOOL_CLASS: dict[str, ToolClass] = {
    "get_account": ToolClass.READ,
    "get_positions": ToolClass.READ,
    "get_quotes": ToolClass.READ,
    "list_orders": ToolClass.READ,
    "place_order": ToolClass.WRITE,
    "cancel_order": ToolClass.WRITE,
}
```

结构上与 `agent/src/live/robinhood_classification.py` 一致，但放在 `extensions/live/` 下以避免修改上游文件。

### 3. `extensions/live/bitget_extractor.py` (~55 行)

提取 `place_order` kwargs 为 `OrderIntent`。与 Robinhood 提取器模式一致，但：
- 默认 `instrument_type=CRYPTO`（Bitget 是纯加密交易所）
- 共享辅助函数从 `extensions/live/_shared_extractor.py` 导入
- 仅 CRYPTO 相关的 instrument 别名，无 EQUITY/ETF/OPTION

### 4. `extensions/live/_shared_extractor.py` (~55 行) — 🆕 非原始计划

提取器共享辅助函数，供后续 broker 提取器复用：
- `extract_symbol(kwargs)` → 标准化大写 symbol
- `extract_side(kwargs)` → 标准化 buy/sell
- `extract_size(kwargs)` → 解析 notional_usd + quantity
- `_first_positive_float(kwargs, keys)` → 首个正浮点值

避免每个新 broker 复制上游 `src.live.extractors.robinhood` 中的 4 个辅助函数。

### 5. `extensions/live/bitget_bridge.py` — 🆕 非原始计划

运行时桥接，patch 6 处：`is_live_broker`、分类表、host 映射、提取器、`schema.is_live_broker_entry`、`api_server` 状态辅助函数（若已加载）。

### 6. `extensions/live/bitget_mcp_seed.py` — Phase 5

`build_bitget_mcp_server_seed()` / `BITGET_MCP_SERVER_SEED`：stdio MCP 配置模板。

### 7. `extensions/live/bitget_agent_setup.py` — Phase 5

`merge_bitget_into_agent_json()`、`setup_status()`、`bitget_credentials_configured()`。

### 8. `extensions/ext_cli/setup_bitget_live.py` — Phase 5

运营商一键写入 `agent.json` 并打印后续步骤。

### 9. 测试文件 (5 个，全部在 `extensions/tests/` 下)

| 文件 | 内容 | 数量 |
|------|------|------|
| `extensions/tests/test_bitget_classification.py` | 分类映射 | 2 |
| `extensions/tests/test_bitget_extractor.py` | 提取器 | 12 |
| `extensions/tests/test_bitget_bridge.py` | 桥接 + schema wildcard + 门控 | 10 |
| `extensions/tests/test_bitget_mcp_server.py` | MCP 冒烟（含 initialize 握手） | 7 |
| `extensions/tests/test_bitget_agent_setup.py` | agent.json 合并 / 凭据状态 | 5 |

## 已修改文件

> **注意**: 原始计划中 `agent/src/config/schema.py`、`agent/src/live/registry.py`、`agent/src/live/extractors/__init__.py` 的修改已在实施过程中**回退**，改用桥接模式替代（详见"架构决策"节）。这是为了严格遵守 `docs/extension-guide.md` 的"不修改上游文件"原则。

### 1. `agent/src/tools/ext_bridge.py`（扩展指南允许的唯一上游修改）

在文件末尾触发 `patch_upstream()`，并在 `api_server` 已加载时补打 API 层补丁（**仅 agent 服务路径**；`live-trading` 不 import 此模块）。

### 2. `extensions/trading/crypto/live/_bitget_exchange.py`

- 新增 `fetch_open_orders(symbol=None)` — MCP `list_orders` 使用
- 新增 `get_balance_snapshot()` — MCP `get_account` 使用（单次 `fetch_balance()`）
- **未改动** `get_account_balance()` / `create_market_order()` 等 —— fork 实盘仍走原方法

### 3. `extensions/trading/crypto/live/exchange.py`

- 基类新增 `get_account_balance` / `get_available_balance` / `get_balance_snapshot` stub（向后兼容）
- fork 实盘 `run_live_trading.py` 仍调用 `get_account_balance()`，行为与改前一致

## 不需要修改（已验证）

- `mandate/model.py` — `InstrumentType.CRYPTO` 和 `AssetClass.CRYPTO` 已存在 ✅
- `enforcement.py` — `check_mandate()` 的 CRYPTO 路径已完备 ✅
- `order_guard.py` — broker-agnostic ✅
- `halt.py` — broker-agnostic ✅
- `paths.py` — `broker_dir()` 通用 ✅

## 架构决策

### 桥接模式（Bridge Pattern）

由于 `docs/extension-guide.md` 禁止修改上游 `agent/src/` 下的文件（除 `ext_bridge.py`），我们采用**运行时桥接**模式：

```
ext_bridge.py 加载 (仅 vibe-trading / API 服务)
  └── bitget_bridge.patch_upstream()
       ├── registry.is_live_broker
       ├── registry._BROKER_CURATED_MAPS / _HOST_SUFFIX_TO_BROKER
       ├── extractors.BROKER_EXTRACTORS
       ├── schema.is_live_broker_entry（config 禁止 bitget 使用 enabled_tools: ["*"]）
       └── api_server._known_live_brokers / _oauth_token_present（凭据 env →「已授权」）
```

**优势**: 零上游业务文件修改，rebase 冲突面小；与 `live-trading` 进程隔离  
**风险**: 上游私有 `_BROKER_*` 重构可能破坏桥接；需为 agent / 实盘 **分开配置 BITGET_***

### 共享提取器辅助函数

`extensions/live/_shared_extractor.py` 提供 `extract_symbol`/`extract_side`/`extract_size` 函数，避免每个新 broker 从上游 `src.live.extractors.robinhood` 复制 4 个辅助函数。后续 broker（如 OKX）直接导入即可。

### 单次 API 调用优化

`ExchangeBase.get_balance_snapshot()` 返回 `{"total": ..., "free": ...}`，`BitgetExchange` 重写为单次 `fetch_balance()` 调用，避免 `get_account_balance()` + `get_available_balance()` 各自调一次 ccxt API 导致的双倍网络开销。

## 兼容性分析

| 方面 | 状态 |
|------|------|
| Mandate `allowed_instruments` | ✅ 已有 `InstrumentType.CRYPTO` |
| Mandate `asset_classes` | ✅ 已有 `AssetClass.CRYPTO` |
| `check_mandate()` CRYPTO 路径 | ✅ 路由到 okx/ccxt loader chain |
| Symbol 格式（`BTCUSDT` vs `BTC-USDT`） | ✅ Robinhood 也用无横线格式 |
| OAuth 令牌 | ✅ stdio 无 auth，`should_register_live_channel` 返回 True |
| 提取器默认 instrument_type | ✅ 默认 CRYPTO |
| 桥接模式 `is_live_broker("bitget")` | ✅ 函数包装返回 True |
| 桥接模式 `wrap_live_broker_tools` | ✅ `_BROKER_CURATED_MAPS` 注入生效 |
| 桥接模式 `get_extractor("bitget")` | ✅ `BROKER_EXTRACTORS` 注入生效 |

## 实现状态

### Phase 1: 注册 + 桥接 ✅
1. ✅ `extensions/live/bitget_classification.py` — 分类映射
2. ✅ `extensions/live/bitget_extractor.py` — 提取器（依赖 `_shared_extractor.py`）
3. ✅ `extensions/live/_shared_extractor.py` — 共享辅助函数（新增）
4. ✅ `extensions/live/bitget_bridge.py` — 桥接补丁（新增，替代上游修改）
5. ✅ `agent/src/tools/ext_bridge.py` — 触发桥接加载

### Phase 2: MCP 服务器 ✅
6. ✅ `extensions/live/bitget_mcp_server.py` — FastMCP stdio 服务器（6 工具）
7. ✅ `_bitget_exchange.py` — 添加 `fetch_open_orders()` + `get_balance_snapshot()`
8. ✅ `exchange.py` — 添加 `get_account_balance()` / `get_available_balance()` / `get_balance_snapshot()` 基类方法

### Phase 3: 测试 ✅
9.  ✅ 分类映射测试（`extensions/tests/test_bitget_classification.py`）
10. ✅ 提取器测试（`extensions/tests/test_bitget_extractor.py`）
11. ✅ 桥接集成测试（`extensions/tests/test_bitget_bridge.py`）
12. ✅ MCP 冒烟测试（`extensions/tests/test_bitget_mcp_server.py`）
13. ✅ agent 接线测试（`extensions/tests/test_bitget_agent_setup.py`）
14. ✅ 扩展 Bitget 套件合计 36 项（`pytest extensions/tests/test_bitget_*`）
15. ✅ 上游 live 回归（classification / mandate / halt 等）
16. ✅ Ruff 零告警（`extensions/live/` + `test_bitget_*`）

### Phase 4: 验证 ⏳（待手动执行）
17. ⏳ 使用 **agent 子账户** API Key 跑 MCP `tools/list`（见下方 §5）
18. ⏳ 端到端：`mandate commit` → 启用 WRITE → `live start bitget` → 小额 `place_order`（测试网或极小名义）
19. ✅ `is_live_broker` / `wrap_live_broker_tools` / `halt_flag_set` — 自动化测试已覆盖

### Phase 5: agent + mandate 接线 ✅
20. ✅ `extensions/live/bitget_mcp_seed.py` — `agent.json` 种子
21. ✅ `extensions/live/bitget_agent_setup.py` — 合并配置 / 凭据检查
22. ✅ `extensions/ext_cli/setup_bitget_live.py` — 运营商安装脚本
23. ✅ Bridge 增强：`is_live_broker_entry` + `api_server` 状态面
24. ✅ 文档：双 API Key、与 `live-trading` 隔离说明

## 验证步骤

```bash
# 1. 编译检查
python3 -m py_compile extensions/live/bitget_mcp_server.py
python3 -m py_compile extensions/live/bitget_classification.py
python3 -m py_compile extensions/live/bitget_extractor.py
python3 -m py_compile extensions/live/bitget_bridge.py
python3 -m py_compile extensions/live/_shared_extractor.py

# 2. Lint（扩展零告警）
ruff check extensions/live/ extensions/tests/test_bitget_* --ignore E501

# 3. 扩展 Bitget 全套测试（36 项）
PYTHONPATH=agent:extensions python3.12 -m pytest \
  extensions/tests/test_bitget_classification.py \
  extensions/tests/test_bitget_extractor.py \
  extensions/tests/test_bitget_bridge.py \
  extensions/tests/test_bitget_mcp_server.py \
  extensions/tests/test_bitget_agent_setup.py \
  -q --tb=short

# 4. 上游 live 模块测试确认无回归
python3.12 -m pytest \
  agent/tests/test_classification.py \
  agent/tests/test_mandate_enforcement.py \
  agent/tests/test_killswitch_blocks_orders.py \
  agent/tests/test_halt.py \
  -v --tb=short

# 5. 手动验证 MCP server 工具列表（mock，无需 API Key）
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3.12 extensions/live/bitget_mcp_server.py --mock

# 6. 运营商接线检查（agent 侧 env 已设置时）
python3.12 extensions/ext_cli/setup_bitget_live.py --status-only
# 期望: mcp_configured / credentials_in_env / mandate_committed 符合当前阶段
```

## 部署对照（双 API Key）

| 组件 | 命令 / 服务 | `BITGET_*` 来源 | 是否加载 bridge |
|------|-------------|-----------------|----------------|
| fork 实盘 | `docker compose up live-trading` | 服务器 `agent/.env`（实盘子账户） | 否 |
| agent 通道 | `vibe-trading serve` + `live start bitget` | 本机 `extensions/config/.env.local` 或独立 env | 是 |

同一台机器若两个服务并存，**务必**用不同 `env_file` 或 compose `environment` 块区分两套 key，避免 MCP 子进程误读实盘 key（或反之）。

## 本地部署与验证（已实测 2026-05-30）

### 一键验证脚本

```bash
cd Vibe-Trading
set -a && source agent/.env && set +a   # agent 专用 BITGET_*（与 live-trading 容器可分 key）
export PYTHONPATH=agent:.

# 写入 agent.json + 测试 mandate + 启服 + live start/stop + CLI 中继
python3.12 extensions/ext_cli/verify_bitget_live_local.py
```

脚本会：合并 `~/.vibe-trading/agent.json` → `propose_mandate_profiles` + `commit`（broker=bitget）→ 启动 API → `GET /live/status?broker=bitget` → `POST /live/runner/start|stop` → `python -m cli live start bitget`。

### 启动 API（须先加载 bridge）

直接 `python -m api_server` 在部分环境下会因 `register_alpha_routes` 失败；推荐：

```bash
# 方式 A：带 bridge 的 uvicorn 入口（推荐）
PYTHONPATH=agent:. python3.12 -m uvicorn extensions.live.serve_with_bridge:app \
  --host 127.0.0.1 --port 8899

# 方式 B：与验证脚本相同，先 setup 再手动
python3.12 extensions/ext_cli/setup_bitget_live.py
export VIBE_TRADING_API_URL=http://127.0.0.1:8899
python3.12 -m cli live start bitget    # 在 agent/ 目录下，或 PYTHONPATH=agent
```

`extensions/live/serve_with_bridge.py` 在加载 `api_server.app` 前执行 `ext_bridge` + `patch_upstream()`，保证 `/live/status` 中 `is_live_broker=true`、凭据检查生效。

### 验证结果摘要（本机）

| 步骤 | 结果 |
|------|------|
| `setup_bitget_live.py` | `mcp_configured=true`, `credentials_in_env=true` |
| mandate commit | `allowed_instruments: ["crypto"]` |
| `GET /live/status?broker=bitget` | `is_live_broker=true`, `oauth_token_present=true`（env 凭据）, mandate 有效 |
| `POST /live/runner/start` | `{"started": true}` |
| `cli live start bitget` | `Live runner started for bitget` |

**注意**：`enabled_tools` 仅 READ 时 runner 可启动，但 agent 自主下单仍需在 mandate 后加入 `place_order` / `cancel_order` 并重启服务。
