import os
import threading
import time
import unittest
from dataclasses import replace


# Allow importing pynput in headless CI; workers inject a fake controller.
os.environ.setdefault("PYNPUT_BACKEND", "dummy")

from autoclicker import (  # noqa: E402
    AutoClickerApp,
    ClickConfig,
    ClickWorker,
    ConfigError,
)


def valid_values(**overrides):
    values = {
        "int_h": "0",
        "int_m": "0",
        "int_s": "0",
        "int_ms": "100",
        "button": "Left",
        "click_type": "Single",
        "hold_ms": "500",
        "position_mode": "current",
        "pos_x": "0",
        "pos_y": "0",
        "repeat_mode": "count",
        "repeat_count": "1",
        "dur_h": "0",
        "dur_m": "0",
        "dur_s": "30",
    }
    values.update(overrides)
    return values


def worker_config(**overrides):
    config = ClickConfig.from_values(valid_values())
    return replace(config, **overrides)


class FakeMouseController:
    def __init__(self, fail_click=False):
        self.events = []
        self.position = (0, 0)
        self.pressed = threading.Event()
        self.fail_click = fail_click

    def click(self, button, count):
        if self.fail_click:
            raise RuntimeError("click failed")
        self.events.append(("click", button, count, time.monotonic()))

    def press(self, button):
        self.events.append(("press", button, time.monotonic()))
        self.pressed.set()

    def release(self, button):
        self.events.append(("release", button, time.monotonic()))


class ClickConfigTests(unittest.TestCase):
    def test_valid_hold_config(self):
        config = ClickConfig.from_values(valid_values(
            click_type="Hold", hold_ms="750", position_mode="fixed",
            pos_x="-120", pos_y="450"))

        self.assertEqual(config.click_type, "Hold")
        self.assertEqual(config.hold_seconds, 0.75)
        self.assertEqual((config.pos_x, config.pos_y), (-120, 450))

    def test_rejects_interval_below_ten_milliseconds(self):
        with self.assertRaisesRegex(ConfigError, "at least 10"):
            ClickConfig.from_values(valid_values(int_ms="9"))

    def test_rejects_invalid_active_hold_duration(self):
        with self.assertRaisesRegex(ConfigError, "Hold duration"):
            ClickConfig.from_values(valid_values(
                click_type="Hold", hold_ms="not-a-number"))

    def test_ignores_invalid_values_in_inactive_controls(self):
        config = ClickConfig.from_values(valid_values(
            hold_ms="invalid", position_mode="current", pos_x="invalid",
            repeat_mode="infinite", repeat_count="invalid", dur_s="invalid"))

        self.assertEqual(config.hold_seconds, 0.5)
        self.assertEqual((config.pos_x, config.pos_y), (0, 0))

    def test_duration_must_be_positive(self):
        with self.assertRaisesRegex(ConfigError, "greater than zero"):
            ClickConfig.from_values(valid_values(
                repeat_mode="duration", dur_h="0", dur_m="0", dur_s="0"))


class ClickWorkerTests(unittest.TestCase):
    def run_worker(self, config, controller, start_delay=0):
        click_events = []
        done_events = []
        worker = ClickWorker(
            config=config,
            run_id=7,
            on_click_cb=lambda run_id, count: click_events.append((run_id, count)),
            on_done_cb=lambda run_id, reason: done_events.append((run_id, reason)),
            controller_factory=lambda: controller,
            start_delay=start_delay,
        )
        worker.start()
        return worker, click_events, done_events

    def test_single_count_finishes_without_extra_interval(self):
        controller = FakeMouseController()
        config = worker_config(
            repeat_count=2, interval_seconds=0.2, click_type="Single")
        started = time.monotonic()

        worker, click_events, done_events = self.run_worker(config, controller)
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 0.35)
        self.assertEqual([event[2] for event in controller.events], [1, 1])
        self.assertEqual(click_events, [(7, 1), (7, 2)])
        self.assertEqual(done_events, [(7, "Reached click limit")])

    def test_completed_hold_waits_then_releases(self):
        controller = FakeMouseController()
        config = worker_config(click_type="Hold", hold_seconds=0.04)

        worker, click_events, done_events = self.run_worker(config, controller)
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual([event[0] for event in controller.events], ["press", "release"])
        held_for = controller.events[1][2] - controller.events[0][2]
        self.assertGreaterEqual(held_for, 0.03)
        self.assertEqual(click_events, [(7, 1)])
        self.assertEqual(done_events, [(7, "Reached click limit")])

    def test_stop_during_hold_releases_without_counting(self):
        controller = FakeMouseController()
        config = worker_config(
            click_type="Hold", hold_seconds=5, repeat_mode="infinite")
        worker, click_events, done_events = self.run_worker(config, controller)
        self.assertTrue(controller.pressed.wait(0.5))

        worker.stop("User stopped")
        worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual([event[0] for event in controller.events], ["press", "release"])
        self.assertEqual(click_events, [])
        self.assertEqual(done_events, [(7, "User stopped")])

    def test_duration_deadline_shortens_hold_and_releases(self):
        controller = FakeMouseController()
        config = worker_config(
            click_type="Hold", hold_seconds=1, repeat_mode="duration",
            duration_seconds=0.04)
        worker, click_events, done_events = self.run_worker(config, controller)

        worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual([event[0] for event in controller.events], ["press", "release"])
        self.assertEqual(click_events, [])
        self.assertEqual(done_events, [(7, "Duration elapsed")])

    def test_duration_does_not_wait_full_click_interval(self):
        controller = FakeMouseController()
        config = worker_config(
            repeat_mode="duration", duration_seconds=0.04,
            interval_seconds=1)
        started = time.monotonic()
        worker, click_events, done_events = self.run_worker(config, controller)

        worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(click_events, [(7, 1)])
        self.assertEqual(done_events, [(7, "Duration elapsed")])

    def test_worker_error_reports_once_and_does_not_count(self):
        controller = FakeMouseController(fail_click=True)
        worker, click_events, done_events = self.run_worker(
            worker_config(), controller)

        worker.join(0.5)

        self.assertEqual(click_events, [])
        self.assertEqual(len(done_events), 1)
        self.assertIn("Error: click failed", done_events[0][1])


class LifecycleTests(unittest.TestCase):
    def test_stale_completion_does_not_change_active_run(self):
        app = AutoClickerApp.__new__(AutoClickerApp)
        app.active_run_id = 2
        app.run_state = "running"
        active_worker = object()
        app.worker = active_worker

        app._finish_run(1, "Old worker finished")

        self.assertEqual(app.run_state, "running")
        self.assertIs(app.worker, active_worker)


if __name__ == "__main__":
    unittest.main()
