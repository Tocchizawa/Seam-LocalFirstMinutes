# v3 evaluation harness implementation notes

- Added `scripts/evaluate_live_notes_v3.py`, a standard-library-only JSON evaluator. It performs no network requests, CLI launches, database access, or provider inference.
- Added synthetic 11-minute and 23-minute fixture cases with the requested checkpoints. They contain abstract topic/claim/question/evidence identifiers and calibration cardinalities only; they do not contain actual meeting content.
- Added normalized synthetic candidate snapshots so both CLI paths can be exercised without a model.
- Added focused unittest coverage for passing cases, topic proliferation, answer-versus-verification state, stable topic IDs, preservation/evidence loss, and rejecting transcript-bearing fixtures.
- The rubric is 100 points: structure 25, topic integration 20, discussion state 15, preservation 10, evidence 30. The passing threshold is 85/100, with structural, state, preservation, and evidence hard gates retained.
- The user's actual 11-minute and 23-minute corpora remain deferred and private. No baseline file was opened, no real provider inference was run, and no provider budget was consumed.

Commands and candidate JSON schema are documented in [docs/live-notes-v3-evaluation.md](docs/live-notes-v3-evaluation.md).
