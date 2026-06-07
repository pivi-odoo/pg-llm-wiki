---
title: "GEQO: Genetic Query Optimizer"
aliases:
  - GEQO
  - genetic query optimizer
  - geqo_threshold
tags:
  - theme/query-optimization
source_files:
  - src/backend/optimizer/geqo/geqo_main.c
  - src/backend/optimizer/geqo/geqo_pool.c
  - src/backend/optimizer/geqo/geqo_eval.c
  - src/backend/optimizer/geqo/geqo_erx.c
  - src/backend/optimizer/geqo/geqo_misc.c
  - src/backend/optimizer/geqo/geqo_random.c
  - src/backend/optimizer/geqo/geqo_selection.c
  - src/backend/optimizer/geqo/geqo_recombination.c
  - src/backend/optimizer/geqo/geqo_cx.c
  - src/backend/optimizer/geqo/geqo_ox1.c
  - src/backend/optimizer/geqo/geqo_ox2.c
  - src/backend/optimizer/geqo/geqo_pmx.c
  - src/backend/optimizer/geqo/geqo_px.c
  - src/backend/optimizer/geqo/geqo_mutation.c
  - src/backend/optimizer/geqo/geqo_copy.c
symbols:
  - geqo
  - gimme_pool_size
  - gimme_number_generations
  - geqo_eval
  - gimme_tree
  - gimme_edge_table
  - gimme_tour
  - spread_chromo
  - geqo_rand
  - geqo_randint
  - geqo_set_seed
  - geqo_selection
  - geqo_copy
  - geqo_mutation
  - init_tour
  - cx
  - ox1
  - ox2
  - pmx
  - px
  - print_pool
  - print_gen
  - print_edge_table
  - Chromosome
  - Pool
  - Gene
  - Edge
  - City
  - GeqoPrivateData
---

GEQO is PostgreSQL's heuristic join-ordering algorithm for queries that join too many relations for the standard dynamic programming search to handle in bounded time. When the number of FROM-clause relations reaches `geqo_threshold` (default 12), the planner stops exhaustive DP enumeration. It instead runs a genetic algorithm that explores the join-order search space stochastically, trading the guarantee of an optimal plan for a hard cap on planning time.

## The Problem GEQO Solves

The standard [[subsystems/planner/join-ordering|join ordering]] algorithm enumerates all possible subsets of the FROM-clause relations at each level of a DP table. The number of subsets grows as O(2^N): a 12-table query has 4,096 potential subsets, a 20-table query has over a million. Planning time for complex reporting queries or ORM-generated queries that join a dozen tables can become substantial before a single row is fetched.

GEQO frames join ordering as an instance of the Traveling Salesman Problem. GEQO encodes each possible join order as a permutation of relation indices — a sequence of integers where position determines join order, not value. The planner searches this permutation space using a genetic algorithm: a population of candidate orderings evolves over many generations, converging toward low-cost plans without ever examining every possibility.

The switch happens inside `standard_join_search()`. If the number of initial relations is at least `geqo_threshold`, `standard_join_search()` calls `geqo()` instead of the DP loop. Setting `geqo = off` disables the switch entirely, forcing exhaustive DP regardless of query size — useful for benchmarking plan quality but dangerous on large joins in production.

## Genetic Encoding: the Chromosome

A single candidate join order is called a chromosome. Its gene string is an array of `Gene` values (which are just integers), where each value is a 1-based index into the list of initial relations. A chromosome of length N represents a complete join order for N relations: the planner reads the gene string left to right and attempts to join relations in that sequence.

```
Relations: [orders, customers, regions, products, ...]
Gene string: [3, 1, 5, 2, 4, ...]
             ^   ^                 join regions first,
                 orders second, etc.
```

The pool is an array of chromosomes, each paired with a `worth` (the estimated total cost of the join plan it encodes). GEQO sorts the pool cheapest-first after initialization. The pool stays in that order throughout the run — `spread_chromo()` maintains the sort invariant by binary-searching for the correct insertion position whenever a new child chromosome is evaluated.

### Pool Layout and the Sort Invariant

The `Pool` struct holds a `data` array of `Chromosome` entries, a `size` (number of live individuals), and a `string_length` (equal to the number of joined relations). GEQO keeps the last slot in the array as a scratch buffer. It allocates the pool with `size + 1` entries so that it can write a candidate chromosome into `data[size]` for evaluation, before deciding whether to insert or discard it.

This layout means the worst individual is always at `data[size - 2]` (one before the scratch slot). The best is always at `data[0]`. Selection, crossover, and replacement all rely on this invariant without needing to scan the array.

## Sizing the Population

GEQO derives the pool size and generation count from the number of relations and `geqo_effort` (1–10, default 5):

