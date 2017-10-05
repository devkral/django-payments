from __future__ import unicode_literals
import json
import six
from uuid import uuid4
from decimal import Decimal

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import models
from django.utils.translation import ugettext_lazy as _

from .core import provider_factory
from .utils import add_address_to_class, create_get_address
from . import FraudStatus, PaymentStatus


class PaymentAttributeProxy(object):

    def __init__(self, payment):
        self._payment = payment
        super(PaymentAttributeProxy, self).__init__()

    def __getattr__(self, item):
        data = json.loads(self._payment.extra_data or '{}')
        try:
            return data[item]
        except KeyError as e:
            six.raise_from(AttributeError(), e)

    def __setattr__(self, key, value):
        if key == '_payment':
            return super(PaymentAttributeProxy, self).__setattr__(key, value)
        try:
            data = json.loads(self._payment.extra_data)
        except ValueError:
            data = {}
        data[key] = value
        self._payment.extra_data = json.dumps(data)

class AbstractBasePayment(object):
    """ Logic of BasePayment """

    def change_status(self, status, message=''):
        '''
        Updates the Payment status and sends the status_changed signal.
        '''
        from .signals import status_changed
        self.status = status
        self.message = message

        self.save()
        status_changed.send_robust(sender=type(self), instance=self)

    def change_fraud_status(self, status, message='', commit=True):
        available_statuses = [choice[0] for choice in FraudStatus.CHOICES]
        if status not in available_statuses:
            raise ValueError(
                'Wrong status "%s", it should be one of: %s' % (
                    status, ', '.join(available_statuses)))
        self.fraud_status = status
        self.fraud_message = message
        if commit:
            self.save()

    def get_form(self, data=None):
        provider = provider_factory(self.variant)
        return provider.get_form(self, data=data)

    def get_purchased_items(self):
        return []

    def get_failure_url(self):
        raise NotImplementedError()

    def get_rejection_url(self):
        return self.get_failure_url()

    def get_success_url(self):
        raise NotImplementedError()

    def get_process_url(self):
        return reverse('process_payment', kwargs={'token': self.token})

    def capture(self, amount=None, final=True):
        """ Captures  a amount """
        if self.status != PaymentStatus.PREAUTH:
            raise ValueError(
                'Only pre-authorized payments can be captured.')
        if not amount:
            amount = min(self.total-self.captured_amount, Decimal("0"))
        provider = provider_factory(self.variant)
        provider.capture(self, amount, final)
        self.captured_amount += amount
        if final:
            self.change_status(PaymentStatus.CONFIRMED)
        return amount

    def release(self):
        if self.status != PaymentStatus.PREAUTH:
            raise ValueError(
                'Only pre-authorized payments can be released.')
        provider = provider_factory(self.variant)
        provider.release(self)
        self.change_status(PaymentStatus.REFUNDED)

    def refund(self, amount=None):
        if self.status != PaymentStatus.CONFIRMED:
            raise ValueError(
                'Only charged payments can be refunded.')
        if amount:
            if amount > self.captured_amount:
                raise ValueError(
                    'Refund amount can not be greater then captured amount')
        else:
            amount = self.captured_amount
        provider = provider_factory(self.variant)
        provider.refund(self, amount)
        self.captured_amount -= amount
        # if something wents wrong negative values can appear, handle gracefully
        if self.captured_amount <= 0 and self.status != PaymentStatus.REFUNDED:
            self.change_status(PaymentStatus.REFUNDED)
        return amount


    @property
    def attrs(self):
        return PaymentAttributeProxy(self)

class BasePayment(models.Model, AbstractBasePayment):
    '''
    Represents a single transaction. Each instance has one or more PaymentItem.
    '''
    variant = models.CharField(max_length=255)
    #: Transaction status
    status = models.CharField(
        max_length=10, choices=PaymentStatus.CHOICES,
        default=PaymentStatus.WAITING)
    fraud_status = models.CharField(
        _('fraud check'), max_length=10, choices=FraudStatus.CHOICES,
        default=FraudStatus.UNKNOWN)
    fraud_message = models.TextField(blank=True, default='')
    #: Creation date and time
    created = models.DateTimeField(auto_now_add=True)
    #: Date and time of last modification
    modified = models.DateTimeField(auto_now=True)
    #: Transaction ID (if applicable)
    transaction_id = models.CharField(max_length=255, blank=True)
    #: Currency code (may be provider-specific)
    currency = models.CharField(max_length=10)
    #: Total amount (gross)
    total = models.DecimalField(max_digits=9, decimal_places=2, default='0.0')
    delivery = models.DecimalField(
        max_digits=9, decimal_places=2, default='0.0')
    tax = models.DecimalField(max_digits=9, decimal_places=2, default='0.0')
    description = models.TextField(blank=True, default='')
    billing_email = models.EmailField(blank=True)
    customer_ip_address = models.GenericIPAddressField(blank=True, null=True)
    extra_data = models.TextField(blank=True, default='')
    message = models.TextField(blank=True, default='')
    token = models.CharField(max_length=36, blank=True, default='')
    captured_amount = models.DecimalField(
        max_digits=9, decimal_places=2, default='0.0')

    class Meta:
        abstract = True

    def save(self, **kwargs):
        if not self.token:
            tries = set()  # Stores a set of tried values
            while True:
                token = str(uuid4())
                if token in tries and len(tries) >= 100:  # After 100 tries we are impliying an infinite loop
                    raise SystemExit('A possible infinite loop was detected')
                else:
                    if not self.__class__._default_manager.filter(token=token).exists():
                        self.token = token
                        break
                tries.add(token)

        return super(BasePayment, self).save(**kwargs)

    def __unicode__(self):
        return self.variant

@add_address_to_class("billing")
class BasePaymentWithAddress(AbstractBasePayment):
    """ Has real billing address + shippingaddress alias on billing address (alias for backward compatibility) """
    get_shipping_address = create_get_address("billing")
