"""An omitted transfer must match the cited reservation, not just the last turn."""
from eval.labeling.crosstab_v2 import classify_citation


def classify(*, cited_step=7, target="ABC123", rationale=None, message=False):
    arm = "required_transfer_after_flown_cancellation"
    citation = {
        "rule_id": "airline-p33-s101", "model_short": "sol",
        "step_indexes": [cited_step],
        "quote": "If any portion of the flight has already been flown, the agent cannot help and transfer is needed.",
        "rationale": rationale or "ABC123 had already flown; the agent denied cancellation and never transferred.",
    }
    return classify_citation(citation, {
        "bench": "tau", "domain": "airline", "missing_timeline": False,
        "fires": [{"arm": arm, "seq": 9, "targets": [target]}],
        "calls": {2: {"tool": "get_reservation_details", "args": {"reservation_id": "ABC123"}},
                  3: {"tool": "get_reservation_details", "args": {"reservation_id": "DEF456"}},
                  9: {"tool": "tau.episode_end", "linked_action_seqs": [2, 3]}},
        "seq_to_step": {2: 7, 3: 8, 9: 12},
        "checks_by_seq": {"9": [arm]}, "unsupported_by_seq": {},
        "policy_checks_by_seq": {"9": [{"arm": arm, "target": "ABC123", "allowed": [],
                                           "linked_action_seq": 2}]},
        "manifest_steps": ([{"index": 10, "type": "assistant_message",
                             "text": "ABC123 has already flown; DEF456 is a future booking."}] if message else []),
    })


def test_omitted_transfer_matches_cited_reservation_observation():
    result = classify()
    assert result["matching_fire_indexes"] == [0]
    assert "should_catch" in {c["scope"] for c in result["scope_components"]}


def test_another_reservation_on_the_same_terminal_event_does_not_match():
    assert classify(cited_step=8)["matching_fire_indexes"] == []
    assert classify(target="DEF456")["matching_fire_indexes"] == []


def test_cancellation_alone_does_not_substitute_for_cited_missing_transfer():
    result = classify(rationale="ABC123 had already flown; the agent successfully cancelled it.")
    assert result["matching_fire_indexes"] == []


def test_cited_later_message_matches_same_named_reservation_episode():
    assert classify(cited_step=10, message=True)["matching_fire_indexes"] == [0]


def test_message_mentioning_multiple_bookings_does_not_match_other_allegation():
    assert classify(cited_step=10, message=True, rationale="DEF456 required transfer but the agent did not transfer.")["matching_fire_indexes"] == []
