# Livinit room designer

> [!NOTE]
> **Archived.** This research prototype is complete. It implemented a custom
> interior decorator agent built on the open-source PI core. The repository is
> kept as a reference.
>
> Development continues in the main private Livinit codebase:
> [Open the product](https://livinit.ai).

Turn a room description, floor plan, and budget into three furnished 3D designs
using real catalog products. Supports living rooms, bedrooms, dining rooms, and
studios.

## Objective

Turn a short brief, a floor plan and a budget into three furnished designs
built from real catalog products.

- **Room types.** Living room, bedroom, dining room and studio. Each type has
  its own required pieces, banned pieces and groups. A studio holds sleeping,
  sitting and dining zones in one room.
- **Sensible layouts.** Every piece fits, doors, windows and walkways stay clear,
  and each group (sofa and TV, table and chairs, bed and nightstands) forms one
  zone.
- **Budget.** Spend 80 to 95% of the budget, and never more than 10% over.
- **Latency.** The first design in about 20 seconds, all three in about 30.
- **Reliability.** A piece that fails a check is swapped or removed, so the
  design is still delivered.

## Demo

![Living room brief and design generation](docs/demo.gif)

## Technical stack

| Layer | Technology and purpose |
| --- | --- |
| Web app | TypeScript, React, Vite, and Tailwind CSS for the brief, progress, comparison, and Studio screens. |
| 3D view | Three.js with React Three Fiber and Drei renders catalog models and their calculated positions. |
| Pipeline | Python, FastAPI, and LangGraph coordinate shared stages and parallel design branches. Server-sent events stream progress and results. |
| Models | Gemini interprets briefs, selects products, and reviews layouts. Jev ranks candidates and checks style and attributes. CLIProxyAPI supports alternative models. |
| Catalog search | PostgreSQL with pgvector searches prepared product embeddings, with filters for dimensions, categories, and prices. |
| Geometry and validation | Shapely, NumPy, and Python rules handle placement and clearance checks. Pydantic and Zod validate service and browser data. |

## How it works

```text
brief ─▶ Interpret ─▶ Room ─▶ Retrieve ─▶ Rank ─┬─▶ Select ─▶ Place ─▶ Finish ─▶ design 1
                                                ├─▶ Select ─▶ Place ─▶ Finish ─▶ design 2
                                                └─▶ Select ─▶ Place ─▶ Finish ─▶ design 3
```

The first four steps run once per request and all three designs share their
results. The designs then run in parallel, and each one streams to the browser
when it is ready.

| Step | Runs | Done by | What it does |
| --- | --- | --- | --- |
| Interpret | once | Gemini | Reads the brief into items, counts, styles and limits. |
| Room | once | Code | Maps walls, doors, windows, walkways and the furniture the room can hold. |
| Retrieve | once | Vector search | Finds catalog candidates for each kind of item. |
| Rank | once | Jev | Orders the candidates for each design's style and deals mostly different products to each design. |
| Select | per design | Gemini, code, Jev | Gemini picks the products. Code fixes the TV and checks fit and budget, and Jev checks style. A failed pick retries with the errors, up to 4 times. |
| Place | per design | Code solver | Arranges every piece against the walls and keeps doors and walkways clear. |
| Finish | per design | Gemini, code | Gemini reviews the layout and moves pieces. Code keeps the moves that pass the checks and removes items that still fail. |

Step times are in the [benchmark](#benchmark-2026-10-11). The full flow, rules
and delivery format are in the [pipeline overview](docs/pipeline/pipeline-overview.md).

## Benchmark (2026-10-11)

The benchmark ran six briefs (living room, Scandi living room, bedroom, dining
room, studio, relaxed studio) 3 times each, for 18 runs and 54 designs. It used
`main` with `gemini-3.8-flash` at low thinking and Jev on. Times cover the
pipeline only, not 3D loading in the browser. The full results are in the
[2026-10-11 report](docs/pipeline/report-2026-10-11.md).

### Delivery and speed

| | Median | p90 | Max |
| --- | ---: | ---: | ---: |
| First design ready | 21.5 s | 32.1 s | 51.0 s |
| All three designs done | 28.8 s | 42.4 s | 54.2 s |

All 54 designs were delivered, and all 18 runs finished under 60 seconds.

### Latency per step

| Step | Runs once per | Median | p90 | Max |
| --- | --- | ---: | ---: | ---: |
| 1 Interpret (Gemini) | request | 6.8 s | 12.0 s | 37.3 s |
| 2 Room (code) | request | < 0.01 s | < 0.01 s | 0.01 s |
| 3 Retrieve (vector search) | request | 1.3 s | 1.4 s | 1.8 s |
| 4a Rank (Jev) | request | 0.6 s | 0.8 s | 1.2 s |
| 4b Select (Gemini, checks, retries) | design | 6.5 s | 20.6 s | 29.1 s |
| 4c Place (code solver) | design | 0.7 s | 1.9 s | 2.7 s |
| 4d Finish (Gemini review, final check) | design | 4.7 s | 5.8 s | 12.5 s |

Select includes every retry, so its p90 comes from briefs that need two to four
turns. The 37 s interpret time is one slow Gemini response.

### Budget

Designs spend a median of 97% of the budget. 47 of 54 spend at least 80%, and
none goes more than 10% over.

### Reviewer scores

The reviewer, `gpt-6.1-sol`, scores each design from 1 to 5. Each row averages
the 9 designs of one brief.

| Case | Prompt match | Style | Budget use | Completeness | Scale | Circulation | Grouping | Use of space | Focal point | Layout quality | Overall | Distinct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Living room | 4.78 | 4.22 | 3.11 | 4.11 | 4.11 | 3.78 | 3.78 | 3.44 | 4.56 | 3.56 | 3.56 | 3.33 |
| Bedroom | 4.33 | 4.00 | 4.11 | 4.56 | 4.56 | 4.89 | 5.00 | 4.11 | 5.00 | 4.11 | 4.00 | 4.00 |
| Dining room | 4.22 | 4.78 | 4.78 | 3.00 | 4.44 | 4.56 | 5.00 | 4.67 | 5.00 | 4.22 | 3.89 | 3.33 |
| Studio | 5.00 | 4.89 | 3.33 | 3.00 | 4.22 | 3.78 | 3.67 | 3.00 | 3.89 | 3.00 | 3.00 | 4.00 |
| Scandi living room | 3.67 | 4.33 | 4.33 | 4.00 | 4.00 | 3.11 | 3.89 | 3.44 | 4.56 | 3.44 | 3.22 | 4.00 |
| Relaxed studio | 3.89 | 4.78 | 3.78 | 3.11 | 4.00 | 3.89 | 4.11 | 3.44 | 4.11 | 3.33 | 3.11 | 4.00 |
| **All cases** | **4.32** | **4.50** | **3.91** | **3.63** | **4.22** | **4.00** | **4.24** | **3.68** | **4.52** | **3.61** | **3.46** | **3.78** |

Prompt match, style, budget use, completeness and scale (pieces sized to the
room) score the product selection. Circulation, grouping, use of space, focal
point and layout quality score the layout. Overall scores the whole design, and
distinct scores how different the three designs of one request are. With 9
designs per brief, a difference under about 0.5 can be chance.
