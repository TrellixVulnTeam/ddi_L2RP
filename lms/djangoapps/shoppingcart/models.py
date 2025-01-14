""" Models for the shopping cart and assorted purchase types """

from collections import namedtuple
from datetime import datetime
from decimal import Decimal
import pytz
import logging
import smtplib
import StringIO
import csv
from courseware.courses import get_course_by_id
from boto.exception import BotoServerError  # this is a super-class of SESError and catches connection errors
from django.dispatch import receiver
from django.db import models
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import send_mail
from django.contrib.auth.models import User
from django.utils.translation import ugettext as _
from django.db import transaction
from django.db.models import Sum
from django.core.urlresolvers import reverse
from model_utils.managers import InheritanceManager
from django.core.mail.message import EmailMessage

from xmodule.modulestore.django import modulestore

from config_models.models import ConfigurationModel
from course_modes.models import CourseMode
from edxmako.shortcuts import render_to_string
from student.models import CourseEnrollment, UNENROLL_DONE
from util.query import use_read_replica_if_available
from xmodule_django.models import CourseKeyField

from verify_student.models import SoftwareSecurePhotoVerification

from .exceptions import (
    InvalidCartItem, PurchasedCallbackException, ItemAlreadyInCartException,
    AlreadyEnrolledInCourseException, CourseDoesNotExistException,
    MultipleCouponsNotAllowedException, RegCodeAlreadyExistException,
    ItemDoesNotExistAgainstRegCodeException
)

from microsite_configuration import microsite

log = logging.getLogger("shoppingcart")

ORDER_STATUSES = (
    # The user is selecting what he/she wants to purchase.
    ('cart', 'cart'),

    # The user has been sent to the external payment processor.
    # At this point, the order should NOT be modified.
    # If the user returns to the payment flow, he/she will start a new order.
    ('paying', 'paying'),

    # The user has successfully purchased the items in the order.
    ('purchased', 'purchased'),

    # The user's order has been refunded.
    ('refunded', 'refunded'),
)

# we need a tuple to represent the primary key of various OrderItem subclasses
OrderItemSubclassPK = namedtuple('OrderItemSubclassPK', ['cls', 'pk'])  # pylint: disable=C0103


class OrderTypes(object):
    """
    This class specify purchase OrderTypes.
    """
    PERSONAL = 'personal'
    BUSINESS = 'business'

    ORDER_TYPES = (
        (PERSONAL, 'personal'),
        (BUSINESS, 'business'),
    )


