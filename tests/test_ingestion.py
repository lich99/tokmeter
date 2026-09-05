import json
import os

from conftest import claude, context, meta, token, write


def test_codex_cumulative_snapshots_and_restart(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token(), token(ts="2026-09-05T12:02:00Z"), token(cumulative=2200)])
    snap = store.refresh()
    assert snap.frame.height == 2
    assert snap.scan["duplicate_snapshots"] == 1
    assert snap.frame["in_t"].sum() == 2000


def test_append_reads_only_delta_and_retains_model(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token()])
    first = store.refresh()
    old_size = path.stat().st_size
    write(path, [token(cumulative=2200)], append=True)
    second = store.refresh()
    assert second.frame.height == 2 and second.models == ("gpt-6-astra",)
    assert second.scan["bytes_read"] == path.stat().st_size - old_size
    assert second.scan["appended"] == 1 and second.scan["rebuilt"] == 0
    assert first.frame.height == 1  # Readers keep an immutable previous snapshot.
    unchanged = store.refresh()
    assert unchanged.generation == second.generation and unchanged.scan["bytes_read"] == 0


def test_incomplete_tail_then_completion(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token()])
    extra = json.dumps(token(cumulative=2200))
    with path.open("a") as f:
        f.write(extra[:30])
    first = store.refresh()
    assert first.frame.height == 1 and first.scan["pending_files"] == 1
    with path.open("a") as f:
        f.write(extra[30:] + "\n")
    second = store.refresh()
    assert second.frame.height == 2 and second.scan["pending_files"] == 0


def test_replacement_truncation_and_deletion(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token(), token(cumulative=2200)])
    store.refresh()
    write(path, [meta(), context("gpt-5.6-sol"), token()])
    s = store.refresh()
    assert s.frame.height == 1 and s.models == ("gpt-5.6-sol",) and s.scan["rebuilt"] == 1
    replacement = path.with_suffix(".new")
    write(replacement, [meta(), context(), token()])
    os.replace(replacement, path)
    assert store.refresh().models == ("gpt-6-astra",)
    path.unlink()
    assert store.refresh().frame.is_empty()


def test_bad_usage_does_not_drop_rest_of_file(store):
    path = store.sources.codex / "sessions/a.jsonl"
    bad = token()
    bad["payload"]["info"]["last_token_usage"]["input_tokens"] = "bad"
    write(path, [meta(), context(), bad, token()])
    s = store.refresh()
    assert s.frame.height == 1 and s.scan["malformed"] == 1


def test_claude_streaming_update_and_cache_ttls(store):
    path = store.sources.claude / "project/a.jsonl"
    write(
        path,
        [
            claude(out=1),
            claude(
                out=100,
                ts="2026-09-05T12:02:00Z",
                cache_creation_input_tokens=50,
                cache_creation={"ephemeral_1h_input_tokens": 20},
            ),
        ],
    )
    s = store.refresh()
    assert s.frame.height == 1 and s.frame["out_t"].sum() == 100
    assert s.frame["cw5m"].sum() == 30 and s.frame["cw1h"].sum() == 20


def test_archives_and_explicit_fork_history(store):
    path = store.sources.codex / "archived_sessions/a.jsonl"
    write(
        path,
        [
            meta(forked_from_id="parent", source={"subagent": {"spawn": {}}}),
            context(),
            token(ts="2026-09-05T11:59:00Z"),
            token(cumulative=2200),
        ],
    )
    s = store.refresh()
    assert s.frame.height == 1 and s.frame["is_sub"].sum() == 1 and s.scan["inherited_events"] == 1


def test_cumulative_reset_is_a_new_segment(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token(cumulative=9000), token(cumulative=1100)])
    s = store.refresh()
    assert s.frame.height == 2 and s.scan["cumulative_resets"] == 1


def test_requested_priority_is_not_served_priority(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(service_tier="priority"), token()])
    s = store.refresh()
    assert s.frame["tier"][0] == 0 and s.frame["tier_assumed"][0]


def test_no_usage_snapshot_rebuild_for_text_only_append(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token()])
    a = store.refresh()
    write(
        path, [{"type": "response_item", "payload": {"type": "message", "content": "unrelated"}}], append=True
    )
    b = store.refresh()
    assert b.generation == a.generation and b.frame is a.frame


def test_invalid_snapshot_does_not_poison_dedup_state(store):
    bad = token(cached=2000)
    write(store.sources.codex / "sessions/a.jsonl", [meta(), context(), bad, token()])
    s = store.refresh()
    assert s.frame.height == 1 and s.scan["invalid_usage"] == 1


def test_negative_cache_write_is_not_masked(store):
    write(store.sources.claude / "p/a.jsonl", [claude(cache_creation_input_tokens=-1), claude(mid="valid")])
    s = store.refresh()
    assert s.frame.height == 1 and s.scan["invalid_usage"] == 1


def test_late_claude_partial_cannot_replace_complete_usage(store):
    write(store.sources.claude / "p/a.jsonl", [claude(out=100), claude(out=1, ts="2026-09-05T12:02:00Z")])
    assert store.refresh().frame["out_t"].sum() == 100


def test_failed_publication_can_retry_without_source_changes(store, monkeypatch):
    import pytest

    write(store.sources.codex / "sessions/a.jsonl", [meta(), context(), token()])
    original = store.catalog.apply
    with monkeypatch.context() as patch:
        patch.setattr(
            store.catalog, "apply", lambda *args: (_ for _ in ()).throw(ValueError("temporary failure"))
        )
        with pytest.raises(ValueError):
            store.refresh()
    assert store.catalog.apply == original
    assert store.refresh().frame.height == 1


def test_changed_append_boundary_rebuilds_file(store):
    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token(inp=1000)])
    store.refresh()
    write(path, [meta(), context(), token(inp=2000, cumulative=3300), token(cumulative=4400)])
    s = store.refresh()
    assert s.scan["rebuilt"] == 1 and s.frame["in_t"].sum() == 3000


def test_discovery_failure_preserves_published_snapshot(store, monkeypatch):
    import pytest
    from tokmeter.sources import Sources

    path = store.sources.codex / "sessions/a.jsonl"
    write(path, [meta(), context(), token()])
    first = store.refresh()
    monkeypatch.setattr(Sources, "discover", lambda self: ([], ["unreadable directory"]))
    with pytest.raises(OSError):
        store.refresh()
    assert store.snapshot() is first
