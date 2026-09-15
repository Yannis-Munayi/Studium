# Content review: the license queue and the weekly triage

Two obligations from the operational calendar land here, both from ingestion
§11. They share a runbook because they share a queue.

| Obligation | Cadence | Owner |
|---|---|---|
| `license_review` | on every source added | yannis |
| `content_review_queue_triage` | weekly | yannis |

`studium ops calendar --on-event on_source_add` lists the first;
`studium ops calendar --due-this-week` lists the second when it is close.

## Classifying a new source (§11.5)

Every source enters as `permission_granted` — the honest default, meaning "the
uploader claims rights and nobody has checked". Retrieval will serve from it,
and that is the point of the default, but a source sitting in that state is one
nobody can answer a rights question about.

```sh
studium sources list --unclassified
studium sources show <source_id>
studium sources classify <source_id> \
    --license proprietary_licensed \
    --note "Direct permission from <rightsholder>, <date>, covering <what>."
```

**Write the conditions into the note verbatim.** Non-commercial only,
revocable, attribution required, a named use case, an expiry — all of it, in
the rightsholder's words rather than a summary. Six months from now nobody will
remember whether the permission covered redistribution, and this note is the
entire record a rights review has to work from. A note that reads "Greg said
yes" is a note that will be re-litigated from scratch.

The classification is written to the source's history, so `sources show` after
the fact says who classified it and when.

## The weekly queue triage (§11.2, §12.3)

```sh
studium queue list                     # everything pending
studium queue list --severity 3        # the ones that block
studium queue show <queue_id>
```

Each item ends one of three ways, and each takes a note:

```sh
studium queue resolve  <queue_id> --note "..."   # fixed; say what you did
studium queue dismiss  <queue_id> --note "..."   # not a problem; say why
studium queue escalate <queue_id> --severity 3   # someone else's call
```

**Dismissals need the better note.** A resolved item has a change behind it
that a diff will show; a dismissed one leaves nothing but the note, and the
same flag will fire again on the next ingest if the reason is not recorded
where the next reader will find it.

Items the nightly consistency check raises (`concept_source_conflict`,
severity 1) are usually a re-extraction that superseded chunks without
repointing the concept. Nothing is broken — retrieval skips a missing chunk by
design — but the concept is grounded in less evidence than its mapping claims.

Resolved rows are deleted a year later by the retention worker
(`ingestion_review_queue` policy). Pending rows are never aged out: an
unresolved license question does not become resolved by being ignored.

## When the queue is empty

Say so in the completion:

```sh
studium ops calendar --complete content_review_queue_triage
```

An empty queue is a result. Skipping the completion because there was nothing
to do makes the calendar say the triage has not happened since whenever it last
found something, which is the state that stops anyone trusting it.
