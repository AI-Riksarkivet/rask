# 0062. Bounding a read changes what every caller of the unbounded contract MEANS (2026-09-07)

`/runs` returned the estate's whole run board — 5,122 rows / 2.65 MB — to an endpoint its own
docstring says is polled every two seconds. `cypher.py` had predicted the growth with a date, calling
the unbounded shape "currently fine — `/runs` measured 272 rows on the live estate 2026-08-23" while
warning that this "is a property of the data, not of the code". Fifteen days later it was 5,122. So
the board was bounded to its newest page, verified live (26x smaller, the limit finally a limit), and
that verification was complete for the endpoint and worthless for the estate.

**Two callers depended on the board being COMPLETE, and both went wrong rather than slow.**

* The A8 provenance check asked "is this ingest run in the graph?" by downloading the board and
  scanning it linearly. Its docstring named the reason it was tolerable — "the endpoint takes no
  run-id filter and no page". A run older than the newest page is now absent from the response while
  present in the graph, so the scan concluded ABSENT and A8 would report a provenance defect that
  does not exist: silently, and only for older runs.
* The cascade lag reader asked "what has this lane consumed?" the same way, filtering the board for
  runs whose outputs named the destination. A lane whose consuming runs have aged off the page
  reports fewer ranges than it has, and `lag_for_edge` publishes a confident number from a short
  answer.

**THE RULE. A bound is not a performance change; it is a CONTRACT change.** "Every row" and "the
newest N rows" are different answers to different questions, and code written against the first does
not fail against the second — it silently answers a smaller question. Before bounding a read, find
every caller and ask what each one does with completeness. A caller that scans for a specific row was
relying on it.

**AND THE FIX IS USUALLY NOT PAGINATION AT THE CALLER.** Both callers were asking a POINT question of
a LIST endpoint, which is why the bound hurt them: "is run X there?" became `GET /runs/{id}`
(`MATCH (r:Run {run_id:$rid})`), and "what did the runs writing dataset D consume?" became
`GET /datasets/{name}/producers` (`MATCH (r:Run)-[:WROTE]->(d:Dataset {name:$name})`). Both are
answers the graph already knew how to give. A caller that scans a list for one thing is describing a
query the API is missing.

**Cost of finding it the second way rather than the first**: the provenance regression shipped and
was caught by the next backlog row; the lag reader was caught by then asking what else read the
board. Nothing was red at any point.
