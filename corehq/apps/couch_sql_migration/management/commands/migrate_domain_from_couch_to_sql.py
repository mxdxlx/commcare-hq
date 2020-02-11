import logging
import os
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from corehq.apps.domain.models import Domain
from couchforms.models import doc_types
from dimagi.utils.chunked import chunked

from corehq.apps.couch_sql_migration.couchsqlmigration import (
    CASE_DOC_TYPES,
    do_couch_to_sql_migration,
    setup_logging,
)
from corehq.apps.couch_sql_migration.missingdocs import find_missing_docs
from corehq.apps.couch_sql_migration.progress import (
    MigrationStatus,
    couch_sql_migration_in_progress,
    get_couch_sql_migration_status,
    set_couch_sql_migration_complete,
    set_couch_sql_migration_not_started,
    set_couch_sql_migration_started,
)
from corehq.apps.couch_sql_migration.rewind import rewind_iteration_state
from corehq.apps.couch_sql_migration.statedb import (
    Counts,
    delete_state_db,
    init_state_db,
    open_state_db,
)
from corehq.form_processor.backends.sql.dbaccessors import (
    CaseAccessorSQL,
    FormAccessorSQL,
)
from corehq.form_processor.models import CommCareCaseSQL, XFormInstanceSQL
from corehq.form_processor.utils import should_use_sql_backend
from corehq.sql_db.util import (
    estimate_partitioned_row_count,
    paginate_query_across_partitioned_databases,
)
from corehq.util.log import with_progress_bar
from corehq.util.markup import shell_green, shell_red

log = logging.getLogger('main_couch_sql_datamigration')

# Script action constants
MIGRATE = "MIGRATE"
COMMIT = "COMMIT"
RESET = "reset"  # was --blow-away
REWIND = "rewind"
STATS = "stats"
DIFF = "diff"

CACHED = "cached"
RESUME = "resume"
REBUILD = "rebuild"

CASE_DIFF = {"process": True, "local": False, "none": None}


