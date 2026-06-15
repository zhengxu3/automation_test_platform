"""Goal 事件窗口测试。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import engine.goal_runtime as goal_runtime


class _FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec, direction=None):
        if isinstance(spec, list):
            for key, order in reversed(spec):
                self.docs.sort(key=lambda d: d.get(key), reverse=(order < 0))
        else:
            self.docs.sort(key=lambda d: d.get(spec), reverse=(direction or 1) < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __iter__(self):
        return iter(self.docs)


class _FakeEvents:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        goal_id = query["goal_id"]
        return _FakeCursor([d for d in self.docs if d["goal_id"] == goal_id])


def test_latest_events_returns_recent_window_in_chronological_order(monkeypatch):
    goal_id = "goal_events_fake"
    now = int(time.time())
    docs = [
        {"_id": i, "goal_id": goal_id, "event": "tick", "actor": "test",
         "payload": {"i": i}, "timestamp": now + i}
        for i in range(305)
    ]

    monkeypatch.setattr(goal_runtime, "get_collection", lambda name: _FakeEvents(docs))

    events = goal_runtime._latest_events(goal_id, limit=300)

    assert len(events) == 300
    assert events[0]["payload"]["i"] == 5
    assert events[-1]["payload"]["i"] == 304
