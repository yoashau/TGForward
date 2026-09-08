"""结果模型：穷举 final snapshot、不可逆状态、源清单与源评论投影。"""

from itertools import product

import pytest

from tgforward.transfers.results import (
    AttemptState,
    CommentResult,
    DeliveryPart,
    DeliverySnapshot,
    ExtractionUnit,
    MessageDeliveryState,
    MessageOutcome,
    MessageResult,
    SideEffectRole,
    SourceResolution,
    derive_outcome,
)


def result(sources=(1, 2, 3)):
    item = MessageResult("message", "task:unit:message")
    item.resolve_source(
        tuple(dict.fromkeys(sources)),
        [DeliveryPart(str(i), source) for i, source in enumerate(sources)],
    )
    return item


def set_final(state, part, final):
    if final == "not_attempted":
        return
    state.begin_attempt(part)
    if final == "confirmed_delivered":
        state.confirm_delivered(part)
    elif final == "confirmed_failed":
        state.reject(part, "explicit rejection")
        state.finalize_failed(part)
    else:
        state.mark_uncertain(part, "lost response")


@pytest.mark.parametrize("resolution", list(SourceResolution))
@pytest.mark.parametrize(
    "states",
    list(
        product(("confirmed_delivered", "confirmed_failed", "uncertain", "not_attempted"), repeat=3)
    ),
)
def test_outcome_exhaustive(resolution, states):
    item = result()
    for i, final in enumerate(states):
        set_final(item.delivery, str(i), final)
    if resolution == SourceResolution.FAILED:
        item.fail_source("source manifest unavailable")
    snapshot = item.delivery.snapshot()
    actual = derive_outcome(resolution, snapshot)
    if "uncertain" in states:
        expected = MessageOutcome.UNCERTAIN
    elif resolution != SourceResolution.COMPLETE:
        expected = MessageOutcome.INCOMPLETE
    elif set(states) == {"confirmed_delivered"}:
        expected = MessageOutcome.SUCCESS
    elif set(states) == {"confirmed_failed"}:
        expected = MessageOutcome.FAILED
    else:
        expected = MessageOutcome.INCOMPLETE
    assert actual == expected
    groups = [
        snapshot.confirmed_delivered,
        snapshot.confirmed_failed,
        snapshot.uncertain,
        snapshot.not_attempted,
    ]
    assert frozenset.union(*groups) == snapshot.all_parts
    assert sum(map(len, groups)) == len(snapshot.all_parts)
    assert (actual == MessageOutcome.FAILED) == (
        resolution == SourceResolution.COMPLETE and set(states) == {"confirmed_failed"}
    )


@pytest.mark.parametrize("resolution", list(SourceResolution))
def test_empty_parts_never_succeed(resolution):
    assert derive_outcome(resolution, MessageDeliveryState().snapshot()) == "incomplete"


@pytest.mark.parametrize("final", ["confirmed_delivered", "confirmed_failed", "uncertain"])
def test_final_state_cannot_be_retried_or_overwritten(final):
    state = result((1,)).delivery
    set_final(state, "0", final)
    before = state.snapshot()
    with pytest.raises(ValueError):
        state.begin_attempt("0")
    for method, name in [
        (state.confirm_delivered, "confirmed_delivered"),
        (state.finalize_failed, "confirmed_failed"),
        (state.mark_uncertain, "uncertain"),
    ]:
        if name == final:
            method("0")  # 同一证据重复提交幂等。
        else:
            with pytest.raises(ValueError):
                method("0")
        assert state.snapshot() == before


@pytest.mark.parametrize("role", [SideEffectRole.DOWNLOAD, SideEffectRole.STAGING_UPLOAD])
def test_nonfinal_operations_never_begin_delivery(role):
    state = result((1,)).delivery
    with pytest.raises(ValueError, match="only final"):
        state.begin_attempt("0", role)
    state.settle()
    assert state.not_attempted == {"0"}
    assert not state.uncertain and not state.confirmed_failed
    assert state.attempt_counts["0"] == 0


