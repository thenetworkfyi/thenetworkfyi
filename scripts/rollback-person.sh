#!/usr/bin/env bash
# Preview or remove an accidentally registered Person and application records
# that refer to it.
#
# The failed-admin/empty-body path does not itself create a Person. Run this
# script only to check whether a row exists from another message, and use
# --commit only after confirming that the row is accidental and taking a
# current database backup.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/rollback-person.sh EMAIL
  scripts/rollback-person.sh --commit EMAIL

Without --commit, all deletions run inside a transaction that is rolled back.
With --commit, the script requires an interactive confirmation before deleting.

The script deletes:
  - the matching people row
  - memories whose refs array contains that person's id
  - structured records removed by foreign-key cascades
  - rate-limit rows keyed to the normalized target email

The script deliberately leaves shared global rate limits, processed-message
dedup records, completed jobs, logs, and mailbox messages unchanged.
EOF
}

commit=false
case "${1:-}" in
    --commit)
        commit=true
        shift
        ;;
    -h|--help)
        usage
        exit 0
        ;;
esac

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi

target_email="$1"
if [[ ! "$target_email" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]]; then
    echo "Refusing invalid email address: $target_email" >&2
    exit 2
fi

cd "$(dirname "$0")/.."

db_service="${DB_SERVICE:-db}"
do_commit=false
if [[ "$commit" == true ]]; then
    if [[ ! -t 0 ]]; then
        echo "--commit requires an interactive terminal." >&2
        exit 2
    fi

    echo "This will permanently remove application data for $target_email."
    echo "That includes its Person row, if present, and email-specific rate limits."
    echo "Memories referring to that person will be deleted in full, including"
    echo "any memory that also refers to another person. Take a backup first."
    expected="delete $target_email"
    read -r -p "Type '$expected' to continue: " confirmation
    if [[ "$confirmation" != "$expected" ]]; then
        echo "Confirmation did not match; nothing was changed."
        exit 1
    fi
    do_commit=true
else
    echo "Dry run for $target_email; the transaction will be rolled back."
fi

sudo docker compose exec -T \
    -e ROLLBACK_TARGET_EMAIL="$target_email" \
    -e ROLLBACK_DO_COMMIT="$do_commit" \
    "$db_service" \
    sh -lc 'psql -X -v ON_ERROR_STOP=1 \
        -v target_email="$ROLLBACK_TARGET_EMAIL" \
        -v do_commit="$ROLLBACK_DO_COMMIT" \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
BEGIN;

CREATE TEMP TABLE rollback_person ON COMMIT DROP AS
SELECT id
FROM people
WHERE lower(email) = lower(:'target_email');

SELECT
    count(*) > 0 AS target_present,
    count(*) > 1 AS target_not_unique
FROM rollback_person
\gset

\if :target_not_unique
    \echo More than one case-insensitive match was found; refusing to continue.
    ROLLBACK;
    \quit 3
\endif

\if :target_present
    \echo
    \echo Matching Person row:
    SELECT p.id, p.email, p.name
    FROM people AS p
    JOIN rollback_person AS target ON target.id = p.id;

    \echo
    \echo Records affected directly or by cascade:
    SELECT relation, row_count
    FROM (
        SELECT 'event_recommendations' AS relation, count(DISTINCT r.id) AS row_count
        FROM event_recommendations AS r
        WHERE r.person_id IN (SELECT id FROM rollback_person)
           OR r.event_id IN (
                SELECT e.id
                FROM events AS e
                WHERE e.submitter_id IN (SELECT id FROM rollback_person)
           )

        UNION ALL

        SELECT 'event_suppressions', count(*)
        FROM event_suppressions AS s
        WHERE s.person_id IN (SELECT id FROM rollback_person)

        UNION ALL

        SELECT 'events', count(*)
        FROM events AS e
        WHERE e.submitter_id IN (SELECT id FROM rollback_person)

        UNION ALL

        SELECT 'introduction_consents', count(*)
        FROM introduction_consents AS c
        WHERE c.person_a_id IN (SELECT id FROM rollback_person)
           OR c.person_b_id IN (SELECT id FROM rollback_person)

        UNION ALL

        SELECT 'memories', count(*)
        FROM memories AS m
        WHERE EXISTS (
            SELECT 1
            FROM rollback_person AS target
            WHERE target.id = ANY(m.refs)
        )

        UNION ALL

        SELECT 'proactive_surfaces', count(*)
        FROM proactive_surfaces AS s
        WHERE s.person_a_id IN (SELECT id FROM rollback_person)
           OR s.person_b_id IN (SELECT id FROM rollback_person)
    ) AS affected
    ORDER BY relation;

    \echo
    \echo Memories selected for deletion; inspect refs for shared memories:
    SELECT m.id, m.created_at, m.refs
    FROM memories AS m
    WHERE EXISTS (
        SELECT 1
        FROM rollback_person AS target
        WHERE target.id = ANY(m.refs)
    )
    ORDER BY m.created_at, m.id;

    \echo
    \echo Deleting memories:
    DELETE FROM memories AS m
    USING rollback_person AS target
    WHERE target.id = ANY(m.refs)
    RETURNING m.id, m.created_at, m.refs;

    \echo
    \echo Deleting Person; foreign-key records cascade:
    DELETE FROM people AS p
    USING rollback_person AS target
    WHERE p.id = target.id
    RETURNING p.id, p.email, p.name;
\else
    \echo No matching Person row was found.
\endif

\echo
\echo Email-specific rate-limit rows selected for deletion:
SELECT key, count, expires_at
FROM rate_limits
WHERE strpos(key, ':' || lower(:'target_email') || '/') > 0
ORDER BY key;

DELETE FROM rate_limits
WHERE strpos(key, ':' || lower(:'target_email') || '/') > 0
RETURNING key, count, expires_at;

\if :do_commit
    COMMIT;
    \echo Deletion committed.
\else
    ROLLBACK;
    \echo Dry run complete; all deletions were rolled back.
\endif
SQL
