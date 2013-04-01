from django.conf import settings
from corehq.apps.commtrack.const import RequisitionActions
from corehq.apps.domain.models import Domain
from casexml.apps.case.models import CommCareCase
from corehq.apps.locations.models import Location
from corehq.apps.commtrack import stockreport, const
from corehq.apps.sms.api import send_sms_to_verified_number
from lxml import etree
import logging
from dimagi.utils.couch.loosechange import map_reduce
from dimagi.utils.parsing import json_format_datetime
from datetime import datetime
from helpers import make_supply_point_product
from corehq.apps.commtrack.util import get_supply_point
from corehq.apps.commtrack.models import Product, CommtrackConfig
import settings

logger = logging.getLogger('commtrack.sms')

def handle(verified_contact, text):
    """top-level handler for incoming stock report messages"""
    domain = Domain.get_by_name(verified_contact.domain)
    if not domain.commtrack_enabled:
        return False

    try:
        data = StockReportHelper(domain, verified_contact).parse(text.lower())
        if not data:
            return False
    except Exception, e:
        if settings.UNIT_TESTING:
            raise
        send_sms_to_verified_number(verified_contact, 'problem with stock report: %s' % str(e))
        return True

    process(domain.name, data)
    send_confirmation(verified_contact, data)
    return True

def process(domain, data):
    logger.debug(data)
    inst_xml = to_instance(data)
    logger.debug(inst_xml)
    
    stockreport.process(domain, inst_xml)

class StockReportHelper(object):
    """a helper object for parsing raw stock report texts"""

    def __init__(self, domain, v):
        self.domain = domain
        self.v = v
        self.C = domain.commtrack_settings

    # TODO sms parsing could really use unit tests
    def parse(self, text, location=None):
        """take in a text and return the parsed stock transactions"""
        args = text.split()

        if args[0] in self.C.all_keywords():
            # single action sms
            # TODO: support single-action by product, as well as by action?
            action_name = self.C.all_keywords()[args[0]]
            action = self.C.all_actions_by_name[action_name]
            args = args[1:]

            if not location:
                location = self.location_from_code(args[0])
                args = args[1:]
        
            _tx = self.single_action_transactions(action, args)

        elif self.C.multiaction_enabled and (self.C.multiaction_keyword is None or args[0] == self.C.multiaction_keyword.lower()):
            # multiple action sms
            if self.C.multiaction_keyword:
                args = args[1:]

            if not location:
                location = self.location_from_code(args[0])
                args = args[1:]

            _tx = self.multiple_action_transactions(args)

        else:
            # initial keyword not recognized; delegate to another handler
            return None

        tx = list(_tx)
        if not tx:
            raise RuntimeError("stock report doesn't have any transactions")

        return {
            'timestamp': datetime.utcnow(),
            'user': self.v.owner,
            'phone': self.v.phone_number,
            'location': location,
            'transactions': tx,
        }

    def single_action_transactions(self, action, args):
        # special case to handle immediate stock-out reports
        if action.action_type == 'stockout':
            if all(looks_like_prod_code(arg) for arg in args):
                for prod_code in args:
                    yield ProductTransaction(self.product_from_code(prod_code), action.name, 0)
                return
            else:
                raise RuntimeError("can't include a quantity for stock-out action")

        if action.action_type in [RequisitionActions.APPROVAL, RequisitionActions.FILL] and not args:
            # these two actions can be submitted generically without specifying products or amounts
            yield BulkTransaction(action.name)

        grouping_allowed = (action.action_type == 'stockedoutfor')

        products = []
        for arg in args:
            if looks_like_prod_code(arg):
                products.append(self.product_from_code(arg))
            else:
                if not products:
                    raise RuntimeError('quantity "%s" doesn\'t have a product' % arg)
                if len(products) > 1 and not grouping_allowed:
                    raise RuntimeError('missing quantity for product "%s"' % products[-1].code)

                try:
                    value = int(arg)
                except:
                    raise RuntimeError('could not understand product quantity "%s"' % arg)

                for p in products:
                    yield ProductTransaction(p, action.name, value)
                products = []
        if products:
            raise RuntimeError('missing quantity for product "%s"' % products[-1].code)

    def multiple_action_transactions(self, args):
        action = None
        action_code = None
        product = None

        _args = iter(args)
        def next():
            return _args.next()

        found_product_for_action = True
        while True:
            try:
                keyword = next()
            except StopIteration:
                if not found_product_for_action:
                    raise RuntimeError('product expected for action "%s"' % action_code)
                break

            try:
                old_action_code = action_code
                action, action_code = self.C.keywords(multi=True)[keyword], keyword
                if not found_product_for_action:
                    raise RuntimeError('product expected for action "%s"' % old_action_code)
                found_product_for_action = False
                continue
            except KeyError:
                pass

            try:
                product = self.product_from_code(keyword)
                found_product_for_action = True
            except:
                product = None
            if product:
                if not action:
                    raise RuntimeError('need to specify an action before product')
                elif action == 'stockout':
                    value = 0
                else:
                    try:
                        value = int(next())
                    except (ValueError, StopIteration):
                        raise RuntimeError('quantity expected for product "%s"' % product.code)

                yield ProductTransaction(product, action, value)
                continue

            raise RuntimeError('do not recognize keyword "%s"' % keyword)

            
    def location_from_code(self, loc_code):
        """return the supply point case referenced by loc_code"""
        result = get_supply_point(self.domain.name, loc_code)['case']
        if not result:
            raise RuntimeError('invalid location code "%s"' % loc_code)
        return result

    def product_from_code(self, prod_code):
        """return the product doc referenced by prod_code"""
        prod_code = prod_code.lower()
        p = Product.get_by_code(self.domain.name, prod_code)
        if p is None:
            raise RuntimeError('invalid product code "%s"' % prod_code)
        return p

