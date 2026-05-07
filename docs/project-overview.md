# Vibe-Trading 项目总览

> 更新时间：2026-05-08

---

## 项目简介

Vibe-Trading 是一个 **AI 驱动的量化交易平台**，由 Python 后端（Agent + 回测引擎）和 React 前端构成，支持多市场、多策略、多数据源。平台兼容 MCP 协议，可直接作为 Claude 等 AI 的工具服务使用。

---

## 目录结构

```
Vibe-Trading/
├── agent/                      # 后端核心
│   ├── api_server.py           # FastAPI HTTP 接口服务
│   ├── cli.py                  # 命令行入口
│   ├── mcp_server.py           # MCP 协议服务（供 Claude 等 AI 调用）
│   ├── requirements.txt
│   │
│   ├── src/                    # 主业务逻辑
│   │   ├── agent/              # AI Agent 运行时
│   │   │   ├── loop.py         # Agent 主循环（工具调用 → LLM → 循环）
│   │   │   ├── context.py      # 上下文管理
│   │   │   ├── tools.py        # 工具注册与分发
│   │   │   ├── skills.py       # 技能加载器
│   │   │   ├── memory.py       # 短期记忆
│   │   │   ├── trace.py        # 执行链路追踪
│   │   │   └── frontmatter.py  # Skill 元数据解析
│   │   │
│   │   ├── core/               # 核心调度
│   │   │   ├── runner.py       # 请求运行器
│   │   │   └── state.py        # 全局状态
│   │   │
│   │   ├── providers/          # LLM 提供商
│   │   │   ├── llm.py          # LLM 统一接口
│   │   │   ├── chat.py         # 对话管理
│   │   │   ├── openai_codex.py # Codex 支持
│   │   │   └── llm_providers.json  # 模型配置
│   │   │
│   │   ├── tools/              # 工具集（Agent 可调用）
│   │   │   ├── backtest_tool.py        # 回测触发工具
│   │   │   ├── bash_tool.py            # Shell 执行
│   │   │   ├── web_search_tool.py      # 网络搜索
│   │   │   ├── web_reader_tool.py      # 网页读取
│   │   │   ├── doc_reader_tool.py      # 文档解析
│   │   │   ├── read_file_tool.py       # 文件读取
│   │   │   ├── edit_file_tool.py       # 文件编辑
│   │   │   ├── write_file_tool.py      # 文件写入
│   │   │   ├── remember_tool.py        # 持久记忆
│   │   │   ├── session_search_tool.py  # 会话搜索
│   │   │   ├── shadow_account_tool.py  # 模拟账户操作
│   │   │   ├── swarm_tool.py           # 多 Agent 协作
│   │   │   ├── skill_writer_tool.py    # 技能自动生成
│   │   │   ├── trade_journal_tool.py   # 交易日志
│   │   │   ├── factor_analysis_tool.py # 因子分析
│   │   │   ├── options_pricing_tool.py # 期权定价
│   │   │   ├── pattern_tool.py         # K 线形态识别
│   │   │   └── compact_tool.py         # 上下文压缩
│   │   │
│   │   ├── skills/             # 领域技能库（80+ 个）
│   │   │   ├── akshare/        # A 股数据
│   │   │   ├── tushare/        # Tushare 数据
│   │   │   ├── ccxt/           # 加密货币交易所
│   │   │   ├── options-strategy/   # 期权策略
│   │   │   ├── multi-factor/       # 多因子模型
│   │   │   ├── global-macro/       # 宏观分析
│   │   │   ├── sentiment-analysis/ # 情绪分析
│   │   │   ├── ml-strategy/        # 机器学习策略
│   │   │   ├── risk-analysis/      # 风险分析
│   │   │   ├── backtest-diagnose/  # 回测诊断
│   │   │   └── ...（共 80+ 个领域技能）
│   │   │
│   │   ├── swarm/              # 多 Agent 集群
│   │   │   ├── runtime.py      # 集群运行时
│   │   │   ├── worker.py       # Worker Agent
│   │   │   ├── presets.py      # 预设集群配置
│   │   │   ├── mailbox.py      # Agent 间通信
│   │   │   ├── store.py        # 状态存储
│   │   │   └── task_store.py   # 任务队列
│   │   │
│   │   ├── memory/             # 持久化记忆
│   │   ├── session/            # 会话管理
│   │   ├── shadow_account/     # 模拟账户（Paper Trading）
│   │   ├── preflight.py        # 启动前检查
│   │   └── ui_services.py      # UI 数据服务层
│   │
│   └── backtest/               # 回测引擎
│       ├── runner.py           # 回测主流程
│       ├── models.py           # 数据模型
│       ├── metrics.py          # 绩效指标（夏普、回撤等）
│       ├── benchmark.py        # 基准对比
│       ├── correlation.py      # 相关性分析
│       ├── validation.py       # 参数校验
│       ├── engines/            # 市场引擎
│       │   ├── base.py             # 基础引擎
│       │   ├── china_a.py          # A 股
│       │   ├── china_futures.py    # 国内期货
│       │   ├── crypto.py           # 加密货币
│       │   ├── forex.py            # 外汇
│       │   ├── global_equity.py    # 全球股票
│       │   ├── global_futures.py   # 全球期货
│       │   └── options_portfolio.py # 期权组合
│       ├── loaders/            # 数据加载器
│       │   ├── akshare_loader.py   # AKShare
│       │   ├── tushare.py          # Tushare
│       │   ├── yfinance_loader.py  # Yahoo Finance
│       │   ├── ccxt_loader.py      # 加密交易所
│       │   ├── futu.py             # 富途
│       │   └── okx.py              # OKX
│       └── optimizers/         # 参数优化器
│
├── frontend/                   # React 前端
│   ├── src/
│   │   ├── pages/              # 页面组件
│   │   ├── components/         # 通用组件
│   │   ├── stores/             # 状态管理（Zustand）
│   │   ├── hooks/              # 自定义 Hooks
│   │   ├── types/              # TypeScript 类型
│   │   ├── lib/                # 工具函数
│   │   └── router.tsx          # 路由配置
│   ├── vite.config.ts
│   └── tailwind.config.ts
│
├── docs/                       # 项目文档
├── scripts/dev                 # 开发启动脚本
├── docker-compose.yml          # Docker 部署
└── pyproject.toml              # Python 项目配置
```

