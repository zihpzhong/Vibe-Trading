"""Tests for Kelly-primary AGT position sizing."""

from __future__ import annotations

from extensions.live.agt_position_sizing import (
    EntrySizePlan,
    KellyEstimate,
    SizingConfig,
    TradeStats,
    blend_kelly_inputs,
    compute_entry_size,
    estimate_kelly,
    format_sizing_prompt_block,
    kelly_optimal_fraction,
    stats_from_closed_pnls,
    validate_entry_notional,
)


class TestKellyFormula:
    def test_classic_example(self) -> None:
        # 60% win, 1:1 payoff → f* = 0.2
        assert abs(kelly_optimal_fraction(0.6, 1.0) - 0.2) < 1e-6

    def test_no_edge(self) -> None:
        assert kelly_optimal_fraction(0.4, 1.0) == 0.0


class TestBlend:
    def test_shrinks_toward_prior(self) -> None:
        cfg = SizingConfig(kelly_prior_weight=20)
        empirical = TradeStats(0.70, 2.0, 5, "computed")
        blended = blend_kelly_inputs(empirical, cfg)
        assert blended.win_rate < empirical.win_rate
        assert blended.win_rate > cfg.default_win_rate

    def test_always_uses_kelly_not_fixed_base(self) -> None:
        cfg = SizingConfig()
        est = estimate_kelly(
            TradeStats(cfg.default_win_rate, cfg.default_payoff_ratio, 0, "prior"),
            cfg,
        )
        assert est.edge_positive
        assert est.margin_pct == max(cfg.min_margin_pct, min(cfg.max_margin_pct, est.kelly_scaled))


class TestEstimateKelly:
    def test_quarter_kelly_clamped(self) -> None:
        cfg = SizingConfig(kelly_fraction=0.25, max_margin_pct=0.15)
        est = estimate_kelly(TradeStats(0.60, 1.5, 100, "computed"), cfg)
        assert est.kelly_full > 0
        assert est.margin_pct <= cfg.max_margin_pct
        assert est.floor_margin_pct <= est.margin_pct

    def test_negative_edge(self) -> None:
        est = estimate_kelly(TradeStats(0.35, 1.0, 50, "computed"))
        assert not est.edge_positive


class TestComputeEntrySize:
    def test_empty_book_uses_kelly_margin(self) -> None:
        cfg = SizingConfig()
        est = estimate_kelly(config=cfg)
        plan = compute_entry_size(
            equity_usdt=50.0,
            available_usdt=48.0,
            exposure_notional_usdt=0.0,
            open_positions=0,
            mandate_max_order_notional=45.0,
            mandate_max_total_exposure=47.0,
            mandate_account_funding=47.0,
            config=cfg,
        )
        expected_margin = 47.0 * est.margin_pct
        assert abs(plan.margin_target_usdt - round(min(expected_margin, 48.0, 47 * cfg.max_margin_pct), 2)) < 0.02


class TestValidate:
    def _plan(self, **kw) -> EntrySizePlan:
        kelly = KellyEstimate(
            win_rate=0.55,
            payoff_ratio=1.5,
            sample_size=30,
            source="blend",
            kelly_full=0.12,
            kelly_scaled=0.03,
            margin_pct=0.12,
            floor_margin_pct=0.08,
            edge_positive=True,
        )
        defaults = dict(
            equity_usdt=47.0,
            available_usdt=44.0,
            exposure_notional_usdt=10.0,
            exposure_headroom_usdt=35.0,
            open_positions=2,
            margin_target_usdt=5.0,
            margin_min_usdt=3.5,
            margin_max_usdt=8.0,
            notional_target_usdt=40.0,
            notional_min_usdt=28.0,
            notional_max_usdt=42.0,
            recommended_leverage=8.0,
            conviction_score=7.0,
            conviction_multiplier=1.0,
            kelly=kelly,
            stats=TradeStats(0.55, 1.5, 30, "json"),
        )
        defaults.update(kw)
        return EntrySizePlan(**defaults)

    def test_rejects_tiny_buy(self) -> None:
        assert validate_entry_notional(7.0, self._plan(), side="buy") is not None

    def test_rejects_negative_edge(self) -> None:
        bad_kelly = KellyEstimate(
            0.4, 1.0, 50, "blend", 0.0, 0.0, 0.08, 0.08, False
        )
        plan = self._plan(kelly=bad_kelly)
        reason = validate_entry_notional(30.0, plan, side="buy")
        assert reason is not None
        assert "Kelly edge" in reason

    def test_allows_target_buy(self) -> None:
        assert validate_entry_notional(35.0, self._plan(), side="buy") is None


class TestStatsFromPnls:
    def test_payoff(self) -> None:
        st = stats_from_closed_pnls([2.0, -1.0, 3.0, -1.0])
        assert st.win_rate == 0.5
        assert st.payoff_ratio == 2.5


class TestPrompt:
    def test_kelly_in_prompt(self) -> None:
        text = format_sizing_prompt_block(
            compute_entry_size(
                equity_usdt=50.0,
                available_usdt=48.0,
                exposure_notional_usdt=0.0,
                open_positions=0,
                mandate_max_order_notional=45.0,
                mandate_max_total_exposure=47.0,
                mandate_account_funding=47.0,
            )
        )
        assert "Kelly f*=" in text
        assert "FLOOR" in text
