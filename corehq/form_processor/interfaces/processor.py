import logging
from collections import namedtuple

from couchdbkit.exceptions import BulkSaveError
from redis.exceptions import RedisError

from casexml.apps.case.exceptions import IllegalCaseId
from dimagi.utils.decorators.memoized import memoized
from ..utils import should_use_sql_backend

CaseUpdateMetadata = namedtuple('CaseUpdateMetadata', ['case', 'is_creation', 'previous_owner_id'])
ProcessedForms = namedtuple('ProcessedForms', ['submitted', 'deprecated'])


class FormProcessorInterface(object):
    """
    The FormProcessorInterface serves as the base transactions that take place in forms. Different
    backends can implement this class in order to make common interface.
    """

    def __init__(self, domain=None):
        self.domain = domain

    @property
    @memoized
    def xform_model(self):
        from couchforms.models import XFormInstance
        from corehq.form_processor.models import XFormInstanceSQL

        if should_use_sql_backend(self.domain):
            return XFormInstanceSQL
        else:
            return XFormInstance

    @property
    @memoized
    def sync_log_model(self):
        from casexml.apps.phone.models import SyncLog

        if should_use_sql_backend(self.domain):
            return SyncLog
        else:
            return SyncLog

    @property
    @memoized
    def processor(self):
        from corehq.form_processor.backends.couch.processor import FormProcessorCouch
        from corehq.form_processor.backends.sql.processor import FormProcessorSQL

        if should_use_sql_backend(self.domain):
            return FormProcessorSQL
        else:
            return FormProcessorCouch

    @property
    @memoized
    def casedb_cache(self):
        from corehq.form_processor.backends.couch.casedb import CaseDbCacheCouch
        from corehq.form_processor.backends.sql.casedb import CaseDbCacheSQL

        if should_use_sql_backend(self.domain):
            return CaseDbCacheSQL
        else:
            return CaseDbCacheCouch

    @property
    @memoized
    def ledger_processor(self):
        from corehq.form_processor.backends.couch.ledger import LedgerProcessorCouch
        from corehq.form_processor.backends.sql.ledger import LedgerProcessorSQL
        if should_use_sql_backend(self.domain):
            return LedgerProcessorSQL(domain=self.domain)
        else:
            return LedgerProcessorCouch(domain=self.domain)

    @property
    @memoized
    def ledger_db(self):
        from corehq.form_processor.backends.couch.ledger import LedgerDBCouch
        from corehq.form_processor.backends.sql.ledger import LedgerDBSQL
        if should_use_sql_backend(self.domain):
            return LedgerDBSQL()
        else:
            return LedgerDBCouch()

    def acquire_lock_for_xform(self, xform_id):
        lock = self.xform_model.get_obj_lock_by_id(xform_id, timeout_seconds=2 * 60)
        try:
            lock.acquire()
        except RedisError:
            lock = None
        return lock

    def get_case_forms(self, case_id):
        return self.processor.get_case_forms(case_id)

    def store_attachments(self, xform, attachments):
        """
        Takes a list of Attachment namedtuples with content, name, and content_type and stores them to the XForm
        """
        return self.processor.store_attachments(xform, attachments)

    def is_duplicate(self, xform_id, domain=None):
        """
        Check if there is already a form with the given ID. If domain is specified only check for
        duplicates within that domain.
        """
        if domain:
            return self.processor.is_duplicate(xform_id, domain=domain)
        else:
            # check across Couch & SQL to ensure global uniqueness
            # check this domains DB first to support existing bad data
            return self.processor.is_duplicate(xform_id) or self.other_db_processor().is_duplicate(xform_id)

    def new_xform(self, form_json):
        return self.processor.new_xform(form_json)

    def xformerror_from_xform_instance(self, instance, error_message, with_new_id=False):
        return self.processor.xformerror_from_xform_instance(instance, error_message, with_new_id=with_new_id)

    def save_processed_models(self, forms, cases=None, stock_result=None):
        forms = _list_to_processed_forms_tuple(forms)
        if stock_result:
            assert stock_result.populated
        try:
            return self.processor.save_processed_models(
                forms,
                cases=cases,
                stock_result=stock_result,
            )
        except BulkSaveError as e:
            logging.error('BulkSaveError saving forms', exc_info=1,
                          extra={'details': {'errors': e.errors}})
            raise
        except Exception as e:
            xforms_being_saved = [form.form_id for form in forms if form]
            error_message = u'Unexpected error bulk saving docs during form processing ({})'.format(
                ', '.join(xforms_being_saved),
            )
            from corehq.form_processor.submission_post import handle_unexpected_error
            handle_unexpected_error(self, forms.submitted, e, error_message)
            raise

    def hard_delete_case_and_forms(self, case, xforms):
        domain = case.domain
        all_domains = {domain} | {xform.domain for xform in xforms}
        assert len(all_domains) == 1, all_domains
        self.processor.hard_delete_case_and_forms(domain, case, xforms)

    def apply_deprecation(self, existing_xform, new_xform):
        return self.processor.apply_deprecation(existing_xform, new_xform)

    def deduplicate_xform(self, xform):
        return self.processor.deduplicate_xform(xform)

    def assign_new_id(self, xform):
        return self.processor.assign_new_id(xform)

    def hard_rebuild_case(self, case_id, detail):
        return self.processor.hard_rebuild_case(self.domain, case_id, detail)

    def get_cases_from_forms(self, case_db, xforms):
        """
        Returns a dict of case_ids to CaseUpdateMetadata objects containing the touched cases
        (with the forms' updates already applied to the cases)
        """
        return self.processor.get_cases_from_forms(case_db, xforms)

    def submission_error_form_instance(self, instance, message):
        return self.processor.submission_error_form_instance(self.domain, instance, message)

    def get_case_with_lock(self, case_id, lock=False, strip_history=False, wrap=False):
        """
        Get a case with option lock. If case not found in domains DB also check other DB
        and raise IllegalCaseId if found.

        :param case_id: ID of case to fetch
        :param lock: Get a Redis lock for the case. Returns None if False or case not found.
        :param strip_history: If False, don't include case actions. (Couch only)
        :param wrap: Return wrapped case if True. (Couch only)
        :return: tuple(case, lock). Either could be None
        :raises: IllegalCaseId
        """
        # check across Couch & SQL to ensure global uniqueness
        # check this domains DB first to support existing bad data
        from corehq.apps.couch_sql_migration.progress import couch_sql_migration_in_progress

        case, lock = self.processor.get_case_with_lock(case_id, lock, strip_history, wrap)
        if case:
            return case, lock

        if not couch_sql_migration_in_progress(self.domain):
            # during migration we're copying from one DB to the other so this check will always fail
            case, _ = self.other_db_processor().get_case_with_lock(
                case_id, lock=False, strip_history=True, wrap=False
            )
            if case:
                # case exists in other database
                raise IllegalCaseId("Bad case id")

        return case, lock

    def other_db_processor(self):
        """Get the processor for the database not used by this domain."""
        from corehq.form_processor.backends.sql.processor import FormProcessorSQL
        from corehq.form_processor.backends.couch.processor import FormProcessorCouch
        (other_processor,) = {FormProcessorSQL, FormProcessorCouch} - {self.processor}
        return other_processor


def _list_to_processed_forms_tuple(forms):
    """
    :param forms: List of forms (either 1 or 2)
    :return: tuple with main form first and deprecated form second
    """
    if len(forms) == 1:
        return ProcessedForms(forms[0], None)
    else:
        assert len(forms) == 2
        return ProcessedForms(*sorted(forms, key=lambda form: form.is_deprecated))
