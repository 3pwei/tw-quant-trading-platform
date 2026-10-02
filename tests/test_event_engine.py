from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from tw_quant.events import (
    BarClosedEvent,
    DeterministicEventEngine,
    EventMetadata,
    SessionEvent,
    SignalEvent,
    VirtualClock,
    deterministic_event_id,
    event_to_dict,
)


TAIPEI = ZoneInfo("Asia/Taipei")
START = datetime(2026, 9, 1, 8, 45, tzinfo=TAIPEI)


def metadata(kind, minute, key, *, cause=None, correlation=None):
    return EventMetadata.create(
        kind=kind,
        occurred_at=START + timedelta(minutes=minute),
        source="test",
        source_key=key,
        causation_id=cause,
        correlation_id=correlation,
        owner_id="owner-1",
    )


def bar(minute: int, key: str) -> BarClosedEvent:
    return BarClosedEvent(
        meta=metadata("bar_closed", minute, key),
        symbol="TMF",
        contract="TMFU6",
        timeframe="1m",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10,
        session="day",
        trading_date=date(2026, 9, 1),
    )


class EventModelTests(unittest.TestCase):
    def test_event_id_is_stable_for_same_source_fact(self):
        first = deterministic_event_id("bar_closed", START, "history", "TMFU6:1m:0845")
        second = deterministic_event_id("bar_closed", START, "history", "TMFU6:1m:0845")
        changed = deterministic_event_id("bar_closed", START, "history", "TMFU6:1m:0846")
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_event_metadata_rejects_naive_time(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            EventMetadata.create(
                kind="session",
                occurred_at=datetime(2026, 9, 1, 8, 45),
                source="test",
                source_key="open",
            )

    def test_event_serialization_is_json_compatible(self):
        payload = event_to_dict(bar(0, "first"))
        self.assertEqual(payload["kind"], "bar_closed")
        self.assertEqual(payload["trading_date"], "2026-09-01")
        self.assertEqual(payload["meta"]["owner_id"], "owner-1")
        self.assertTrue(str(payload["meta"]["occurred_at"]).endswith("+08:00"))

    def test_bar_rejects_invalid_ohlc(self):
        with self.assertRaisesRegex(ValueError, "invalid OHLC"):
            BarClosedEvent(
                meta=metadata("bar_closed", 0, "invalid"),
                symbol="TMF",
                contract="TMFU6",
                timeframe="1m",
                open=100,
                high=99,
                low=98,
                close=100,
                volume=1,
                session="day",
                trading_date=date(2026, 9, 1),
            )


class DeterministicEventEngineTests(unittest.TestCase):
    def test_processes_by_time_then_fifo_and_advances_clock(self):
        engine = DeterministicEventEngine()
        later = bar(2, "later")
        first_same_time = bar(1, "same-a")
        second_same_time = bar(1, "same-b")
        engine.publish(later)
        engine.publish(first_same_time)
        engine.publish(second_same_time)

        run = engine.run()

        self.assertEqual(
            [record.event.meta.event_id for record in run.processed],
            [
                first_same_time.meta.event_id,
                second_same_time.meta.event_id,
                later.meta.event_id,
            ],
        )
        self.assertEqual(engine.clock.current, later.meta.occurred_at)
        self.assertEqual([record.sequence for record in run.processed], [1, 2, 3])

    def test_handler_order_and_causal_event_chain_are_stable(self):
        engine = DeterministicEventEngine()
        source = bar(0, "bar")

        def first_handler(event):
            signal = SignalEvent(
                meta=metadata(
                    "signal",
                    0,
                    "strategy-a",
                    cause=event.meta.event_id,
                    correlation=event.meta.correlation_id,
                ),
                strategy_id="strategy-a",
                strategy_version=1,
                symbol="TMF",
                contract="TMFU6",
                direction="long",
                action="enter",
                reference_price=event.close,
                reason="breakout",
            )
            return [signal]

        called = []
        engine.subscribe("bar_closed", first_handler)
        engine.subscribe("bar_closed", lambda event: called.append(event.meta.event_id))
        engine.publish(source)

        run = engine.run()

        self.assertEqual([item.event.kind for item in run.processed], ["bar_closed", "signal"])
        signal = run.processed[1].event
        self.assertEqual(signal.meta.causation_id, source.meta.event_id)
        self.assertEqual(signal.meta.correlation_id, source.meta.correlation_id)
        self.assertEqual(called, [source.meta.event_id])

    def test_duplicate_event_id_is_processed_once(self):
        engine = DeterministicEventEngine()
        source = bar(0, "same")
        self.assertTrue(engine.publish(source))
        self.assertFalse(engine.publish(source))

        run = engine.run()

        self.assertEqual(len(run.processed), 1)
        self.assertEqual(run.duplicate_event_ids, (source.meta.event_id,))
        self.assertFalse(engine.publish(source))

    def test_handler_cannot_emit_event_before_cause(self):
        engine = DeterministicEventEngine()
        source = bar(1, "cause")
        earlier = SessionEvent(
            meta=metadata("session", 0, "early"),
            symbol="TMF",
            contract="TMFU6",
            session="day",
            trading_date=date(2026, 9, 1),
            action="opened",
        )
        engine.subscribe("bar_closed", lambda _: [earlier])
        engine.publish(source)
        with self.assertRaisesRegex(ValueError, "before its cause"):
            engine.run()

    def test_virtual_clock_never_moves_backwards(self):
        clock = VirtualClock(START)
        clock.advance_to(START + timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError, "cannot move backwards"):
            clock.advance_to(START)

    def test_event_limit_breaks_accidental_infinite_chains(self):
        engine = DeterministicEventEngine(max_events=2)
        events = [bar(index, f"event-{index}") for index in range(3)]
        for event in events:
            engine.publish(event)
        with self.assertRaisesRegex(RuntimeError, "event limit exceeded"):
            engine.run()


if __name__ == "__main__":
    unittest.main()