class Order(models.Model):
    """
    This is the model for an order.  Before purchase, an Order and its related OrderItems are used
    as the shopping cart.
    FOR ANY USER, THERE SHOULD ONLY EVER BE ZERO OR ONE ORDER WITH STATUS='cart'.
    """
    user = models.ForeignKey(User, db_index=True)
    currency = models.CharField(default="usd", max_length=8)  # lower case ISO currency codes
    status = models.CharField(max_length=32, default='cart', choices=ORDER_STATUSES)
    purchase_time = models.DateTimeField(null=True, blank=True)
    refunded_time = models.DateTimeField(null=True, blank=True)
    # Now we store data needed to generate a reasonable receipt
    # These fields only make sense after the purchase
    bill_to_first = models.CharField(max_length=64, blank=True)
    bill_to_last = models.CharField(max_length=64, blank=True)
    bill_to_street1 = models.CharField(max_length=128, blank=True)
    bill_to_street2 = models.CharField(max_length=128, blank=True)
    bill_to_city = models.CharField(max_length=64, blank=True)
    bill_to_state = models.CharField(max_length=8, blank=True)
    bill_to_postalcode = models.CharField(max_length=16, blank=True)
    bill_to_country = models.CharField(max_length=64, blank=True)
    bill_to_ccnum = models.CharField(max_length=8, blank=True)  # last 4 digits
    bill_to_cardtype = models.CharField(max_length=32, blank=True)
    # a JSON dump of the CC processor response, for completeness
    processor_reply_dump = models.TextField(blank=True)

    # bulk purchase registration code workflow billing details
    company_name = models.CharField(max_length=255, null=True, blank=True)
    company_contact_name = models.CharField(max_length=255, null=True, blank=True)
    company_contact_email = models.CharField(max_length=255, null=True, blank=True)
    recipient_name = models.CharField(max_length=255, null=True, blank=True)
    recipient_email = models.CharField(max_length=255, null=True, blank=True)
    customer_reference_number = models.CharField(max_length=63, null=True, blank=True)
    order_type = models.CharField(max_length=32, default='personal', choices=OrderTypes.ORDER_TYPES)

    @classmethod
    def get_cart_for_user(cls, user):
        """
        Always use this to preserve the property that at most 1 order per user has status = 'cart'
        """
        # find the newest element in the db
        try:
            cart_order = cls.objects.filter(user=user, status='cart').order_by('-id')[:1].get()
        except ObjectDoesNotExist:
            # if nothing exists in the database, create a new cart
            cart_order, _created = cls.objects.get_or_create(user=user, status='cart')
        return cart_order

    @classmethod
    def user_cart_has_items(cls, user, item_types=None):
        """
        Returns true if the user (anonymous user ok) has
        a cart with items in it.  (Which means it should be displayed.
        If a item_type is passed in, then we check to see if the cart has at least one of
        those types of OrderItems
        """
        if not user.is_authenticated():
            return False
        cart = cls.get_cart_for_user(user)

        if not item_types:
            # check to see if the cart has at least some item in it
            return cart.has_items()
        else:
            # if the caller is explicitly asking to check for particular types
            for item_type in item_types:
                if cart.has_items(item_type):
                    return True

        return False

    @property
    def total_cost(self):
        """
        Return the total cost of the cart.  If the order has been purchased, returns total of
        all purchased and not refunded items.
        """
        return sum(i.line_cost for i in self.orderitem_set.filter(status=self.status))  # pylint: disable=E1101

    def has_items(self, item_type=None):
        """
        Does the cart have any items in it?
        If an item_type is passed in then we check to see if there are any items of that class type
        """
        if not item_type:
            return self.orderitem_set.exists()  # pylint: disable=E1101
        else:
            items = self.orderitem_set.all().select_subclasses()  # pylint: disable=E1101
            for item in items:
                if isinstance(item, item_type):
                    return True
            return False

    def reset_cart_items_prices(self):
        """
        Reset the items price state in the user cart
        """
        for item in self.orderitem_set.all():  # pylint: disable=E1101
            if item.list_price:
                item.unit_cost = item.list_price
                item.list_price = None
                item.save()

    def clear(self):
        """
        Clear out all the items in the cart
        """
        self.orderitem_set.all().delete()  # pylint: disable=E1101

    @transaction.commit_on_success
    def start_purchase(self):
        """
        Start the purchase process.  This will set the order status to "paying",
        at which point it should no longer be modified.

        Future calls to `Order.get_cart_for_user()` will filter out orders with
        status "paying", effectively creating a new (empty) cart.
        """
        if self.status == 'cart':
            self.status = 'paying'
            self.save()

            for item in OrderItem.objects.filter(order=self).select_subclasses():
                item.start_purchase()

    def update_order_type(self):
        """
        updating order type. This method wil inspect the quantity associated with the OrderItem.
        In the application, it is implied that when qty > 1, then the user is to purchase
        'RegistrationCodes' which are randomly generated strings that users can distribute to
        others in order for them to enroll in paywalled courses.

        The UI/UX may change in the future to make the switching between PaidCourseRegistration
        and CourseRegCodeItems a more explicit UI gesture from the purchaser
        """
        cart_items = self.orderitem_set.all()  # pylint: disable=E1101
        is_order_type_business = False
        for cart_item in cart_items:
            if cart_item.qty > 1:
                is_order_type_business = True

        items_to_delete = []
        if is_order_type_business:
            for cart_item in cart_items:
                if hasattr(cart_item, 'paidcourseregistration'):
                    CourseRegCodeItem.add_to_order(self, cart_item.paidcourseregistration.course_id, cart_item.qty)
                    items_to_delete.append(cart_item)
        else:
            for cart_item in cart_items:
                if hasattr(cart_item, 'courseregcodeitem'):
                    PaidCourseRegistration.add_to_order(self, cart_item.courseregcodeitem.course_id)
                    items_to_delete.append(cart_item)
                    # CourseRegCodeItem.add_to_order

        for item in items_to_delete:
            item.delete()

        self.order_type = OrderTypes.BUSINESS if is_order_type_business else OrderTypes.PERSONAL
        self.save()

    def generate_registration_codes_csv(self, orderitems, site_name):
        """
        this function generates the csv file
        """
        course_info = []
        csv_file = StringIO.StringIO()
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Course Name', 'Registration Code', 'URL'])
        for item in orderitems:
            course_id = item.course_id
            course = get_course_by_id(getattr(item, 'course_id'), depth=0)
            registration_codes = CourseRegistrationCode.objects.filter(course_id=course_id, order=self)
            course_info.append((course.display_name, ' (' + course.start_date_text + '-' + course.end_date_text + ')'))
            for registration_code in registration_codes:
                redemption_url = reverse('register_code_redemption', args=[registration_code.code])
                url = '{base_url}{redemption_url}'.format(base_url=site_name, redemption_url=redemption_url)
                csv_writer.writerow([course.display_name, registration_code.code, url])

        return csv_file, course_info

    def send_confirmation_emails(self, orderitems, is_order_type_business, csv_file, site_name, courses_info):
        """
        send confirmation e-mail
        """
        recipient_list = [(self.user.username, getattr(self.user, 'email'), 'user')]  # pylint: disable=E1101
        if self.company_contact_email:
            recipient_list.append((self.company_contact_name, self.company_contact_email, 'company_contact'))
        joined_course_names = ""
        if self.recipient_email:
            recipient_list.append((self.recipient_name, self.recipient_email, 'email_recipient'))
            courses_names_with_dates = [course_info[0] + course_info[1] for course_info in courses_info]
            joined_course_names = " " + ", ".join(courses_names_with_dates)

        if not is_order_type_business:
            subject = _("Order Payment Confirmation")
        else:
            subject = _('Confirmation and Registration Codes for the following courses: {course_name_list}').format(
                course_name_list=joined_course_names
            )

        dashboard_url = '{base_url}{dashboard}'.format(
            base_url=site_name,
            dashboard=reverse('dashboard')
        )
        try:
            from_address = microsite.get_value(
                'email_from_address',
                settings.PAYMENT_SUPPORT_EMAIL
            )
            # send a unique email for each recipient, don't put all email addresses in a single email
            for recipient in recipient_list:
                message = render_to_string(
                    'emails/business_order_confirmation_email.txt' if is_order_type_business else 'emails/order_confirmation_email.txt',
                    {
                        'order': self,
                        'recipient_name': recipient[0],
                        'recipient_type': recipient[2],
                        'site_name': site_name,
                        'order_items': orderitems,
                        'course_names': ", ".join([course_info[0] for course_info in courses_info]),
                        'dashboard_url': dashboard_url,
                        'order_placed_by': '{username} ({email})'.format(username=self.user.username, email=getattr(self.user, 'email')),  # pylint: disable=E1101
                        'has_billing_info': settings.FEATURES['STORE_BILLING_INFO'],
                        'platform_name': microsite.get_value('platform_name', settings.PLATFORM_NAME),
                        'payment_support_email': microsite.get_value('payment_support_email', settings.PAYMENT_SUPPORT_EMAIL),
                        'payment_email_signature': microsite.get_value('payment_email_signature'),
                    }
                )
                email = EmailMessage(
                    subject=subject,
                    body=message,
                    from_email=from_address,
                    to=[recipient[1]]
                )
                email.content_subtype = "html"
                if csv_file:
                    email.attach(u'RegistrationCodesRedemptionUrls.csv', csv_file.getvalue(), 'text/csv')
                email.send()
        except (smtplib.SMTPException, BotoServerError):  # sadly need to handle diff. mail backends individually
            log.error('Failed sending confirmation e-mail for order %d', self.id)  # pylint: disable=E1101

    def purchase(self, first='', last='', street1='', street2='', city='', state='', postalcode='',
                 country='', ccnum='', cardtype='', processor_reply_dump=''):
        """
        Call to mark this order as purchased.  Iterates through its OrderItems and calls
        their purchased_callback

        `first` - first name of person billed (e.g. John)
        `last` - last name of person billed (e.g. Smith)
        `street1` - first line of a street address of the billing address (e.g. 11 Cambridge Center)
        `street2` - second line of a street address of the billing address (e.g. Suite 101)
        `city` - city of the billing address (e.g. Cambridge)
        `state` - code of the state, province, or territory of the billing address (e.g. MA)
        `postalcode` - postal code of the billing address (e.g. 02142)
        `country` - country code of the billing address (e.g. US)
        `ccnum` - last 4 digits of the credit card number of the credit card billed (e.g. 1111)
        `cardtype` - 3-digit code representing the card type used (e.g. 001)
        `processor_reply_dump` - all the parameters returned by the processor

        """
        if self.status == 'purchased':
            return
        self.status = 'purchased'
        self.purchase_time = datetime.now(pytz.utc)
        self.bill_to_first = first
        self.bill_to_last = last
        self.bill_to_city = city
        self.bill_to_state = state
        self.bill_to_country = country
        self.bill_to_postalcode = postalcode
        if settings.FEATURES['STORE_BILLING_INFO']:
            self.bill_to_street1 = street1
            self.bill_to_street2 = street2
            self.bill_to_ccnum = ccnum
            self.bill_to_cardtype = cardtype
            self.processor_reply_dump = processor_reply_dump

        # save these changes on the order, then we can tell when we are in an
        # inconsistent state
        self.save()
        # this should return all of the objects with the correct types of the
        # subclasses
        orderitems = OrderItem.objects.filter(order=self).select_subclasses()
        site_name = microsite.get_value('SITE_NAME', settings.SITE_NAME)

        if self.order_type == OrderTypes.BUSINESS:
            self.update_order_type()

        for item in orderitems:
            item.purchase_item()

        csv_file = None
        courses_info = []
        if self.order_type == OrderTypes.BUSINESS:
            #
            # Generate the CSV file that contains all of the RegistrationCodes that have already been
            # generated when the purchase has transacted
            #
            csv_file, courses_info = self.generate_registration_codes_csv(orderitems, site_name)

        self.send_confirmation_emails(orderitems, self.order_type == OrderTypes.BUSINESS, csv_file, site_name, courses_info)

    def add_billing_details(self, company_name='', company_contact_name='', company_contact_email='', recipient_name='',
                            recipient_email='', customer_reference_number=''):
        """
        This function is called after the user selects a purchase type of "Business" and
        is asked to enter the optional billing details. The billing details are updated
        for that order.

        company_name - Name of purchasing organization
        company_contact_name - Name of the key contact at the company the sale was made to
        company_contact_email - Email of the key contact at the company the sale was made to
        recipient_name - Name of the company should the invoice be sent to
        recipient_email - Email of the company should the invoice be sent to
        customer_reference_number - purchase order number of the organization associated with this Order
        """

        self.company_name = company_name
        self.company_contact_name = company_contact_name
        self.company_contact_email = company_contact_email
        self.recipient_name = recipient_name
        self.recipient_email = recipient_email
        self.customer_reference_number = customer_reference_number

        self.save()

    def generate_receipt_instructions(self):
        """
        Call to generate specific instructions for each item in the order.  This gets displayed on the receipt
        page, typically.  Instructions are something like "visit your dashboard to see your new courses".
        This will return two things in a pair.  The first will be a dict with keys=OrderItemSubclassPK corresponding
        to an OrderItem and values=a set of html instructions they generate.  The second will be a set of de-duped
        html instructions
        """
        instruction_set = set([])  # heh. not ia32 or alpha or sparc
        instruction_dict = {}
        order_items = OrderItem.objects.filter(order=self).select_subclasses()
        for item in order_items:
            item_pk_with_subclass, set_of_html = item.generate_receipt_instructions()
            instruction_dict[item_pk_with_subclass] = set_of_html
            instruction_set.update(set_of_html)
        return instruction_dict, instruction_set


