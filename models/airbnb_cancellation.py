# -*- coding: utf-8 -*-
"""Unwind a cancelled booking's accounting as well as its calendar.

Cancelling a reservation that has already been invoiced and paid leaves
three things behind, and missing any one of them is silent:

    the invoice          revenue for a stay that never happened
    the deferral         a liability for a room nobody is coming to
    the cash             money the books say is still owed or still held

So cancelling posts a credit note, unwinds the deferral if the revenue
has not yet been earned, and deals with the cash - differently depending
on who took it.

AIRBNB: REVERSE. Airbnb holds the guest's money and refunds it from
their own balance, netting it off the host's next payout. No cash ever
leaves the client's bank, so there is nothing for a bank reconciliation
to match and reversing the settlement is the honest record. What it
reverses is the entry that put the money into the clearing account; the
clearing balance drops, which is exactly what happened in reality.

DIRECT: REFUND. A walk-in handed over real money, and refunding means
real money going back. That is an OUTBOUND PAYMENT against the credit
note, not a reversal of the receipt. The bank statement will show two
lines - received on the 2nd, refunded on the 6th - and the books have to
show two entries or the reconciliation will never close. Reversing the
original receipt would make Odoo claim the first one never happened.

AFTER THE STAY, NOTHING. The client does not refund a guest who has
already had the room, so there is deliberately no path to it here. A
completed booking cannot be cancelled (the base action_cancel refuses
it) and _airbnb_unwind_deferral refuses to run once revenue has been
recognised. Both refusals are the rule being enforced rather than a
limitation to work around: if that policy ever changes it needs a
credit note and a decision about which period the reversal belongs to,
not a quiet unwinding of entries that are already in the P&L.

PARTIAL REFUNDS are deliberately not handled here. They need a decision
from the client about cancellation fees, and the retained portion is
earned income on the day of cancellation rather than something to leave
sitting in the deferred account.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

SETTLED = ("paid", "in_payment", "partial")


class AirbnbReservation(models.Model):
    _inherit = "airbnb.reservation"

    credit_note_id = fields.Many2one(
        "account.move",
        string="Credit Note",
        readonly=True, copy=False,
        help="Raised when the booking was cancelled, to take the invoice "
             "back out of the books.",
    )
    refund_move_id = fields.Many2one(
        "account.move",
        string="Refund Entry",
        readonly=True, copy=False,
        help="For an Airbnb booking, the reversal of the settlement. For a "
             "direct booking, the journal entry behind the refund.",
    )
    refund_payment_id = fields.Many2one(
        "account.payment",
        string="Refund Payment",
        readonly=True, copy=False,
        help="The outbound payment that sent a direct guest's money back. "
             "Recorded separately from its journal entry, because a "
             "payment on a journal with no outstanding account configured "
             "exists without one.",
    )

    # ================================================= helpers
    

    def _airbnb_payments(self):
        """The payment moves reconciled against this booking's invoice.

        Found by following the reconciliation off the receivable line
        rather than by a field name, which has moved between versions.
        """
        self.ensure_one()
        if not self.invoice_id:
            return self.env["account.move"]
        recv = self.invoice_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        partials = recv.matched_credit_ids | recv.matched_debit_ids
        counter = (partials.mapped("credit_move_id")
                   | partials.mapped("debit_move_id"))
        # move_type is the real guard: a payment always posts a plain
        # entry, while anything invoice-shaped is by definition not a
        # payment. Without it the credit note raised moments earlier by
        # the cancellation is auto-matched against this same receivable
        # line and gets read back as "the payment", pointing the refund
        # at the Sales journal.
        return counter.mapped("move_id").filtered(
            lambda m: m != self.invoice_id
            and m != self.credit_note_id
            and m.move_type == "entry"
            and m.state == "posted")

    def _airbnb_outbound_method_line(self, journal):
        """A way for this journal to pay money OUT.

        A journal set up only to take money in has nothing under its
        Outgoing Payments tab. The register wizard then computes an
        empty payment method line, and account.payment refuses the
        record outright - "Please define a payment method line on your
        payment" - which is how a refund fails with the credit note
        already posted.

        Deliberately read-only: the module does not create journal
        configuration. Which account refunds pass through is the
        accountant's decision, and a line invented here would be an
        account on the client's books that nobody chose.
        """
        self.ensure_one()
        lines = journal.outbound_payment_method_line_ids
        if not lines:
            return self.env["account.payment.method.line"]
        # One with an outstanding account set actually posts an entry;
        # without it the payment is recorded and the money never moves.
        return (lines.filtered("payment_account_id") or lines)[:1]

    @api.model
    def _airbnb_reconcile(self, moves):
        """Match the receivable lines across a set of moves."""
        lines = moves.mapped("line_ids").filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
            and not l.reconciled)
        if len(lines) > 1:
            lines.reconcile()

    # ================================================= the three steps

    def _airbnb_credit_note(self):
        """Take the invoice back out of the books."""
        self.ensure_one()
        if self.credit_note_id:
            return self.credit_note_id
        inv = self.invoice_id
        if not inv or inv.state != "posted":
            return self.env["account.move"]

        credit = inv._reverse_moves([{
            "date": fields.Date.context_today(self),
            "ref": _("Cancelled - %s") % self.display_name,
            "invoice_date": fields.Date.context_today(self),
        }])
        credit.line_ids.write({
            "name": _("Booking cancelled: %s") % self.display_name,
        })
        if credit.state == "draft":
            credit.action_post()
        self.credit_note_id = credit.id
        return credit

    def _airbnb_reverse_settlement(self):
        """Airbnb: undo the entry that put the money into clearing.

        No cash moved, so there is nothing for the bank to reconcile and
        a reversal is the truthful record.
        """
        self.ensure_one()
        moves = self._airbnb_payments()
        if not moves:
            return self.env["account.move"]

        rev = moves._reverse_moves([{
            "date": fields.Date.context_today(self),
            "ref": _("Airbnb refunded the guest - %s") % self.display_name,
        }])
        rev.line_ids.write({
            "name": _("Refunded by Airbnb, netted off the next payout: %s")
            % self.display_name,
        })
        if rev.filtered(lambda m: m.state == "draft"):
            rev.action_post()
        self.refund_move_id = rev[:1].id
        return rev

    def _airbnb_refund_payment(self, credit):
        """Direct booking: send the guest's money back out of the bank.

        An outbound payment against the credit note, so the statement's
        two lines have two entries to match.
        """
        self.ensure_one()
        if not credit:
            return self.env["account.payment"]

        # Which bank the guest paid into has to be read BEFORE anything
        # is unreconciled. Odoo groups reconciled lines into one full
        # reconcile, so breaking the credit note's match tears down the
        # invoice's match to the original payment as well - and then
        # there is nothing left to tell us where the money came from.
        paid_from = self._airbnb_payments()
        journal = paid_from[:1].journal_id

        # Odoo matches a reversal against its original, which would
        # leave the credit note settled and no residual for the refund
        # to pay against.
        recv = credit.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        already = recv.filtered("reconciled")
        if already:
            already.remove_move_reconcile()
        if not journal:
            self.message_post(body=_(
                "No refund was paid out - the original payment's journal "
                "could not be identified. Register the refund by hand "
                "against %s.") % credit.name)
            return self.env["account.payment"]

        # Resolved BEFORE the wizard is created, so the wizard's own
        # compute finds it on the journal and agrees with what is passed
        # in. Provisioning it afterwards would leave the wizard holding a
        # cached, empty list of available methods.
        method = self._airbnb_outbound_method_line(journal)
        if not method:
            self.message_post(body=_(
                "No refund was paid out - %(journal)s has no outgoing "
                "payment method. Add one under Accounting > Configuration "
                "> Journals > %(journal)s > Outgoing Payments, with an "
                "Outstanding Payments account set, then register the "
                "refund against %(credit)s."
            ) % {"journal": journal.display_name, "credit": credit.name})
            return self.env["account.payment"]

        payment = self.env["account.payment.register"].with_context(
            active_model="account.move",
            active_ids=credit.ids,
        ).create({
            "journal_id": journal.id,
            "payment_method_line_id": method.id,
            "payment_date": fields.Date.context_today(self),
        })._create_payments()

        if payment:
            self.refund_payment_id = payment[:1].id
            if payment[:1].move_id:
                self.refund_move_id = payment[:1].move_id.id
            else:
                # Same trap as the clearing journal: without an
                # Outstanding Payments account the payment is recorded
                # but posts no entry, so the money never actually leaves
                # the books.
                self.message_post(body=_(
                    "The refund was registered but posted no journal "
                    "entry - %s has no Outstanding Payments account set. "
                    "Set it under the journal's Outgoing Payments tab, "
                    "then re-post the refund."
                ) % journal.display_name)
        return payment

    # ================================================= orchestration

    def _airbnb_unwind_accounting(self):
        """Credit note, deferral, cash - in that order."""
        self.ensure_one()
        inv = self.invoice_id
        if not inv or inv.state != "posted":
            return

        was_settled = inv.payment_state in SETTLED
        credit = self._airbnb_credit_note()

        # Only if the stay never happened. After checkout the revenue is
        # earned and the credit note is what reduces it, in the period
        # the refund is actually given.
        if self.deferral_move_id and not self.revenue_move_id:
            self._airbnb_unwind_deferral()

        if not was_settled:
            # Nobody has paid yet, so there is nothing to give back - the
            # credit note alone closes the receivable.
            self._airbnb_reconcile(inv | credit)
            self.message_post(body=_(
                "Cancelled before payment. %s cancels the invoice; no "
                "refund was due.") % credit.name)
            return

        if self.source == "airbnb":
            rev = self._airbnb_reverse_settlement()
            self._airbnb_reconcile(credit | rev)
            self.message_post(body=_(
                "Cancelled. %(credit)s reverses the invoice and the "
                "settlement has been reversed out of the Airbnb clearing "
                "account - Airbnb refunds the guest and nets it off the "
                "next payout, so no money leaves the bank."
            ) % {"credit": credit.name})
        else:
            payment = self._airbnb_refund_payment(credit)
            self.message_post(body=_(
                "Cancelled. %(credit)s reverses the invoice and "
                "%(amount)s has been refunded to the guest out of "
                "%(journal)s."
            ) % {
                "credit": credit.name,
                "amount": credit.amount_total,
                "journal": (payment[:1].journal_id.name
                            if payment else _("the bank")),
            })

    # ================================================= entry point

    def action_cancel(self):
        """Free the nights AND unwind the money.

        The accounting is attempted first but never blocks the
        cancellation: the guest is not coming either way, and a room held
        for a no-show costs more than an entry somebody has to fix by
        hand. Anything that fails lands on the chatter.
        """
        todo = self.filtered(lambda r: r.state != "cancelled")

        if not (self.env.su
                or self.env.user.has_group("AirBNB.group_airbnb_manager")
                or self.env.user.has_group("base.group_system")):
            raise AccessError(_(
                "Only a Short Stay Manager can cancel a booking. Cancelling "
                "reverses the invoice and, for a direct booking, pays money "
                "back out of the bank."))

        # super() first, deliberately. It is what refuses a completed
        # stay, and the books must not be touched for a cancellation
        # that is not going to happen - otherwise a refused cancel
        # still leaves a credit note behind.
        res = super().action_cancel()

        for rec in todo:
            try:
                rec._airbnb_unwind_accounting()
            except Exception as e:
                _logger.exception("Cancellation accounting failed for %s",
                                  rec.display_name)
                rec.message_post(body=_(
                    "The booking was cancelled but its accounting could "
                    "not be unwound: %s. The invoice, and any payment "
                    "against it, still need reversing by hand.") % e)
        return res