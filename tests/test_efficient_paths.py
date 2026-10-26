"""Efficient-path registry tests."""

from ops.efficient_paths import EfficientPathRegistry

EXPECTED_GOALS = {
    "universe_build",
    "regime_validation",
    "three_arm_backtest",
    "gate_audit",
    "provenance_audit",
    "full_pipeline",
}


def test_default_registry_has_house_goals():
    registry = EfficientPathRegistry()
    assert EXPECTED_GOALS <= set(registry.goals())


def test_label_path_contains_goal_files_and_entrypoint():
    registry = EfficientPathRegistry()
    label = registry.label_path("three_arm_backtest")
    assert label.startswith("EFFICIENT: three_arm_backtest")
    assert "ingestion/harness/three_arm_runner.py" in label
    assert "data/backtests" in label
    assert "python -m ingestion.harness.three_arm_runner" in label
    assert "run:" in label


def test_label_path_unknown_goal_is_unlabeled():
    registry = EfficientPathRegistry()
    label = registry.label_path("nope")
    assert label.startswith("UNLABELED:") and "nope" in label


def test_register_get_roundtrip():
    registry = EfficientPathRegistry()
    entry = {
        "primary_files": ["a/b.py"],
        "support_folders": ["a"],
        "entrypoint": "python -m a.b",
        "expected_time": "1 min",
    }
    registry.register("custom_goal", entry)
    got = registry.get("custom_goal")
    assert got is not None
    assert got["primary_files"] == ["a/b.py"]
    assert got["entrypoint"] == "python -m a.b"
    assert registry.label_path("custom_goal").count("custom_goal") == 1


def test_register_replaces_existing_goal():
    registry = EfficientPathRegistry()
    registry.register("universe_build", {"primary_files": ["z.py"]})
    assert registry.get("universe_build")["primary_files"] == ["z.py"]


def test_all_labels_cover_every_goal():
    registry = EfficientPathRegistry()
    labels = registry.all_labels()
    assert set(labels) == set(registry.goals())
    for label in labels.values():
        assert label.startswith("EFFICIENT:") or label.startswith("UNLABELED:")


def test_to_dict_is_detached_copy():
    registry = EfficientPathRegistry()
    snapshot = registry.to_dict()
    snapshot["universe_build"]["primary_files"].append("mutant.py")
    assert "mutant.py" not in registry.get("universe_build")["primary_files"]


def test_get_missing_goal_returns_none():
    assert EfficientPathRegistry().get("absent_goal") is None
