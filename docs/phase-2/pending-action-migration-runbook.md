# PendingAction Active-Row Migration Runbook

## Scope

This runbook applies before `ent_006` creates the PostgreSQL partial unique
index `uq_pending_action_active_step`. It covers only duplicate rows whose
status is `pending` or `approved`. The migration itself never edits approval
history.

## Read-Only Preflight

`ent_006` runs this check before creating the index and aborts when it finds a
duplicate:

```sql
SELECT task_id, step_id, COUNT(*) AS active_rounds
FROM pending_actions
WHERE status IN ('pending', 'approved')
GROUP BY task_id, step_id
HAVING COUNT(*) > 1;
```

Operators may run the same query before deployment. An empty result is required
before running `alembic upgrade heads` in an approved target environment.

## Manual Resolution

1. Stop new approval-pause creation for each reported Task/Step and preserve a
   database backup or transactionally consistent export.
2. Inspect every reported PendingAction and its linked ApprovalRequest. Record
   the incident identifier, row IDs, status, row version, timestamps, and the
   responsible operator.
3. An authorized service owner decides which, if any, approval round remains
   active. Do not infer the decision from timestamps or action payloads.
4. Apply an explicitly approved, separately reviewed data repair that moves
   only the non-canonical rows to an appropriate terminal state. The repair is
   outside this migration and must retain the rows for audit history.
5. Re-run the read-only preflight. Only after it returns no rows may `ent_006`
   create the unique index.

## Rehearsal Boundary

The static migration tests validate ordering and SQL presence only. Executing
this runbook against a disposable PostgreSQL database requires explicit
approval; this repository task neither connects to a database nor runs Alembic
against one.

## Static Acceptance Checklist

Repository-only validation may confirm all of the following without a database:

- the Phase 2 revisions form the linear `ent_002 -> ... -> ent_006` chain;
- no Phase 2 `upgrade()` drops a table, column, or index;
- the `ent_006` duplicate-active preflight appears before index creation;
- `ent_006` contains no UPDATE or DELETE repair statement;
- the manual procedure preserves operator authority and terminal history.

These checks are evidence about migration source, not evidence that PostgreSQL
accepted the migration. A disposable-database rehearsal remains a separate,
explicitly authorized deployment activity.
