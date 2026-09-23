# Recall evaluation corpus

`scenarios.py` contains synthetic, authored events and five questions per
scenario. Project families are split before tuning: 100 answerable and 25
missing-answer development questions; 200 answerable and 50 missing-answer
held-out questions. Extra related records, copied events, and unrelated
distractors are searched with every question. No personal history is used.

From the repository root, run:

```sh
python3 packages/cli/scripts/evaluate_recall.py
```

The script requires the CLI/DB dependencies and the pinned local embedding
model. Its JSON report is written under ignored `benchmark_results/` and
contains the fixture hash, model revision, hit@5, MRR@10, missing-answer output,
and individual failed questions. It compares production scoped recall, the
legacy encrypted-vault hybrid path, exact cosine with the same model, a simple
in-memory keyword baseline, and the in-memory fusion ranker.

The held-out questions are public and synthetic, so their score is a regression
signal rather than a guarantee about private histories. Keep whole families in
their assigned split. Review any label correction separately from ranking
changes. Scores order results; they are not probabilities or a promise that an
answer exists. Missing-answer behavior is reported separately because the
development set does not support a reliable abstention cutoff.

Batch vector encoding in this script makes the exact baseline affordable. Its
ranking-loop timing does not measure the app's cold model load, vault scan,
decryption, cache build, or full user-visible query latency. Those costs need a
separate 10,000-event run on the reference Mini.
