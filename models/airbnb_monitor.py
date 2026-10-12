# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# How long a feed can go unimported before we consider it broken.
ICAL_STALE_HOURS = 3


class AirbnbReservationCron(models.Model):
    _inherit = "airbnb.reservation"

    @api.model
    def _cron_complete_past_stays(self):
        """Flip confirmed reservations to done once checkout has passed.

        Runs daily. Without this, reservations sit in 'confirmed'
        forever and the overlap constraint keeps evaluating them, which
        also means the iCal feed keeps exporting stays from months ago.
        """
        today = fields.Date.context_today(self)
        past = self.search([
            ("state", "=", "confirmed"),
            ("checkout_date", "<", today),
        ])
        if past:
            past.action_complete()
            unpaid = past.filtered(
                lambda r: r.invoice_id
                and r.invoice_id.payment_state not in ("paid", "in_payment"))
            for rec in unpaid:
                rec.message_post(body=_(
                    "Stay completed with %(amt)s still outstanding on "
                    "invoice %(inv)s."
                ) % {"amt": rec.invoice_id.amount_residual,
                     "inv": rec.invoice_id.name})
            if unpaid:
                _logger.warning(
                    "%s stay(s) completed with payment outstanding", len(unpaid))
            _logger.info("Completed %s past reservation(s)", len(past))


class AirbnbSyncMonitor(models.AbstractModel):
    """Dead man's switch for the sync pipeline.

    Everything in this system fails silently - a stale feed, an expired
    OAuth token, and a parser that stops matching all look identical
    from the outside: nothing happens. This cron is what turns that into
    a visible alert.
    """

    _name = "airbnb.sync.monitor"
    _description = "Airbnb Sync Health Monitor"

    @api.model
    def _get_alert_recipients(self):
        """Users to notify. Set the 'airbnb.alert_emails' system parameter
        to a comma-separated list to override; otherwise falls back to
        admins, who can actually fix a broken sync.
        """
        override = self.env["ir.config_parameter"].sudo().get_param(
            "airbnb.alert_emails"
        )
        if override:
            return [e.strip() for e in override.split(",") if e.strip()]

        users = self.env["res.users"].search([
            ("active", "=", True),
            ("share", "=", False),
        ])
        admins = users.filtered(
            lambda u: u.email and u.has_group("base.group_system")
        )
        return admins.mapped("email")

    @api.model
    def _check_ical_health(self):
        """Return a list of human-readable problems with iCal imports."""
        problems = []
        threshold = fields.Datetime.now() - timedelta(hours=ICAL_STALE_HOURS)

        units = self.env["airbnb.unit"].search([
            ("airbnb_ical_import_url", "!=", False),
        ])

        for unit in units:
            if unit.ical_import_error:
                problems.append(
                    _("%(unit)s: import failing - %(error)s") % {
                        "unit": unit.display_name,
                        "error": (unit.ical_import_error or "")[:200],
                    }
                )
            elif not unit.last_ical_import:
                problems.append(
                    _("%(unit)s: calendar has never been imported") % {
                        "unit": unit.display_name
                    }
                )
            elif unit.last_ical_import < threshold:
                problems.append(
                    _("%(unit)s: last import was %(when)s (over %(hours)s hours ago)") % {
                        "unit": unit.display_name,
                        "when": unit.last_ical_import,
                        "hours": ICAL_STALE_HOURS,
                    }
                )
        return problems

    @api.model
    def _check_unmatched_blocks(self):
        """Airbnb thinks dates are booked but Odoo has no reservation.

        With placeholder creation enabled this should normally be empty -
        anything here means the placeholder step itself failed.
        """
        problems = []
        unmatched = self.env["airbnb.ical.block"].search([("is_unmatched", "=", True)])
        for block in unmatched:
            problems.append(
                _("%(unit)s: Airbnb shows %(start)s to %(end)s booked, "
                  "but Odoo has no matching reservation") % {
                    "unit": block.unit_id.display_name,
                    "start": block.start_date,
                    "end": block.end_date,
                }
            )
        return problems

    @api.model
    def _cron_health_check(self):
        """Assemble problems and email them. Silence means healthy -
        we deliberately do not send an all-clear, so a mail that does
        arrive always means something needs attention.
        """
        problems = self._check_ical_health() + self._check_unmatched_blocks()

        if not problems:
            _logger.info("Airbnb sync health check: all clear")
            return

        for problem in problems:
            _logger.warning("Airbnb sync issue: %s", problem)

        recipients = self._get_alert_recipients()
        if not recipients:
            _logger.error(
                "Airbnb sync problems detected but no alert recipients configured"
            )
            return

        body = _(
            "<p>The Airbnb sync health check found the following issues:</p><ul>%s</ul>"
            "<p>Until these are resolved, calendar availability in Odoo may not "
            "reflect what Airbnb shows, which risks double-booking.</p>"
        ) % "".join(f"<li>{p}</li>" for p in problems)

        self.env["mail.mail"].sudo().create({
            "subject": _("Airbnb sync: %s issue(s) need attention") % len(problems),
            "body_html": body,
            "email_to": ",".join(recipients),
            "auto_delete": True,
        }).send()


        self.env["mail.mail"].sudo().create({
            "subject": _("Airbnb sync: %s issue(s) need attention") % len(problems),
            "body_html": body,
            "email_to": ",".join(recipients.mapped("email")),
            "auto_delete": True,
        }).send()