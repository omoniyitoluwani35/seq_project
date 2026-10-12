# -*- coding: utf-8 -*-
"""Recognise Airbnb revenue when the stay completes, not when it sells.

Money taken for a stay that has not happened is a liability, not income:
the company owes the guest a service. Recognising it at booking
overstates revenue in every month carrying forward bookings, and turns a
refund into negative income in a later period.

So each booking produces three entries rather than one:

    At invoice          Dr  Receivable           $140
                            Cr  Rental Income        $140
    Deferral (same day) Dr  Rental Income        $140
                            Cr  Deferred Revenue     $140
    At checkout         Dr  Deferred Revenue     $140
                            Cr  Rental Income        $140

Net income is nil until the guest checks out, and the Deferred Revenue
balance is, at any moment, what the company owes in undelivered stays.

WHY NOT ODOO'S DEFERRED REVENUE ENGINE. It was tried first and silently
generated nothing - no entries, no drafts, no error - for these
invoices. The likeliest reason is that they are denominated in USD while
the company books in NGN, which its deferral logic does not appear to
handle. This is deliberately a thin layer over Odoo's own reversal
machinery rather than a reimplementation.

WHY THE RECOGNITION IS A REVERSAL. The deferral is reversed rather than
rebuilt, so the two entries are exact mirrors and the Deferred Revenue
account clears to zero in BOTH currencies. Rebuilding it at the checkout
date would translate at that day's rate and leave a Naira remainder
behind for ever. That is also the correct treatment: deferred revenue is
a non-monetary liability, settled by delivering a service rather than
cash, so it is not retranslated. Any rate movement belongs to the
clearing account, which IS monetary, and surfaces when Airbnb pays out.
"""
import logging

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AirbnbReservation(models.Model):
    _inherit = "airbnb.reservation"

    deferral_move_id = fields.Many2one(
        "account.move",
        string="Deferral Entry",
        readonly=True, copy=False,
        help="Moves the invoice's revenue into Deferred Revenue until the "
             "guest has actually stayed.",
    )
    revenue_move_id = fields.Many2one(
        "account.move",
        string="Revenue Recognition",
        readonly=True, copy=False,
        help="Releases the deferred amount into income at checkout.",
    )
    revenue_recognised = fields.Boolean(
        compute="_compute_revenue_recognised", store=True,
        help="True once the stay has completed and its revenue has been "
             "taken to income.",
    )

    @api.depends("revenue_move_id", "revenue_move_id.state")
    def _compute_revenue_recognised(self):
        for rec in self:
            rec.revenue_recognised = bool(
                rec.revenue_move_id and rec.revenue_move_id.state == "posted")

    # ================================================= configuration

    def _airbnb_deferred_account(self):
        """The liability stays sit in until the guest has had them."""
        acc = self.env.company.deferred_revenue_account_id
        if not acc:
            raise UserError(_(
                "No Deferred Revenue account is configured. Set one under "
                "Accounting > Configuration > Settings > Deferred Revenue "
                "- it should be a current liability of its own, not the "
                "generic Current Liabilities account."))
        return acc

    def _airbnb_deferral_journal(self):
        return (self.env.company.deferred_revenue_journal_id
                or self.env["account.journal"].search(
                    [("type", "=", "general"),
                     ("company_id", "=", self.env.company.id)], limit=1))

    # ================================================= deferral

    def _airbnb_defer_revenue(self, invoice):
        """Park this invoice's revenue in Deferred Revenue.

        Dated the same day as the invoice, so the two net to nil in the
        period the booking was made.
        """
        self.ensure_one()
        if self.deferral_move_id:
            return self.deferral_move_id

        deferred = self._airbnb_deferred_account()
        journal = self._airbnb_deferral_journal()
        if not journal:
            raise UserError(_("No miscellaneous journal to post deferrals to."))

        lines = []
        total_balance = 0.0
        total_currency = 0.0

        for line in invoice.invoice_line_ids:
            if line.display_type != "product" or not line.balance:
                continue
            # The invoice CREDITED income, so the deferral debits it back.
            amount = -line.balance
            amount_cur = -line.amount_currency
            lines.append(Command.create({
                "account_id": line.account_id.id,
                "name": _("Deferred until stay: %s") % self.display_name,
                "debit": amount if amount > 0 else 0.0,
                "credit": -amount if amount < 0 else 0.0,
                "currency_id": invoice.currency_id.id,
                "amount_currency": amount_cur,
                "partner_id": self.guest_partner_id.id,
            }))
            total_balance += amount
            total_currency += amount_cur

        if not lines:
            return self.env["account.move"]

        lines.append(Command.create({
            "account_id": deferred.id,
            "name": _("Stay not yet delivered: %s") % self.display_name,
            "debit": 0.0 if total_balance > 0 else -total_balance,
            "credit": total_balance if total_balance > 0 else 0.0,
            "currency_id": invoice.currency_id.id,
            "amount_currency": -total_currency,
            "partner_id": self.guest_partner_id.id,
        }))

        move = self.env["account.move"].create({
            "move_type": "entry",
            "journal_id": journal.id,
            "date": invoice.invoice_date or fields.Date.context_today(self),
            "ref": _("Deferred revenue - %s") % (invoice.name or self.display_name),
            "currency_id": invoice.currency_id.id,
            "line_ids": lines,
        })
        move.action_post()
        self.deferral_move_id = move.id

        self.message_post(body=_(
            "Revenue deferred. %(amt)s will be recognised when the guest "
            "checks out on %(date)s - until then it sits in %(acc)s as a "
            "stay the company owes."
        ) % {"amt": invoice.amount_total, "date": self.checkout_date,
             "acc": deferred.display_name})
        return move

    # ================================================= recognition

    def action_recognise_revenue(self):
        """Release the deferred amount into income. Called at checkout.

        Reversing the deferral rather than building a fresh entry is what
        makes the Deferred Revenue account clear to zero in both
        currencies - see the module docstring.
        """
        for rec in self:
            if rec.revenue_move_id:
                continue
            if not rec.deferral_move_id:
                continue
            if rec.state == "cancelled":
                continue



            reversal = rec.deferral_move_id._reverse_moves([{
                "date": rec.checkout_date or fields.Date.context_today(rec),
                "ref": _("Revenue recognised - %s") % rec.display_name,
            }])
            reversal.line_ids.write({
                "name": _("Stay completed %(from)s to %(to)s - revenue earned")
                % {"from": rec.checkin_date, "to": rec.checkout_date},
            })
            rec.revenue_move_id = reversal.id
            if reversal.state == "draft":
                reversal.action_post()
            rec.message_post(body=_(
                "Stay completed on %(date)s - revenue recognised in full."
            ) % {"date": rec.checkout_date})
        return True

    def _airbnb_unwind_deferral(self):
        """Undo the deferral for a booking that will never be delivered.

        For a cancellation before checkout: nothing was ever taken to
        income, so this only reverses the liability. Call it from
        action_cancel alongside reversing the payment and credit-noting
        the invoice.
        """
        for rec in self:
            if rec.revenue_move_id:
                raise UserError(_(
                    "%s has already been recognised as revenue. Credit-note "
                    "the invoice instead of unwinding the deferral.")
                    % rec.display_name)
            if not rec.deferral_move_id:
                continue
            reversal = rec.deferral_move_id._reverse_moves([{
                "date": fields.Date.context_today(rec),
                "ref": _("Cancelled before stay - %s") % rec.display_name,
            }])
            if reversal.state == "draft":
                reversal.action_post()
            rec.message_post(body=_(
                "Booking cancelled before the stay. The deferred revenue "
                "has been reversed; nothing reached income."))
        return True

    # ================================================= cron

    @api.model
    def _cron_complete_past_stays(self):
        """REPLACES the version in airbnb_reservation.py.

        The stay ending is both the operational signal (the unit is free,
        housekeeping is due) and the accounting one (the service has been
        delivered in full, so the revenue is earned). Same trigger, so
        the two cannot drift apart.
        """
        past = self.search([
            ("state", "=", "reserved"),
            ("checkout_date", "<", fields.Date.context_today(self)),
        ])
        past.write({"state": "done"})

        for rec in past:
            if not rec.deferral_move_id or rec.revenue_move_id:
                continue
            try:
                rec.action_recognise_revenue()
            except Exception as e:
                # One booking's accounting problem must not stop the rest
                # of the night's stays from completing.
                _logger.exception("Revenue recognition failed for %s",
                                  rec.display_name)
                rec.message_post(body=_(
                    "Revenue recognition failed: %s. The stay is complete "
                    "but its revenue is still deferred.") % e)
        return len(past)