class OrderItem(models.Model):
    """
    This is the basic interface for order items.
    Order items are line items that fill up the shopping carts and orders.

    Each implementation of OrderItem should provide its own purchased_callback as
    a method.
    """
    objects = InheritanceManager()
    order = models.ForeignKey(Order, db_index=True)
    # this is denormalized, but convenient for SQL queries for reports, etc. user should always be = order.user
    user = models.ForeignKey(User, db_index=True)
    # this is denormalized, but convenient for SQL queries for reports, etc. status should always be = order.status
    status = models.CharField(max_length=32, default='cart', choices=ORDER_STATUSES, db_index=True)
    qty = models.IntegerField(default=1)
    unit_cost = models.DecimalField(default=0.0, decimal_places=2, max_digits=30)
    list_price = models.DecimalField(decimal_places=2, max_digits=30, null=True)
    line_desc = models.CharField(default="Misc. Item", max_length=1024)
    currency = models.CharField(default="usd", max_length=8)  # lower case ISO currency codes
    fulfilled_time = models.DateTimeField(null=True, db_index=True)
    refund_requested_time = models.DateTimeField(null=True, db_index=True)
    service_fee = models.DecimalField(default=0.0, decimal_places=2, max_digits=30)
    # general purpose field, not user-visible.  Used for reporting
    report_comments = models.TextField(default="")

    @property
    def line_cost(self):
        """ Return the total cost of this OrderItem """
        return self.qty * self.unit_cost

    @classmethod
    def add_to_order(cls, order, *args, **kwargs):
        """
        A suggested convenience function for subclasses.

        NOTE: This does not add anything to the cart. That is left up to the
        subclasses to implement for themselves
        """
        # this is a validation step to verify that the currency of the item we
        # are adding is the same as the currency of the order we are adding it
        # to
        currency = kwargs.get('currency', 'usd')
        if order.currency != currency and order.orderitem_set.exists():
            raise InvalidCartItem(_("Trying to add a different currency into the cart"))

    @transaction.commit_on_success
    def purchase_item(self):
        """
        This is basically a wrapper around purchased_callback that handles
        modifying the OrderItem itself
        """
        self.purchased_callback()
        self.status = 'purchased'
        self.fulfilled_time = datetime.now(pytz.utc)
        self.save()

    def start_purchase(self):
        """
        Start the purchase process.  This will set the order item status to "paying",
        at which point it should no longer be modified.
        """
        self.status = 'paying'
        self.save()

    def purchased_callback(self):
        """
        This is called on each inventory item in the shopping cart when the
        purchase goes through.
        """
        raise NotImplementedError

    def generate_receipt_instructions(self):
        """
        This is called on each item in a purchased order to generate receipt instructions.
        This should return a list of `ReceiptInstruction`s in HTML string
        Default implementation is to return an empty set
        """
        return self.pk_with_subclass, set([])

    @property
    def pk_with_subclass(self):
        """
        Returns a named tuple that annotates the pk of this instance with its class, to fully represent
        a pk of a subclass (inclusive) of OrderItem
        """
        return OrderItemSubclassPK(type(self), self.pk)

    @property
    def single_item_receipt_template(self):
        """
        The template that should be used when there's only one item in the order
        """
        return 'shoppingcart/receipt.html'

    @property
    def single_item_receipt_context(self):
        """
        Extra variables needed to render the template specified in
        `single_item_receipt_template`
        """
        return {}

    @property
    def additional_instruction_text(self):
        """
        Individual instructions for this order item.

        Currently, only used for e-mails.
        """
        return ''


