---
name: crypto-entry-analysis
description: Binance top 50 coins rapid screening — scores every symbol for long/short entry signals every 10-30 minutes, ranks by signal strength, outputs entry/stop/target for top candidates. Two-phase: quick screen + optional deep-dive with 8 analysis dimensions.
category: crypto
---

# Crypto Entry Analysis

## Overview

两阶段分析系统:

- **Phase 1: 快速扫描** — 对币安前 20 币种做轻量技术评分，筛选出 TOP 3 信号。~2 分钟
- **Phase 2: 深度分析** — 对 TOP 3 币种调用多维分析引擎，用户可从 8 个维度中按需选择。每个维度 ~1 分钟

---

## Phase 1: Quick Screen (Top 50 → Top 3)

### Step 0: Get Binance Top 50

获取币安现货 USDT 交易对，按 24h 交易量排序取前 50。

格式: `{symbol: "BTCUSDT", price: xxxx, volume_24h: xxxx}`

对每支币同时获取:
- 1h K 线 (最近 50 根) → 用于 RSI(14)、布林带、EMA200、成交量均值
- 15m K 线 (最近 20 根) → 用于 RSI(14) 跨周期确认

全部数据可一次性从 market data 获取，无额外 API 开销。

### Step 1: Lightweight Indicators (7 indicators)

| 指标 | 来源 | 用途 |
|------|------|------|
| 当前价格 | market data | 基准价 |
| 24h 涨跌幅 | market data | 短期动量 |
| RSI(14, 1h) | 1h K线计算 | 主周期超买超卖 |
| RSI(14, 15m) | 15m K线计算 | 跨周期确认 (同上笔获取) |
| 价格 vs EMA200(1h) | 1h K线计算 | 大趋势方向 |
| 成交量比 (当前量 / 24h均量) | 1h K线计算 | 量能异动检测 |
| 布林带位置 (BB% = (price - lower) / (upper - lower)) | 1h K线计算 (20,2) | 超买超卖验证 |

**流动性过滤**: 24h 交易量 < 100 万 USDT → 跳过

### Step 2: Enhanced Two-Sided Scoring (max 9 / side)

```
LONG_Score:
  — 技术超卖 —
  + 2  if RSI(1h) < 30
  + 1  if RSI(1h) 30-40
  + 1  if RSI(15m) < 30                  (跨周期确认：15m 也超卖 → 信号更强)
  + 1  if RSI(15m) < RSI(1h)             (RSI 在更低周期率先反转)
  — 价格回落 —
  + 1  if 24h 跌 > 5%
  + 2  if 24h 跌 > 10%
  — 位置确认 —
  + 1  if BB% < 0.2                      (触碰下轨)
  + 1  if price in lower 20% of 8h range (近8h低位区)
  — 趋势过滤 —
  + 1  if price > EMA200                 (大趋势向上 = 回调做多)
  0   if price < EMA200 AND RSI > 40     (下降趋势中非超卖 → 不做多)
  — 量能验证 —
  + 1  if volume_ratio > 1.5 AND 24h% < 0 (放量下跌 → 恐慌抛售 = 抄底信号)

SHORT_Score:
  — 技术超买 —
  + 2  if RSI(1h) > 70
  + 1  if RSI(1h) 60-70
  + 1  if RSI(15m) > 70                  (跨周期确认)
  + 1  if RSI(15m) > RSI(1h)             (RSI 在更低周期率先见顶)
  — 价格上涨 —
  + 1  if 24h 涨 > 5%
  + 2  if 24h 涨 > 10%
  — 位置确认 —
  + 1  if BB% > 0.8                      (触碰上轨)
  + 1  if price in upper 20% of 8h range (近8h高位区)
  — 趋势过滤 —
  + 1  if price < EMA200                 (大趋势向下 = 反弹做空)
  0   if price > EMA200 AND RSI < 60     (上升趋势中非超买 → 不做空)
  — 量能验证 —
  + 1  if volume_ratio > 1.5 AND 24h% > 0 (放量上涨 → 高潮 = 做空信号)
```