- Default pool size: `2^(N+1)`, clamped to `[10 * effort, 50 * effort]`. With default effort this is between 50 and 250 individuals.
- Default generation count: equal to the pool size, ensuring that every initial individual is displaced at least once before the run ends.

Setting `geqo_pool_size` or `geqo_generations` to a positive value overrides the defaults entirely. `geqo_effort` is a single dial that scales both together: effort 1 runs a small, fast search; effort 10 runs a large, thorough one.

The initialization fills the pool with randomly shuffled gene strings. The initialization immediately evaluates each candidate. It discards and replaces any permutation that produces a logically invalid join tree (returning `DBL_MAX` from `geqo_eval()`). The initialization attempts up to 10,000 retries before giving up with an error. This can only happen if join constraints are so restrictive that almost no valid orderings exist.

## Selection and Selective Pressure

Each generation begins by selecting two parent chromosomes (called `momma` and `daddy` in the code) from the sorted pool. Selection uses a linear bias function: the probability of choosing a chromosome at rank r from a pool of size n is proportional to `selection_bias - (2 * (selection_bias - 1) * (r - 1) / (n - 1))`.

The `geqo_selection_bias` GUC (range 1.5–2.0, default 2.0) controls how steeply the bias favors better-ranked individuals. At 2.0 the best individual is twice as likely to be chosen as the worst; at 1.5 the gradient is gentler. Higher bias accelerates convergence but risks premature convergence to a local optimum — the population becomes homogeneous before the search space is well explored.

## Edge Recombination Crossover

PostgreSQL defaults to Edge Recombination Crossover (ERX), implemented in `geqo_erx.c`. ERX is designed to preserve adjacency relationships from both parents: if both parents join relation X followed by relation Y in some part of their gene strings, the child will likely do so too.

The mechanism works through an edge table. For each relation, the table records which relations appear adjacent to it in either parent's gene string — these are the "edges" in the TSP analogy. GEQO marks shared edges (present in both parents) with a negative sign to give them priority.

Building the child gene string (`gimme_tour()`):
1. Pick a random starting relation.
2. At each step, look up the current relation's entry in the edge table.
3. If any neighbor is a shared edge (negative), follow it immediately.
4. Otherwise, follow the neighbor with the fewest remaining unused edges — the "greedy" heuristic that minimizes dead ends.
5. If no unused neighbors remain (an edge failure), pick a random unused relation and continue.

Debug builds track and log edge failures. A high failure rate indicates that the two parents are very similar (low diversity). This is a sign that selective pressure is too high or the pool has converged.

After ERX generates the child, `geqo_eval()` evaluates its fitness. If it beats the worst individual in the pool, `spread_chromo()` inserts it at the correct sorted position, displacing the current worst.

## Alternative Crossover Operators

ERX is the only crossover algorithm active in a standard PostgreSQL build, but the source tree ships five alternatives that can be compiled in by changing a single `#define` in `src/include/optimizer/geqo.h`. Developers must select exactly one operator at compile time — the `#if defined(ERX)` / `#elif defined(PMX)` chain in `geqo_main.c` ensures the build fails if none is chosen. The alternatives live in separate files and share a common infrastructure provided by `geqo_recombination.c`.

The core challenge for all crossover operators is the same: a join-order genome is a permutation — every relation index must appear exactly once. Naive one-point or two-point crossover (the kind used for binary-string GAs) will produce offspring with duplicated or missing genes. All of GEQO's crossover operators are designed specifically to produce valid permutations from two valid parent permutations.

`geqo_recombination.c` provides two shared utilities used by most operators. `init_tour()` generates a random valid permutation using the Fisher-Yates inside-out shuffle. Pool initialization uses it. `alloc_city_table()` / `free_city_table()` manage a lookup array (`City *`) indexed by gene value. Several operators use this array to track which relations have already been placed in the child. These helpers are conditionally compiled only when at least one of the operators that needs them (CX, PX, OX1, OX2) is selected.

### Cycle Crossover (CX)

CX (`geqo_cx.c`, Oliver et al.) identifies cycles between the two parent gene strings — positions where following the "what does tour2 put at tour1's position?" mapping eventually loops back to the start. The child inherits positions within a detected cycle from the first parent; it fills positions outside the cycle from the second. This preserves the absolute position of each gene value from one parent or the other, ensuring no duplicates without needing a used-position scan.

One consequence of CX is that when both parents are identical in the selected cycle, the child is also identical to both parents — no new genetic material is introduced. GEQO handles this with `geqo_mutation()`: if `cx()` reports zero differences between the child and the first parent, GEQO triggers a round of mutation immediately before evaluation. This is the only place in the GEQO codebase where mutation is applied.

### Order Crossover Variants (OX1, OX2)

