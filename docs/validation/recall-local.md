# Local retrieval scorecard (relevance, not latency)

The coordinator reported this synthetic, local real-model run while integrating
the daily-use alpha search path. It is an **interim relevance result**; the
integrated evaluator change is still awaiting its final commit SHA. Re-run the
frozen evaluator on the review candidate and record that SHA before marking R1
passed. The raw generated report is ignored at
`benchmark_results/alpha-recall-integrated.json`.

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
| Previous encrypted hybrid path | 0.760 | 0.641 |
| Exact cosine baseline | 0.895 | 0.814 |
| In-memory keyword baseline | 0.920 | 0.850 |

All 50 no-answer questions still receive a top-five suggestion. Search scores
are ranking values, not answer probabilities. The fixture is authored synthetic
data with disjoint scenario families; the result does not establish performance
on an individual's real memories. It also does not establish the **R2** Mini
latency target. That requires the separate 10,000-event encrypted-vault run
described in [the validation guide](README.md).
