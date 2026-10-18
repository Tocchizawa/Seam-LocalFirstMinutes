#!/usr/bin/env python3
"""Deterministic, local-only evaluator for normalized Live Notes v3 snapshots.

This tool does not invoke a model, provider, network service, database, or CLI
agent. It compares candidate JSON snapshots with synthetic evaluation metadata.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


RUBRIC = {
    "structure": 25,
    "topic_integration": 20,
    "discussion_state": 15,
    "preservation": 10,
    "evidence": 30,
}
TOTAL_POINTS = sum(RUBRIC.values())
VERIFIED = "verified_against_source"


class EvaluationError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"{path} must contain a JSON object")
    return value


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _index(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        item_key = item.get(key)
        if isinstance(item_key, str):
            result[item_key] = item
    return result


def _ratio_points(weight: int, passed: int, total: int) -> tuple[int, str]:
    if total == 0:
        return weight, "no applicable checks"
    points = round(weight * passed / total)
    return points, f"{passed}/{total} checks passed"


def load_case(fixture: dict[str, Any]) -> dict[str, Any]:
    if fixture.get("schema_version") != 1:
        raise EvaluationError("fixture schema_version must be 1")
    if fixture.get("synthetic") is not True:
        raise EvaluationError("fixture must explicitly be synthetic")
    if fixture.get("contains_transcript_text") is not False:
        raise EvaluationError("fixture must declare contains_transcript_text=false")
    checkpoints = fixture.get("checkpoints")
    if not isinstance(checkpoints, list) or not checkpoints:
        raise EvaluationError("fixture must contain checkpoints")
    return fixture


def evaluate_checkpoint(
    case: dict[str, Any], expected: dict[str, Any], actual: dict[str, Any]
) -> dict[str, Any]:
    check_results: dict[str, list[tuple[bool, str]]] = {
        name: [] for name in RUBRIC
    }

    topics = _as_list(actual.get("topics"))
    claims = _as_list(actual.get("claims"))
    topic_keys = [t.get("semantic_key") for t in topics if isinstance(t, dict)]
    topic_ids_by_key: dict[str, set[str]] = {}
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        key, topic_id = topic.get("semantic_key"), topic.get("id")
        if isinstance(key, str) and isinstance(topic_id, str):
            topic_ids_by_key.setdefault(key, set()).add(topic_id)

    expected_topic_keys = set(expected.get("topic_keys", []))
    actual_topic_keys = set(key for key in topic_keys if isinstance(key, str))
    max_topics = int(expected.get("max_topic_count", len(expected_topic_keys)))
    check_results["structure"].extend([
        (actual_topic_keys == expected_topic_keys,
         f"topic keys expected {sorted(expected_topic_keys)}, got {sorted(actual_topic_keys)}"),
        (len(topics) <= max_topics,
         f"topic count {len(topics)} within maximum {max_topics}"),
    ])

    expected_claim_topics = case.get("claim_topics", {})
    claim_by_key = _index([c for c in claims if isinstance(c, dict)], "key")
    placed_claims: dict[str, list[str]] = {}
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_key = topic.get("semantic_key")
        for block_key in _as_list(topic.get("block_keys")):
            if isinstance(block_key, str):
                placed_claims.setdefault(block_key, []).append(str(topic_key))
    placement_checks = []
    for claim_key in expected.get("required_claim_keys", []):
        expected_topic = expected_claim_topics.get(claim_key)
        placement_checks.append(expected_topic in placed_claims.get(claim_key, []))
    check_results["structure"].append((
        all(placement_checks),
        f"required claims placed under expected topics ({sum(placement_checks)}/{len(placement_checks)})",
    ))
    # Duplicate keys in the visible outline are direct evidence of topic proliferation.
    check_results["structure"].append((
        len(topic_keys) == len(set(topic_keys)),
        "no duplicate semantic topic keys",
    ))

    required_topics = set(case.get("revisited_topic_keys", []))
    stable_topic_checks: list[bool] = []
    for key in required_topics:
        ids = topic_ids_by_key.get(key, set())
        stable_topic_checks.append(len(ids) <= 1)
    check_results["topic_integration"].append((
        all(stable_topic_checks),
        f"revisited topics retain one ID within snapshot ({sum(stable_topic_checks)}/{len(stable_topic_checks)})",
    ))
    claim_keys = [claim.get("key") for claim in claims if isinstance(claim, dict)]
    check_results["topic_integration"].append((
        len(claim_keys) == len(set(claim_keys)),
        "no repeated canonical claim keys",
    ))
    snapshot_map = case.get("_snapshots_by_minute", {})
    for key in required_topics:
        ids_seen = set()
        for minute in snapshot_map:
            snap = snapshot_map[minute]
            for topic in _as_list(snap.get("topics")):
                if isinstance(topic, dict) and topic.get("semantic_key") == key and isinstance(topic.get("id"), str):
                    ids_seen.add(topic["id"])
        stable = len(ids_seen) <= 1
        check_results["topic_integration"].append((
            stable, f"revisited topic {key} keeps a stable ID across submitted checkpoints"
        ))

    expected_states = expected.get("question_states", {})
    actual_states = actual.get("question_states", {})
    if not isinstance(actual_states, dict):
        actual_states = {}
    state_checks = [actual_states.get(key) == status for key, status in expected_states.items()]
    check_results["discussion_state"].append((
        all(state_checks),
        f"question states match expected transitions ({sum(state_checks)}/{len(state_checks)})",
    ))
    open_ids = set(_as_list(actual.get("open_question_ids")))
    expected_open = set(expected.get("open_question_ids", []))
    check_results["discussion_state"].append((
        open_ids == expected_open,
        f"open question IDs expected {sorted(expected_open)}, got {sorted(open_ids)}",
    ))
    answered_open = [key for key, status in actual_states.items()
                     if status == "answered_in_discussion" and key in open_ids]
    check_results["discussion_state"].append((
        not answered_open,
        f"answered questions removed from open list ({len(answered_open)} left open)",
    ))
    allowed_verified = set(expected.get("verified_claim_keys", []))
    falsely_verified = [claim.get("key") for claim in claims if isinstance(claim, dict)
                        and claim.get("epistemic_state") == VERIFIED
                        and claim.get("key") not in allowed_verified]
    check_results["discussion_state"].append((
        not falsely_verified,
        f"no discussion-only claim mislabeled source-verified ({len(falsely_verified)} violations)",
    ))

    required_claims = set(expected.get("required_claim_keys", []))
    visible_claims = set(claim_by_key)
    check_results["preservation"].append((
        required_claims <= visible_claims,
        f"required claim keys preserved ({len(required_claims & visible_claims)}/{len(required_claims)})",
    ))
    prior_claims = set(expected.get("preserve_from_prior", []))
    check_results["preservation"].append((
        prior_claims <= visible_claims,
        f"prior checkpoint claims remain represented ({len(prior_claims & visible_claims)}/{len(prior_claims)})",
    ))

    valid_segment_ids = set(case.get("synthetic_segment_ids", []))
    evidence_by_claim = case.get("evidence_by_claim", {})
    evidence_checks: list[bool] = []
    unknown_evidence = []
    missing_required_evidence = []
    for claim_key in expected.get("required_claim_keys", []):
        claim = claim_by_key.get(claim_key)
        if claim is None:
            evidence_checks.append(False)
            continue
        evidence = set(_as_list(claim.get("evidence_segment_ids")))
        unknown_evidence.extend(sorted(evidence - valid_segment_ids))
        required_evidence = set(evidence_by_claim.get(claim_key, []))
        if evidence & required_evidence:
            evidence_checks.append(True)
        else:
            evidence_checks.append(False)
            missing_required_evidence.append(claim_key)
    check_results["evidence"].append((
        all(evidence_checks),
        f"required claims link expected synthetic evidence ({sum(evidence_checks)}/{len(evidence_checks)})",
    ))
    check_results["evidence"].append((
        not unknown_evidence,
        f"evidence IDs all exist in fixture ({len(unknown_evidence)} unknown references)",
    ))
    allowed_claims = set(case.get("allowed_claim_keys", []))
    unexpected_claims = sorted(visible_claims - allowed_claims)
    check_results["evidence"].append((
        not unexpected_claims,
        f"candidate contains no unsupported claim keys ({len(unexpected_claims)} unexpected)",
    ))
    check_results["evidence"].append((
        not any(not isinstance(claim.get("evidence_segment_ids"), list) or not claim.get("evidence_segment_ids")
                for claim in claims if isinstance(claim, dict)),
        "every candidate claim has at least one evidence link",
    ))

    category_results = {}
    for category, entries in check_results.items():
        passed = sum(1 for ok, _ in entries if ok)
        points, description = _ratio_points(RUBRIC[category], passed, len(entries))
        category_results[category] = {
            "points": points,
            "max_points": RUBRIC[category],
            "summary": description,
            "checks": [{"passed": ok, "detail": detail} for ok, detail in entries],
        }
    total = sum(result["points"] for result in category_results.values())
    critical_failures = []
    hard_gate_indexes = {
        "structure": {0, 1, 3},
        "topic_integration": {1},
        "discussion_state": {0, 1, 2, 3},
        "preservation": {0, 1},
        "evidence": {0, 1, 2, 3},
    }
    for category, indexes in hard_gate_indexes.items():
        for index in indexes:
            if index < len(check_results[category]) and not check_results[category][index][0]:
                critical_failures.append(f"{category}: {check_results[category][index][1]}")
    return {
        "minute": expected["minute"],
        "score": total,
        "max_score": TOTAL_POINTS,
        "passed": total >= int(case.get("pass_threshold", 85)) and not critical_failures,
        "critical_failures": critical_failures,
        "categories": category_results,
    }


def evaluate(fixture: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    case = load_case(fixture)
    if candidate.get("case_id") != case.get("case_id"):
        raise EvaluationError(
            f"candidate case_id {candidate.get('case_id')!r} does not match {case.get('case_id')!r}"
        )
    candidate_snapshots = candidate.get("snapshots")
    if not isinstance(candidate_snapshots, list):
        raise EvaluationError("candidate must contain a snapshots array")
    actual_by_minute = {}
    for snapshot in candidate_snapshots:
        if isinstance(snapshot, dict) and isinstance(snapshot.get("minute"), int):
            actual_by_minute[snapshot["minute"]] = snapshot
    expected_minutes = [cp["minute"] for cp in case["checkpoints"]]
    missing = [minute for minute in expected_minutes if minute not in actual_by_minute]
    if missing:
        raise EvaluationError(f"candidate is missing checkpoints: {missing}")
    case["_snapshots_by_minute"] = actual_by_minute
    results = [evaluate_checkpoint(case, cp, actual_by_minute[cp["minute"]])
               for cp in case["checkpoints"]]
    del case["_snapshots_by_minute"]
    average = round(sum(result["score"] for result in results) / len(results), 2)
    return {
        "case_id": case["case_id"],
        "local_only": True,
        "provider_calls": 0,
        "rubric_total": TOTAL_POINTS,
        "pass_threshold": case.get("pass_threshold", 85),
        "checkpoint_results": results,
        "mean_score": average,
        "passed": all(result["passed"] for result in results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path, help="synthetic evaluation case JSON")
    parser.add_argument("--candidate", required=True, type=Path, help="normalized candidate snapshot JSON")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = evaluate(read_json(args.fixture), read_json(args.candidate))
    except EvaluationError as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"{result['case_id']}: {result['mean_score']}/{TOTAL_POINTS} mean; "
              f"threshold {result['pass_threshold']}; {'PASS' if result['passed'] else 'FAIL'}")
        print("Provider calls: 0 (local deterministic evaluation)")
        for checkpoint in result["checkpoint_results"]:
            print(f"  {checkpoint['minute']:>2}m: {checkpoint['score']}/{checkpoint['max_score']} "
                  f"{'PASS' if checkpoint['passed'] else 'FAIL'}")
            for category, value in checkpoint["categories"].items():
                print(f"      {category}: {value['points']}/{value['max_points']} ({value['summary']})")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