def test_retry_rejection_is_not_final_failure():
    state = result((1,)).delivery
    state.begin_attempt("0")
    state.retryable_rejection("0", "FloodWait")
    assert state.attempts["0"] == AttemptState.RETRY_WAIT
    assert state.not_attempted == {"0"} and not state.confirmed_failed
    state.begin_attempt("0")
    state.confirm_delivered("0")
    assert state.confirmed_delivered == {"0"}
    assert state.attempt_counts["0"] == 2


def test_retry_wait_cancel_settles_only_attempted_parts():
    state = result().delivery
    state.begin_attempt("0")
    state.retryable_rejection("0", "entity rejection")
    state.begin_attempt("1")
    state.settle()
    assert state.confirmed_failed == {"0"}
    assert state.uncertain == {"1"}
    assert state.not_attempted == {"2"}


def test_ready_cannot_be_fabricated_as_failed_or_uncertain_or_delivered():
    state = result((1,)).delivery
    for method in (state.finalize_failed, state.mark_uncertain, state.confirm_delivered):
        with pytest.raises(ValueError):
            method("0")
    assert state.not_attempted == {"0"}


def test_public_collections_are_read_only():
    state = result().delivery
    with pytest.raises(AttributeError):
        state.confirmed_delivered.add("0")
    with pytest.raises(TypeError):
        state.parts["4"] = DeliveryPart("4", 4)
    with pytest.raises(TypeError):
        state.attempts["0"] = AttemptState.SUCCEEDED
    with pytest.raises(AttributeError):
        state.confirmed_failed = {"0"}


def test_source_manifest_is_sealed_before_attempt_and_cannot_change():
    item = MessageResult("id", "key")
    with pytest.raises(ValueError):
        item.delivery.begin_attempt("0")
    item.resolve_source([1], [DeliveryPart("0", 1)])
    item.resolve_source([1], [DeliveryPart("0", 1)])
    with pytest.raises(ValueError):
        item.resolve_source([1, 2], [DeliveryPart("0", 1), DeliveryPart("1", 2)])
    with pytest.raises(ValueError):
        item.delivery.seal([DeliveryPart("0", 2)])
    assert item.source_message_ids == (1,)


@pytest.mark.parametrize(
    "sources, parts",
    [
        ([1, 2], [DeliveryPart("0", 1)]),
        ([1], [DeliveryPart("0", 2)]),
        ([1, 1], [DeliveryPart("0", 1)]),
        ([1], [DeliveryPart("0", 1), DeliveryPart("0", 1)]),
    ],
)
def test_invalid_source_manifest_never_seals(sources, parts):
    item = MessageResult("id", "key")
    with pytest.raises(ValueError):
        item.resolve_source(sources, parts)
    assert item.source_resolution == "pending"
    assert not item.delivery.sealed


def test_source_failure_preserves_delivered_and_is_not_rpc_unknown():
    item = result()
    set_final(item.delivery, "0", "confirmed_delivered")
    item.fail_source("cannot determine remaining members")
    assert item.outcome == "incomplete"
    assert item.delivery.confirmed_delivered == {"0"}
    assert not item.delivery.uncertain and not item.delivery.confirmed_failed
    assert item.source_error == "cannot determine remaining members"


def test_hydration_is_atomic_and_cannot_overwrite_final_states():
    state = result().delivery
    with pytest.raises(ValueError):
        state.hydrate_delivered(["0", "unknown"])
    assert not state.confirmed_delivered
    state.hydrate_delivered(["0"])
    state.hydrate_delivered(["0"])
    assert state.confirmed_delivered == {"0"} and state.attempt_counts["0"] == 0
    set_final(state, "1", "uncertain")
    with pytest.raises(ValueError):
        state.hydrate_delivered(["2", "1"])
    assert state.confirmed_delivered == {"0"}