class Invoice(models.Model):
    """
         This table capture all the information needed to support "invoicing"
         which is when a user wants to purchase Registration Codes,
         but will not do so via a Credit Card transaction.
    """
    company_name = models.CharField(max_length=255, db_index=True)
    company_contact_name = models.CharField(max_length=255)
    company_contact_email = models.CharField(max_length=255)
    recipient_name = models.CharField(max_length=255)
    recipient_email = models.CharField(max_length=255)
    address_line_1 = models.CharField(max_length=255)
    address_line_2 = models.CharField(max_length=255, null=True)
    address_line_3 = models.CharField(max_length=255, null=True)
    city = models.CharField(max_length=255, null=True)
    state = models.CharField(max_length=255, null=True)
    zip = models.CharField(max_length=15, null=True)
    country = models.CharField(max_length=64, null=True)
    course_id = CourseKeyField(max_length=255, db_index=True)
    total_amount = models.FloatField()
    internal_reference = models.CharField(max_length=255, null=True)
    customer_reference_number = models.CharField(max_length=63, null=True)
    is_valid = models.BooleanField(default=True)


class CourseRegistrationCode(models.Model):
    """
    This table contains registration codes
    With registration code, a user can register for a course for free
    """
    code = models.CharField(max_length=32, db_index=True, unique=True)
    course_id = CourseKeyField(max_length=255, db_index=True)
    created_by = models.ForeignKey(User, related_name='created_by_user')
    created_at = models.DateTimeField(default=datetime.now(pytz.utc))
    order = models.ForeignKey(Order, db_index=True, null=True, related_name="purchase_order")
    invoice = models.ForeignKey(Invoice, null=True)

    @classmethod
    @transaction.commit_on_success
    def free_user_enrollment(cls, cart):
        """
        Here we enroll the user free for all courses available in shopping cart
        """
        cart_items = cart.orderitem_set.all().select_subclasses()
        if cart_items:
            for item in cart_items:
                CourseEnrollment.enroll(cart.user, item.course_id)
                log.info("Enrolled '{0}' in free course '{1}'"
                         .format(cart.user.email, item.course_id))  # pylint: disable=E1101
                item.status = 'purchased'
                item.save()

            cart.status = 'purchased'
            cart.purchase_time = datetime.now(pytz.utc)
            cart.save()


class RegistrationCodeRedemption(models.Model):
    """
    This model contains the registration-code redemption info
    """
    order = models.ForeignKey(Order, db_index=True, null=True)
    registration_code = models.ForeignKey(CourseRegistrationCode, db_index=True)
    redeemed_by = models.ForeignKey(User, db_index=True)
    redeemed_at = models.DateTimeField(default=datetime.now(pytz.utc), null=True)

    @classmethod
    def delete_registration_redemption(cls, user, cart):
        """
        This method delete registration redemption
        """
        reg_code_redemption = cls.objects.filter(redeemed_by=user, order=cart)
        if reg_code_redemption:
            reg_code_redemption.delete()
            log.info('Registration code redemption entry removed for user {0} for order {1}'.format(user, cart.id))

    @classmethod
    def add_reg_code_redemption(cls, course_reg_code, order):
        """
        add course registration code info into RegistrationCodeRedemption model
        """
        cart_items = order.orderitem_set.all().select_subclasses()

        for item in cart_items:
            if getattr(item, 'course_id'):
                if item.course_id == course_reg_code.course_id:
                    # If another account tries to use a existing registration code before the student checks out, an
                    # error message will appear.The reg code is un-reusable.
                    code_redemption = cls.objects.filter(registration_code=course_reg_code)
                    if code_redemption:
                        log.exception("Registration code '{0}' already used".format(course_reg_code.code))
                        raise RegCodeAlreadyExistException

                    code_redemption = RegistrationCodeRedemption(registration_code=course_reg_code, order=order, redeemed_by=order.user)
                    code_redemption.save()
                    item.list_price = item.unit_cost
                    item.unit_cost = 0
                    item.save()
                    log.info("Code '{0}' is used by user {1} against order id '{2}' "
                             .format(course_reg_code.code, order.user.username, order.id))
                    return course_reg_code

        log.warning("Course item does not exist against registration code '{0}'".format(course_reg_code.code))
        raise ItemDoesNotExistAgainstRegCodeException

    @classmethod
    def create_invoice_generated_registration_redemption(cls, course_reg_code, user):
        """
        This function creates a RegistrationCodeRedemption entry in case the registration codes were invoice generated
        and thus the order_id is missing.
        """
        code_redemption = RegistrationCodeRedemption(registration_code=course_reg_code, redeemed_by=user)
        code_redemption.save()


