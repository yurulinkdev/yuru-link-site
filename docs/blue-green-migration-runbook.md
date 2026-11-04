# Blue/Green data generation: execution contract

## Scope
- **Blue**: original Book0, Book1, Book2 spreadsheet IDs. Existing collectors continue reading/writing these.
- **Green**: Drive copies of those three files. Only these copies receive staged migration rows.
- **Not activated**: a READY_FOR_ACTIVATION manifest is not a production cutover. No consumer reads Green until activation is explicitly implemented and approved.
- Book0 and Book3 are authoritative user input; long-term deployment should not replace these sources. Copying Book0 here is for migration isolation, not to make user-editable inputs swappable.

## Runbook
1. Inspect the previous failed apply's partial writes in Blue. Do not clear or overwrite them.
2. Review PR #6 and run its offline tests. Confirm the Google service account can **create Drive copies** of shared Sheets; Sheets edit permission alone does not prove Drive copy works. Service accounts may require a shared drive or another suitable ownership/permission arrangement.
3. Merge after review. Run `Prepare YuruLink Blue-Green migration` with `prepare_green=false`. Compare expected/missing counts against the live state.
4. Only then run `prepare_green=true`. This creates three independent Green files; it does not touch Blue or switch consumers. Preserve the workflow artifact `green-generation-manifest`.
5. Check manifest status `READY_FOR_ACTIVATION`, unique keys, row counts, and spreadsheet IDs. Verify original Blue tabs remain unchanged.
6. Do not activate until a persistent manifest pointer, consumer resolution, and rollback workflow are implemented.

## Remaining engineering gates
- **Persistence**: workflow artifacts expire; generation registry must be durable and access-controlled.
- **Cutover**: update all consumers to resolve the same generation once per run; a single authoritative pointer avoids mixed generations.
- **Rollback**: pointer-only rollback to a verified prior generation; never overwrite Book0/Book3 human inputs.
- **Source concurrency**: freeze or snapshot writes while taking a consistent source generation. Drive copies of separate books are not atomic.
- **Verification**: validate complete key sets and referential integrity, not only counts; verify actual tests and staging against Google APIs.
- **Quotas**: size-limit payloads, pace requests, retry transient failures with verification, and monitor Google Drive storage/permissions.
- **Cleanup**: retain failed Green copies for diagnosis, then delete by policy after approval.

## Safety
The legacy migration workflow's `apply=true` path is disabled in PR #6.
PR #5 (batch-size workaround) is not required for this approach.
