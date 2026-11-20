# The code graph

The crew uses [graphify](https://github.com/Graphify-Labs/graphify) as its shared map of the repository. graphify parses code with tree-sitter into a graph of files, symbols and the calls, imports and inheritance between them. Building it needs no model and no API key, and nothing leaves the machine, so the map costs seconds instead of the tokens it would take each agent to read the repository.

`crew graph` builds or refreshes it (`graphify update .`) and writes `graphify-out/`, which `crew init` hides from git. If graphify is not installed, `crew graph` says so and the crew maps with `rg` and `Read`. `crew graph --install` installs the CLI with `uv tool install graphifyy` when uv is present; mention that in the plan before doing it.

## Who uses it for what

| Role | Question | Command |
|---|---|---|
| Mapper | What are the subsystems and the most connected files? | read `graphify-out/GRAPH_REPORT.md`; `graphify god-nodes` |
| Mapper, Breaker | Where does this behaviour live? | `graphify query "<question>" --budget 1500` |
| Breaker | Which tests already cover this file? | `graphify affected "<file>"` |
| Builder | What depends on what I am about to change? | `graphify affected "<symbol or file>"` |
| Reviewer | What is the blast radius of this diff? | `graphify affected` for each changed file |
| Lead | Which issues are safe to build in parallel? | `crew waves` |
| anyone | How are these two things connected? | `graphify path "<A>" "<B>"` |
| anyone | What is this symbol and what touches it? | `graphify explain "<symbol>"` |

From an issue worktree, add `--graph <project>/graphify-out/graph.json` to every command, since the graph lives in the main checkout.

`crew waves` asks the graph which files depend on each issue's lane. Two issues go into different waves when their lanes overlap, or when one lane depends on a file in the other, because two builders changing a function and its caller at the same time produce a conflict that neither of their test runs will show.

## Reading the answers

- Exit code 0 is an answer, 3 an ambiguous name (the candidates are printed: choose by id or path), 4 no such node.
- Each edge is tagged `EXTRACTED` (stated in the source) or `INFERRED` (resolved by graphify). Treat an inferred edge as a lead and open the file before relying on it.
- `query` output is capped by `--budget` (tokens, default 2000). Narrow the question before raising the budget.
- The graph is a map for finding things. It is never evidence that a change works; only a tool run on the changed code is.
- After changes land on the main checkout, run `crew graph` again so later waves see the new code.
