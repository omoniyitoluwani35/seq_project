# -*- coding: utf-8 -*-
import logging
from datetime import timedelta
import psycopg2

import requests

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

try:
    from icalendar import Calendar
except ImportError:
    Calendar = None
    _logger.warning("icalendar library not installed - iCal import disabled")


class AirbnbIcalBlock(models.Model):
    """What Airbnb's own calendar says is blocked for a unit.

    Deliberately NOT a reservation. Gmail parsing creates reservations
    with guest names and amounts; this feed only carries dates. Storing
    them separately lets us compare the two sources and surface
    disagreements instead of silently trusting either one.
    """

    _name = "airbnb.ical.block"
    _description = "Airbnb Calendar Block (imported)"
    _order = "start_date desc"

    _uid_unit_uniq = models.Constraint(
        "unique(uid, unit_id)",
        "Duplicate calendar block for this unit.",
    )

    unit_id = fields.Many2one("airbnb.unit", required=True, ondelete="cascade")
    uid = fields.Char(required=True, index=True)
    start_date = fields.Date(required=True)
    end_date = fields.Date(required=True)
    summary = fields.Char()

    matched_reservation_id = fields.Many2one("airbnb.reservation", readonly=True)
    is_unmatched = fields.Boolean(
        compute="_compute_is_unmatched",
        store=True,
        help="Airbnb thinks these dates are booked but Odoo has no "
        "matching reservation. Usually means a booking email was never "
        "parsed - investigate before the dates are resold.",
    )

    _sql_constraints = [
        ("uid_unit_uniq", "unique(uid, unit_id)", "Duplicate calendar block."),
    ]

    @api.depends("matched_reservation_id")
    def _compute_is_unmatched(self):
        for rec in self:
            rec.is_unmatched = not rec.matched_reservation_id


class AirbnbUnitIcal(models.Model):
    _inherit = "airbnb.unit"

    ical_feed_url = fields.Char(
        compute="_compute_ical_feed_url",
        string="Odoo Export URL",
        help="Paste this into Airbnb: Calendar > Availability > "
        "Import Calendar. Airbnb polls it roughly hourly.",
    )
    ical_block_ids = fields.One2many("airbnb.ical.block", "unit_id")
    last_ical_import = fields.Datetime(readonly=True)
    ical_import_error = fields.Text(readonly=True)

    def _compute_ical_feed_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        for rec in self:
            rec.ical_feed_url = (
                f"{base}/airbnb/ical/{rec.ical_token}.ics" if rec.ical_token else False
            )

    def action_import_ical(self):
        """Fetch this unit's Airbnb export feed and reconcile it against
        our own reservations.

        Takes a row-level lock per unit so a cron run and a manual UI
        trigger can't collide - without this, concurrent imports on the
        same unit raise SerializationFailure in Postgres.
        """
        if Calendar is None:
            raise UserError(_("Install the 'icalendar' Python package first."))

        for unit in self:
            if not unit.airbnb_ical_import_url:
                continue

            # NOWAIT: if another process holds the lock, skip this unit
            # rather than queueing behind it. The next cron run retries,
            # so a skipped import is never a lost import.
            try:
                self.env.cr.execute(
                    "SELECT id FROM airbnb_unit WHERE id = %s FOR UPDATE NOWAIT",
                    (unit.id,),
                )
            except psycopg2.errors.LockNotAvailable:
                self.env.cr.rollback()
                _logger.info(
                    "iCal import for %s skipped - already running elsewhere",
                    unit.display_name,
                )
                continue

            try:
                resp = requests.get(unit.airbnb_ical_import_url, timeout=30)
                resp.raise_for_status()
                unit._sync_ical_blocks(resp.text)
                unit.write({
                    "last_ical_import": fields.Datetime.now(),
                    "ical_import_error": False,
                })
                self.env.cr.commit()
            except Exception as e:
                self.env.cr.rollback()
                _logger.exception("iCal import failed for %s", unit.display_name)
                unit.ical_import_error = str(e)
                self.env.cr.commit()

    def _sync_ical_blocks(self, ics_text):
        """Parse the feed and upsert blocks, removing ones Airbnb dropped."""
        self.ensure_one()
        Block = self.env["airbnb.ical.block"]
        cal = Calendar.from_ical(ics_text)

        seen_uids = []
        for component in cal.walk("VEVENT"):
            uid = str(component.get("UID") or "")
            start = component.get("DTSTART")
            end = component.get("DTEND")
            if not uid or not start or not end:
                continue

            # .dt is a date for VALUE=DATE, a datetime otherwise.
            start_date = start.dt
            end_date = end.dt
            if hasattr(start_date, "date"):
                start_date = start_date.date()
            if hasattr(end_date, "date"):
                end_date = end_date.date()

            seen_uids.append(uid)
            vals = {
                "unit_id": self.id,
                "uid": uid,
                "start_date": start_date,
                "end_date": end_date,
                "summary": str(component.get("SUMMARY") or ""),
            }
            existing = Block.search(
                [("uid", "=", uid), ("unit_id", "=", self.id)], limit=1
            )
            block = existing.write(vals) or existing if existing else Block.create(vals)
            (existing or block)._match_reservation()

        # Airbnb removed these - a cancellation on their side.
        stale = Block.search([
            ("unit_id", "=", self.id),
            ("uid", "not in", seen_uids),
        ])
        for block in stale:
            placeholder = block.matched_reservation_id
            if placeholder and placeholder.from_ical and placeholder.state == "confirmed":
                placeholder.action_cancel()
        stale.unlink()

    @api.model
    def _cron_import_ical(self):
        self.search([("airbnb_ical_import_url", "!=", False)]).action_import_ical()


class AirbnbIcalBlockMatch(models.Model):
    _inherit = "airbnb.ical.block"

    def _match_reservation(self):
        """Link this block to a reservation covering the same dates, or
        create a placeholder if none exists.

        The placeholder is what makes Airbnb bookings actually block the
        calendar: it lands in the Gantt and trips the overlap constraint
        on airbnb.reservation, so staff physically cannot create a
        conflicting booking over those dates.
        """
        for block in self:
            match = self.env["airbnb.reservation"].search([
                ("unit_id", "=", block.unit_id.id),
                ("state", "in", ("confirmed", "done")),
                ("checkin_date", "<=", block.start_date),
                ("checkout_date", ">=", block.end_date),
            ], limit=1)

            if not match:
                match = block._create_placeholder_reservation()

            block.matched_reservation_id = match

    def _create_placeholder_reservation(self):
        """Create a confirmed Airbnb reservation from calendar data alone.

        Deliberately minimal: iCal gives dates only, no guest name or
        amount. Gmail parsing enriches this record later by matching on
        unit + dates, rather than creating a second one.
        """
        self.ensure_one()
        partner = self.env.ref("base.public_partner", raise_if_not_found=False)
        if not partner:
            partner = self.env["res.partner"].search([("name", "=", "Airbnb Guest")], limit=1)
            if not partner:
                partner = self.env["res.partner"].create({"name": "Airbnb Guest"})

        return self.env["airbnb.reservation"].create({
            "unit_id": self.unit_id.id,
            "guest_partner_id": partner.id,
            "source": "airbnb",
            "booking_type": "instant",
            "state": "reserved",
            "checkin_date": self.start_date,
            "checkout_date": self.end_date,
            "gross_amount": (unit.nightly_rate or 0.0)
                            * (checkout - checkin).days,
            "from_ical": True,
            "notes": f"Auto-created from Airbnb calendar feed. UID: {self.uid}",
        })