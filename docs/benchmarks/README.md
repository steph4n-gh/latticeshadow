# Selected historical benchmark reports

These small JSON reports were copied from the original DB checkout. Generated
vectors, SQLite databases, and native indexes are excluded.

| Workload | Search mode | Recall@10 | Median | p95 |
| --- | --- | --- | --- | --- |
| 1,000,000 vectors, 768 dimensions, 50 queries | Streaming exact | 1.0 | 31.0 ms | 61.8 ms |
| 100,000 vectors, 384 dimensions, 10 queries | Streaming exact | 1.0 | 3.7 ms | 7.9 ms |
| 100,000 vectors, 384 dimensions, 10 queries | Native HNSW with exact rerank | 1.0 | 25.0 ms | 36.8 ms |

- [Million-vector report](moonshot_cleanroom_20260701_131840.json)
- [100k-vector comparison](native_hnsw_100k_384_m32ef1536_c3000.json)

These are synthetic retrieval results from the historical source version, not
end-to-end client measurements or comparisons with other databases. Both reports
record `gates.achieved = false`; the million-vector run missed its 50 ms p95 target.
JSON paths identify original local artifacts and are not links to files in GitHub.
