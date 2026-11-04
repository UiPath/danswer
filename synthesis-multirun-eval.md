# Multi-run synthesis test (5x/question) — signal vs noise

_3 questions x 5 runs. Cross-attribution as a RATE; completeness mean±stdev. Fresh decompose+retrieve per run (real pipeline noise); judge temp 0._

## Per-question

| question | x-attr holistic | x-attr map-reduce | comp holistic (m±sd) | comp map (m±sd) | wins H/M/tie |
|---|---|---|---|---|---|
| What do I need for a high-availability | 2/5 | 1/5 | 4.20±0.40 | 2.00±0.63 | 4/1/0 |
| What are the differences between a sin | 2/5 | 2/5 | 4.80±0.40 | 3.80±0.40 | 3/2/0 |
| What must be configured for Orchestrat | 3/5 | 2/5 | 4.80±0.40 | 2.60±0.49 | 3/2/0 |

## Pooled
- **cross-attribution rate** — holistic 7/15 (47%) vs map-reduce 5/15 (33%)
- **completeness** — holistic 4.60 vs map-reduce 2.80
- **wins** — holistic 10, map-reduce 5, tie 0

## Per-run detail

**What do I need for a high-availability Automation Suite inst**
- run 1: x-attr H=False M=False | comp H=4 M=3 | win=hol
- run 2: x-attr H=True M=False | comp H=4 M=2 | win=map
- run 3: x-attr H=True M=False | comp H=4 M=1 | win=hol
- run 4: x-attr H=False M=True | comp H=5 M=2 | win=hol
- run 5: x-attr H=False M=False | comp H=4 M=2 | win=hol

**What are the differences between a single-node and a multi-n**
- run 1: x-attr H=False M=True | comp H=5 M=4 | win=hol
- run 2: x-attr H=False M=True | comp H=4 M=3 | win=hol
- run 3: x-attr H=True M=False | comp H=5 M=4 | win=map
- run 4: x-attr H=False M=False | comp H=5 M=4 | win=hol
- run 5: x-attr H=True M=False | comp H=5 M=4 | win=map

**What must be configured for Orchestrator dynamic/elastic job**
- run 1: x-attr H=False M=True | comp H=5 M=3 | win=hol
- run 2: x-attr H=True M=False | comp H=5 M=3 | win=map
- run 3: x-attr H=False M=True | comp H=5 M=2 | win=hol
- run 4: x-attr H=True M=False | comp H=5 M=3 | win=map
- run 5: x-attr H=True M=False | comp H=4 M=2 | win=hol