class SoftDeleteCouponManager(models.Manager):
    """ Use this manager to get objects that have a is_active=True """

    def get_active_coupons_query_set(self):
        """
        filter the is_active = True Coupons only
        """
        return super(SoftDeleteCouponManager, self).get_query_set().filter(is_active=True)

    def get_query_set(self):
        """
        get all the coupon objects
        """
        return super(SoftDeleteCouponManager, self).get_query_set()


class Coupon(models.Model):
    """
    This table contains coupon codes
    A user can get a discount offer on course if provide coupon code
    """
    code = models.CharField(max_length=32, db_index=True)
    description = models.CharField(max_length=255, null=True, blank=True)
    course_id = CourseKeyField(max_length=255)
    percentage_discount = models.IntegerField(default=0)
    created_by = models.ForeignKey(User)
    created_at = models.DateTimeField(default=datetime.now(pytz.utc))
    is_active = models.BooleanField(default=True)

    def __unicode__(self):
        return "[Coupon] code: {} course: {}".format(self.code, self.course_id)

    objects = SoftDeleteCouponManager()


class CouponRedemption(models.Model):
    """
    This table contain coupon redemption info
    """
    order = models.ForeignKey(Order, db_index=True)
    user = models.ForeignKey(User, db_index=True)
    coupon = models.ForeignKey(Coupon, db_index=True)

    @classmethod
    def delete_coupon_redemption(cls, user, cart):
        """
        This method delete coupon redemption
        """
        coupon_redemption = cls.objects.filter(user=user, order=cart)
        if coupon_redemption:
            coupon_redemption.delete()
            log.info('Coupon redemption entry removed for user {0} for order {1}'.format(user, cart.id))

    @classmethod
    def get_discount_price(cls, percentage_discount, value):
        """
        return discounted price against coupon
        """
        discount = Decimal("{0:.2f}".format(Decimal(percentage_discount / 100.00) * value))
        return value - discount

    @classmethod
    def add_coupon_redemption(cls, coupon, order, cart_items):
        """
        add coupon info into coupon_redemption model
        """
        is_redemption_applied = False
        coupon_redemptions = cls.objects.filter(order=order, user=order.user)
        for coupon_redemption in coupon_redemptions:
            if coupon_redemption.coupon.code != coupon.code or coupon_redemption.coupon.id == coupon.id:
                log.exception("Coupon redemption already exist for user '{0}' against order id '{1}'"
                              .format(order.user.username, order.id))
                raise MultipleCouponsNotAllowedException

        for item in cart_items:
            if getattr(item, 'course_id'):
                if item.course_id == coupon.course_id:
                    coupon_redemption = cls(order=order, user=order.user, coupon=coupon)
                    coupon_redemption.save()
                    discount_price = cls.get_discount_price(coupon.percentage_discount, item.unit_cost)
                    item.list_price = item.unit_cost
                    item.unit_cost = discount_price
                    item.save()
                    log.info("Discount generated for user {0} against order id '{1}' "
                             .format(order.user.username, order.id))
                    is_redemption_applied = True
                    return is_redemption_applied

        return is_redemption_applied


class PaidCourseRegistration(OrderItem):
    """
    This is an inventory item for paying for a course registration
    """
    course_id = CourseKeyField(max_length=128, db_index=True)
    mode = models.SlugField(default=CourseMode.DEFAULT_MODE_SLUG)

    @classmethod
    def contained_in_order(cls, order, course_id):
        """
        Is the course defined by course_id contained in the order?
        """
        return course_id in [
            item.course_id
            for item in order.orderitem_set.all().select_subclasses("paidcourseregistration")
            if isinstance(item, cls)
        ]

    @classmethod
    def get_total_amount_of_purchased_item(cls, course_key):
        """
        This will return the total amount of money that a purchased course generated
        """
        total_cost = 0
        result = cls.objects.filter(course_id=course_key, status='purchased').aggregate(total=Sum('unit_cost', field='qty * unit_cost'))  # pylint: disable=E1101

        if result['total'] is not None:
            total_cost = result['total']

        return total_cost

    @classmethod
    @transaction.commit_on_success
    def add_to_order(cls, order, course_id, mode_slug=CourseMode.DEFAULT_MODE_SLUG, cost=None, currency=None):
        """
        A standardized way to create these objects, with sensible defaults filled in.
        Will update the cost if called on an order that already carries the course.

        Returns the order item
        """
        # First a bunch of sanity checks
        course = modulestore().get_course(course_id)  # actually fetch the course to make sure it exists, use this to
                                                # throw errors if it doesn't
        if not course:
            log.error("User {} tried to add non-existent course {} to cart id {}"
                      .format(order.user.email, course_id, order.id))
            raise CourseDoesNotExistException

        if cls.contained_in_order(order, course_id):
            log.warning("User {} tried to add PaidCourseRegistration for course {}, already in cart id {}"
                        .format(order.user.email, course_id, order.id))
            raise ItemAlreadyInCartException

        if CourseEnrollment.is_enrolled(user=order.user, course_key=course_id):
            log.warning("User {} trying to add course {} to cart id {}, already registered"
                        .format(order.user.email, course_id, order.id))
            raise AlreadyEnrolledInCourseException

        ### Validations done, now proceed
        ### handle default arguments for mode_slug, cost, currency
        course_mode = CourseMode.mode_for_course(course_id, mode_slug)
        if not course_mode:
            # user could have specified a mode that's not set, in that case return the DEFAULT_MODE
            course_mode = CourseMode.DEFAULT_MODE
        if not cost:
            cost = course_mode.min_price
        if not currency:
            currency = course_mode.currency

        super(PaidCourseRegistration, cls).add_to_order(order, course_id, cost, currency=currency)

        item, created = cls.objects.get_or_create(order=order, user=order.user, course_id=course_id)
        item.status = order.status
        item.mode = course_mode.slug
        item.qty = 1
        item.unit_cost = cost
        item.line_desc = _(u'Registration for Course: {course_name}').format(
            course_name=course.display_name_with_default)
        item.currency = currency
        order.currency = currency
        item.report_comments = item.csv_report_comments
        order.save()
        item.save()
        log.info("User {} added course registration {} to cart: order {}"
                 .format(order.user.email, course_id, order.id))
        return item

    def purchased_callback(self):
        """
        When purchased, this should enroll the user in the course.  We are assuming that
        course settings for enrollment date are configured such that only if the (user.email, course_id) pair is found
        in CourseEnrollmentAllowed will the user be allowed to enroll.  Otherwise requiring payment
        would in fact be quite silly since there's a clear back door.
        """
        if not modulestore().has_course(self.course_id):
            raise PurchasedCallbackException(
                "The customer purchased Course {0}, but that course doesn't exist!".format(self.course_id))

        CourseEnrollment.enroll(user=self.user, course_key=self.course_id, mode=self.mode)

        log.info("Enrolled {0} in paid course {1}, paid ${2}"
                 .format(self.user.email, self.course_id, self.line_cost))  # pylint: disable=E1101

    def generate_receipt_instructions(self):
        """
        Generates instructions when the user has purchased a PaidCourseRegistration.
        Basically tells the user to visit the dashboard to see their new classes
        """
        notification = (_('Please visit your <a href="{dashboard_link}">dashboard</a>  to see your new course.')
                        .format(dashboard_link=reverse('dashboard')))

        return self.pk_with_subclass, set([notification])

    @property
    def csv_report_comments(self):
        """
        Tries to fetch an annotation associated with the course_id from the database.  If not found, returns u"".
        Otherwise returns the annotation
        """
        try:
            return PaidCourseRegistrationAnnotation.objects.get(course_id=self.course_id).annotation
        except PaidCourseRegistrationAnnotation.DoesNotExist:
            return u""