class SmsTransaction(object):

    def __init__(self, action, value, inferred=False):
        self.action = action
        self.value = value
        self.inferred = inferred

    @property
    def product_id(self):
        raise NotImplemented()

    def to_xml(self, E=None, **kwargs):
        raise NotImplemented()

class ProductTransaction(SmsTransaction):
    """
    An SMS transaction helper class that represents a product and a value
    """
    def __init__(self, product, action, value, inferred=False):
        super(ProductTransaction, self).__init__(action, value, inferred)
        self.product = product

    @property
    def product_id(self):
        return self.product._id

    def to_xml(self, E=None, case_id=None):
        if not E:
            E = stockreport.XML()

        assert case_id is not None
        attr = {}
        if self.inferred:
            attr['inferred'] = 'true'

        return E.transaction(
            E.product(self.product_id),
            E.product_entry(case_id),
            E.action(self.action),
            E.value(str(self.value)),
            **attr
        )

class BulkTransaction(SmsTransaction):
    """
    An SMS transaction helper class that represents a product and a value
    """
    def __init__(self, action):
        super(SmsTransaction, self).__init__(action, None, False)

    @property
    def product_id(self):
        return const.ALL_PRODUCTS_TRANSACTION_TAG

    def to_xml(self, E=None):
        if not E:
            E = stockreport.XML()

        return E.transaction(
            E.product(self.product_id),
            E.action(self.action),
        )


def looks_like_prod_code(code):
    try:
        int(code)
        return False
    except:
        return True

def product_subcases(supply_point):
    """given a supply point, return all the sub-cases for each product stocked at that supply point
    actually returns a mapping: product doc id => sub-case id
    ACTUALLY returns a dict that will create non-existent product sub-cases on demand
    """
    product_subcase_uuids = [ix.referenced_id for ix in supply_point.reverse_indices if ix.identifier == const.PARENT_CASE_REF]
    product_subcases = CommCareCase.view('_all_docs', keys=product_subcase_uuids, include_docs=True)
    product_subcase_mapping = dict((subcase.dynamic_properties().get('product'), subcase._id) for subcase in product_subcases)

    def create_product_subcase(product_uuid):
        return make_supply_point_product(supply_point, product_uuid)._id

    class DefaultDict(dict):
        """similar to collections.defaultdict(), but factory function has access
        to 'key'
        """
        def __init__(self, factory, *args, **kwargs):
            super(DefaultDict, self).__init__(*args, **kwargs)
            self.factory = factory

        def __getitem__(self, key):
            if key in self:
                val = self.get(key)
            else:
                val = self.factory(key)
                self[key] = val
            return val

    return DefaultDict(create_product_subcase, product_subcase_mapping)

def to_instance(data):
    """convert the parsed sms stock report into an instance like what would be
    submitted from a commcare phone"""
    E = stockreport.XML()
    M = stockreport.XML(stockreport.META_XMLNS, 'jrm')

    product_subcase_mapping = product_subcases(data['location'])

    def mk_xml_tx(tx):
        # this is ugly but if we want to take advantage of the product_subcase_mapping
        # it's necessary unless we want to make the api even uglier
        def _kwargs(tx):
            def product_kwargs():
                return {
                    'case_id': product_subcase_mapping.get(tx.product_id, None)
                }

            bulk_kwargs = lambda: {}

            return {
                ProductTransaction: product_kwargs,
                BulkTransaction: bulk_kwargs,
            }[tx.__class__]()
        return tx.to_xml(E, **_kwargs(tx))

    deviceID = ''
    if data.get('phone'):
        deviceID = 'sms:%s' % data['phone']
    timestamp = json_format_datetime(data['timestamp'])

    root = E.stock_report(
        M.meta(
            M.userID(data['user']._id),
            M.deviceID(deviceID),
            M.timeStart(timestamp),
            M.timeEnd(timestamp)
        ),
        E.location(data['location']._id),
        *[mk_xml_tx(tx) for tx in data['transactions']]
    )

    return etree.tostring(root, encoding='utf-8', pretty_print=True)

def truncate(text, maxlen, ellipsis='...'):
    if len(text) > maxlen:
        return text[:maxlen-len(ellipsis)] + ellipsis
    else:
        return text

def send_confirmation(v, data):
    C = CommtrackConfig.for_domain(v.domain)

    static_loc = Location.get(data['location'].location_[-1])
    location_name = static_loc.name

    action_to_code = dict((v, k) for k, v in C.all_keywords().iteritems())
    tx_by_action = map_reduce(lambda tx: [(tx.action,)], data=data['transactions'], include_docs=True)
    def summarize_action(action, txs):
        def fragment(tx):
            quantity = tx.value if tx.value is not None else ''
            return '%s%s' % (tx.product.code.lower(), quantity)
        return '%s %s' % (action_to_code[action].upper(), ' '.join(sorted(fragment(tx) for tx in txs)))

    msg = 'received stock report for %s(%s) %s' % (
        static_loc.site_code,
        truncate(location_name, 20),
        ' '.join(sorted(summarize_action(a, txs) for a, txs in tx_by_action.iteritems()))
    )

    send_sms_to_verified_number(v, msg)
