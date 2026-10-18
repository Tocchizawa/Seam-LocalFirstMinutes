# Live Notes v3 local evaluation harness

This harness checks the shape and state of normalized Live Notes snapshots against two synthetic checkpoint cases. It makes no provider calls and does not read a database, audio, transcript, or the private evaluation corpus. The fixtures contain synthetic IDs and metadata only; they contain no meeting passages or expected prose.

The 11-minute case checkpoints at 2, 4, 6, 9, and 11 minutes. It exercises a compact three-topic structure, gradual claim preservation, question transitions from open to answered in discussion, and one item that still needs outside verification. The 23-minute case checkpoints at 5, 12, 18, and 23 minutes. It exercises repeated returns to one topic, cumulative preservation, answers that should not be mislabeled as source-verified, and a final state with no open questions.

The reference cardinalities in the fixture files are count-only calibration metadata supplied for synthetic coverage: 11 minutes is shaped around 3 topics, 13 blocks, and 3 question items; 23 minutes around 1 topic, 31 blocks, and 0 question items in the reference count. These are calibration metadata only; the candidate question-state ledger and visible unresolved list are evaluated independently. These counts are not transcript content and are not exact-output assertions. The checks use synthetic concept keys, expected topic bounds, state transitions, and evidence-ID links.

## Rubric

Each checkpoint receives up to 100 points. A checkpoint must score at least 85 and pass all hard gates:

| Category | Points | Checks |
| --- | ---: | --- |
| Structure | 25 | Expected themes, topic-count ceiling, claim placement, duplicate theme keys |
| Topic integration | 20 | No duplicate claim keys and stable IDs when a theme reappears |
| Discussion state | 15 | Expected question transitions, correct open list, no answer left open, no unsupported source verification |
| Preservation | 10 | Required current and earlier claims remain represented |
| Evidence | 30 | Claim links point to expected fixture evidence, no unknown source IDs or unsupported claim keys |
| **Total** | **100** | |

The default threshold is 85/100, following the v3 plan. Structural duplication, incorrect question states, unsupported verification, lost required claims, and invalid or missing evidence are hard-gate failures regardless of the numeric score. The mean is reported for comparison; every checkpoint must pass.

This is a deterministic structural and evidence-link evaluator. It does not judge Japanese prose quality, infer semantic equivalence, or prove that a provider produced a faithful note. Human review and a separately authorized real-corpus evaluation are still required for those questions.

## Candidate snapshot format

Create a local JSON file with the case ID and one normalized snapshot per required minute. The included `candidate_11m.json` and `candidate_23m.json` files are synthetic passing examples for exercising the evaluator, not model outputs.

```json
{
  "schema_version": 1,
  "case_id": "synthetic_11m",
  "snapshots": [
    {
      "minute": 2,
      "topics": [
        {"id": "topic-a", "semantic_key": "topic_alpha", "title": "Synthetic theme", "block_keys": ["fact_01"]}
      ],
      "claims": [
        {"key": "fact_01", "text": "Synthetic claim", "epistemic_state": "supported_in_discussion", "evidence_segment_ids": ["s11_01"]}
      ],
      "question_states": {"q_alpha": "open"},
      "open_question_ids": ["q_alpha"]
    }
  ]
}
```

Question states include `open`, `answered_in_discussion`, and `needs_verification`. A candidate must use `verified_against_source` only for a claim explicitly allowed by the case and backed by external source evidence. Discussion alone supports `answered_in_discussion`, not external verification. `question_states` is the evaluator's state ledger; `open_question_ids` represents the visible unresolved list.

## Commands

Run the focused evaluator tests:

```bash
python3 -m unittest discover -s tests/fixtures/live_notes_v3 -p 'test_*.py'
```

Evaluate the synthetic 11-minute case:

```bash
python3 scripts/evaluate_live_notes_v3.py \
  --fixture tests/fixtures/live_notes_v3/synthetic_11m.json \
  --candidate tests/fixtures/live_notes_v3/candidate_11m.json
```

Evaluate the synthetic 23-minute case:

```bash
python3 scripts/evaluate_live_notes_v3.py \
  --fixture tests/fixtures/live_notes_v3/synthetic_23m.json \
  --candidate tests/fixtures/live_notes_v3/candidate_23m.json
```

Use `--format json` to capture all per-check details. Exit code is `0` on pass, `1` when an evaluation fails, and `2` for invalid input. The evaluator only reads the two explicitly supplied JSON files and prints the report. It has no option that launches Codex CLI, Claude CLI, or another provider.

## Real corpus and provider evaluation

The actual 11-minute and 23-minute corpus remains private and outside Git. Do not add its transcript, audio, candidate notes, or derived text to these fixtures. The user separately authorized a bounded real-corpus evaluation in this work session. Nine initial Codex CLI invocations and nine additional invocations with the explicitly selected `gpt-6-luna` model were attempted; all 18 ended with `CLI_LOCAL_RUNTIME` before a structured note could be produced. No provider-generated checkpoint or quality score was obtained. Claude Code CLI was unavailable, so it was not invoked. The synthetic 100-point examples above therefore cannot be interpreted as a pass of the real-model 85-point gate. Further real-corpus sends require fresh authorization; the local evaluator itself never launches a provider.
