"""
couch models go here
"""
from __future__ import absolute_import
from datetime import datetime
from django.contrib.auth.models import User
from django.db import models
from djangocouchuser.models import CouchUserProfile
from couchdbkit.ext.django.schema import *
from couchdbkit.schema.properties_proxy import SchemaListProperty
from djangocouch.utils import model_to_doc
from corehq.apps.domain.models import Domain

COUCH_USER_AUTOCREATED_STATUS = 'autocreated'

class DjangoUser(Document):
    id = IntegerProperty()
    username = StringProperty()
    first_name = StringProperty()
    last_name = StringProperty()
    django_type = StringProperty()
    is_active = BooleanProperty()
    email = StringProperty()
    is_superuser = BooleanProperty()
    is_staff = BooleanProperty()
    last_login = DateTimeProperty()
    groups = ListProperty()
    user_permissions = ListProperty()
    password = StringProperty()
    date_joined = DateTimeProperty()
        
    class Meta:
        app_label = 'users'

class DomainMembership(Document):
    """
    Each user can have multiple accounts on the 
    web domain. This is primarily for Dimagi staff.
    """
    domain = StringProperty()
    permissions = StringListProperty()
    last_login = DateTimeProperty()
    date_joined = DateTimeProperty()
    
    class Meta:
        app_label = 'users'

class CommCareAccount(Document):
    """
    This is the information associated with a 
    particular commcare user. Right now, we 
    associate one commcare user to one web user
    (with multiple domain logins, phones, SIMS)
    but we could always extend to multiple commcare
    users if desired later.
    """
    # django_user should always be created when registering a commcare account properly
    # however, sometimes we autocreate users (e.g. when getting a form from an unknown user)
    # in which case, we don't know the password and can't create django_user
    django_user = SchemaProperty(DjangoUser) # null = True
    UUID = StringProperty()
    registering_phone_id = StringProperty()
    user_data = DictProperty()
    domain = StringProperty()
    
    class Meta:
        app_label = 'users'

class PhoneDevice(Document):
    """
    This is a physical device with a unique IMEI
    Note, though, that the same physical device can 
    be used with multiple SIM cards (and multiple phone numbers)
    """
    is_default = BooleanProperty()
    IMEI = StringProperty()
    created = DateTimeProperty()
    
    class Meta:
        app_label = 'users'

class PhoneNumber(Document):
    """
    This is the SIM card with a unique phone number
    The same SIM card can be used in multiple phone
    devices
    """
    is_default = BooleanProperty()
    number = StringProperty()
    created = DateTimeProperty()
    
    class Meta:
        app_label = 'users'