def test_one_link_many_messages_and_request_progress_are_independent():
    unit = ExtractionUnit("unit:0", "task", request_total=10)
    first = unit.message((100, "album"))
    assert unit.message((100, "album")) is first
    assert unit.message((100, "album")).commit_key == first.commit_key
    unit.message((100, 200))
    assert len(unit.message_results) == 2 and unit.request_current == 0
    for _ in range(10):
        unit.advance()
    assert len(unit.message_results) == 2 and unit.request_current == 10
    with pytest.raises(ValueError):
        unit.advance()
    other = ExtractionUnit("unit:1", "task")
    assert other.message((100, "album")).commit_key != first.commit_key


def test_long_comment_chunks_count_as_one_partial_source():
    item = result((1, 1, 1))
    for part, final in zip(
        ("0", "1", "2"),
        ("confirmed_delivered", "confirmed_delivered", "confirmed_failed"),
        strict=True,
    ):
        set_final(item.delivery, part, final)
    comments = CommentResult()
    comments.observe(item)
    comments.observe(item)
    assert comments.partial == 1
    assert (comments.success, comments.failed, comments.uncertain, comments.skipped) == (0, 0, 0, 0)


def test_album_comments_count_each_source_not_the_whole_result():
    item = result((1, 2, 3, 4, 5))
    for i in range(5):
        set_final(item.delivery, str(i), "confirmed_delivered" if i < 3 else "confirmed_failed")
    assert item.outcome == "incomplete"
    comments = CommentResult()
    comments.observe(item)
    assert comments.success == 3 and comments.failed == 2 and comments.partial == 0


def test_realtime_projection_replaces_counts_without_double_counting():
    item = result((1, 1, 2))
    comments = CommentResult()
    comments.observe(item)
    assert comments.skipped == 2
    set_final(item.delivery, "0", "confirmed_delivered")
    comments.observe(item)
    assert comments.partial == 1 and comments.skipped == 1
    set_final(item.delivery, "1", "confirmed_delivered")
    set_final(item.delivery, "2", "uncertain")
    comments.observe(item)
    assert (comments.success, comments.partial, comments.uncertain, comments.skipped) == (
        1,
        0,
        1,
        0,
    )
    comments.stopped = True
    assert comments.success == 1


def test_comment_not_run_empty_and_error_are_distinct():
    not_run = None
    empty = CommentResult(empty=True)
    error = CommentResult(last_error="discussion read failed")
    assert not_run is None and empty.empty and not error.empty
    item = MessageResult("id", "key")
    item.fail_source("album read failed")
    empty.observe(item)
    assert not empty.empty and empty.last_error == "album read failed"
    assert empty.success + empty.failed + empty.skipped == 0


def test_snapshot_rejects_overlapping_or_unknown_final_states():
    with pytest.raises(ValueError):
        DeliverySnapshot(frozenset({"a"}), frozenset({"a"}), frozenset({"a"}), frozenset())
    with pytest.raises(ValueError):
        DeliverySnapshot(frozenset(), frozenset({"a"}), frozenset(), frozenset())


def test_source_failure_keeps_confirmed_source_comment_counts():
    item = result()
    set_final(item.delivery, "0", "confirmed_delivered")
    item.fail_source("source read failed after an earlier confirmed delivery")
    comments = CommentResult()
    comments.observe(item)
    assert comments.success == 1 and comments.skipped == 2
    assert comments.last_error and item.outcome == "incomplete"


def test_snapshot_copies_mutable_input_sets():
    parts = {"a"}
    snapshot = DeliverySnapshot(parts, set(), set(), set())
    parts.add("b")
    assert snapshot.all_parts == frozenset({"a"})


def test_task_unit_identity_includes_input_position_and_reentry_is_stable():
    from tgforward.runtime.tasks import Task

    task = Task(1, "batch", 20)
    first = task.extraction_unit((0, "same-link"), 10)
    second = task.extraction_unit((1, "same-link"), 10)
    assert first is task.extraction_unit((0, "same-link"), 10)
    assert first.id != second.id and len(task.units) == 2
    first.message((100, 1))
    first.message((100, 2))
    assert task.current == 0 and first.request_current == 0
    with pytest.raises(ValueError):
        task.extraction_unit((0, "same-link"), 11)