### Step 3: Ranking & TOP 3 Selection

```
1. Signal = max(LONG_Score, SHORT_Score), 方向为评分高者
2. Score 相同 → 比较 RSI 极端程度 (RSI 越接近 0/100 优先级越高)
3. Score < 3 → 不纳入 (噪音过滤)
4. 标记 TOP 3 进入 Phase 2
```

**Phase 1 评分报告格式** (Agent 输出给用户看到的信息):

```
=== CRYPTO SCAN — Phase 1 Complete (50 coins, 2 min) ===

  # | Symbol    | Dir   | Score | RSI(1h) | RSI(15m) | 24h%   | VolRatio
 ───┼───────────┼───────┼───────┼─────────┼──────────┼────────┼──────────
  1 | SOLUSDT   | LONG  | 8/9   | 25.3    | 22.1     | -8.2%  | 2.1x    ← TOP 1
  2 | ETHUSDT   | SHORT | 6/9   | 74.8    | 78.2     | +7.5%  | 1.8x    ← TOP 2
  3 | DOGEUSDT  | LONG  | 5/9   | 33.5    | 31.0     | -4.1%  | 0.9x    ← TOP 3
 ───┼───────────┼───────┼───────┼─────────┼──────────┼────────┼──────────
  4 | BTCUSDT   | SHRT  | 3/9   | 58.2    | 62.5     | +2.1%  | 0.7x
  5 | ...       |       |       |         |          |        |

  ⚠️ 47 coins filtered out (26 no signal, 15 low volume, 6 score < 3)
  
  进入 Phase 2 深度分析 TOP 3...
```

---

## Phase 2: Deep Dive — 8 Analysis Dimensions

对 TOP 3 币种做深度多维分析。每个维度对应一个技能，Agent 按需加载。

用户/Agent 可以在请求时指定需要哪些维度，例如:
- "默认全部" → 执行所有 8 个维度
- "只做技术面 + 链上 + 合约" → 只加载对应维度
- "快速深度" → 只做技术面 + 资金费率 (最快)

---

### Dimension 1: 📊 技术面分析

**技能**: `load_skill("technical-basic")` + `load_skill("candlestick")`

| 指标 | 用途 |
|------|------|
| EMA 12/26/50/200 | 多周期趋势判断 |
| RSI(14) | 超买超卖 (30/70) |
| ADX(14) | 趋势强度 (>25 有趋势) |
| Bollinger Bands(20,2) | 波动率 + 支撑阻力 |
| OBV + 量比 | 量价配合确认 |
| K 线形态 (candlestick Skill) | 吞没/十字星/锤子线 |

**输出**: 趋势方向 / 关键支撑阻力位 / 信号一致性

---

### Dimension 2: 🔗 链上数据分析

**技能**: `load_skill("onchain-analysis")`

| 指标 | 来源 | 信号含义 |
|------|------|---------|
| 活跃地址数 7d 趋势 | onchain-analysis | ↑ = 网络使用增长 |
| 大额转账 (>100k/1M USDT) | onchain-analysis | 交易所流入 = 抛压 / 流出 = 囤积 |
| MVRV 比率 | onchain-analysis | <1 = 低估 / >3 = 高估 |
| NVT 比率 | onchain-analysis | 高 = 高估 / 低 = 低估 |
| SOPR | onchain-analysis | <1 = 亏损卖出 / >1 = 盈利卖出 |
| 交易所净流入/流出 | onchain-analysis | 持续流出 = 积累信号 |
| TVL 趋势 | onchain-analysis | 生态健康度 |

---

### Dimension 3: 💰 资金费率 & 合约

**技能**: `load_skill("perp-funding-basis")` + `load_skill("liquidation-heatmap")`