OX1 (`geqo_ox1.c`, Davis) selects a random contiguous segment of the first parent and copies it verbatim into the child at the same positions. OX1 then fills the remaining positions left-to-right (wrapping around) with genes from the second parent in the order they appear in the second parent, skipping any gene already present in the copied segment. This preserves the relative ordering of relations from the second parent while inheriting an absolute block from the first.

OX2 (`geqo_ox2.c`, Syswerda) takes a different approach: instead of a contiguous segment, it selects a random number of individual positions from the first parent. OX2 notes the genes at those positions, then inserts them into the child at the positions where those same genes appear in the second parent, in the order they appeared in the first parent. The net effect is that OX2 preserves the relative order of a random subset of genes from the first parent while keeping the relative order of all other genes from the second parent.

### Partially Matched Crossover (PMX)

PMX (`geqo_pmx.c`, Goldberg and Lingle) selects a random contiguous segment and copies it from the first parent. For each gene in the first parent's segment that conflicts with a gene already placed from the second parent, PMX traces a mapping chain to find a legal placement. The algorithm works in three passes: direct mapping of the segment, resolution of simple conflicts, and a final sweep to fix any remaining duplicates using genes not yet placed. PMX tends to preserve absolute position information from both parents more than OX does.

### Position Crossover (PX)

PX (`geqo_px.c`, Syswerda) selects a random set of positions (between one-third and two-thirds of the genome length) and copies the genes at those positions directly from the first parent, preserving their absolute locations. PX fills the remaining empty positions by scanning the second parent left-to-right and inserting any gene not already placed. PX is similar to OX2 in concept but simpler: it preserves absolute position for the selected subset rather than relative order.

## Mutation

Mutation in GEQO is a swap operator implemented in `geqo_mutation.c`. When invoked, it performs up to `num_gene / 3` random two-gene swaps on the child's gene string, each swap exchanging two distinct randomly chosen positions. The result is a valid permutation — swapping two elements of a permutation always produces another valid permutation — so no repair step is needed.

As noted above, GEQO only triggers mutation in CX mode, and only when crossover produced a child identical to its first parent. This targeted application prevents the population from stagnating when two identical (or near-identical) parents are selected for mating. It also avoids the disruptive effect that indiscriminate mutation would have on an otherwise well-converged search.

## Chromosome Copy

`geqo_copy.c` provides a single utility, `geqo_copy()`, that copies one `Chromosome` to another — both the gene string and the `worth` value. `geqo_selection()` uses it to copy the selected parent chromosomes out of the pool into the working `momma` and `daddy` buffers before crossover. Copying out of the pool (rather than working with pointers into it) ensures that the crossover operators can read the parents freely even if the pool's sort order changes during the generation.

## Debug Instrumentation

`geqo_misc.c` contains all of GEQO's human-readable debug output, compiled only when the `GEQO_DEBUG` preprocessor symbol is defined. Production builds include none of this code.

`geqo_misc.c` provides three functions:

- `print_pool(fp, pool, start, stop)` — dumps a range of chromosomes from the pool as tab-separated gene sequences followed by their `worth` value. Useful for inspecting the state of the population at any generation.
- `print_gen(fp, pool, generation)` — prints a one-line generation summary: generation number, best cost, worst cost, median cost, and average cost. `print_gen()` computes the average by dividing each entry by `pool->size` before accumulating, avoiding floating-point overflow when the pool contains `DBL_MAX` entries (invalid chromosomes awaiting displacement).
- `print_edge_table(fp, edge_table, num_gene)` — dumps the full ERX edge table, showing each relation's adjacency list with the remaining unused-edge count.

To enable this output, build PostgreSQL with `-DGEQO_DEBUG` added to `CFLAGS`, or define it in a custom `pg_config.h`. The output goes to the `FILE *` pointer passed by the caller (typically `stderr` in the GEQO main loop).

## Evaluating a Join Order

`geqo_eval()` translates a gene string into an actual plan cost by calling `gimme_tree()` — the same join-tree construction that the DP algorithm uses for individual join rels, just driven by the candidate permutation rather than exhaustive enumeration.

Because this evaluation happens hundreds or thousands of times per query, `geqo_eval()` allocates a private [[subsystems/memory/contexts|memory context]] for each evaluation and deletes it afterward. Without this, intermediate `RelOptInfo` nodes and path structures would accumulate in the planner's normal context and exhaust memory long before the generations complete.

`gimme_tree()` does not simply join relations in strict gene-string order. It maintains a list of "clumps" — groups of already-joined relations — and processes the gene string as a sequence of hints rather than mandates. `gimme_tree()` merges each new relation from the gene string into the first clump it can legally and desirably join. Desirability means there is a join clause or join-order restriction connecting the new relation to the clump; `gimme_tree()` defers undesirable (Cartesian) joins. After processing all relations, it force-joins any remaining separate clumps in some legal order. This relaxation allows GEQO to produce bushy plans and to recover gracefully from gene strings that would otherwise create illegal intermediate joins due to outer-join constraints.