class CourseRegCodeItem(OrderItem):
    """
    This is an inventory item for paying for
    generating course registration codes
    """
    course_id = CourseKeyField(max_length=128, db_index=True)
    mode = models.SlugField(default=CourseMode.DEFAULT_MODE_SLUG)

    @classmethod
    def contained_in_order(cls, order, course_id):
        """
        Is the course defined by course_id contained in the order?
        """
        return course_id in [
            item.course_id
            for item in order.orderitem_set.all().select_subclasses("courseregcodeitem")
            if isinstance(item, cls)
        ]

    @classmethod
    def get_total_amount_of_purchased_item(cls, course_key):
        """
        This will return the total amount of money that a purchased course generated
        """
        total_cost = 0
        result = cls.objects.filter(course_id=course_key, status='purchased').aggregate(total=Sum('unit_cost', field='qty * unit_cost'))  # pylint: disable=E1101

        if result['total'] is not None:
            total_cost = result['total']

        return total_cost

    @classmethod
    @transaction.commit_on_success
    def add_to_order(cls, order, course_id, qty, mode_slug=CourseMode.DEFAULT_MODE_SLUG, cost=None, currency=None):  # pylint: disable=W0221
        """
        A standardized way to create these objects, with sensible defaults filled in.
        Will update the cost if called on an order that already carries the course.

        Returns the order item
        """
        # First a bunch of sanity checks
        course = modulestore().get_course(course_id)  # actually fetch the course to make sure it exists, use this to
                                                # throw errors if it doesn't
        if not course:
            log.error("User {} tried to add non-existent course {} to cart id {}"
                      .format(order.user.email, course_id, order.id))
            raise CourseDoesNotExistException

        if cls.contained_in_order(order, course_id):
            log.warning("User {} tried to add PaidCourseRegistration for course {}, already in cart id {}"
                        .format(order.user.email, course_id, order.id))
            raise ItemAlreadyInCartException

        if CourseEnrollment.is_enrolled(user=order.user, course_key=course_id):
            log.warning("User {} trying to add course {} to cart id {}, already registered"
                        .format(order.user.email, course_id, order.id))
            raise AlreadyEnrolledInCourseException

        ### Validations done, now proceed
        ### handle default arguments for mode_slug, cost, currency
        course_mode = CourseMode.mode_for_course(course_id, mode_slug)
        if not course_mode:
            # user could have specified a mode that's not set, in that case return the DEFAULT_MODE
            course_mode = CourseMode.DEFAULT_MODE
        if not cost:
            cost = course_mode.min_price
        if not currency:
            currency = course_mode.currency

        super(CourseRegCodeItem, cls).add_to_order(order, course_id, cost, currency=currency)

        item, created = cls.objects.get_or_create(order=order, user=order.user, course_id=course_id)  # pylint: disable=W0612
        item.status = order.status
        item.mode = course_mode.slug
        item.unit_cost = cost
        item.qty = qty
        item.line_desc = _(u'Enrollment codes for Course: {course_name}').format(
            course_name=course.display_name_with_default)
        item.currency = currency
        order.currency = currency
        item.report_comments = item.csv_report_comments
        order.save()
        item.save()
        log.info("User {} added course registration {} to cart: order {}"
                 .format(order.user.email, course_id, order.id))
        return item

    def purchased_callback(self):
        """
        The purchase is completed, this OrderItem type will generate Registration Codes that will
        be redeemed by users
        """
        if not modulestore().has_course(self.course_id):
            raise PurchasedCallbackException(
                "The customer purchased Course {0}, but that course doesn't exist!".format(self.course_id))
        total_registration_codes = int(self.qty)

        # we need to import here because of a circular dependency
        # we should ultimately refactor code to have save_registration_code in this models.py
        # file, but there's also a shared dependency on a random string generator which
        # is in another PR (for another feature)
        from instructor.views.api import save_registration_code
        for i in range(total_registration_codes):  # pylint: disable=W0612
            save_registration_code(self.user, self.course_id, invoice=None, order=self.order)

        log.info("Enrolled {0} in paid course {1}, paid ${2}"
                 .format(self.user.email, self.course_id, self.line_cost))  # pylint: disable=E1101

    @property
    def csv_report_comments(self):
        """
        Tries to fetch an annotation associated with the course_id from the database.  If not found, returns u"".
        Otherwise returns the annotation
        """
        try:
            return CourseRegCodeItemAnnotation.objects.get(course_id=self.course_id).annotation
        except CourseRegCodeItemAnnotation.DoesNotExist:
            return u""