| 指标 | 用途 |
|------|------|
| 当前资金费率 | 多空情绪: >0.05% 多头拥挤 / <0 = 空头主导 |
| 资金费率 7d 趋势 | 情绪是否在转向 |
| 年化基差 | 期货升贴水 |
| 未平仓量变化 | OI↑+价格上涨 = 趋势确认 / OI↓+价格上涨 = 反弹乏力 |
| 清算聚类 | 上方清算墙 = 可能涨到那里 / 下方 = 可能跌到那里 |
| 清算磁石效应 | 价格倾向于向清算密集区移动 |

---

### Dimension 4: 🧠 市场情绪

**技能**: `load_skill("sentiment-analysis")` + `load_skill("social-media-intelligence")`

| 指标 | 用途 |
|------|------|
| Fear & Greed Index | 极度恐惧 = 买入机会 / 极度贪婪 = 风险 |
| 社交媒体热度趋势 | 讨论量激增 = 短期顶部信号 |
| 多空比 | 过度一致 = 反转信号 |
| Put/Call 比率 | 期权市场情绪 |
| 资金流向 (现货 vs 合约) | 现货主导 = 健康 / 合约主导 = 投机 |

---

### Dimension 5: ⚡ 波动率分析

**技能**: `load_skill("volatility")`

| 指标 | 用途 |
|------|------|
| HV(10/30/60) 历史波动率 | 当前波动率百分位 |
| HV 均值回归信号 | 波动率极端 = 回归概率高 |
| 平均真实波幅 ATR(14) | 动态止损参考 |
| 布林带宽度 | 收缩 = 变盘前兆 / 扩张 = 趋势延续 |

---

### Dimension 6: 🏦 稳定币 & 资金流

**技能**: `load_skill("stablecoin-flow")` + `load_skill("market-microstructure")`

| 指标 | 用途 |
|------|------|
| USDT/USDC 总供应量变化 | 新增 = 资金入场 / 赎回 = 资金离场 |
| 交易所稳定币储备 | 储备高 = 购买力强 |
| 稳定币转账速度 | 加速 = 交易活跃 |
| 买卖盘口深度 | 吃单/挂单比判断方向 |
| 成交量分布 (Volume Profile) | 高量节点 = 关键支撑阻力 |

---

### Dimension 7: 📉 风险评估

**技能**: `load_skill("risk-analysis")`

| 指标 | 用途 |
|------|------|
| 最大回撤 (近期) | 当前回撤幅度 |
| VaR(95%/99%) | 单日最大可能亏损 |
| 波动率聚类 | 高风险期预警 |
| 相关性 (vs BTC) | 山寨币 vs BTC 强弱 |
| 尾部风险 | 极端行情概率 |

---

### Dimension 8: 🔄 相关性 & 板块轮动

**技能**: `load_skill("correlation-analysis")` + `load_skill("sector-rotation")`

| 指标 | 用途 |
|------|------|
| 与 BTC 相关性 | DeFi/L1/L2 各板块与 BTC 的联动程度 |
| 板块资金轮动 | 资金在 L1/DeFi/Meme/AI 间流动方向 |
| 相对强度 | 该币种相对于大盘的强弱 |

---

## Execution Modes

Agent 根据用户请求选择执行模式:

| 模式 | Phase 1 | Phase 2 | 总时间 | 适用场景 |
|------|---------|---------|--------|---------|
| ⚡ **闪电** | TOP 50 扫描 | 不做 | ~2min | 每 10-15min 高频扫描 |
| 🔍 **标准** | TOP 50 扫描 | 技术面 + 合约 | ~4min | 每 30min 常规扫描 |
| 🧪 **全面** | TOP 50 扫描 | 全部 8 维度 | ~10min | 每日晨报 / 首次分析 |
| 🎯 **定制** | TOP 50 扫描 | 用户指定维度 | 按需 | "只看链上+情绪" |

### 用户指令示例

```
# 闪电模式
扫描币安前50入场信号

# 标准模式
扫描币安前50，对TOP3做技术面和资金费率分析

# 全面模式
全面分析币安前50，对TOP3做完整的8维深度分析

# 定制模式
筛选币安前50，重点看链上数据和情绪指标

# 指定时间间隔
每15分钟扫描一次币安前50
```

---

## Phase 2 Output Format

