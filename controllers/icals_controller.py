# -*- coding: utf-8 -*-
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class AirbnbIcalController(http.Controller):

    @http.route(
        "/airbnb/ical/<string:token>.ics",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def unit_ical_feed(self, token, **kwargs):
        """Public iCal feed for one unit, protected by its ical_token.

        Registered in Airbnb under Calendar > Availability > Import
        Calendar. Airbnb polls this roughly hourly and blocks the dates
        it finds here.

        Only confirmed, non-Airbnb bookings are exported - Airbnb already
        knows its own reservations.
        """
        unit = request.env["airbnb.unit"].sudo().search(
            [("ical_token", "=", token)], limit=1
        )
        if not unit:
            _logger.warning("iCal feed requested with unknown token")
            return request.make_response("Not found", status=404)

        reservations = request.env["airbnb.reservation"].sudo().search([
            ("unit_id", "=", unit.id),
            ("state", "=", "confirmed"),
            ("source", "!=", "airbnb"),
        ])

        ics = self._build_ics(unit, reservations)
        return request.make_response(
            ics,
            headers=[
                ("Content-Type", "text/calendar; charset=utf-8"),
                ("Content-Disposition", f'inline; filename="{unit.name}.ics"'),
                ("Cache-Control", "no-cache, must-revalidate"),
            ],
        )

    def _build_ics(self, unit, reservations):
        """Hand-build the .ics text.

        DTSTART/DTEND use VALUE=DATE (all-day) deliberately. Using
        datetime values here is the single most common source of
        off-by-one-day bugs in hand-rolled feeds, because the server
        timezone then shifts the boundary.

        DTEND in iCal is EXCLUSIVE, which happens to match hotel
        checkout semantics exactly: a stay of Sep 4 -> Sep 5 blocks
        only the night of the 4th, leaving the 5th bookable.
        """
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//DBSoft//Airbnb Sync//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            f"X-WR-CALNAME:{self._escape(unit.display_name)}",
        ]

        for res in reservations:
            if not res.checkin_date or not res.checkout_date:
                continue
            lines += [
                "BEGIN:VEVENT",
                f"UID:{res.ical_uid or f'resa-{res.id}@airbnb-sync.dbsoft'}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{res.checkin_date.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{res.checkout_date.strftime('%Y%m%d')}",
                f"SUMMARY:{self._escape('Booked - ' + (res.guest_partner_id.name or 'Guest'))}",
                "STATUS:CONFIRMED",
                "TRANSP:OPAQUE",
                "END:VEVENT",
            ]

        lines.append("END:VCALENDAR")

        # RFC 5545 requires CRLF line endings. Some parsers tolerate LF;
        # do not rely on that.
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def _escape(text):
        """Escape per RFC 5545: backslash, semicolon, comma, newline."""
        if not text:
            return ""
        return (
            text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
        )