# -*- coding: utf-8 -*-
"""Collect payment for a direct reservation.

A guest at the desk scans the QR with their phone and pays on Odoo's
portal; a remote guest is sent the same link. One path serves both,
which is why this uses the payment portal rather than a POS terminal.

Airbnb reservations are excluded: the guest has already paid Airbnb, and
that settlement is handled by the clearing journal instead.
"""
import base64
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AirbnbReservation(models.Model):
    _inherit = "airbnb.reservation"

    invoice_id = fields.Many2one("account.move", readonly=True, copy=False)
    invoice_payment_state = fields.Selection(
        related="invoice_id.payment_state", string="Payment Status")
    amount_residual = fields.Monetary(
        related="invoice_id.amount_residual", string="Amount Due")

    payment_link = fields.Char(readonly=True, copy=False)
    payment_qr = fields.Binary(readonly=True, copy=False, attachment=True)

    def action_collect_payment(self):
        """Raise the invoice if needed, then produce a link and QR."""
        self.ensure_one()

        if self.source == "airbnb":
            raise UserError(_(
                "Airbnb guests pay Airbnb directly. This booking is settled "
                "against the Airbnb clearing account when the payout arrives."
            ))
        if self.state != "confirmed":
            raise UserError(_("Confirm the reservation before taking payment."))
        if not self.gross_amount:
            raise UserError(_("Set a booking value before taking payment."))

        invoice = self.invoice_id or self._create_invoice()

        if invoice.payment_state in ("paid", "in_payment"):
            raise UserError(_("This booking has already been paid."))

        self._build_payment_link(invoice)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("Payment link ready. Show the QR code to the "
                             "guest, or copy the link to send it."),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _create_invoice(self):
        self.ensure_one()
        if not self.sale_order_id:
            raise UserError(_("No sale order exists for this reservation."))

        invoice = self.sale_order_id.sudo()._create_invoices().sudo(False)
        if not invoice:
            raise UserError(_("The sale order produced no invoice. Check "
                              "that it has an invoiceable line."))
        invoice.invoice_date = fields.Date.context_today(self)
        invoice.action_post()
        self.invoice_id = invoice
        return invoice

    def _build_payment_link(self, invoice):
        """Portal URL plus a QR image of it.

        The access token is what lets an unauthenticated guest open the
        page, so the link is sensitive - anyone holding it can view the
        invoice.
        """
        self.ensure_one()
        invoice._portal_ensure_token()
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        url = f"{base}{invoice.get_portal_url()}"
        self.payment_link = url

        try:
            qr = self.env["ir.actions.report"].barcode(
                "QR", url, width=300, height=300)
            self.payment_qr = base64.b64encode(qr)
        except Exception as e:
            # Barcode rendering needs the rlPyCairo backend. Without it
            # the link still works; only the QR is unavailable.
            _logger.warning("QR generation failed: %s", e)
            self.payment_qr = False

    def action_open_invoice(self):
        self.ensure_one()
        if not self.invoice_id:
            raise UserError(_("No invoice has been raised yet."))
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": self.invoice_id.id,
            "view_mode": "form",
        }