class CourseRegCodeItemAnnotation(models.Model):
    """
    A model that maps course_id to an additional annotation.  This is specifically needed because when Stanford
    generates report for the paid courses, each report item must contain the payment account associated with a course.
    And unfortunately we didn't have the concept of a "SKU" or stock item where we could keep this association,
    so this is to retrofit it.
    """
    course_id = CourseKeyField(unique=True, max_length=128, db_index=True)
    annotation = models.TextField(null=True)

    def __unicode__(self):
        # pylint: disable=no-member
        return u"{} : {}".format(self.course_id.to_deprecated_string(), self.annotation)


class PaidCourseRegistrationAnnotation(models.Model):
    """
    A model that maps course_id to an additional annotation.  This is specifically needed because when Stanford
    generates report for the paid courses, each report item must contain the payment account associated with a course.
    And unfortunately we didn't have the concept of a "SKU" or stock item where we could keep this association,
    so this is to retrofit it.
    """
    course_id = CourseKeyField(unique=True, max_length=128, db_index=True)
    annotation = models.TextField(null=True)

    def __unicode__(self):
        # pylint: disable=no-member
        return u"{} : {}".format(self.course_id.to_deprecated_string(), self.annotation)


class CertificateItem(OrderItem):
    """
    This is an inventory item for purchasing certificates
    """
    course_id = CourseKeyField(max_length=128, db_index=True)
    course_enrollment = models.ForeignKey(CourseEnrollment)
    mode = models.SlugField()

    @receiver(UNENROLL_DONE)
    def refund_cert_callback(sender, course_enrollment=None, **kwargs):
        """
        When a CourseEnrollment object calls its unenroll method, this function checks to see if that unenrollment
        occurred in a verified certificate that was within the refund deadline.  If so, it actually performs the
        refund.

        Returns the refunded certificate on a successful refund; else, it returns nothing.
        """

        # Only refund verified cert unenrollments that are within bounds of the expiration date
        if not course_enrollment.refundable():
            return

        target_certs = CertificateItem.objects.filter(course_id=course_enrollment.course_id, user_id=course_enrollment.user, status='purchased', mode='verified')
        try:
            target_cert = target_certs[0]
        except IndexError:
            log.error("Matching CertificateItem not found while trying to refund.  User %s, Course %s", course_enrollment.user, course_enrollment.course_id)
            return
        target_cert.status = 'refunded'
        target_cert.refund_requested_time = datetime.now(pytz.utc)
        target_cert.save()
        target_cert.order.status = 'refunded'
        target_cert.order.save()

        order_number = target_cert.order_id
        # send billing an email so they can handle refunding
        subject = _("[Refund] User-Requested Refund")
        message = "User {user} ({user_email}) has requested a refund on Order #{order_number}.".format(user=course_enrollment.user,
                                                                                                       user_email=course_enrollment.user.email,
                                                                                                       order_number=order_number)
        to_email = [settings.PAYMENT_SUPPORT_EMAIL]
        from_email = microsite.get_value('payment_support_email', settings.PAYMENT_SUPPORT_EMAIL)
        try:
            send_mail(subject, message, from_email, to_email, fail_silently=False)
        except Exception as exception:  # pylint: disable=broad-except
            err_str = ('Failed sending email to billing to request a refund for verified certificate'
                       ' (User {user}, Course {course}, CourseEnrollmentID {ce_id}, Order #{order})\n{exception}')
            log.error(err_str.format(
                user=course_enrollment.user,
                course=course_enrollment.course_id,
                ce_id=course_enrollment.id,
                order=order_number,
                exception=exception,
            ))

        return target_cert

    @classmethod
    @transaction.commit_on_success
    def add_to_order(cls, order, course_id, cost, mode, currency='usd'):
        """
        Add a CertificateItem to an order

        Returns the CertificateItem object after saving

        `order` - an order that this item should be added to, generally the cart order
        `course_id` - the course that we would like to purchase as a CertificateItem
        `cost` - the amount the user will be paying for this CertificateItem
        `mode` - the course mode that this certificate is going to be issued for

        This item also creates a new enrollment if none exists for this user and this course.

        Example Usage:
            cart = Order.get_cart_for_user(user)
            CertificateItem.add_to_order(cart, 'edX/Test101/2013_Fall', 30, 'verified')

        """
        super(CertificateItem, cls).add_to_order(order, course_id, cost, currency=currency)

        course_enrollment = CourseEnrollment.get_or_create_enrollment(order.user, course_id)

        # do some validation on the enrollment mode
        valid_modes = CourseMode.modes_for_course_dict(course_id)
        if mode in valid_modes:
            mode_info = valid_modes[mode]
        else:
            raise InvalidCartItem(_("Mode {mode} does not exist for {course_id}").format(mode=mode, course_id=course_id))
        item, _created = cls.objects.get_or_create(
            order=order,
            user=order.user,
            course_id=course_id,
            course_enrollment=course_enrollment,
            mode=mode,
        )
        item.status = order.status
        item.qty = 1
        item.unit_cost = cost
        course_name = modulestore().get_course(course_id).display_name
        # Translators: In this particular case, mode_name refers to a
        # particular mode (i.e. Honor Code Certificate, Verified Certificate, etc)
        # by which a user could enroll in the given course.
        item.line_desc = _("{mode_name} for course {course}").format(
            mode_name=mode_info.name,
            course=course_name
        )
        item.currency = currency
        order.currency = currency
        order.save()
        item.save()
        return item

    def purchased_callback(self):
        """
        When purchase goes through, activate and update the course enrollment for the correct mode
        """
        try:
            verification_attempt = SoftwareSecurePhotoVerification.active_for_user(self.course_enrollment.user)
            verification_attempt.submit()
        except Exception:
            log.exception(
                "Could not submit verification attempt for enrollment {}".format(self.course_enrollment)
            )
        self.course_enrollment.change_mode(self.mode)
        self.course_enrollment.activate()

    @property
    def single_item_receipt_template(self):
        if self.mode in ('verified', 'professional'):
            return 'shoppingcart/verified_cert_receipt.html'
        else:
            return super(CertificateItem, self).single_item_receipt_template

    @property
    def single_item_receipt_context(self):
        course = modulestore().get_course(self.course_id)
        return {
            "course_id": self.course_id,
            "course_name": course.display_name_with_default,
            "course_org": course.display_org_with_default,
            "course_num": course.display_number_with_default,
            "course_start_date_text": course.start_date_text,
            "course_has_started": course.start > datetime.today().replace(tzinfo=pytz.utc),
            "course_root_url": reverse(
                'course_root',
                kwargs={'course_id': self.course_id.to_deprecated_string()}  # pylint: disable=no-member
            ),
            "dashboard_url": reverse('dashboard'),
        }

    @property
    def additional_instruction_text(self):
        return _("Note - you have up to 2 weeks into the course to unenroll from the Verified Certificate option "
                 "and receive a full refund. To receive your refund, contact {billing_email}. "
                 "Please include your order number in your e-mail. "
                 "Please do NOT include your credit card information.").format(
                     billing_email=settings.PAYMENT_SUPPORT_EMAIL)

    @classmethod
    def verified_certificates_count(cls, course_id, status):
        """Return a queryset of CertificateItem for every verified enrollment in course_id with the given status."""
        return use_read_replica_if_available(
            CertificateItem.objects.filter(course_id=course_id, mode='verified', status=status).count())

    # TODO combine these three methods into one
    @classmethod
    def verified_certificates_monetary_field_sum(cls, course_id, status, field_to_aggregate):
        """
        Returns a Decimal indicating the total sum of field_to_aggregate for all verified certificates with a particular status.

        Sample usages:
        - status 'refunded' and field_to_aggregate 'unit_cost' will give the total amount of money refunded for course_id
        - status 'purchased' and field_to_aggregate 'service_fees' gives the sum of all service fees for purchased certificates
        etc
        """
        query = use_read_replica_if_available(
            CertificateItem.objects.filter(course_id=course_id, mode='verified', status=status)).aggregate(Sum(field_to_aggregate))[field_to_aggregate + '__sum']
        if query is None:
            return Decimal(0.00)
        else:
            return query

    @classmethod
    def verified_certificates_contributing_more_than_minimum(cls, course_id):
        return use_read_replica_if_available(
            CertificateItem.objects.filter(
                course_id=course_id,
                mode='verified',
                status='purchased',
                unit_cost__gt=(CourseMode.min_course_price_for_verified_for_currency(course_id, 'usd')))).count()


