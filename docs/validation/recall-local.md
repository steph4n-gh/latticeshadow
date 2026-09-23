# Local retrieval scorecard (relevance, not latency)

The coordinator ran the integrated production search and frozen evaluator at
commit `44482e887f01b2fc4e974d46477582b43019e438` on a local Apple Silicon
Mac:

```sh
.venv/bin/python packages/cli/scripts/evaluate_recall.py --output benchmark_results/alpha-recall-final.json
```

The raw report is ignored by Git. This is a synthetic relevance result; it does
not measure search latency.

| Measurement | Result |
| --- | ---: |
| Held-out answerable questions | 200 |
| Held-out questions with no recorded answer | 50 |
| Synthetic records | 124, including 9 duplicates and 40 decoys |
| Fixture SHA-256 | `05c0ad46f44318f4b1e8bee5eede7a4e230d42bce6691df0cd056e6abddb8fa0` |
| Pinned model | `static-retrieval-mrl-en-v1`, revision `f60985c706f192d45d218078e49e5a8b6f15283a`, 128 dimensions |

| Ranking path | Hit@5 | MRR@10 |
| --- | ---: | ---: |
| Integrated production `search_events` | 0.960 | 0.888 |
| Previous encrypted hybrid path | 0.765 | 0.669 |
| Exact cosine baseline | 0.895 | 0.814 |
| In-memory keyword baseline | 0.920 | 0.850 |

All 50 no-answer questions still receive a top-five suggestion. Search scores
are ranking values, not answer probabilities. The fixture is authored synthetic
data with disjoint scenario families; the result does not establish performance
on an individual's real memories. It also does not establish the **R2** Mini
latency target. That requires the separate 10,000-event encrypted-vault run
described in [the validation guide](README.md).
