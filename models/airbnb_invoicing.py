# -*- coding: utf-8 -*-
"""Invoice and settle confirmed Airbnb reservations.

The guest has already paid Airbnb by the time the confirmation email
arrives, so the invoice is settled immediately. The cash has not reached
the bank yet, though - Airbnb remits days later - so payment is
registered against a clearing journal rather than a bank account. The
real payout is then reconciled against that clearing balance, and the
commission falls out as the difference.

    On confirmation:
        Dr  Airbnb Clearing      52.00
            Cr  Rental Income        52.00

    On payout:
        Dr  Bank                 43.34
        Dr  Airbnb Commission     8.66
            Cr  Airbnb Clearing      52.00
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# The only states that take a unit off the market. A hold is
# deliberately absent: two guests may hold the same nights, and the one
# who pays first takes them.

_logger = logging.getLogger(__name__)


class AirbnbReservation(models.Model):
    _inherit = "airbnb.reservation"

    invoice_id = fields.Many2one("account.move", readonly=True, copy=False)
    invoice_state = fields.Selection(
        related="invoice_id.payment_state", string="Invoice Status")

    def _airbnb_clearing_journal(self):
        """Bank-type journal representing money held by Airbnb but not
        yet received. Configured once, via system parameter."""
        code = self.env["ir.config_parameter"].sudo().get_param(
            "airbnb.clearing_journal_code", "ABNB")
        journal = self.env["account.journal"].search(
            [("code", "=", code), ("company_id", "=", self.env.company.id)],
            limit=1)
        if not journal:
            raise UserError(_(
                "No journal with code '%s' exists. Create a bank journal "
                "for Airbnb settlements, or set the "
                "airbnb.clearing_journal_code system parameter."
            ) % code)
        return journal

    def action_create_and_settle_invoice(self):
        """Raise the invoice and mark it paid against the clearing
        journal. Called automatically when an Airbnb booking confirms."""
        for rec in self:
            if rec.invoice_id:
                continue
            if rec.state != "reserved":
                raise UserError(_(
                    "Reserve the booking first - that is the step that takes "
                    "payment and blocks the nights."))
            if not rec.gross_amount:
                _logger.warning(
                    "Reservation %s has no amount - invoice skipped", rec.name)
                continue
            clearing = rec._airbnb_clearing_journal()
            acc_cur = clearing.default_account_id.currency_id
            if rec.source == "airbnb" and acc_cur and rec.currency_id != acc_cur:
                rec.message_post(body=_(
                    "No invoice raised. This booking is priced in %(book)s "
                    "but the Airbnb clearing account is held in %(acc)s - "
                    "the amount came from the unit's list rate, not the "
                    "email, so it needs checking against Airbnb before it "
                    "can be invoiced."
                ) % {"book": rec.currency_id.name, "acc": acc_cur.name})
                continue

            invoice = rec.sale_order_id.sudo()._create_invoices().sudo(False)
            if not invoice:
                continue
            if rec.currency_id and invoice.currency_id != rec.currency_id:
                invoice.currency_id = rec.currency_id

            invoice.invoice_date = rec.checkin_date or fields.Date.today()
            # invoice.invoice_line_ids.write({
            #     "deferred_start_date": rec.checkin_date,
            #     "deferred_end_date": rec.checkout_date,
            # })
            invoice.action_post()
            rec.invoice_id = invoice
            rec._airbnb_defer_revenue(invoice)

            # if rec.source == "airbnb":
            #     rec._airbnb_settle_invoice(invoice)
            # rec.invoice_id = invoice

            if rec.source == "airbnb":
                rec._airbnb_settle_invoice(invoice)
            # A direct booking is paid at the desk through Monnify, so
            # its invoice is left open for that payment to reconcile
            # against. Settling it here would pay it twice.

    def _airbnb_settle_invoice(self, invoice):
        """Register payment against the clearing journal and reconcile."""
        self.ensure_one()
        # A second payment would put the money into clearing twice, and it
        # would be invisible to any report that follows reconciliation -
        # nothing would be matched against it.
        recv = invoice.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        if recv and all(recv.mapped("reconciled")):
            return
        journal = self._airbnb_clearing_journal()

        payment = self.env["account.payment"].create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": self.guest_partner_id.id,
            "amount": invoice.amount_total,
            "currency_id": invoice.currency_id.id,
            "journal_id": journal.id,
            "date": self.checkin_date or fields.Date.today(),
            "memo": _("Airbnb %s") % (self.airbnb_confirmation_code or self.name),
        })
        payment.action_post()

        lines = (payment.move_id.line_ids + invoice.line_ids).filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
            and not l.reconciled
        )
        if lines:
            lines.reconcile()

        self.payout_state = "pending"
        self.message_post(body=_(
            "Invoice %(inv)s raised for %(amt)s and settled against the "
            "Airbnb clearing account. Awaiting payout of %(payout)s; the "
            "difference of %(fee)s is Airbnb's commission."
        ) % {
            "inv": invoice.name,
            "amt": invoice.amount_total,
            "payout": self.payout_amount or 0.0,
            "fee": self.airbnb_host_fee or 0.0,
        })

    def action_confirm(self):
        """Extend the state machine: an Airbnb booking is already paid,
        so invoice and settle it as part of confirming."""
        res = super().action_confirm()
        for rec in self:
            if rec.state != "reserved":
                raise UserError(_(
                    "Reserve the booking first - that is the step that takes "
                    "payment and blocks the nights."))
            if (rec.source == "airbnb" and rec.state == "confirmed"
                    and not rec.invoice_id and rec.gross_amount):
                try:
                    rec.action_create_and_settle_invoice()
                except Exception as e:
                    # Never let an accounting failure roll back the
                    # booking itself - the calendar block matters more.
                    _logger.exception("Airbnb invoicing failed for %s", rec.name)
                    rec.message_post(body=_(
                        "Automatic invoicing failed: %s. Raise the invoice "
                        "by hand.") % e)
        return res