---

## 核心架构

```
用户 / Claude / 外部系统
         ↓
api_server.py / mcp_server.py / cli.py
         ↓
core/runner.py  ──→  agent/loop.py（主循环）
                          ↓
                    providers/llm.py（调用 LLM）
                          ↓
                    tools/ + skills/（工具执行）
                          ↓
                    backtest/engines + loaders（数据 & 回测）
                          ↓
                    swarm/（多 Agent 并行协作）
```

---

## 关键设计特点

| 特性 | 说明 |
|------|------|
| **80+ 领域技能** | 每个 `skills/` 子目录是独立的可插拔分析模块，覆盖 A 股、期货、加密、期权、宏观等领域 |
| **多市场回测引擎** | A 股、国内期货、加密货币、外汇、全球股票、全球期货、期权组合各有专用引擎 |
| **多数据源** | 支持 AKShare、Tushare、Yahoo Finance、CCXT、富途、OKX 等数据接入 |
| **Shadow Account** | 内置模拟交易账户，支持 Paper Trading，无需真实资金验证策略 |
| **Swarm 多 Agent** | 支持多 Agent 并行协作处理复杂分析任务，内置任务队列与消息邮箱 |
| **MCP 兼容** | 可直接作为 Claude 的 MCP 工具服务使用，支持 AI 原生调用 |
| **持久记忆** | Agent 具备跨会话记忆能力，支持知识积累与上下文复用 |

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端语言 | Python |
| API 框架 | FastAPI |
| 前端框架 | React + TypeScript |
| 构建工具 | Vite |
| 样式 | Tailwind CSS |
| 状态管理 | Zustand |
| 部署 | Docker / docker-compose |
| 数据源 | AKShare、Tushare、yfinance、CCXT、富途、OKX |