class DonationConfiguration(ConfigurationModel):
    """Configure whether donations are enabled on the site."""
    pass


class Donation(OrderItem):
    """A donation made by a user.

    Donations can be made for a specific course or to the organization as a whole.
    Users can choose the donation amount.
    """

    # Types of donations
    DONATION_TYPES = (
        ("general", "A general donation"),
        ("course", "A donation to a particular course")
    )

    # The type of donation
    donation_type = models.CharField(max_length=32, default="general", choices=DONATION_TYPES)

    # If a donation is made for a specific course, then store the course ID here.
    # If the donation is made to the organization as a whole,
    # set this field to CourseKeyField.Empty
    course_id = CourseKeyField(max_length=255, db_index=True)

    @classmethod
    @transaction.commit_on_success
    def add_to_order(cls, order, donation_amount, course_id=None, currency='usd'):
        """Add a donation to an order.

        Args:
            order (Order): The order to add this donation to.
            donation_amount (Decimal): The amount the user is donating.


        Keyword Args:
            course_id (CourseKey): If provided, associate this donation with a particular course.
            currency (str): The currency used for the the donation.

        Raises:
            InvalidCartItem: The provided course ID is not valid.

        Returns:
            Donation

        """
        # This will validate the currency but won't actually add the item to the order.
        super(Donation, cls).add_to_order(order, currency=currency)

        # Create a line item description, including the name of the course
        # if this is a per-course donation.
        # This will raise an exception if the course can't be found.
        description = cls._line_item_description(course_id=course_id)

        params = {
            "order": order,
            "user": order.user,
            "status": order.status,
            "qty": 1,
            "unit_cost": donation_amount,
            "currency": currency,
            "line_desc": description
        }

        if course_id is not None:
            params["course_id"] = course_id
            params["donation_type"] = "course"
        else:
            params["donation_type"] = "general"

        return cls.objects.create(**params)

    def purchased_callback(self):
        """Donations do not need to be fulfilled, so this method does nothing."""
        pass

    def generate_receipt_instructions(self):
        """Provide information about tax-deductible donations in the receipt.

        Returns:
            tuple of (Donation, unicode)

        """
        return self.pk_with_subclass, set([self._tax_deduction_msg()])

    @property
    def additional_instruction_text(self):
        """Provide information about tax-deductible donations in the confirmation email.

        Returns:
            unicode

        """
        return self._tax_deduction_msg()

    def _tax_deduction_msg(self):
        """Return the translated version of the tax deduction message.

        Returns:
            unicode

        """
        return _(
            u"We greatly appreciate this generous contribution and your support of the {platform_name} mission.  "
            u"This receipt was prepared to support charitable contributions for tax purposes.  "
            u"We confirm that neither goods nor services were provided in exchange for this gift."
        ).format(platform_name=settings.PLATFORM_NAME)

    @classmethod
    def _line_item_description(self, course_id=None):
        """Create a line-item description for the donation.

        Includes the course display name if provided.

        Keyword Arguments:
            course_id (CourseKey)

        Raises:
            CourseDoesNotExistException: The course ID is not valid.

        Returns:
            unicode

        """
        # If a course ID is provided, include the display name of the course
        # in the line item description.
        if course_id is not None:
            course = modulestore().get_course(course_id)
            if course is None:
                err = _(
                    u"Could not find a course with the ID '{course_id}'"
                ).format(course_id=course_id)
                raise CourseDoesNotExistException(err)

            return _(u"Donation for {course}").format(course=course.display_name)

        # The donation is for the organization as a whole, not a specific course
        else:
            return _(u"Donation for {platform_name}").format(platform_name=settings.PLATFORM_NAME)

    @property
    def single_item_receipt_context(self):
        return {
            'receipt_has_donation_item': True,
        }
