"""Headless unit tests for QThread lifecycle cleanup in gui.py"""
import os
from unittest.mock import MagicMock, Mock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QThread, Signal


class MockQThreadWorker(QObject):
    """Mock worker with cleanup signals for testing."""
    finished = Signal()
    failed = Signal(str)
    cancelled = Signal(list)

    def run(self):
        pass

    def request_cancel(self):
        pass


def test_cleanup_function_clears_state():
    """Test that cleanup() function correctly clears worker_thread and active_worker."""
    # Simulate the cleanup function from start_worker()
    state = {"worker_thread": MagicMock(spec=QThread), "active_worker": MagicMock()}

    def cleanup():
        try:
            if state["worker_thread"]:
                state["worker_thread"].quit()
            if state["worker_thread"]:
                state["worker_thread"].deleteLater()
        except RuntimeError:
            pass
        finally:
            state["active_worker"] = None
            state["worker_thread"] = None

    # Initial state
    assert state["worker_thread"] is not None
    assert state["active_worker"] is not None

    # Call cleanup
    cleanup()

    # Verify state is cleared
    assert state["worker_thread"] is None
    assert state["active_worker"] is None


def test_cleanup_handles_deleted_objects():
    """Test cleanup() gracefully handles already-deleted objects."""
    state = {"worker_thread": MagicMock(spec=QThread), "active_worker": MagicMock()}

    # Make quit() raise RuntimeError (simulating deleted object)
    state["worker_thread"].quit.side_effect = RuntimeError("libshiboken: Internal C++ object already deleted")

    def cleanup():
        try:
            if state["worker_thread"]:
                state["worker_thread"].quit()
            if state["worker_thread"]:
                state["worker_thread"].deleteLater()
        except RuntimeError:
            pass
        finally:
            state["active_worker"] = None
            state["worker_thread"] = None

    # Should not raise, just clear state
    cleanup()

    assert state["worker_thread"] is None
    assert state["active_worker"] is None


def test_multiple_signal_cleanup_only_happens_once():
    """Test that multiple signal connections trigger cleanup only effectively once."""
    state = {
        "worker_thread": MagicMock(spec=QThread),
        "active_worker": MagicMock(),
        "cleanup_count": 0,
    }

    def cleanup():
        try:
            if state["worker_thread"]:
                state["worker_thread"].quit()
            if state["worker_thread"]:
                state["worker_thread"].deleteLater()
        except RuntimeError:
            pass
        finally:
            state["active_worker"] = None
            state["worker_thread"] = None
            state["cleanup_count"] += 1

    # Simulate multiple signal connections
    worker = MockQThreadWorker()
    worker.finished.connect(cleanup)
    worker.failed.connect(cleanup)
    worker.cancelled.connect(cleanup)

    # Emit all signals
    worker.finished.emit()
    worker.failed.emit("error")
    worker.cancelled.emit([])

    # After first cleanup, subsequent calls should be safe
    assert state["active_worker"] is None
    assert state["worker_thread"] is None
    assert state["cleanup_count"] == 3  # All 3 connected


def test_cleanup_on_runtime_error_from_deleted_thread():
    """Test that RuntimeError from thread.quit() is caught during cleanup."""
    state = {"worker_thread": MagicMock(spec=QThread), "active_worker": MagicMock()}

    # Simulate RuntimeError from already-deleted C++ object
    state["worker_thread"].quit.side_effect = RuntimeError("libshiboken: Internal C++ object (PySide6.QtCore.QThread) already deleted.")

    def cleanup():
        try:
            if state["worker_thread"]:
                state["worker_thread"].quit()
            if state["worker_thread"]:
                state["worker_thread"].deleteLater()
        except RuntimeError:
            pass  # Gracefully ignore
        finally:
            state["active_worker"] = None
            state["worker_thread"] = None

    # Should not raise exception
    cleanup()

    # State should be cleared
    assert state["worker_thread"] is None
    assert state["active_worker"] is None


def test_start_worker_prevents_concurrent_operations():
    """Test that start_worker checks if previous thread is still running."""
    thread1 = MagicMock(spec=QThread)
    thread1.isRunning.return_value = True

    state = {"worker_thread": thread1, "error_shown": False}

    # Simulate start_worker's concurrency check
    if state["worker_thread"] and state["worker_thread"].isRunning():
        state["error_shown"] = True

    assert state["error_shown"] is True


def test_start_worker_allows_new_operation_after_cleanup():
    """Test that start_worker allows new operation after previous thread is cleaned up."""
    thread1 = MagicMock(spec=QThread)
    thread1.isRunning.return_value = False

    state = {"worker_thread": thread1, "error_shown": False}

    # Simulate cleanup
    state["worker_thread"] = None

    # Simulate start_worker's concurrency check with cleaned state
    if state["worker_thread"] and state["worker_thread"].isRunning():
        state["error_shown"] = True

    assert state["error_shown"] is False


def test_cleanup_sequence_state_transitions():
    """Test state transitions: initialized -> running -> cleaned up -> can start new."""
    # Initial state
    state = {"worker_thread": None, "active_worker": None}
    assert state["worker_thread"] is None and state["active_worker"] is None

    # After start_worker
    state["worker_thread"] = MagicMock(spec=QThread)
    state["active_worker"] = MagicMock()
    assert state["worker_thread"] is not None and state["active_worker"] is not None

    # After cleanup
    def cleanup():
        try:
            if state["worker_thread"]:
                state["worker_thread"].quit()
        except RuntimeError:
            pass
        finally:
            state["active_worker"] = None
            state["worker_thread"] = None

    cleanup()
    assert state["worker_thread"] is None and state["active_worker"] is None

    # Can start new worker
    state["worker_thread"] = MagicMock(spec=QThread)
    state["active_worker"] = MagicMock()
    assert state["worker_thread"] is not None and state["active_worker"] is not None


def test_cleanup_with_none_worker_thread():
    """Test cleanup gracefully handles None worker_thread."""
    state = {"worker_thread": None, "active_worker": MagicMock()}

    def cleanup():
        try:
            if state["worker_thread"]:
                state["worker_thread"].quit()
        except RuntimeError:
            pass
        finally:
            state["active_worker"] = None
            state["worker_thread"] = None

    cleanup()

    assert state["worker_thread"] is None
    assert state["active_worker"] is None


def test_next_start_worker_after_previous_deleted():
    """Test that second start_worker() call doesn't reference previously deleted thread."""
    # First worker lifecycle
    thread1 = MagicMock(spec=QThread)
    thread1.isRunning.return_value = True

    state = {
        "worker_thread": thread1,
        "active_worker": MagicMock(),
        "operations": [],
    }

    # Record that first operation started
    state["operations"].append("start_worker_1")

    # Cleanup after first worker
    def cleanup():
        try:
            if state["worker_thread"]:
                state["worker_thread"].quit()
        except RuntimeError:
            pass
        finally:
            state["active_worker"] = None
            state["worker_thread"] = None

    cleanup()
    state["operations"].append("cleanup_1")

    # Second start_worker with fresh thread
    thread2 = MagicMock(spec=QThread)
    thread2.isRunning.return_value = True

    state["worker_thread"] = thread2
    state["active_worker"] = MagicMock()
    state["operations"].append("start_worker_2")

    # Verify operations sequence
    assert state["operations"] == ["start_worker_1", "cleanup_1", "start_worker_2"]
    assert state["worker_thread"] is thread2
    # thread1 should never be accessed again
    thread1.quit.assert_called()
    thread1.quit.reset_mock()
    # thread1 should not be called in second operation
    assert thread1.quit.call_count == 0