每个维度的输出格式示例:

```
=== Dimension 1: Technical Analysis ===
  BTCUSDT | Trend: BULLISH (EMA12>EMA26>EMA50)
  Support: $61,200 / $60,500 / $59,000
  Resist:  $63,800 / $65,000 / $66,500
  RSI: 56 (neutral) | ADX: 28 (trending) | BB: middle band touch
  Signal: ✅ 趋势和 RSI 一致

=== Dimension 2: On-Chain ===
  Active addrs: +8% 7d (organic growth ✅)
  Exchange netflow: -12,500 BTC last 24h (accumulation ✅)
  MVRV: 2.1 (fair value) | SOPR: 1.05 (mild profit)
  Signal: ✅ 积累信号

=== Dimension 3: Derivatives ===
  Funding: 0.003% (neutral ✅)
  OI: +5% with price up (confirms trend ✅)
  Liq clusters: $59,500 below / $64,200 above
  Signal: ✅ 合约健康

=== DIMENSION SUMMARY ===
  Technical | On-Chain | Derivative | Sentiment | Volatility | Stablecoin | Risk | Correlation
  ✅ PASS   | ✅ PASS  | ✅ PASS    | ⚠️ NEUT  | ✅ PASS    | ✅ PASS    | ✅   | ✅
```

---

## Final Entry Report

所有维度完成后，汇总输出:

```
╔══════════════════════════════════════════╗
║  CRYPTO ENTRY SIGNAL REPORT             ║
║  Scan time: 2026-05-08 14:30 (UTC+8)    ║
║  Mode: 全面 (8 dimensions)              ║
╚══════════════════════════════════════════╝

=== RANKING ===
  # | Symbol  | Dir | Score | RSI(1h/15m) | 24h%  | VolRatio | Conviction
  ───┼─────────┼─────┼───────┼─────────────┼───────┼──────────┼───────────
  1  | SOLUSDT | LONG| 8/9   | 25/22       | -8.2% | 2.1x     | HIGH
  2  | ETHUSDT | SHRT| 6/9   | 75/78       | +7.5% | 1.8x     | MEDIUM
  3  | DOGEUSDT| LONG| 5/9   | 34/31       | -4.1% | 0.9x     | LOW

=== TOP 1: SOLUSDT — LONG (6/8 dimensions passed) ===

  Entry:     $142.50 - $144.80 (积极/保守)
  Stop:      $138.00 (-3.5%)
  Targets:   $149.0 / $152.0 / $156.0
  R:R:       1:1.5 | Confidence: HIGH

  Dimension Pass/Fail:
  ✅ Tech ✅ OnChain ✅ Derivative ⚠️ Sentiment
  ✅ Volatility ✅ Stablecoin ✅ Risk ❌ Correlation

  Key Risk: BTC 如果跌破 $60k 可能带动 SOL 联动下跌

=== TOP 2: ETHUSDT — SHORT (4/8 dimensions passed) ===

  Entry:     $3,150 - $3,200
  Stop:      $3,250 (+3.2%)
  Targets:   $3,050 / $2,990 / $2,900
  R:R:       1:1.6 | Confidence: MEDIUM

  Key Risk: ETH ETF 资金持续流入可能推动突破

=== TOP 3: DOGEUSDT — LONG (WATCH ONLY) ===

  Entry:     $0.152 - $0.155
  Stop:      $0.148 (-3.5%)
  R:R:       1:1.2 | Confidence: LOW
  Note:      R:R 不足, 建议等待 RSI < 30
```

---

## Rules

1. **流动性过滤**: 24h 交易量 < 100 万 USDT → 跳过
2. **并发执行**: 50 个币分批并行，每次 10 个
3. **时间控制**: Phase 1 ≤ 2min. 每增加一个维度约 +1min
4. **R:R 过滤**: 风险回报比 < 1:1 → WATCH ONLY
5. **重复信号**: 与上轮相同 → 标记 "REPEAT"
6. **每轮独立**: 不依赖历史数据，不计算持仓
