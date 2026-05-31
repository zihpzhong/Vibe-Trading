"""AGT 开仓金额 — 分数凯利为主（贝叶斯融合历史 + 先验）。

Kelly-primary position sizing for Bitget AGT:
  f* = (p·b − q) / b
  margin_pct = clamp(f* × kelly_fraction, min, max)
  notional = margin × leverage

Empirical win-rate / payoff blend toward priors via pseudo-count (default 20).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

#: 凯利负期望时的最小保证金占比 / Min margin when Kelly edge ≤ 0
DEFAULT_MIN_MARGIN_PCT = 0.08
DEFAULT_MAX_MARGIN_PCT = 0.18
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_KELLY_PRIOR_WEIGHT = 20
DEFAULT_WIN_RATE = 0.52
DEFAULT_PAYOFF_RATIO = 1.5
DEFAULT_FLOOR_KELLY_RATIO = 0.50
DEFAULT_MAX_CONCURRENT = 5
DEFAULT_LEVERAGE = 8.0
DEFAULT_CONVICTION = 7.0
DEFAULT_DB_PATH = Path.home() / ".vibe-trading" / "trading.db"

_CONVICTION_MULTIPLIERS: dict[int, float] = {
    10: 1.0,
    9: 1.0,
    8: 1.0,
    7: 1.0,
    6: 0.75,
    5: 0.50,
    4: 0.0,
    3: 0.0,
    2: 0.0,
    1: 0.0,
}


@dataclass(frozen=True)
class SizingConfig:
    """可调参数 / Tunable sizing knobs."""

    min_margin_pct: float = DEFAULT_MIN_MARGIN_PCT
    max_margin_pct: float = DEFAULT_MAX_MARGIN_PCT
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    kelly_prior_weight: float = DEFAULT_KELLY_PRIOR_WEIGHT
    floor_kelly_ratio: float = DEFAULT_FLOOR_KELLY_RATIO
    default_win_rate: float = DEFAULT_WIN_RATE
    default_payoff_ratio: float = DEFAULT_PAYOFF_RATIO
    max_concurrent_positions: int = DEFAULT_MAX_CONCURRENT
    default_leverage: float = DEFAULT_LEVERAGE
    default_conviction: float = DEFAULT_CONVICTION
    stats_db_path: Path = DEFAULT_DB_PATH


@dataclass(frozen=True)
class TradeStats:
    """原始统计 / Raw empirical or prior stats."""

    win_rate: float
    payoff_ratio: float
    sample_size: int
    source: str


@dataclass(frozen=True)
class KellyEstimate:
    """凯利估计结果 / Blended Kelly inputs and fractions."""

    win_rate: float
    payoff_ratio: float
    sample_size: int
    source: str
    kelly_full: float
    kelly_scaled: float
    margin_pct: float
    floor_margin_pct: float
    edge_positive: bool


@dataclass(frozen=True)
class EntrySizePlan:
    """单次开仓建议 / Recommended size for one new entry."""

    equity_usdt: float
    available_usdt: float
    exposure_notional_usdt: float
    exposure_headroom_usdt: float
    open_positions: int
    margin_target_usdt: float
    margin_min_usdt: float
    margin_max_usdt: float
    notional_target_usdt: float
    notional_min_usdt: float
    notional_max_usdt: float
    recommended_leverage: float
    conviction_score: float
    conviction_multiplier: float
    kelly: KellyEstimate
    stats: TradeStats

    @property
    def kelly_full(self) -> float:
        return self.kelly.kelly_full

    @property
    def kelly_scaled(self) -> float:
        return self.kelly.kelly_scaled

    @property
    def margin_pct_used(self) -> float:
        return self.kelly.margin_pct

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kelly"] = asdict(self.kelly)
        payload["stats"] = asdict(self.stats)
        return payload


def load_sizing_config() -> SizingConfig:
    """从环境变量加载配置 / Load config from optional env vars."""

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def _int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    db_raw = os.environ.get("AGT_SIZING_DB_PATH", "").strip()
    db_path = Path(db_raw).expanduser() if db_raw else DEFAULT_DB_PATH

    return SizingConfig(
        min_margin_pct=_float("AGT_MIN_MARGIN_PCT", DEFAULT_MIN_MARGIN_PCT),
        max_margin_pct=_float("AGT_MAX_MARGIN_PCT", DEFAULT_MAX_MARGIN_PCT),
        kelly_fraction=_float("AGT_KELLY_FRACTION", DEFAULT_KELLY_FRACTION),
        kelly_prior_weight=_float("AGT_KELLY_PRIOR_WEIGHT", DEFAULT_KELLY_PRIOR_WEIGHT),
        floor_kelly_ratio=_float("AGT_FLOOR_KELLY_RATIO", DEFAULT_FLOOR_KELLY_RATIO),
        default_win_rate=_float("AGT_DEFAULT_WIN_RATE", DEFAULT_WIN_RATE),
        default_payoff_ratio=_float("AGT_DEFAULT_PAYOFF_RATIO", DEFAULT_PAYOFF_RATIO),
        max_concurrent_positions=_int("AGT_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT),
        default_leverage=_float("AGT_DEFAULT_LEVERAGE", DEFAULT_LEVERAGE),
        default_conviction=_float("AGT_DEFAULT_CONVICTION", DEFAULT_CONVICTION),
        stats_db_path=db_path,
    )


def kelly_optimal_fraction(win_rate: float, payoff_ratio: float) -> float:
    """Full Kelly: f* = (p·b − q) / b."""
    if payoff_ratio <= 0:
        return 0.0
    p = max(0.0, min(1.0, win_rate))
    q = 1.0 - p
    edge = p * payoff_ratio - q
    if edge <= 0:
        return 0.0
    return edge / payoff_ratio


def conviction_multiplier(score: float) -> float:
    """Map conviction 1–10 to size multiplier."""
    bucket = max(1, min(10, int(round(score))))
    return _CONVICTION_MULTIPLIERS.get(bucket, 0.0)


def _stats_path(broker: str = "bitget") -> Path:
    custom = os.environ.get("AGT_SIZING_STATS_PATH", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".vibe-trading" / "live" / broker / "sizing_stats.json"


def stats_from_closed_pnls(pnls: Sequence[float]) -> TradeStats:
    """Build Kelly stats from realized PnL samples."""
    if not pnls:
        return TradeStats(
            win_rate=DEFAULT_WIN_RATE,
            payoff_ratio=DEFAULT_PAYOFF_RATIO,
            sample_size=0,
            source="prior",
        )
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]
    n = len(pnls)
    win_rate = len(wins) / n if n else DEFAULT_WIN_RATE
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 1e-9 else DEFAULT_PAYOFF_RATIO
    return TradeStats(
        win_rate=round(win_rate, 4),
        payoff_ratio=round(payoff, 4),
        sample_size=n,
        source="computed",
    )


def load_pnls_from_db(db_path: Path) -> list[float]:
    """Load closed-trade PnL list from ``trading.db``."""
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT pnl_usdt FROM closed_trades ORDER BY closed_at"
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        logger.debug("sizing db read failed %s: %s", db_path, exc)
        return []
    out: list[float] = []
    for row in rows:
        try:
            out.append(float(row[0]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def load_trade_stats(broker: str = "bitget", config: SizingConfig | None = None) -> TradeStats:
    """Load empirical stats: JSON cache → trading.db fallback → prior."""
    cfg = config or load_sizing_config()
    path = _stats_path(broker)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                wr = float(raw.get("win_rate", cfg.default_win_rate))
                pr = float(raw.get("payoff_ratio", cfg.default_payoff_ratio))
                n = int(raw.get("sample_size", 0))
                if n > 0 and 0 < wr < 1 and pr > 0:
                    return TradeStats(
                        win_rate=wr,
                        payoff_ratio=pr,
                        sample_size=n,
                        source="sizing_stats.json",
                    )
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("sizing stats unreadable %s: %s", path, exc)

    pnls = load_pnls_from_db(cfg.stats_db_path)
    if pnls:
        st = stats_from_closed_pnls(pnls)
        return TradeStats(
            win_rate=st.win_rate,
            payoff_ratio=st.payoff_ratio,
            sample_size=st.sample_size,
            source=f"trading.db({cfg.stats_db_path.name})",
        )

    return TradeStats(
        win_rate=cfg.default_win_rate,
        payoff_ratio=cfg.default_payoff_ratio,
        sample_size=0,
        source="prior",
    )


def save_trade_stats(stats: TradeStats, broker: str = "bitget") -> Path:
    """Persist Kelly stats JSON cache."""
    path = _stats_path(broker)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "win_rate": stats.win_rate,
        "payoff_ratio": stats.payoff_ratio,
        "sample_size": stats.sample_size,
        "source": stats.source,
        "kelly_full": round(
            kelly_optimal_fraction(stats.win_rate, stats.payoff_ratio), 6
        ),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def blend_kelly_inputs(
    empirical: TradeStats,
    config: SizingConfig,
) -> TradeStats:
    """贝叶斯收缩：经验统计向先验收缩 / Shrink empirical toward priors."""
    n0 = max(1.0, config.kelly_prior_weight)
    n = max(0, empirical.sample_size)
    if n <= 0:
        return TradeStats(
            win_rate=config.default_win_rate,
            payoff_ratio=config.default_payoff_ratio,
            sample_size=0,
            source="prior",
        )
    p = (n * empirical.win_rate + n0 * config.default_win_rate) / (n + n0)
    b = (n * empirical.payoff_ratio + n0 * config.default_payoff_ratio) / (n + n0)
    return TradeStats(
        win_rate=round(p, 4),
        payoff_ratio=round(b, 4),
        sample_size=n,
        source=f"blend(n={n},prior={n0:.0f})",
    )


def estimate_kelly(
    empirical: TradeStats | None = None,
    config: SizingConfig | None = None,
) -> KellyEstimate:
    """Compute blended Kelly fractions and margin percentages."""
    cfg = config or load_sizing_config()
    raw = empirical or load_trade_stats(config=cfg)
    blended = blend_kelly_inputs(raw, cfg)

    kelly_full = kelly_optimal_fraction(blended.win_rate, blended.payoff_ratio)
    kelly_scaled = kelly_full * cfg.kelly_fraction
    edge_positive = kelly_full > 0

    if edge_positive:
        margin_pct = max(
            cfg.min_margin_pct,
            min(cfg.max_margin_pct, kelly_scaled),
        )
        floor_margin_pct = max(
            cfg.min_margin_pct,
            min(margin_pct, kelly_scaled * cfg.floor_kelly_ratio),
        )
    else:
        margin_pct = cfg.min_margin_pct
        floor_margin_pct = cfg.min_margin_pct

    return KellyEstimate(
        win_rate=blended.win_rate,
        payoff_ratio=blended.payoff_ratio,
        sample_size=blended.sample_size,
        source=blended.source,
        kelly_full=round(kelly_full, 4),
        kelly_scaled=round(kelly_scaled, 4),
        margin_pct=round(margin_pct, 4),
        floor_margin_pct=round(floor_margin_pct, 4),
        edge_positive=edge_positive,
    )


def _slot_factor(open_positions: int, max_concurrent: int) -> float:
    """Smaller clips when book is crowded."""
    if max_concurrent <= 0:
        return 1.0
    ratio = open_positions / max_concurrent
    return max(0.45, 1.0 - ratio * 0.35)


def parse_equity_usdt(balance: Mapping[str, Any] | None) -> tuple[float, float]:
    """Extract (equity, available) USDT from MCP ``get_account`` payload."""
    if not balance:
        return 0.0, 0.0
    equity_block = balance.get("equity") if isinstance(balance.get("equity"), dict) else balance
    avail_block = balance.get("available") if isinstance(balance.get("available"), dict) else balance
    for key in ("USDT", "usdt"):
        if isinstance(equity_block, dict) and key in equity_block:
            eq = float(equity_block.get(key, 0) or 0)
            av = float(avail_block.get(key, eq) if isinstance(avail_block, dict) else eq)
            return eq, av
    for key in ("account_funding_usd", "equity", "cash", "buying_power"):
        if key in balance:
            try:
                eq = float(balance[key])
                return eq, eq
            except (TypeError, ValueError):
                continue
    return 0.0, 0.0


def parse_exposure_notional(positions: Sequence[Mapping[str, Any]] | None) -> float:
    """Sum absolute notional of open positions."""
    total = 0.0
    for pos in positions or []:
        try:
            notional = float(pos.get("notional", 0) or 0)
        except (TypeError, ValueError):
            notional = 0.0
        if notional > 0:
            total += abs(notional)
            continue
        try:
            qty = float(pos.get("quantity", 0) or 0)
            price = float(pos.get("mark_price", 0) or pos.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            qty, price = 0.0, 0.0
        if qty > 0 and price > 0:
            total += abs(qty * price)
    return total


def compute_entry_size(
    *,
    equity_usdt: float,
    available_usdt: float,
    exposure_notional_usdt: float,
    open_positions: int,
    mandate_max_order_notional: float,
    mandate_max_total_exposure: float,
    mandate_account_funding: float,
    conviction_score: float | None = None,
    leverage: float | None = None,
    config: SizingConfig | None = None,
    stats: TradeStats | None = None,
) -> EntrySizePlan:
    """Compute Kelly-driven margin/notional targets for one new entry."""
    cfg = config or load_sizing_config()
    raw_stats = stats or load_trade_stats(config=cfg)
    kelly = estimate_kelly(raw_stats, cfg)

    lev = leverage if leverage is not None else cfg.default_leverage
    lev = max(1.0, min(lev, 20.0))
    conviction = conviction_score if conviction_score is not None else cfg.default_conviction
    conv_mult = conviction_multiplier(conviction)

    funding = mandate_account_funding if mandate_account_funding > 0 else equity_usdt
    slot = _slot_factor(open_positions, cfg.max_concurrent_positions)

    margin_target = funding * kelly.margin_pct * conv_mult * slot
    margin_min = funding * kelly.floor_margin_pct * max(conv_mult, 0.5)
    margin_max = funding * cfg.max_margin_pct * conv_mult

    margin_target = min(margin_target, available_usdt, margin_max)
    margin_min = min(margin_min, margin_target)
    margin_max = min(margin_max, available_usdt)

    headroom = max(0.0, mandate_max_total_exposure - exposure_notional_usdt)
    notional_target = margin_target * lev
    notional_min = margin_min * lev
    notional_max = min(
        margin_max * lev,
        mandate_max_order_notional if mandate_max_order_notional > 0 else margin_max * lev,
        headroom if headroom > 0 else margin_max * lev,
        available_usdt * lev,
    )
    notional_target = min(notional_target, notional_max)
    notional_min = min(notional_min, notional_max)

    return EntrySizePlan(
        equity_usdt=round(equity_usdt, 2),
        available_usdt=round(available_usdt, 2),
        exposure_notional_usdt=round(exposure_notional_usdt, 2),
        exposure_headroom_usdt=round(headroom, 2),
        open_positions=open_positions,
        margin_target_usdt=round(margin_target, 2),
        margin_min_usdt=round(margin_min, 2),
        margin_max_usdt=round(margin_max, 2),
        notional_target_usdt=round(notional_target, 2),
        notional_min_usdt=round(notional_min, 2),
        notional_max_usdt=round(notional_max, 2),
        recommended_leverage=lev,
        conviction_score=conviction,
        conviction_multiplier=conv_mult,
        kelly=kelly,
        stats=raw_stats,
    )


def validate_entry_notional(
    notional_usdt: float,
    plan: EntrySizePlan,
    *,
    side: str,
    is_reduce_only: bool = False,
) -> str | None:
    """Return denial reason if buy notional violates Kelly sizing; else None."""
    if is_reduce_only or side.strip().lower() != "buy":
        return None
    if plan.conviction_multiplier <= 0:
        return (
            f"conviction {plan.conviction_score:.0f}/10 below minimum (5) — do not open new positions"
        )
    if not plan.kelly.edge_positive:
        return (
            f"Kelly edge ≤ 0 (f*={plan.kelly_full:.2%}, "
            f"WR={plan.kelly.win_rate:.0%}, payoff={plan.kelly.payoff_ratio:.2f}) — "
            "no new long entries until edge turns positive"
        )
    if notional_usdt + 1e-6 < plan.notional_min_usdt:
        return (
            f"order notional ${notional_usdt:.2f} below Kelly floor "
            f"${plan.notional_min_usdt:.2f} "
            f"(~${plan.margin_min_usdt:.2f} margin at {plan.recommended_leverage:.0f}X; "
            f"target ~${plan.notional_target_usdt:.2f})"
        )
    return None


def format_sizing_prompt_block(plan: EntrySizePlan) -> str:
    """Runner prompt section with Kelly-computed targets."""
    k = plan.kelly
    st = plan.stats
    edge_note = (
        "edge POSITIVE — new entries allowed"
        if k.edge_positive
        else "edge ≤ 0 — DO NOT open new longs (Kelly says no bet)"
    )
    return (
        "\n\n=== AGT KELLY POSITION SIZING (mandatory) ===\n"
        "Gate limits NOTIONAL; margin ≈ notional / leverage.\n"
        f"- Equity ${plan.equity_usdt:.2f}, available ${plan.available_usdt:.2f}, "
        f"exposure ${plan.exposure_notional_usdt:.2f}, "
        f"headroom ${plan.exposure_headroom_usdt:.2f}, open {plan.open_positions}\n"
        f"- Stats: WR={k.win_rate:.1%}, payoff={k.payoff_ratio:.2f} "
        f"({st.sample_size} trades, raw={st.source}, blended={k.source})\n"
        f"- Kelly f*={k.kelly_full:.2%}, f_used={k.kelly_scaled:.2%} "
        f"(fraction={load_sizing_config().kelly_fraction:.2f}), "
        f"{edge_note}\n"
        f"- Margin target {k.margin_pct:.1%} of funding, floor {k.floor_margin_pct:.1%}\n"
        f"- Conviction {plan.conviction_score:.0f}/10 (×{plan.conviction_multiplier:.2f}); "
        "7-10 full, 5-6 half, <5 no entries\n"
        f"- TARGET: margin ~${plan.margin_target_usdt:.2f} → "
        f"notional ~${plan.notional_target_usdt:.2f} at {plan.recommended_leverage:.0f}X\n"
        f"- FLOOR (enforced): notional ≥ ${plan.notional_min_usdt:.2f}\n"
        f"- CEILING: notional ≤ ${plan.notional_max_usdt:.2f}\n"
        "Use ``notional_usd`` ≈ TARGET (or half for conviction 5-6). Below FLOOR rejected."
    )


def plan_from_account_snapshot(
    balance: Mapping[str, Any] | None,
    positions: Sequence[Mapping[str, Any]] | None,
    *,
    mandate_max_order_notional: float,
    mandate_max_total_exposure: float,
    mandate_account_funding: float,
    conviction_score: float | None = None,
    leverage: float | None = None,
    config: SizingConfig | None = None,
) -> EntrySizePlan:
    """Build :class:`EntrySizePlan` from live account + mandate caps."""
    equity, available = parse_equity_usdt(balance)
    exposure = parse_exposure_notional(positions)
    open_count = len(list(positions or []))
    return compute_entry_size(
        equity_usdt=equity,
        available_usdt=available,
        exposure_notional_usdt=exposure,
        open_positions=open_count,
        mandate_max_order_notional=mandate_max_order_notional,
        mandate_max_total_exposure=mandate_max_total_exposure,
        mandate_account_funding=mandate_account_funding,
        conviction_score=conviction_score,
        leverage=leverage,
        config=config,
    )
