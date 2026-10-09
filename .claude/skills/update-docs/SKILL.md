---
name: update-docs
description: Bring the repo's documentation in line with a finished change. Use at the end of an implement run, before the commit, or when asked to update the README or docs.
---

# Update docs

Docs describe the code as it is after this change. Every doc below gets a **verdict**: updated, or unchanged with a one-line reason.

## Steps

1. **Collect the change.** Read the ticket and the diff of this run: `git diff HEAD` plus untracked files, or `git diff <commit before the run>` if it is already committed. List what a user or contributor can now do, run, configure or must know that they couldn't before.
2. **Check each doc** in the table against that list, and edit where its trigger fires. Match the doc's existing voice, structure and length.
3. **Verify** every command you wrote by running it (`uv run dbx <cmd> --help`, the `make` target), and every path by opening it.
4. **Report** the verdicts in your final message, one line per doc. The doc edits go in the same commit as the code.

Done when every row of the table has a verdict and every command in the edited docs runs.

## Docs

| Doc | Update when the change… | What to write |
|---|---|---|
| `README.md` § Usage | adds or changes a `dbx` command, option, env var or local file | The flow a user follows, one command per line with a short comment. `--help` holds the full option list. |
| `README.md` § Development | adds or changes a `make` target, fixture, test marker or setup step | The command and when to use it. |
| `CONTEXT.md` | introduces or sharpens a domain term (a name in code, a test or the ticket that a domain expert would use) | An entry in the existing format: bold term, definition, `_Avoid_:` synonyms. Load `mattpocock-skills:domain-modeling` first. |
| `docs/adr/` | makes a decision that is hard to reverse, surprising without context, or a real trade-off between options | A new ADR numbered after the last, in the format of the existing ones. A choice the ticket or proposal already dictated needs none. |
| `docs/setup/` | changes the privileges, extensions or settings the analyzer needs on a target database | The SQL or the step, in `postgres_role.sql` or `azure.md`. |
| `docs/proposal.md` | deliberately departs from the design | Nothing in the proposal: record the departure as an ADR, which supersedes the proposal on that point. |

Leave `docs/execution-plan.md` alone; it is the plan of record, re-planned at milestone reviews.
