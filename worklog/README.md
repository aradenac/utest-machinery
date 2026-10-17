# worklog — agent working record (versioned)

This directory is the **agent's own working record** for uTest Machinery. It is versioned
on purpose so decisions, reconnaissance and arbitration are auditable alongside the code.

**Boundary:** this is *not* product documentation and it does not change the original
documentation structure. The spec, plan and qualification pages under `docs/` remain the
authority. When a worklog note contradicts a `docs/` page, the `docs/` page wins and the
note is corrected. Nothing here declares product behavior or implemented status.

## Contents

| Path | Purpose |
| --- | --- |
| [`log/`](log/) | Dated session journals: what was inspected, done, observed, what is next |
| [`decisions/`](decisions/) | Numbered decision records: context, options, decision, status, consequences |
| [`questions.md`](questions.md) | Open questions and arbitrations waiting on the user |

## Conventions

- One dated file per working session under `log/`, named `YYYY-MM-DD.md` (append a suffix
  if a second session is opened the same day).
- Decision records are numbered `NNNN-short-slug.md`, immutable once decided; supersede
  rather than rewrite, and mark the superseding ID.
- Record **facts observed** versus **claims/inferences** distinctly. Never record a test
  result, metric or status that was not actually observed.
- Pin exact versions, digests and tags; no floating references.
- Keep secrets out: reference a variable name, never a value. Presence checks only.
