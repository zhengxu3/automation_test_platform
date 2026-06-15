import copy
import time

from engine import goal_runtime, goal_scheduler


def _get_path(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_path(doc: dict, path: str, value):
    cur = doc
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _unset_path(doc: dict, path: str):
    cur = doc
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur.get(part, {})
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in (query or {}).items():
        actual = _get_path(doc, key)
        if isinstance(expected, dict):
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$exists" in expected and ((actual is not None) != bool(expected["$exists"])):
                return False
            continue
        if actual != expected:
            return False
    return True


def _project(doc: dict, projection: dict | None) -> dict:
    data = copy.deepcopy(doc)
    if not projection:
        return data
    if any(v for v in projection.values()):
        out = {}
        for key, include in projection.items():
            if key == "_id" or not include:
                continue
            value = _get_path(data, key)
            if value is not None:
                _set_path(out, key, value)
        return out
    for key, include in projection.items():
        if key == "_id" and include == 0:
            continue
        if include == 0:
            _unset_path(data, key)
    return data


class FakeCursor(list):
    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return FakeCursor(self[:n])


class FakeCollection:
    def __init__(self, db):
        self.database = db
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))
        return None

    def find_one(self, query, projection=None):
        for doc in self.docs:
            if _matches(doc, query):
                return _project(doc, projection)
        return None

    def find(self, query=None, projection=None):
        return FakeCursor([_project(d, projection) for d in self.docs if _matches(d, query or {})])

    def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if _matches(doc, query):
                for key, value in (update.get("$set") or {}).items():
                    _set_path(doc, key, copy.deepcopy(value))
                for key in (update.get("$unset") or {}).keys():
                    _unset_path(doc, key)
                return None
        if upsert:
            doc = copy.deepcopy(query)
            for key, value in (update.get("$set") or {}).items():
                _set_path(doc, key, copy.deepcopy(value))
            self.docs.append(doc)
        return None

    def delete_many(self, query):
        self.docs = [d for d in self.docs if not _matches(d, query)]
        return None


class FakeDb(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = FakeCollection(self)
        return dict.__getitem__(self, name)


def _fake_db(monkeypatch):
    db = FakeDb()

    def get_collection(name):
        return db[name]

    monkeypatch.setattr(goal_runtime, "get_collection", get_collection)
    monkeypatch.setattr(goal_scheduler, "get_collection", get_collection)
    return db


def test_plan_transition_persists_and_due_executes(monkeypatch):
    db = _fake_db(monkeypatch)
    goal_id = "goal_plan_transition_due"
    now = int(time.time())
    db["ai_goals"].insert_one({
        "goal_id": goal_id,
        "title": "plan transition",
        "status": "running",
        "sources": [],
        "feasibility": {"input_mode": "empty", "allowed_evidence_types": [], "producible_evidence_types": []},
        "evidence_policy": {},
        "goal_statement": "验证计划切换",
        "acceptance": [{"id": "A1", "desc": "x", "evidence_type": "doc_review", "verdict": "pending"}],
        "goal_confidence": 0.8,
        "plan_version": 1,
        "current_plan_kind": "discovery",
        "created_at": now,
        "updated_at": now,
    })
    monkeypatch.setenv("GOAL_PLAN_TRANSITION_DELAY_SEC", "3")
    monkeypatch.setattr(goal_runtime, "_start_plan_transition_timer", lambda *args, **kwargs: None)

    scheduled = goal_runtime._schedule_plan_transition(
        goal_id,
        from_kind="discovery",
        to_kind="objective",
        plan_version=2,
        trigger="goal_generated",
        context={"acceptance": [{"id": "A1"}]},
    )
    assert scheduled["ok"] is True
    assert scheduled["waiting_transition"]["status"] == "scheduled"

    goal = db["ai_goals"].find_one({"goal_id": goal_id})
    pending = goal["pending_plan_transition"]
    assert pending["to_kind"] == "objective"
    assert pending["next_plan_at"] > int(time.time())

    event = db["ai_goal_events"].find_one({"goal_id": goal_id, "event": "plan_transition"})
    assert event["payload"]["to_kind"] == "objective"

    captured = {}

    def fake_plan_and_start(goal_id_arg, profile, policy, goal_statement, acceptance, goal_confidence,
                            memory_ctx, plan_kind="objective", plan_version=None, prior_context=""):
        captured.update({
            "goal_id": goal_id_arg,
            "plan_kind": plan_kind,
            "plan_version": plan_version,
            "acceptance": acceptance,
        })
        return {"ok": True, "started": True}

    monkeypatch.setattr(goal_runtime, "_plan_and_start", fake_plan_and_start)
    monkeypatch.setattr(goal_runtime.steward, "retrieve_memory", lambda **kwargs: "")
    db["ai_goals"].update_one(
        {"goal_id": goal_id},
        {"$set": {"pending_plan_transition.next_plan_at": int(time.time()) - 1}},
    )

    processed = goal_runtime.process_due_plan_transitions()
    assert processed["processed"][0]["result"]["ok"] is True
    assert captured["goal_id"] == goal_id
    assert captured["plan_kind"] == "objective"
    assert captured["plan_version"] == 2
    assert captured["acceptance"] == [{"id": "A1"}]
    assert "pending_plan_transition" not in db["ai_goals"].find_one({"goal_id": goal_id})


def test_scheduler_waits_when_plan_transition_pending():
    db = FakeDb()
    goal_id = "goal_plan_transition_wait"
    now = int(time.time())
    db["ai_goals"].insert_one({
        "goal_id": goal_id,
        "title": "wait transition",
        "status": "running",
        "sources": [],
        "acceptance": [],
        "plan_version": 1,
        "current_plan_kind": "discovery",
        "pending_plan_transition": {
            "transition_id": "ptr_wait",
            "status": "scheduled",
            "from_kind": "discovery",
            "to_kind": "objective",
            "plan_version": 2,
            "next_plan_at": now + 3,
        },
        "created_at": now,
        "updated_at": now,
    })
    db["ai_goal_steps"].insert_one({
        "goal_id": goal_id,
        "step_id": "d1",
        "name": "目标发现",
        "status": "completed",
        "plan_kind": "discovery",
        "plan_version": 1,
        "created_at": now,
        "updated_at": now,
    })

    original_get_collection = goal_scheduler.get_collection
    goal_scheduler.get_collection = lambda name: db[name]
    try:
        result = goal_scheduler.advance(goal_id)
    finally:
        goal_scheduler.get_collection = original_get_collection

    assert result["ok"] is True
    assert result["waiting_transition"]["transition_id"] == "ptr_wait"
