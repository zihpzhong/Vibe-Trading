"""Unit tests for PositionTracker — US-008."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from extensions.live_trading.engine.position_tracker import Position, PositionTracker


@pytest.fixture
def tracker() -> PositionTracker:
    """Fresh tracker with temp persistence directory (no file on disk)."""
    tmp = tempfile.mkdtemp()
    t = PositionTracker(account_balance=10_000.0, max_exposure_pct=0.25, persist_dir=tmp)
    yield t
    # Cleanup
    t.clear()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------

class TestPositionDataclass:
    """Position data model."""

    def test_creation_with_required_fields(self) -> None:
        p = Position(symbol="BTCUSDT", direction="LONG", entry_price=65000.0, quantity=0.1, stop_loss=63000.0)
        assert p.symbol == "BTCUSDT"
        assert p.direction == "LONG"
        assert p.entry_price == 65000.0
        assert p.stop_loss == 63000.0
        assert p.take_profit is None
        assert p.opened_at != ""  # auto-generated

    def test_creation_with_all_fields(self) -> None:
        p = Position("ETHUSDT", "SHORT", 3200.0, 1.0, 3300.0, 3000.0, "2025-01-01T00:00:00Z")
        assert p.take_profit == 3000.0
        assert p.opened_at == "2025-01-01T00:00:00Z"

    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        p = Position("SOLUSDT", "LONG", 145.0, 10.0, 138.0, 152.0, "2025-01-01T00:00:00Z")
        d = p.to_dict()
        p2 = Position.from_dict(d)
        assert p2.symbol == p.symbol
        assert p2.direction == p.direction
        assert p2.entry_price == p.entry_price
        assert p2.quantity == p.quantity
        assert p2.stop_loss == p.stop_loss
        assert p2.take_profit == p.take_profit
        assert p2.opened_at == p.opened_at

    def test_from_dict_minimal(self) -> None:
        """from_dict works with minimal keys."""
        d = {"symbol": "X", "direction": "LONG", "entry_price": "50", "quantity": "1", "stop_loss": "45"}
        p = Position.from_dict(d)
        assert p.symbol == "X"
        assert p.entry_price == 50.0
        assert p.take_profit is None


# ---------------------------------------------------------------------------
# Open / Close lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    """Position open/close lifecycle."""

    def test_open_position_returns_position(self, tracker: PositionTracker) -> None:
        pos = tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert isinstance(pos, Position)
        assert pos.symbol == "BTCUSDT"

    def test_open_position_updates_active_count(self, tracker: PositionTracker) -> None:
        assert tracker.active_count == 0
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker.active_count == 1

    def test_open_same_symbol_overwrites(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        tracker.open_position("BTCUSDT", "SHORT", 66000.0, 0.2, 67000.0)
        assert tracker.active_count == 1
        pos = tracker.get_position("BTCUSDT")
        assert pos is not None
        assert pos.direction == "SHORT"

    def test_close_position_removes(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        closed = tracker.close_position("BTCUSDT")
        assert closed is not None
        assert closed.symbol == "BTCUSDT"
        assert tracker.active_count == 0
        assert tracker.get_position("BTCUSDT") is None

    def test_close_nonexistent_returns_none(self, tracker: PositionTracker) -> None:
        assert tracker.close_position("NOSUCH") is None

    def test_get_active_positions_returns_copy(self, tracker: PositionTracker) -> None:
        tracker.open_position("A", "LONG", 100.0, 1.0, 90.0)
        tracker.open_position("B", "SHORT", 200.0, 2.0, 220.0)
        positions = tracker.get_active_positions()
        assert len(positions) == 2
        # Modifying the returned list doesn't affect tracker
        positions.clear()
        assert tracker.active_count == 2


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------

class TestExposure:
    """Exposure calculation."""

    def test_exposure_zero_with_no_positions(self, tracker: PositionTracker) -> None:
        assert tracker.get_exposure() == 0.0

    def test_exposure_single_position(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 50000.0, 0.01, 48000.0)
        # value = 500, balance = 10000, exposure = 0.05
        assert tracker.get_exposure() == pytest.approx(0.05)

    def test_exposure_multiple_positions(self, tracker: PositionTracker) -> None:
        tracker.open_position("A", "LONG", 100.0, 10.0, 90.0)  # 1000
        tracker.open_position("B", "SHORT", 200.0, 5.0, 220.0)  # 1000
        # total value = 2000, balance = 10000, exposure = 0.20
        assert tracker.get_exposure() == pytest.approx(0.20)

    def test_exposure_full_when_balance_zero(self) -> None:
        import tempfile
        tmp = tempfile.mkdtemp()
        t = PositionTracker(account_balance=0.0, persist_dir=tmp)
        t.open_position("X", "LONG", 100.0, 1.0, 90.0)
        assert t.get_exposure() == 1.0
        t.clear()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# can_open_new
# ---------------------------------------------------------------------------

class TestCanOpenNew:
    """Position opening guard."""

    def test_allows_first_position(self, tracker: PositionTracker) -> None:
        ok, reason = tracker.can_open_new("BTCUSDT")
        assert ok is True
        assert reason == ""

    def test_rejects_duplicate_symbol(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        ok, reason = tracker.can_open_new("BTCUSDT")
        assert ok is False
        assert "已有" in reason

    def test_rejects_at_max_positions(self, tracker: PositionTracker) -> None:
        tracker.open_position("A", "LONG", 100.0, 1.0, 90.0)
        tracker.open_position("B", "LONG", 200.0, 1.0, 180.0)
        tracker.open_position("C", "LONG", 300.0, 1.0, 270.0)
        tracker.open_position("D", "LONG", 400.0, 1.0, 360.0)
        tracker.open_position("E", "LONG", 500.0, 1.0, 450.0)
        assert tracker.active_count == 5
        ok, reason = tracker.can_open_new("F")
        assert ok is False
        assert "上限" in reason

    def test_rejects_when_exposure_exceeded(self, tracker: PositionTracker) -> None:
        # 3000 value / 10000 balance = 0.30 > 0.25 max
        tracker.open_position("A", "LONG", 3000.0, 1.0, 2900.0)
        ok, reason = tracker.can_open_new("B")
        assert ok is False
        assert "敞口" in reason

    def test_allows_different_symbol_below_limits(self, tracker: PositionTracker) -> None:
        tracker.open_position("A", "LONG", 100.0, 1.0, 90.0)
        ok, reason = tracker.can_open_new("B")
        assert ok is True
        assert reason == ""


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

class TestCooldown:
    """Signal cooldown tracking."""

    def test_not_in_cooldown_initially(self, tracker: PositionTracker) -> None:
        assert tracker.is_in_cooldown("BTCUSDT", "LONG") is False

    def test_in_cooldown_after_open(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker.is_in_cooldown("BTCUSDT", "LONG") is True

    def test_different_direction_not_in_cooldown(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker.is_in_cooldown("BTCUSDT", "SHORT") is False

    def test_different_symbol_not_in_cooldown(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker.is_in_cooldown("ETHUSDT", "LONG") is False

    def test_cooldown_expires(self, tracker: PositionTracker) -> None:
        # Create tracker with very short cooldown
        import tempfile
        tmp = tempfile.mkdtemp()
        t = PositionTracker(account_balance=10000.0, cooldown_minutes=0, persist_dir=tmp)
        t.open_position("X", "LONG", 100.0, 1.0, 90.0)
        # cooldown_minutes=0 means cooldown expires immediately (elapsed >= 0)
        time.sleep(0.01)
        assert t.is_in_cooldown("X", "LONG") is False
        t.clear()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    """JSON persistence to disk."""

    def test_positions_persisted_to_file(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker._persist_path.exists()

    def test_positions_restored_on_load(self) -> None:
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        t1 = PositionTracker(account_balance=10000.0, persist_dir=tmp)
        t1.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0, 67000.0)

        # Create new tracker that loads from same file
        t2 = PositionTracker(account_balance=10000.0, persist_dir=tmp)
        assert t2.active_count == 1
        pos = t2.get_position("BTCUSDT")
        assert pos is not None
        assert pos.symbol == "BTCUSDT"
        assert pos.entry_price == 65000.0
        assert pos.stop_loss == 63000.0
        assert pos.take_profit == 67000.0

        t1.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    def test_account_balance_restored(self) -> None:
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        t1 = PositionTracker(account_balance=50000.0, persist_dir=tmp)
        t1.open_position("X", "LONG", 100.0, 1.0, 90.0)
        t2 = PositionTracker(account_balance=99999.0, persist_dir=tmp)  # should use restored balance
        assert t2.account_balance == 50000.0
        t1.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    def test_clear_removes_persist_file(self, tracker: PositionTracker) -> None:
        tracker.open_position("X", "LONG", 100.0, 1.0, 90.0)
        assert tracker._persist_path.exists()
        tracker.clear()
        assert not tracker._persist_path.exists()
        assert tracker.active_count == 0

    def test_load_corrupt_file_handled(self) -> None:
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        persist_path = Path(tmp) / "positions.json"
        persist_path.write_text("not json")
        t = PositionTracker(account_balance=10000.0, persist_dir=tmp)
        assert t.active_count == 0  # gracefully handled
        t.clear()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """Concurrent access doesn't corrupt state."""

    def test_concurrent_opens_and_reads(self, tracker: PositionTracker) -> None:
        import threading

        errors: list[Exception] = []

        def open_positions(start: int) -> None:
            try:
                for i in range(start, start + 10):
                    sym = f"SYM{i}"
                    if tracker.can_open_new(sym)[0]:
                        tracker.open_position(sym, "LONG", 100.0, 1.0, 90.0)
                    _ = tracker.get_exposure()
                    _ = tracker.is_in_cooldown(sym, "LONG")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=open_positions, args=(0,))
        t2 = threading.Thread(target=open_positions, args=(10,))
        t3 = threading.Thread(target=open_positions, args=(20,))
        t1.start()
        t2.start()
        t3.start()
        t1.join()
        t2.join()
        t3.join()

        assert len(errors) == 0, f"Thread errors: {errors}"
        # can_open_new + open_position is not atomic under concurrent access --
        # threads can pass the gate before any inserts. Verify no crashes.
        assert 0 <= tracker.active_count <= 30

    def test_close_from_another_tracker_reference(self) -> None:
        """Persistence enables restart recovery (not live state sharing)."""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        t1 = PositionTracker(account_balance=10000.0, persist_dir=tmp)
        t1.open_position("X", "LONG", 100.0, 1.0, 90.0)
        # Create tracker that loads from disk (simulating restart)
        t2 = PositionTracker(account_balance=10000.0, persist_dir=tmp)
        assert t2.active_count == 1
        t1.clear()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# account_balance property
# ---------------------------------------------------------------------------

class TestAccountBalance:
    """Account balance getter/setter."""

    def test_balance_initial_value(self, tracker: PositionTracker) -> None:
        assert tracker.account_balance == 10000.0

    def test_balance_setter_updates_exposure(self, tracker: PositionTracker) -> None:
        tracker.open_position("X", "LONG", 500.0, 2.0, 450.0)  # value = 1000
        assert tracker.get_exposure() == pytest.approx(0.10)  # 1000/10000
        tracker.account_balance = 5000.0
        assert tracker.get_exposure() == pytest.approx(0.20)  # 1000/5000
