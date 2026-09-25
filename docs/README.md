# The `docs/` directory — what to read, and what to skip

This directory is **4.3 MB across five files plus 86 round reports**, and almost none of it is
onboarding material. It is the engineering record of how the harness was built: a specification,
a decision log, a per-round checkpoint journal, a defect ledger and an acceptance-criteria
backlog. All five are working files that agents and tests read at exact line offsets. None of
them is a tutorial, and reading one top to bottom is not a reasonable way to learn this project.

This page is the map.

---

## If you are new here

Read these three, in this order, and nothing else:

1. **[`../README.md`](../README.md)** — what Fleet is, how to install it, and every command.
2. **[`../CLAUDE.md`](../CLAUDE.md)** — the operating directives for changing this repo. Read it
   before you touch `src/`, not after. It is long, but it is the only document here whose whole
   content applies to whatever you are about to do.
3. **The one `SPEC.md` section your task names.** Section numbers (§1–§14) are the stable
   reference and tasks are written against them. Open the section; do not open the file.

That is the newcomer path. Everything below is reference and archive.

---

## Reference — consult by section, never read end to end

| File | Size | What it is | When you open it |
|---|---|---|---|
| [`SPEC.md`](SPEC.md) | ~8,000 lines | The specification. Every field, table, flag and module name here is normative. §12 holds the 48 acceptance criteria. | A task names a section. Jump to it. |
| [`DECISIONS.md`](DECISIONS.md) | ~16,100 lines | The ADR log — every non-obvious choice with its rationale and rejected alternatives. Entries are appended, never edited; a superseded decision is replaced by a later numbered entry that names it. | You want to know *why* something is the way it is, and the code comment cites an ADR number. |

`SPEC.md` is subordinate to `DECISIONS.md`: where the two disagree, the ADR wins.

---

## Archive — the build record, not documentation

These are historical. They accumulate; they are not maintained as descriptions of the current
tree. **Do not read them to learn how the system works** — you will learn how it was built, which
is a different and much longer story.

| File | Size | What it is |
|---|---|---|
| [`PROGRESS.md`](PROGRESS.md) | ~11,700 lines | The checkpoint journal. One entry per development round: what was completed, what was verified, what the next task is. |
| [`INTEGRATION_HONESTY.md`](INTEGRATION_HONESTY.md) | ~12,200 lines | The defect ledger (D-numbers) and a per-external-tool verdict on what the test suite actually proves versus what it appears to prove. |
| [`CRITERIA_PLAN.md`](CRITERIA_PLAN.md) | ~4,400 lines | The per-criterion closure backlog for SPEC §12: what each acceptance criterion still needs, and a bounded "done bar" for each. |
| [`superpowers/plans/`](superpowers/plans/) | 86 files | Per-task research notes, design reviews and round reports written by development subagents. Point-in-time working documents. |

Two things to know before you open `PROGRESS.md`:

- **Its first page is dated 2026-08-09 and says implementation has not started.** That was true
  when it was written and is preserved deliberately — this file is a journal, and journals are
  annotated, not rewritten. **The last checkpoint in the file is the current state**, not the
  first.
- The same applies to every entry in `INTEGRATION_HONESTY.md`: an entry records what was true at
  its own commit. Its **status heading** is a field and is updated when a defect is fixed; its
  **body** is a record and is not.

### Why these are not in an `archive/` subdirectory

Because moving them would break the tree. They are load-bearing inputs, not inert history:

- Tests read them at literal paths and assert on their contents —
  `tests/test_floor_rule_statements.py`, `tests/test_blocked_by_writer_statements.py`,
  `tests/test_findings_kinds.py`, `tests/test_llm_cache.py`,
  `tests/test_read_transaction_statements.py` and
  `tests/test_integration_honesty_citations.py` all resolve `docs/` paths directly, including one
  specific file under `superpowers/plans/`.
- They are cited by path roughly 2,400 times across the repository, and many of those citations
  carry line numbers. A relocation would rot all of them at once, and this project treats a
  silently wrong citation as a defect in its own right.

So the separation here is editorial rather than physical: the tiering above tells you what to
read, and the files stay where every existing reference expects to find them.

---

## Conventions used throughout

| Convention | Meaning |
|---|---|
| `§N` / `§N.M` | A section of `SPEC.md`. |
| `ADR-NNNN` | An entry in `DECISIONS.md`. |
| `D<n>` | An entry in `INTEGRATION_HONESTY.md`'s defect ledger. |
| **NOT YET IMPLEMENTED** | In `SPEC.md`, marks a target state — a statement about intent, not about this tree. |
| `REAL` / `KNOWN_INERT` | In `INTEGRATION_HONESTY.md`, whether a test drives the genuine external tool or only a stand-in. |

A dated in-place marker beside a claim means that claim was later falsified and the marker names
what falsified it. The original wording is left intact on purpose.
