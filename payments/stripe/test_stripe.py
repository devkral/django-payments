# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from contextlib import contextmanager

import stripe
from unittest import TestCase
try:
    from unittest.mock import patch, Mock
except ImportError:
    from mock import patch, Mock

from . import StripeProvider, StripeCardProvider
from .. import FraudStatus, PaymentStatus, RedirectNeeded
from ..testcommon import create_test_payment

SECRET_KEY = '1234abcd'
PUBLIC_KEY = 'abcd1234'


_Payment = create_test_payment("stripe")
class Payment(Mock, _Payment):
    def capture(self, amount=None):
        amount = amount or self.total
        self.captured_amount = amount
        self.change_status(PaymentStatus.CONFIRMED)

@contextmanager
def mock_stripe_Charge_create(error_msg=None):
    json_body = {
        'error': {
            'charge': 'charge_id'
        }
    }
    with patch('stripe.Charge.create') as mocked_charge_create:
        if error_msg:
            mocked_charge_create.side_effect = stripe.CardError(
                error_msg, param=None, code=None, json_body=json_body)
        else:
            mocked_charge_create.side_effect = lambda **kwargs: {}
        yield mocked_charge_create


@contextmanager
def mock_stripe_Charge_retrieve(fraudulent=False):
    with patch('stripe.Charge.retrieve') as mocked_charge_retrieve:
        fraud_details = {
            'stripe_report': None
        }
        if fraudulent:
            fraud_details['stripe_report'] = 'fraudulent'
        mocked_charge_retrieve.side_effect = lambda charge_id: {
            'id': charge_id,
            'fraud_details': fraud_details
        }
        yield mocked_charge_retrieve


class TestStripeProvider(TestCase):

    def test_form_contains_stripe_script(self):
        payment = Payment()
        store_name = 'Test store'
        provider = StripeProvider(
            name=store_name,
            secret_key=SECRET_KEY, public_key=PUBLIC_KEY)
        form = provider.get_form(payment)
        self.assertTrue(
            '<script class="stripe-button" data-amount="10000" '
            'data-currency="USD" data-description="payment" data-image="" '
            'data-key="%s" data-name="%s" '
            'src="https://checkout.stripe.com/checkout.js"></script>' % (
                PUBLIC_KEY, store_name)
            in str(form))

    def test_provider_raises_redirect_needed_when_token_does_not_exist(self):
        payment = Payment()
        provider = StripeProvider(
            name='Example.com store',
            secret_key=SECRET_KEY, public_key=PUBLIC_KEY)
        data = {}
        with self.assertRaises(RedirectNeeded) as exc:
            provider.get_form(payment, data)
            self.assertEqual(exc.args[0], payment.get_failure_url())
        self.assertEqual(payment.status, PaymentStatus.REJECTED)

    def test_provider_raises_redirect_needed_on_success(self):
        payment = Payment()
        provider = StripeProvider(
            name='Example.com store',
            secret_key=SECRET_KEY, public_key=PUBLIC_KEY)
        data = {'stripeToken': 'abcd'}
        with patch('stripe.util.json.dumps'):
            with patch('stripe.Charge.create'):
                with self.assertRaises(RedirectNeeded) as exc:
                    provider.get_form(payment, data)
                    self.assertEqual(exc.args[0], payment.get_success_url())
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(payment.captured_amount, payment.total)

    def test_provider_shows_validation_error_message(self):
        error_msg = 'Error message'

        payment = Payment()
        provider = StripeProvider(
            name='Example.com store',
            secret_key=SECRET_KEY, public_key=PUBLIC_KEY)
        data = {'stripeToken': 'abcd'}
        with mock_stripe_Charge_create(error_msg=error_msg):
            with mock_stripe_Charge_retrieve():
                form = provider.get_form(payment, data=data)
                self.assertEqual(form.errors['__all__'][0], error_msg)
        self.assertEqual(payment.status, PaymentStatus.ERROR)
        self.assertEqual(payment.message, error_msg)
        self.assertEqual(payment.captured_amount, 0)

    def test_provider_set_fraudulent_status(self):
        error_msg = 'Error message'
        payment = Payment()
        provider = StripeProvider(
            name='Example.com store',
            secret_key=SECRET_KEY, public_key=PUBLIC_KEY)
        data = {'stripeToken': 'abcd'}
        with mock_stripe_Charge_create(error_msg=error_msg):
            with mock_stripe_Charge_retrieve(fraudulent=True):
                provider.get_form(payment, data=data)
        self.assertEqual(payment.status, PaymentStatus.ERROR)
        self.assertEqual(payment.fraud_status, FraudStatus.REJECT)
        self.assertEqual(payment.captured_amount, 0)

    def test_provider_detect_already_processed_payment(self):
        payment = Payment()
        payment.transaction_id = 'existing_transaction_id'
        provider = StripeProvider(
            name='Example.com store',
            secret_key=SECRET_KEY, public_key=PUBLIC_KEY)
        data = {'stripeToken': 'abcd'}
        with mock_stripe_Charge_create():
            with mock_stripe_Charge_retrieve():
                form = provider.get_form(payment, data=data)
                msg = 'This payment has already been processed.'
                self.assertEqual(form.errors['__all__'][0], msg)

    def test_form_doesnt_have_name_attributes_on_fields(self):
        payment = Payment()
        store_name = 'Test store'
        provider = StripeCardProvider(
            name=store_name,
            secret_key=SECRET_KEY, public_key=PUBLIC_KEY)
        form = provider.get_form(payment)
        sensitive_fields = ['name', 'cvv2', 'expiration', 'number']
        for field_name in sensitive_fields:
            field = form[field_name]
            self.assertFalse('name=' in str(field), "%s is not escaped" % field_name)