class CouchUser(Document):
    """
    a user (for web+commcare+sms)
    can be associated with multiple usename/password/login tuples
    can be associated with multiple phone numbers/SIM cards
    can be associated with multiple phones/device IDs
    """
    # not used e.g. when user is only a commcare user
    django_user = SchemaProperty(DjangoUser) # null = True
    domain_memberships = SchemaListProperty(DomainMembership)
    commcare_accounts = SchemaListProperty(CommCareAccount)
    phone_devices = SchemaListProperty(PhoneDevice)
    phone_numbers = SchemaListProperty(PhoneNumber)
    created_on = DateTimeProperty()
    is_duplicate = BooleanProperty(default=False)
    """
    For now, 'status' is things like:
        ('auto_created',     'Automatically created from form submission.'),   
        ('phone_registered', 'Registered from phone'),    
        ('site_edited',     'Manually added or edited from the HQ website.'),        
    """
    status = StringProperty()

    _user = None
    _user_checked = False

    class Meta:
        app_label = 'users'
        
    @property
    def username(self):
        # first choice: web user login
        if self.django_user.username:
            return self.django_user.username
        # second choice: latest registered commcare account
        if len(self.commcare_accounts)>0:
            return self.commcare_accounts[-1].django_user.username
        # third choice: phone number
        if len(self.phone_numbers)>0:
            return self.phone_numbers[-1].number
        # failing all else, default to global uuid
        return self.get_id
        
    def save(self, *args, **kwargs):
        self.django_type = "users.hquserprofile"
        super(CouchUser, self).save(*args, **kwargs) # Call the "real" save() method.
    
    def delete(self, *args, **kwargs):
        try:
            django_user = self.get_django_user()
            django_user.delete()
        except User.DoesNotExist:
            pass
        super(CouchUser, self).delete(*args, **kwargs) # Call the "real" save() method.
    
    def add_django_user(self, username, password, **kwargs):
        # DO NOT implement this. It will create an endless loop.
        raise NotImplementedError

    def get_django_user(self): 
        return User.objects.get(id = self.django_user.id)

    def add_domain_membership(self, domain, **kwargs):
        for d in self.domain_memberships:
            if d.domain == domain:
                # membership already exists
                return
        self.domain_memberships.append(DomainMembership(domain = domain,
                                                        **kwargs))
    
    @property
    def domain_names(self):
        return [dm.domain for dm in self.domain_memberships]

    def get_active_domains(self):
        return Domain.objects.filter(name__in=self.domain_names)

    def is_member_of(self, domain_qs):
        membership_count = domain_qs.filter(name__in=self.domain_names).count()
        if membership_count > 0:
            return True
        return False

    def create_commcare_user(self, domain, username, password, uuid='', imei='', date='', **kwargs):
        def _create_commcare_django_user_from_registration_data(domain, username, password):
            """ 
            From registration xml data, automatically build a django user
            Note that we set the 'is_commcare_user' flag here, so that we don't
            auto-generate the couch user
            """
            user = User()
            user.username = username
            user.set_password(password)
            user.first_name = ''
            user.last_name  = ''
            user.email = ""
            user.is_staff = False # Can't log in to admin site
            user.is_active = True # Activated upon receipt of confirmation
            user.is_superuser = False # Certainly not, although this makes login sad
            user.last_login =  datetime(1970,1,1)
            user.date_joined = datetime.utcnow()
            user.is_commcare_user = True
            return user
        def _add_commcare_account(django_user, domain, UUID, registering_phone_id, **kwargs):
            """ 
            This function expects a real django user.
            Note that it doesn't create a django user.
            """
            for commcare_account in self.commcare_accounts:
                if commcare_account.django_user.id == django_user.id  and \
                   commcare_account.domain == domain and \
                   commcare_account.UUID == UUID and \
                   commcare_account.registering_phone_id == registering_phone_id:
                        # already exists
                        return
            django_user_doc = model_to_doc(django_user)
            commcare_account = CommCareAccount(django_user=django_user_doc,
                                               domain=domain,
                                               UUID=UUID,
                                               registering_phone_id=registering_phone_id,
                                               **kwargs)
            self.commcare_accounts.append(commcare_account)
        commcare_django_user =_create_commcare_django_user_from_registration_data(domain, username, password)
        commcare_django_user.save()
        _add_commcare_account(commcare_django_user, domain, uuid, imei, date_registered = date, **kwargs)

    def add_commcare_username(self, domain, username, uuid, **kwargs):
        """
        This function doesn't set up a full commcare account, it only  has the username
        This is placeholder, used when we receive xforms from a user who hasn't registered
        and so for whom we don't have a password. This needs to be manually linked to
        a commcare/django account asap.
        """
        django_user = DjangoUser(username=username)
        for commcare_account in self.commcare_accounts:
            if commcare_account.domain == domain and commcare_account.django_user.username == username:
                return
        commcare_account = CommCareAccount(django_user=django_user,
                                           domain=domain,
                                           UUID=uuid, 
                                           **kwargs)
        self.commcare_accounts.append(commcare_account)
       
    def link_commcare_account(self, domain, from_couch_user_id, commcare_username, **kwargs):
        from_couch_user = CouchUser.get(from_couch_user_id)
        for i in range(0, len(from_couch_user.commcare_accounts)):
            if from_couch_user.commcare_accounts[i].django_user.username == commcare_username:
                # this generates a 'document update conflict'. why?
                self.commcare_accounts.append(from_couch_user.commcare_accounts[i])
                self.save()
                del from_couch_user.commcare_accounts[i]
                from_couch_user.save()
                return
    
    def unlink_commcare_account(self, domain, commcare_user_index, **kwargs):
        commcare_user_index = int(commcare_user_index)
        c = CouchUser()
        c.created_on = datetime.now()
        original = self.commcare_accounts[commcare_user_index]
        c.commcare_accounts.append(original)
        c.status = 'unlinked from %s' % self._id
        c.save()
        # is there a more atomic way to do this?
        del self.commcare_accounts[commcare_user_index]
        self.save()
        
    def add_phone_device(self, IMEI, default=False, **kwargs):
        """ Don't add phone devices if they already exist """
        for device in self.phone_devices:
            if device.IMEI == IMEI:
                return
        self.phone_devices.append(PhoneDevice(IMEI=IMEI,
                                              default=default,
                                              **kwargs))
    
    def add_phone_number(self, number, default=False, **kwargs):
        """ Don't add phone numbers if they already exist """
        if not isinstance(number,basestring):
            number = str(number)
        for phone in self.phone_numbers:
            if phone.number == number:
                return
        self.phone_numbers.append(PhoneNumber(number=number,
                                              default=default,
                                              **kwargs))

    def get_phone_numbers(self):
        return [phone.number for phone in self.phone_numbers if phone.number]
    
    def default_phone_number(self):
        for phone_number in self.phone_numbers:
            if phone_number.is_default:
                return phone_number.number
        # if no default set, default to the last number added
        return self.phone_numbers[-1].number
    
    @property
    def couch_id(self):
        return self._id