class Command(BaseCommand):
    help = """
    Step 1: Run 'MIGRATE'
    Step 2a: If diffs, use 'diff' to view diffs
    Step 2b: Use 'stats --verbose' to view more stats output
    Step 3: If no diffs or diffs are acceptable run 'COMMIT'
    Step 4: Run 'reset' to abort the current migration
    """

    def add_arguments(self, parser):
        parser.add_argument('domain')
        parser.add_argument('action', choices=[
            MIGRATE,
            COMMIT,
            RESET,
            REWIND,
            STATS,
            DIFF,
        ])
        parser.add_argument('--no-input', action='store_true', default=False)
        parser.add_argument('--debug', action='store_true', default=False)
        parser.add_argument('--verbose', action='store_true', default=False,
            help="Show verbose stats output.")
        parser.add_argument('--state-dir',
            default=os.environ.get("CCHQ_MIGRATION_STATE_DIR"),
            required="CCHQ_MIGRATION_STATE_DIR" not in os.environ,
            help="""
                Directory for couch2sql logs and migration state. This must not
                reside on an NFS volume for migration state consistency.
                Can be set in environment: CCHQ_MIGRATION_STATE_DIR
            """)
        parser.add_argument('--live',
            dest="live_migrate", action='store_true', default=False,
            help='''
                Do migration in a way that will not be seen by
                `any_migrations_in_progress(...)` so it does not block
                operations like syncs, form submissions, sms activity,
                etc. A "live" migration will stop when it encounters a
                form that has been submitted within an hour of the
                current time. Live migrations can be resumed after
                interruption or to top off a previous live migration by
                processing unmigrated forms that are older than one
                hour. Migration state must be present in the state
                directory to resume. A live migration may be followed by
                a normal (non-live) migration, which will commit the
                result if all goes well.
            ''')
        parser.add_argument('--rebuild-state',
            dest="rebuild_state", action='store_true', default=False,
            help="""
                Rebuild migration state by restarting form iterations.
                Check each form to see if it has already been migrated,
                and enqueue unmigrated forms as necessary until the
                iteration reaches the place where it had previously
                stopped. Finally resume the migration as usual. Forms
                (and associated cases) encountered while rebuilding
                state will not be diffed unless the form is not found
                in SQL, which means that some cases that were previously
                queued to diff may not be diffed.
            """)
        parser.add_argument('--case-diff',
            dest='case_diff', default="process",
            choices=["process", "local", "none"],
            help='''
                process: diff cases in a separate process (default).
                local: diff cases in the migration process.
                none: do not diff cases.
            ''')
        parser.add_argument('--forms', default=None,
            help="""
                Migrate specific forms. The value of this option should
                be a space-delimited list of form ids OR a file path to
                a file having one form id per line OR 'skipped' to
                migrate forms skipped by previous migration OR 'missing'
                to migrate missing forms cached in the statedb by the
                'stats' command. The file path must begin with / or ./
            """)
        parser.add_argument('-x', '--stop-on-error',
            dest="stop_on_error", action='store_true', default=False,
            help="""
                Stop on form migration error rather than recording diffs
                and continuing with next form.
            """)
        parser.add_argument('--to', dest="rewind", help="Rewind iteration state.")
        parser.add_argument('--missing-docs',
            choices=[RESUME, REBUILD, CACHED], default=RESUME,
            help="""
                By default missing docs will be calculated before stats
                are printed, resuming from the previous run if possible.
                Use "rebuild" to discard previous results and
                recalculate all missing docs that are in Couch but not
                SQL. Use "cached" to use existing data from state db.
            """)

    def handle(self, domain, action, **options):
        if should_use_sql_backend(domain):
            raise CommandError('It looks like {} has already been migrated.'.format(domain))

        for opt in [
            "no_input",
            "verbose",
            "state_dir",
            "live_migrate",
            "case_diff",
            "rebuild_state",
            "stop_on_error",
            "forms",
            "rewind",
            "missing_docs",
        ]:
            setattr(self, opt, options[opt])

        if self.no_input and not settings.UNIT_TESTING:
            raise CommandError('--no-input only allowed for unit testing')
        if action != MIGRATE and self.live_migrate:
            raise CommandError(f"{action} --live not allowed")
        if action != MIGRATE and self.rebuild_state:
            raise CommandError("--rebuild-state only allowed with `MIGRATE`")
        if action != MIGRATE and self.forms:
            raise CommandError("--forms only allowed with `MIGRATE`")
        if action != MIGRATE and self.stop_on_error:
            raise CommandError("--stop-on-error only allowed with `MIGRATE`")
        if action != STATS and self.verbose:
            raise CommandError("--verbose only allowed for `stats`")
        if action not in [MIGRATE, STATS] and self.missing_docs != RESUME:
            raise CommandError(f"{action} --missing-docs not allowed")
        if action != REWIND and self.rewind:
            raise CommandError("--to=... only allowed for `rewind`")

        assert Domain.get_by_name(domain), f'Unknown domain "{domain}"'
        slug = f"{action.lower()}-{domain}"
        setup_logging(self.state_dir, slug, options['debug'])
        getattr(self, "do_" + action)(domain)

    def do_MIGRATE(self, domain):
        set_couch_sql_migration_started(domain, self.live_migrate)
        do_couch_to_sql_migration(
            domain,
            self.state_dir,
            with_progress=not self.no_input,
            live_migrate=self.live_migrate,
            diff_process=CASE_DIFF[self.case_diff],
            rebuild_state=self.rebuild_state,
            stop_on_error=self.stop_on_error,
            forms=self.forms,
        )

        return_code = 0
        if self.live_migrate:
            print("Live migration completed.")
            has_diffs = True
        else:
            has_diffs = self.print_stats(domain, short=True, diffs_only=True)
            return_code = int(has_diffs)
        if has_diffs:
            print("\nRun `diff` or `stats [--verbose]` for more details.\n")
        if return_code:
            sys.exit(return_code)

    def do_reset(self, domain):
        if not self.no_input:
            _confirm(
                "This will delete all SQL forms and cases for the domain {}. "
                "Are you sure you want to continue?".format(domain)
            )
        set_couch_sql_migration_not_started(domain)
        blow_away_migration(domain, self.state_dir)

    def do_COMMIT(self, domain):
        if not couch_sql_migration_in_progress(domain, include_dry_runs=False):
            raise CommandError("cannot commit a migration that is not in state in_progress")
        if not self.no_input:
            _confirm(
                "This will convert the domain to use the SQL backend and"
                "allow new form submissions to be processed. "
                "Are you sure you want to do this for domain '{}'?".format(domain)
            )
        set_couch_sql_migration_complete(domain)

    def do_stats(self, domain):
        self.print_stats(domain, short=not self.verbose)

    def do_diff(self, domain):
        print(f"replaced by: couch_sql_diff show {domain} [--select=DOC_TYPE]")

    def do_rewind(self, domain):
        db = open_state_db(domain, self.state_dir)
        assert os.path.exists(db.db_filepath), db.db_filepath
        db = init_state_db(domain, self.state_dir)
        rewind_iteration_state(db, domain, self.rewind)

    def print_stats(self, domain, short=True, diffs_only=False):
        status = get_couch_sql_migration_status(domain)
        if not self.live_migrate:
            self.live_migrate = status == MigrationStatus.DRY_RUN
        if self.missing_docs != CACHED:
            resume = self.missing_docs == RESUME
            find_missing_docs(domain, self.state_dir, self.live_migrate, resume)
        print(f"Couch to SQL migration status for {domain}: {status}")
        statedb = open_state_db(domain, self.state_dir)
        doc_counts = statedb.get_doc_counts()
        has_diffs = False
        ZERO = Counts()
        for doc_type in (
            list(doc_types())
            + ["HQSubmission", "XFormInstance-Deleted"]
            + CASE_DOC_TYPES
        ):
            has_diffs |= self._print_status(
                doc_type,
                doc_counts.get(doc_type, ZERO),
                statedb,
                short,
                diffs_only,
            )

        pending = statedb.count_undiffed_cases()
        if pending:
            print(shell_red(f"\nThere are {pending} case diffs pending."))
            print(f"Resolution: couch_sql_diff cases {domain} --select=pending")
            return True

        if diffs_only and not has_diffs:
            print(shell_green("No differences found between old and new docs!"))
        return has_diffs

    def _print_status(self, doc_type, counts, statedb, short, diffs_only):
        has_diffs = counts.missing or counts.diffs
        if diffs_only and not has_diffs:
            return False

        if not short:
            print("_" * 40)
        stats = [
            f"{doc_type:<22} {counts.total:>9} docs",
            shell_red(f"{counts.missing} missing from SQL") if counts.missing else "",
            shell_red(f"{counts.diffs} have diffs") if counts.diffs else "",
        ]
        print(", ".join(x for x in stats if x))
        if not short:
            # print ids found in Couch but not in SQL
            i = 0
            missing_ids = statedb.iter_missing_doc_ids(doc_type)
            for i, missing_id in enumerate(missing_ids, start=1):
                print(missing_id)
            assert i == counts.missing, (i, counts.missing)
        return has_diffs


def _confirm(message):
    response = input('{} [y/N]'.format(message)).lower()
    if response != 'y':
        raise CommandError('abort')


def blow_away_migration(domain, state_dir):
    assert not should_use_sql_backend(domain)
    delete_state_db(domain, state_dir)

    log.info("deleting forms...")
    for form_ids in iter_chunks(XFormInstanceSQL, "form_id", domain):
        FormAccessorSQL.hard_delete_forms(domain, form_ids, delete_attachments=False)

    log.info("deleting cases...")
    for case_ids in iter_chunks(CommCareCaseSQL, "case_id", domain):
        CaseAccessorSQL.hard_delete_cases(domain, case_ids)

    log.info("blew away migration for domain %s\n", domain)


def iter_chunks(model_class, field, domain, chunk_size=5000):
    where = Q(domain=domain)
    row_count = estimate_partitioned_row_count(model_class, where)
    rows = paginate_query_across_partitioned_databases(
        model_class,
        where,
        values=[field],
        load_source='couch_to_sql_migration',
        query_size=chunk_size,
    )
    values = (r[0] for r in rows)
    values = with_progress_bar(values, row_count, oneline="concise")
    yield from chunked(values, chunk_size, list)