If `gimme_tree()` cannot merge all relations into a single join rel, it returns NULL and `geqo_eval()` returns `DBL_MAX`, marking the chromosome as invalid.

## The Private PRNG

Every random decision in GEQO — initial population shuffling, parent selection, ERX starting point, edge-failure recovery — flows through three thin wrappers in `geqo_random.c`: `geqo_rand()` returns a float in `[0.0, 1.0)`, `geqo_randint()` returns an integer in a closed `[lower, upper]` range, and `geqo_set_seed()` initialises the generator from a `double` seed value.

All three delegate to PostgreSQL's `pg_prng` family (`pg_prng_double`, `pg_prng_uint64_range`, `pg_prng_fseed`). The state lives in `GeqoPrivateData.random_state`, a `pg_prng_state` struct stored on `PlannerInfo.join_search_private`. Keeping state per-query rather than in a process-global variable has two consequences worth understanding:

- **No cross-query interference.** Concurrent backend sessions each carry their own `GeqoPrivateData`, so one session's GEQO run cannot perturb another's random stream.
- **No interference with other code paths.** The planner calls `random()` in other contexts (e.g., for sampling). GEQO using `pg_prng` rather than libc `rand()` means those two streams are completely independent, making plan behaviour easier to reason about.

The `pg_prng` generator is a xoroshiro128** PRNG — not cryptographically secure, but fast and with good statistical properties for the sizes of populations GEQO uses (typically 50–250 individuals).

## Reproducibility and the Random Seed

GEQO's output is non-deterministic by default. Two executions of the same query can produce different plans because the initial population is seeded differently each time. `geqo_seed` (range 0.0–1.0, default 0.0) fixes the internal pseudo-random number generator to a specific starting state. With a fixed seed, the same query produces the same plan on the same PostgreSQL version — invaluable for debugging plan instability or writing reproducible benchmarks.

GEQO sets the seed at the start of each invocation via `geqo_set_seed()` (geqo_random.c). This function calls `pg_prng_fseed()` on the per-query state. It has no effect when GEQO is not triggered (query has fewer than `geqo_threshold` relations) and no effect on the DP algorithm. PostgreSQL does not guarantee reproducibility across major versions, because the `pg_prng` implementation or the way the seed value is derived could change.

## Trade-offs and Tuning

GEQO will not always find the optimal join order. By design it explores a bounded fraction of the full search space, so on any given query a better permutation may exist that the algorithm never encountered. The practical question is not whether GEQO is optimal but whether its plans are good enough — for most multi-table OLTP and reporting queries, the difference between GEQO's best and the true optimum is small relative to network or I/O overhead.

Several situations demand attention:

**ORM-generated queries with 12+ tables.** ORM frameworks often generate joins in a deterministic but untuned order. When those queries cross `geqo_threshold`, GEQO randomly reshuffles that order each execution. If statistics are good, GEQO finds reasonable plans. If statistics are poor, plan quality varies across executions. Setting `geqo_seed` to a fixed value stabilizes plans during investigation; setting `geqo_threshold` higher (e.g., 15 or 20) forces exhaustive DP to run on these queries at the cost of longer planning time.

**Reporting queries where planning time matters.** For long-running analytical queries, even several hundred milliseconds of planning time is acceptable. Raising `geqo_threshold` is safe in this context and may produce meaningfully better plans by giving DP more room to search.

**Debugging plan instability.** If a query produces wildly different execution times on different runs, check whether it crosses `geqo_threshold`. Fix `geqo_seed` to isolate a specific bad plan, then diagnose why that gene ordering is suboptimal. Common causes are stale statistics on one of the joined tables or a missing index that makes some join orderings catastrophically expensive.

**Effort vs. quality.** Raising `geqo_effort` (e.g., to 8 or 10) increases both pool size and generation count, giving the genetic search more chances to find good plans. The cost is proportionally more planning time. This is a middle ground between accepting GEQO's defaults and raising `geqo_threshold` to force full DP.

```sql
-- Stabilize GEQO plans for debugging
SET geqo_seed = 0.42;

-- Force exhaustive DP for up to 20 relations (expensive planning)
SET geqo_threshold = 20;

-- More thorough GEQO search without switching to DP
SET geqo_effort = 8;

-- Disable GEQO entirely (never use in production on large joins)
SET geqo = off;
```

## Related Topics

- [[subsystems/planner/join-ordering|join ordering]] — the DP algorithm GEQO replaces above the threshold, and the RelOptInfo structures GEQO reuses
- [[subsystems/planner/overview|planner]] — the overall planning pipeline that invokes GEQO
- [[subsystems/memory/contexts|memory contexts]] — how geqo_eval isolates per-candidate allocations to avoid memory exhaustion