class PhoneUser(Document):
    """A wrapper for response returned by phone_users_by_domain, etc."""
    id = StringProperty()
    name = StringProperty()
    phone_number = StringProperty()

"""
Django  models go here
"""

class HqUserProfile(CouchUserProfile):
    """
    The CoreHq Profile object, which saves the user data in couch along
    with annotating whatever additional fields we need for Hq
    (Right now, none additional are required)
    """
    is_commcare_user = models.BooleanField(default=False)

    class Meta:
        app_label = 'users'
    
    def __unicode__(self):
        return "%s @ %s" % (self.user)
        
    def get_couch_user(self):
        # This caching could be useful, but for now we leave it out since
        # we don't yet have a good intelligent refresh mechanism
        # def get_couch_user(self, force_update=False):
        #if not hasattr(self,'couch_user') or force_update:
        #    self.couch_user = CouchUser.get(self._id)
        #return self.couch_user
        return CouchUser.get(self._id)
    

def create_hq_user_from_commcare_registration(domain, username, password, uuid='', imei='', date='', **kwargs):
    """
    This alias is just to improve readability
    """
    couch_user = create_commcare_user_without_web_user(domain, username, password, uuid, imei, date)
    return couch_user

def create_commcare_user_without_web_user(domain, username, password, uuid='', imei='', date='', **kwargs):
    """ a 'commcare user' is a couch user which:
    * does not have a web user
    * does have an associated commcare account,
        * has a django account linked to the commcare account for httpdigest auth
    """
    num_couch_users = len(CouchUser.view("users/by_commcare_username_domain", 
                                         key=[username, domain]))
    # TODO: add a check for when uuid is not unique
    couch_user = CouchUser()
    if num_couch_users > 0:
        couch_user.is_duplicate = True
        couch_user.save()
    # add metadata to couch user
    couch_user.add_domain_membership(domain)
    if not date:
        date = datetime.now()
    couch_user.create_commcare_user(domain, username, password, uuid, imei, date)
    couch_user.add_phone_device(IMEI=imei)
    # TODO: fix after clarifying desired behaviour
    # if 'user_data' in xform.form: couch_user.user_data = user_data
    couch_user.save()
    return couch_user

def create_commcare_user_without_django_login(domain, username, uuid, imei='', status='', **kwargs):
    """
    This function is used when autocreating a user on form submission from an unknown user
    Note that we don't know the user's password, so we cannot create a django user for
    later authentication. This needs to be linked to a django user later.
    
    """
    c = CouchUser()
    c.created_on = datetime.now()
    c.add_commcare_username(domain, username, uuid, registering_phone_id=imei)
    c.commcare_accounts[0].user_data = kwargs
    c.add_phone_device(imei)
    c.status = status
    c.save()
    return c

# make sure our signals are loaded
import corehq.apps.users.signals
