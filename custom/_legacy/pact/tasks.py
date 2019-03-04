from __future__ import absolute_import
from __future__ import unicode_literals
import traceback
from datetime import datetime

from celery.task import task
import json

from couchforms.models import XFormInstance
from dimagi.utils.logging import notify_exception
from pact.enums import PACT_DOTS_DATA_PROPERTY
from pact.utils import get_case_id
import six


DOT_RECOMPUTE = True


@task(serializer='pickle', ignore_result=True)
def recalculate_dots_data(case_id, cc_user, sync_token=None):
    """
    Recalculate the dots data and resubmit calling the pact api for dot recompute
    """
    from pact.models import PactPatientCase
    from pact.api import recompute_dots_casedata
    if DOT_RECOMPUTE:
        try:
            casedoc = PactPatientCase.get(case_id)
            recompute_dots_casedata(casedoc, cc_user, sync_token=sync_token)
        except Exception as ex:
            tb = traceback.format_exc()
            notify_exception(None, message="PACT error recomputing DOTS case block: %s\n%s" % (ex, tb))


@task(serializer='pickle', ignore_result=True)
def eval_dots_block(xform_json, callback=None):
    """
    Evaluate the dots block in the xform submission and put it in the computed_ block for the xform.
    """
    case_id = get_case_id(xform_json)
    do_continue = False

    #first, set the pact_data to json if the dots update stuff is there.
    try:
        if 'processed' in xform_json.get(PACT_DOTS_DATA_PROPERTY, {}):
            #already processed, skipping
            return

        xform_json[PACT_DOTS_DATA_PROPERTY] = {}
        if not isinstance(xform_json['form']['case'].get('update', None), dict):
            #no case update property, skipping
            pass
        else:
            #update is a dict
            if 'dots' in xform_json['form']['case']['update']:
                dots_json = xform_json['form']['case']['update']['dots']
                if isinstance(dots_json, str) or isinstance(dots_json, six.text_type):
                    json_data = json.loads(dots_json)
                    xform_json[PACT_DOTS_DATA_PROPERTY]['dots'] = json_data
                do_continue=True
            else:
                #no dots data in doc
                pass
        xform_json[PACT_DOTS_DATA_PROPERTY]['processed']=datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        XFormInstance.get_db().save_doc(xform_json)

    except Exception as ex:
        #if this gets triggered, that's ok because web entry don't got them
        tb = traceback.format_exc()
        notify_exception(None, message="PACT error evaluating DOTS block docid %s, %s\n\tTraceback: %s" % (xform_json['_id'], ex, tb))


# Originally there was a task here:
# @periodic_task(run_every=crontab(hour="12", minute="0", day_of_week="*"))
# def update_schedule_case_properties():
# This comment is a tombstone for someone later trying to resolve a mystery.
# This task had been failing once a day for nearly a full year
# starting on Mar 28, 2018 8:00:08 AM EDT.
# When getting rid of the long-broken cases_get_lite couch design doc,
# Danny decided to remove this failing task entirely,
# which should have the same effect as running once a day and failing once a day.
