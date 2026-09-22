"""The policy-sentence -> promise map: alignment, arm validity, and the keys.

The map is only worth anything if the annotated policy still lines up with the
canonical segments the labelers saw, so most of this file re-derives that
alignment rather than trusting the tracked JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.labeling import aggregate
from eval.labeling import rule_map
from eval.tau import promises

REPO_ROOT = Path(__file__).resolve().parents[3]
DOMAINS = ("retail", "airline")
EXPECTED_ARMS = 19


# --- alignment ---------------------------------------------------------------


@pytest.mark.parametrize("domain", DOMAINS)
def test_annotated_policy_aligns_with_canonical_segments(domain):
    segments = rule_map.canonical_segments(domain)
    records = rule_map.annotate(domain)

    assert len(segments) == rule_map.EXPECTED_SEGMENTS[domain]
    assert len(records) == len(segments)
    for segment, record in zip(segments, records):
        assert record["sentence"] == segment["sentence"], segment["id"]
        assert record["text"] == segment["text"], segment["id"]


@pytest.mark.parametrize("domain", DOMAINS)
def test_annotation_block_coverage(domain):
    records = rule_map.annotate(domain)
    covered = [record for record in records if record["block_line"] is not None]
    assert len(covered) == rule_map.EXPECTED_ANNOTATED[domain]

    payload = rule_map.load_rule_map()
    segments = payload["domains"][domain]["segments"]
    assert sum(1 for e in segments.values() if e["block_line"] is not None) == len(covered)

    # A paragraph with no block leaves its sentences untagged unless an
    # explicit "attachment" entry rescues one -- only airline-p41-s114 does.
    blockless = {sid for sid, e in segments.items() if e["block_line"] is None}
    untagged = {sid for sid, e in segments.items() if e["bucket"] == "untagged"}
    rescued = {
        e["segment"]
        for e in payload["reassignments"]
        if e["kind"] == "attachment" and e["domain"] == domain
    }
    assert len(blockless) == len(records) - len(covered)
    assert untagged == blockless - rescued


def test_last_segment_ids_are_the_canonical_ones():
    assert rule_map.canonical_segments("retail")[-1]["id"] == "retail-p48-s81"
    assert rule_map.canonical_segments("airline")[-1]["id"] == "airline-p43-s116"


def test_paragraph_counter_is_taken_from_canonical_not_re_segmented():
    """Airline pNN drifts, so the map must not renumber paragraphs itself."""

    entry = rule_map._entry("airline", "airline-p20-s51")
    assert entry["paragraph"] == 20 and entry["sentence"] == 51


# --- arms --------------------------------------------------------------------


def test_declared_arms_match_promises_module():
    rule_map.verify_arms()
    assert len(rule_map.ARMS) == EXPECTED_ARMS

    literals = set()
    for name in dir(promises):
        value = getattr(promises, name)
        if isinstance(value, dict) and name == "COUNT_CAPS":
            literals |= {cap["arm"] for caps in value.values() for cap in caps}
    assert literals <= set(rule_map.ARMS)


def test_every_arm_reaches_at_least_one_segment():
    arms = rule_map.load_rule_map()["arms"]
    assert set(arms) == set(rule_map.ARMS)
    assert len(arms) == EXPECTED_ARMS
    for arm, entry in arms.items():
        assert entry["segments"], f"{arm} maps to no segment"
        assert entry["domain"] == rule_map.ARMS[arm][0]
        assert entry["predicate"] == rule_map.ARMS[arm][1]


def test_every_arm_named_in_the_map_exists_in_promises():
    source = (REPO_ROOT / "eval" / "tau" / "promises.py").read_text(encoding="utf-8")
    payload = rule_map.load_rule_map()
    named = set(payload["arms"])
    for domain in DOMAINS:
        for entry in payload["domains"][domain]["segments"].values():
            named |= set(entry["arms"])
    for arm in named:
        assert f'"arm": "{arm}"' in source, f"{arm} is not declared in promises.py"


def test_arm_segments_agree_with_the_segment_table():
    payload = rule_map.load_rule_map()
    for arm, entry in payload["arms"].items():
        segments = payload["domains"][entry["domain"]]["segments"]
        for segment_id in entry["segments"]:
            assert arm in segments[segment_id]["arms"]
        listed = [sid for sid, e in segments.items() if arm in e["arms"]]
        assert listed == entry["segments"]


def test_predicates_are_the_three_families_in_use():
    families = {entry["predicate"] for entry in rule_map.load_rule_map()["arms"].values()}
    assert families == {"AAP-2/scope", "AAP-3/mandate", "AAP-5/count"}


# --- buckets and the spot expectations ---------------------------------------


def test_every_segment_has_a_known_bucket():
    payload = rule_map.load_rule_map()
    for domain in DOMAINS:
        for segment_id, entry in payload["domains"][domain]["segments"].items():
            assert entry["bucket"] in rule_map.BUCKETS, segment_id
            assert bool(entry["arms"]) == (entry["bucket"] == "promise"), segment_id
            if entry["secondary"] is not None:
                assert entry["partial"] is True, segment_id


@pytest.mark.parametrize(
    "domain,segment_id,bucket,arms",
    [
        ("retail", "retail-p03-s06", "promise", ["auth_first"]),
        ("retail", "retail-p07-s11", "judge", []),
        ("retail", "retail-p37-s64", "aap1_not_run", []),
        ("retail", "retail-p42-s71", "untagged", []),
        ("retail", "retail-p37-s63", "aap1_not_run", []),
        ("retail", "retail-p45-s75", "aap1_not_run", []),
        ("retail", "retail-p28-s50", "prose", []),
        ("retail", "retail-p28-s49", "promise", ["once_per_order"]),
        ("airline", "airline-p33-s101", "promise", ["cancel_flown"]),
    ],
)
def test_spot_expectations(domain, segment_id, bucket, arms):
    assert rule_map.bucket_for(domain, segment_id) == bucket
    assert rule_map.arms_for(domain, segment_id) == arms
    assert rule_map.tag_for(domain, segment_id)[0] == bucket


@pytest.mark.parametrize(
    "domain,segment_id,arm,secondary",
    [
        ("retail", "retail-p30-s52", "cancel_reason_enum", "aap1_not_run"),
        ("retail", "retail-p46-s76", "new_item_differs", "graded"),
        ("airline", "airline-p30-s96", "payment_in_profile", "no_fit"),
    ],
)
def test_partials_keep_the_arm_and_the_other_tag(domain, segment_id, arm, secondary):
    entry = rule_map._entry(domain, segment_id)
    assert entry["bucket"] == "promise"
    assert entry["arms"] == [arm]
    assert entry["partial"] is True
    assert entry["secondary"] == secondary
    assert rule_map.tag_for(domain, segment_id) == ("promise", secondary)


def test_cancel_eligibility_sits_under_cancel_flight():
    segments = rule_map.segments_for("cancel_eligibility")
    assert segments
    assert {rule_map._entry("airline", s)["heading"] for s in segments} == {"Cancel flight"}


def test_product_type_sentences_do_not_claim_new_item_differs():
    """`new_item_differs` only compares item id to new item id."""

    for segment_id in ("retail-p38-s66", "retail-p46-s77"):
        assert rule_map.arms_for("retail", segment_id) == []
        assert rule_map.bucket_for("retail", segment_id) == "graded"
    assert rule_map.segments_for("new_item_differs") == ["retail-p38-s65", "retail-p46-s76"]


def test_unknown_ids_raise():
    with pytest.raises(KeyError):
        rule_map.bucket_for("retail", "retail-p99-s99")
    with pytest.raises(KeyError):
        rule_map.bucket_for("hotel", "retail-p03-s06")
    with pytest.raises(KeyError):
        rule_map.segments_for("no_such_arm")


# --- the reassignment table --------------------------------------------------


REVIEWED = {
    "retail-p37-s63",
    "retail-p37-s64",
    "retail-p45-s75",
    "retail-p28-s50",
    "retail-p30-s52",
    "airline-p30-s96",
    "retail-p46-s76",
    "retail-p42-s71",
    "retail-p09-s14",
    "airline-p06-s08",
}


def test_reassignments_are_well_formed_and_applied():
    payload = rule_map.load_rule_map()
    seen = set()
    for entry in payload["reassignments"]:
        segment_id = entry["segment"]
        assert segment_id not in seen
        seen.add(segment_id)
        assert entry["source"] in ("reviewed", "proposed")
        assert entry["bucket"] in rule_map.BUCKETS
        assert entry["domain"] == segment_id.split("-")[0]
        applied = payload["domains"][entry["domain"]]["segments"][segment_id]
        assert applied["bucket"] == entry["bucket"]
        assert applied["arms"] == entry["arms"]
        assert applied["secondary"] == entry["secondary"]
        assert applied["partial"] == entry["partial"]
        assert entry["kind"] in applied["note"] and entry["source"] in applied["note"]
        if entry["kind"] != "orphan":
            assert entry["quote"], segment_id


def test_the_reviewed_corrections_are_all_present():
    payload = rule_map.load_rule_map()
    reviewed = {e["segment"] for e in payload["reassignments"] if e["source"] == "reviewed"}
    assert reviewed == REVIEWED


def _mechanical_arms(domain):
    """Segment id -> the arms block-level inheritance alone would give it."""

    segments = rule_map.canonical_segments(domain)
    records = rule_map.annotate(domain)
    return {
        segment["id"]: rule_map.arms_of(record["block_text"]) if record["block_text"] else []
        for segment, record in zip(segments, records)
    }


def test_no_arm_depends_only_on_an_added_reassignment():
    """An arm the table alone puts on the map would make that call decisive."""

    payload = rule_map.load_rule_map()
    mechanical = {d: _mechanical_arms(d) for d in DOMAINS}
    for arm, entry in payload["arms"].items():
        inherited = [s for s in entry["segments"] if arm in mechanical[entry["domain"]][s]]
        assert inherited, f"{arm} reaches the map only through REASSIGNMENTS"


def test_rule_map_is_not_part_of_the_tau_evaluator_runtime():
    """The evaluator must not import the map (it pulls in eval.labeling)."""

    init = (REPO_ROOT / "eval" / "tau" / "__init__.py").read_text(encoding="utf-8")
    assert "rule_map" not in init
    for name in ("adapter", "contracts", "corpus", "effects", "evaluate", "monitor",
                 "promises", "replay", "trace_adapter"):
        source = (REPO_ROOT / "eval" / "tau" / f"{name}.py").read_text(encoding="utf-8")
        assert "rule_map" not in source, name


def test_deny_out_of_policy_is_a_promise_with_no_arm():
    assert rule_map.bucket_for("retail", "retail-p09-s14") == "promise_no_arm"
    assert rule_map.bucket_for("airline", "airline-p06-s08") == "promise_no_arm"
    assert rule_map.arms_for("retail", "retail-p09-s14") == []


def test_serial_call_rule_is_measured_not_scored():
    for domain, ids in (("retail", ("retail-p08-s12", "retail-p08-s13")),
                        ("airline", ("airline-p05-s06", "airline-p05-s07"))):
        for segment_id in ids:
            assert rule_map.bucket_for(domain, segment_id) == "measured_not_scored"


# --- rule keys ---------------------------------------------------------------


def test_rule_key_matches_aggregate_on_every_calibration_segment():
    tasks = sorted((rule_map.TAU_DATA / "tasks").glob("run-*.json"))
    assert tasks
    checked = 0
    for path in tasks:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        domain = manifest["domain"]
        expected = aggregate.rule_keys_for(
            "tau", path.stem, rule_map.TAU_DATA, rule_map.CLAWS_TASKS
        )
        assert expected
        for segment_id, key in expected.items():
            assert rule_map.rule_key_for(domain, segment_id) == key, segment_id
            checked += 1
    assert checked == sum(
        rule_map.EXPECTED_SEGMENTS[json.loads(p.read_text(encoding="utf-8"))["domain"]]
        for p in tasks
    )


def test_preamble_keys_are_per_paragraph():
    assert rule_map.rule_key_for("retail", "retail-p03-s06") == "Retail agent policy · p03"
    assert rule_map.rule_key_for("retail", "retail-p07-s11") == "Retail agent policy · p07"
    assert rule_map.rule_key_for("retail", "retail-p29-s51") == "Cancel pending order"
    assert rule_map.rule_key_for("airline", "airline-p01-s01") == "Airline Agent Policy · p01"


# --- the tracked files -------------------------------------------------------


def test_check_passes_against_the_tracked_files(capsys):
    # The v3 evaluator preserves the label-time map and its original source.
    # A historical map must be checked against that version, not new promises.
    baseline = rule_map.REPO_ROOT / "eval/reference_v2/baseline_source"
    args = ["--check"]
    if baseline.exists():
        args += ["--claws-source-root", str(baseline)]
    assert rule_map.main(args) == 0
    assert "up to date" in capsys.readouterr().out


def test_build_is_deterministic():
    assert rule_map.serialize(rule_map.build()) == rule_map.serialize(rule_map.build())
    tracked = rule_map.MAP_PATH.read_text(encoding="utf-8")
    assert rule_map.serialize(rule_map.build()) == tracked


def test_sources_hash_every_input():
    sources = rule_map.load_rule_map()["sources"]
    assert set(sources) == {
        "canonical-segments/retail",
        "canonical-segments/airline",
        "eval/tau/retail_policy.md",
        "eval/tau/airline_policy.md",
        "eval/tau/promises.py",
        "eval/labeling/segment_policy.py",
    }
    assert all(len(digest) == 64 for digest in sources.values())


# --- ClawsBench --------------------------------------------------------------


def test_clawsbench_map_has_ten_rules_and_four_arms():
    payload = json.loads(rule_map.CLAWS_MAP_PATH.read_text(encoding="utf-8"))
    rules = payload["rules"]
    assert list(rules) == ["S1", "S2", "S3", "S4", "S5", "E6", "E7", "E8", "E9", "E10"]

    registered = {rule: entry["arm"] for rule, entry in rules.items() if entry["arm"]}
    assert registered == {
        "S3": "verified_destructive_target",
        "S4": "read_before_external_permission_grant",
        "E8": "identical_call_cap",
        "E10": "read_before_mutation",
    }
    assert len(set(registered.values())) == 4
    for rule, entry in rules.items():
        assert entry["title"]
        assert entry["note"]
        if rule in registered:
            assert entry["bucket"] == "promise"
            assert entry["predicate"] == ("AAP-5/count" if rule == "E8" else "AAP-2/scope")
        else:
            assert entry["bucket"] == "not_registered"
            assert entry["predicate"] is None


def test_clawsbench_unregistered_rules_carry_their_reason():
    rules = json.loads(rule_map.CLAWS_MAP_PATH.read_text(encoding="utf-8"))["rules"]
    for rule in ("S1", "S2", "S5"):
        assert rules[rule]["note"].startswith("Not registered.")
    for rule in ("E6", "E7", "E9"):
        assert rules[rule]["note"] == rule_map.NO_BLOCK
    assert "Operational arm" in rules["E8"]["note"]


def test_clawsbench_arm_names_exist_in_its_own_modules():
    promises_src = (rule_map.CLAWS_DIR / "promises.py").read_text(encoding="utf-8")
    run_src = (rule_map.CLAWS_DIR / "run.py").read_text(encoding="utf-8")
    rules = json.loads(rule_map.CLAWS_MAP_PATH.read_text(encoding="utf-8"))["rules"]
    for entry in rules.values():
        if entry["arm"]:
            assert f'"{entry["arm"]}"' in promises_src
            assert f'"{entry["arm"]}"' in run_src


def test_clawsbench_titles_come_from_the_shared_agents_md():
    manifests = sorted(rule_map.CLAWS_TASKS.glob("*.json"))
    assert len(manifests) == 60
    docs = {
        json.loads(path.read_text(encoding="utf-8"))["canonical"]["instructions"]["bootstrap"]["AGENTS.md"]
        for path in manifests
    }
    assert len(docs) == 1
    rules = json.loads(rule_map.CLAWS_MAP_PATH.read_text(encoding="utf-8"))["rules"]
    assert rules["S3"]["title"] == "Verify Before Destructive Actions"
    assert rules["E10"]["title"] == "Scope Mutations Precisely"
