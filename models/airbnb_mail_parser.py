# -*- coding: utf-8 -*-
"""Parse forwarded Airbnb notification emails into reservations.

Mail arrives through Odoo's incoming mail server (IMAP); Odoo handles
fetching, decoding and MIME walking. This module classifies a message
and extracts booking data from it.

Every pattern here was developed against real Airbnb emails from the
client's own account (Aug 2026 templates) rather than assumed, and the
behaviours below were observed rather than guessed:

  - Mail arrives FORWARDED, so the real sender and subject live inside
    the body and have to be recovered before anything else.
  - Two Airbnb senders exist. automated@airbnb.com carries bookings;
    express@airbnb.com carries guest messages whose subject reads
    "Reservation for ..." and which must never create a booking.
  - Some templates omit the year ("Sun, Aug 30"), so it is inferred
    from the send date.
  - Confirmation emails carry BOTH the guest total and the host payout,
    so no separate payout email is needed.

MONEY. Airbnb prices, discounts and pays this host in USD, while the
company's books are in Naira. The email is therefore the only reliable
record of what a stay actually sold for: the unit's nightly_rate is a
list price that Airbnb's own discounts move away from, so using it
would silently overstate revenue on every discounted booking.

So the reservation is recorded in USD and the invoice is raised in USD.
Odoo still stores a Naira book value against it - every journal line
must carry one - but that figure is provisional, computed at the
invoice-date rate. The real Naira is settled later: the booking sits in
the Airbnb clearing account in dollars until Airbnb pays out, and the
rate difference falls to exchange gain/loss when the payout reconciles.

The unit's nightly rate survives only as a fallback for a booking whose
money block could not be read at all. When that fallback is used the
amount is Naira, NOT dollars, and the record is flagged for review -
mislabelling a Naira figure as USD would overstate it some 1,350-fold.

The calendar feed is the first source of truth: iCal blocks the nights
as soon as Airbnb publishes them, and the email arrives later carrying
the guest, the code and the money. So a confirmation email ENRICHES the
placeholder the feed already created rather than creating a second
reservation for the same stay.

On failure this module RAISES rather than returning. Odoo's message_new
contract requires returning a record, and a reservation without a unit
is meaningless - the not-null constraint on unit_id would reject it
anyway. Fetchmail catches the exception, logs it, and moves to the next
message, which is the behaviour we want.
"""
import logging
import re
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

MONTHS = ("January|February|March|April|May|June|July|August|September|"
          "October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
          "Sept|Sep|Oct|Nov|Dec")

DECORATIVE = {"airbnb", "app store", "google play", "instagram",
              "tiktok", "youtube", "twitter", "facebook"}


class AirbnbReservation(models.Model):
    _inherit = "airbnb.reservation"

    airbnb_listing_id = fields.Char(
        string="Airbnb Listing ID",
        index=True,
        copy=False,
        help="Numeric listing identifier taken from the email. More "
        "reliable than the listing title, which the host can rename.",
    )
    airbnb_host_fee = fields.Monetary(
        string="Airbnb Commission",
        readonly=True,
        help="Host service fee Airbnb deducts before paying out.",
    )
    airbnb_occupancy_tax = fields.Monetary(
        string="Occupancy Tax",
        readonly=True,
        help="Collected and remitted by Airbnb on the host's behalf. "
        "Not company revenue.",
    )
    airbnb_guest_total = fields.Monetary(
        string="Guest Paid",
        readonly=True,
        help="Total charged to the guest, including tax Airbnb remits.",
    )
    source_message_id = fields.Char(readonly=True, copy=False, index=True)
    parse_confidence = fields.Selection(
        [("high", "High"), ("low", "Needs Review")],
        readonly=True, copy=False,
    )
    parse_notes = fields.Text(readonly=True, copy=False)

    # ================================================= mail gateway

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        subject = msg_dict.get("subject") or ""
        body = html2plaintext(msg_dict.get("body") or "")
        received = self._airbnb_received_date(msg_dict)

        sender, subject, body_urls = self._airbnb_unwrap(subject, body)
        text = self._airbnb_clean(body_urls)
        kind = self._airbnb_classify(sender, subject)

        _logger.info("Airbnb mail: kind=%s sender=%s subject=%s",
                     kind, sender, subject[:70])

        # Guest messages, reminders and anything unrecognised are not
        # bookings, so there is no record to return.
        if kind in ("message", "reminder", "unknown"):
            raise ValueError(
                "Not an Airbnb booking notification (kind=%s): %s"
                % (kind, subject[:100])
            )

        code = self._airbnb_code(subject, text)

        if kind == "cancellation":
            existing = code and self.search(
                [("airbnb_confirmation_code", "=", code)], limit=1)
            if existing:
                if existing.state != "cancelled":
                    existing.action_cancel()
                    existing.message_post(
                        body=_("Cancelled per Airbnb notification email."))
                return existing
            raise ValueError(
                "Cancellation for booking %s, which is not in the system."
                % (code or "with no readable code")
            )

        if kind == "payout":
            existing = code and self.search(
                [("airbnb_confirmation_code", "=", code)], limit=1)
            if existing:
                money = self._airbnb_amounts(text)
                if money["payout"]:
                    existing.write({
                        "payout_amount": money["payout"],
                        "payout_state": "received",
                    })
                    existing.message_post(body=_(
                        "Payout of %s recorded from Airbnb notification.")
                        % money["payout"])
                return existing
            raise ValueError(
                "Payout notice for booking %s, which is not in the system."
                % (code or "with no readable code")
            )

        if kind in ("confirmation", "request", "inquiry"):
            return self._airbnb_build(kind, sender, subject, text, body_urls,
                                      received, msg_dict, custom_values)

        raise ValueError("Unhandled Airbnb email kind: %s" % kind)

    # ================================================= preparation

    @api.model
    def _airbnb_received_date(self, msg_dict):
        d = msg_dict.get("date")
        if isinstance(d, datetime):
            return d
        if isinstance(d, str):
            try:
                return fields.Datetime.to_datetime(d)
            except (ValueError, TypeError):
                pass
        return fields.Datetime.now()

    @api.model
    def _airbnb_unwrap(self, subject, body):
        """Recover the original sender and subject from a forwarded mail
        and drop the forwarding header block.

        html2plaintext treats <angle-bracketed> addresses as HTML tags
        and strips them, so the From: line usually arrives without its
        address. The sender is therefore searched for anywhere in the
        body rather than parsed out of the header.
        """
        sender = None
        m = re.search(
            r"\b((?:automated|express|noreply|no-reply)@airbnb\.com)\b",
            body, re.I)
        if m:
            sender = m.group(1).lower()

        m = re.search(r"^Subject:\s*(.+?)(?=^\s*To:)", body,
                      re.MULTILINE | re.DOTALL)
        if m:
            subject = " ".join(m.group(1).split())

        m = re.search(r"^To:\s*<[^>]*>\s*$", body, re.MULTILINE)
        if m:
            body = body[m.end():]

        subject = re.sub(r"^\s*(Fwd|FW|RE):\s*", "", subject,
                         flags=re.I).strip()
        return sender, subject, body

    @api.model
    def _airbnb_clean(self, body):
        body = re.sub(r"<https?://[^>]*>", " ", body)
        body = body.replace("͏", "").replace("­", "")
        body = re.sub(r"[ \t]+", " ", body)
        return re.sub(r"\n{3,}", "\n\n", body)

    # ================================================= classification

    @api.model
    def _airbnb_classify(self, sender, subject):
        """Order matters. express@ is rejected first because its subject
        reads "Reservation for ..." and would otherwise be mistaken for a
        confirmation. Cancellations are checked before confirmations
        because they quote the original reservation.

        The "Reservation for ..." subject is also treated as a message
        when the sender could not be read, since a genuine confirmation
        always says "Reservation confirmed".
        """
        s = (subject or "").lower()

        if sender and "express@" in sender:
            return "message"
        if s.startswith(("canceled", "cancelled")) or "canceled:" in s:
            return "cancellation"
        if s.startswith("inquiry") or "inquiry for" in s:
            return "inquiry"
        if "reservation confirmed" in s or "booking confirmed" in s:
            return "confirmation"
        if "reminder" in s:
            return "reminder"
        if "payout" in s or "sent you money" in s or "you've been paid" in s:
            return "payout"
        if "reservation request" in s or "wants to book" in s:
            return "request"
        if s.startswith("reservation for"):
            return "message"
        return "unknown"

    # ================================================= extraction

    @api.model
    def _airbnb_listing_id(self, raw_body):
        m = re.search(r"Listing\s*#(\d{10,25})", raw_body)
        if m:
            return m.group(1)
        m = re.search(r"airbnb\.com/(?:rooms|hosting/listings)/(\d{10,25})",
                      raw_body)
        return m.group(1) if m else None

    @api.model
    def _airbnb_listing_name(self, text):
        """Titles appear as an [image: ...] alt tag. Guest avatars use the
        same form, so short values are skipped - a listing title carries
        location or descriptive text and is always longer."""
        for m in re.finditer(r"\[image:\s*([^\]]+?)\]", text):
            name = " ".join(m.group(1).split())
            if name.lower() in DECORATIVE:
                continue
            if len(name) > 15:
                return name
        return None

    @api.model
    def _airbnb_code(self, subject, text):
        m = re.search(r"\b(HM[A-Z0-9]{8})\b", subject or "")
        if m:
            return m.group(1)
        m = re.search(r"Confirmation code\s*\n+\s*([A-Z0-9]{8,12})", text)
        if m:
            return m.group(1)
        m = re.search(r"\b(HM[A-Z0-9]{8})\b", text)
        return m.group(1) if m else None

    @api.model
    def _airbnb_mkdate(self, month, day, year):
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(f"{month} {day} {year}", fmt).date()
            except ValueError:
                continue
        return None

    @api.model
    def _airbnb_dates(self, subject, text, received):
        """Two layouts occur in the body: "Check-in\\n\\nSun, Aug 30" and
        "Check-in\\nFriday\\n\\nDecember 18, 2026". Cancellations carry the
        range in the subject instead.

        Where the year is absent it is taken from the send date, then
        rolled forward if that would place the stay in the past."""
        blk = (rf"(?:\s*\n+\s*)(?:[A-Za-z]+,?\s*\n*\s*)?"
               rf"({MONTHS})\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?")
        ci = re.search(rf"Check-?in{blk}", text, re.I)
        co = re.search(rf"Check-?out{blk}", text, re.I)

        if ci and co:
            a = self._airbnb_mkdate(ci.group(1), ci.group(2),
                                    ci.group(3) or received.year)
            b = self._airbnb_mkdate(co.group(1), co.group(2),
                                    co.group(3) or received.year)
            if a and b:
                if not ci.group(3) and a < received.date() - timedelta(days=30):
                    a = a.replace(year=a.year + 1)
                    b = b.replace(year=b.year + 1)
                if b <= a:
                    b = b.replace(year=b.year + 1)
                return a, b

        m = re.search(rf"({MONTHS})\s+(\d{{1,2}})\s*[-–—]\s*"
                      rf"(\d{{1,2}})(?:,\s*(\d{{4}}))?", subject or "", re.I)
        if m:
            yr = int(m.group(4) or received.year)
            a = self._airbnb_mkdate(m.group(1), m.group(2), yr)
            b = self._airbnb_mkdate(m.group(1), m.group(3), yr)
            if a and b:
                if not m.group(4) and a < received.date() - timedelta(days=30):
                    a = a.replace(year=a.year + 1)
                    b = b.replace(year=b.year + 1)
                return a, b
        return None, None

    @api.model
    def _airbnb_amounts(self, text):
        """Break the confirmation's money block into its parts.

        The guest total is NOT the host's revenue. Airbnb collects
        occupancy tax and remits it to the authority itself, so it never
        belongs to the host. Invoicing the guest total would overstate
        income and create a tax liability that is not the company's.

          room fee            52.00   <- revenue
          occupancy tax        3.90   <- Airbnb's, not ours
          guest total         55.90
          host service fee     8.66   <- commission expense
          host payout         43.34   <- cash eventually received
        """
        out = {"room_fee": None, "host_fee": None, "occupancy_tax": None,
               "guest_total": None, "payout": None, "currency": None,
               "nights": None}

        m = re.search(r"(\d+)\s+nights?\s+room fee\s*\n+\s*"
                      r"[$₦]?\s*([\d,]+\.?\d{0,2})", text, re.I)
        if m:
            out["nights"] = int(m.group(1))
            out["room_fee"] = float(m.group(2).replace(",", ""))

        m = re.search(r"Host service fee[^\n]*\n+\s*-?[$₦]?\s*"
                      r"([\d,]+\.?\d{0,2})", text, re.I)
        if m:
            out["host_fee"] = float(m.group(1).replace(",", ""))

        m = re.search(r"Occupancy taxes?\s*\n+\s*[$₦]?\s*"
                      r"([\d,]+\.?\d{0,2})", text, re.I)
        if m:
            out["occupancy_tax"] = float(m.group(1).replace(",", ""))

        m = re.search(r"Total\s*\((\w{3})\)\s*\n*\s*[$₦]?\s*"
                      r"([\d,]+\.?\d{0,2})", text, re.I)
        if m:
            out["currency"] = m.group(1).upper()
            out["guest_total"] = float(m.group(2).replace(",", ""))

        m = re.search(r"You\s+earn\s*[\s\S]{0,20}?[$₦]?\s*"
                      r"([\d,]+\.?\d{0,2})", text, re.I)
        if m:
            out["payout"] = float(m.group(1).replace(",", ""))

        m = re.search(r"Guest service fee\s*\n+\s*[$₦]?\s*"
                      r"([\d,]+\.?\d{0,2})", text, re.I)
        guest_fee = float(m.group(1).replace(",", "")) if m else 0.0

        # Derive whatever the patterns missed. html2plaintext flattens
        # Airbnb's HTML money table differently from the plain-text part
        # the patterns were written against, so a label and its amount do
        # not always land where expected. These identities always hold,
        # so arithmetic is more dependable than another regex:
        #
        #   guest total = room fee + occupancy tax + guest service fee
        #   host payout = room fee - host service fee
        if out["room_fee"] is not None:
            if out["payout"] is None and out["host_fee"] is not None:
                out["payout"] = round(out["room_fee"] - out["host_fee"], 2)
            if out["guest_total"] is None:
                out["guest_total"] = round(
                    out["room_fee"] + (out["occupancy_tax"] or 0.0)
                    + guest_fee, 2)

        # And the reverse, where the room fee itself was the one missed.
        if out["room_fee"] is None:
            if out["payout"] is not None and out["host_fee"] is not None:
                out["room_fee"] = round(out["payout"] + out["host_fee"], 2)
            elif out["guest_total"] is not None:
                out["room_fee"] = round(
                    out["guest_total"] - (out["occupancy_tax"] or 0.0)
                    - guest_fee, 2)

        return out

    @api.model
    def _airbnb_guest(self, subject, text, kind):
        if kind == "confirmation":
            m = re.search(
                r"Reservation confirmed\s*[-–]\s*(.+?)\s+arrives",
                subject or "")
            if m:
                return m.group(1).strip()
        if kind == "cancellation":
            m = re.search(r"your guest\s+([A-Z][a-zA-Z'’-]+)", text)
            if m:
                return m.group(1)
        if kind == "inquiry":
            m = re.search(r"Respond to\s+([A-Z][a-zA-Z'’-]+)"
                          r"[’']s inquiry", text)
            if m:
                return m.group(1)
        m = re.search(r"New booking confirmed!\s+([A-Z][a-zA-Z'’-]+)",
                      text)
        return m.group(1) if m else None

    # ================================================= unit matching

    @api.model
    def _airbnb_match_unit(self, listing_id, listing_name):
        """Match on the numeric listing id first: it is stable and exact.
        Fall back to the title only if the id is unknown, since titles
        can be edited on Airbnb at any time."""
        Unit = self.env["airbnb.unit"]
        if listing_id:
            unit = Unit.search(
                [("airbnb_listing_id", "=", listing_id)], limit=1)
            if unit:
                return unit
        if listing_name:
            units = Unit.search([("airbnb_listing_name", "!=", False)])
            matches = [u for u in units
                       if u.airbnb_listing_name.strip() == listing_name.strip()]
            if matches:
                return matches[0]
            matches = [u for u in units
                       if u.airbnb_listing_name in listing_name
                       or listing_name in u.airbnb_listing_name]
            if matches:
                return max(matches, key=lambda u: len(u.airbnb_listing_name))
        return Unit

    # ================================================= build

    @api.model
    def _airbnb_partner(self, name):
        name = (name or "").strip() or _("Airbnb Guest")
        partner = self.env["res.partner"].search(
            [("name", "=", name)], limit=1)
        return partner or self.env["res.partner"].create({"name": name})

    @api.model
    def _airbnb_currency(self, code):
        """Resolve the currency Airbnb reported, or None if it cannot be
        used - either Airbnb named something unexpected, or the currency
        exists in Odoo but is not active."""
        if not code:
            return None
        cur = self.env["res.currency"].search([("name", "=", code)], limit=1)
        if cur and cur.active:
            return cur
        if cur:
            _logger.warning(
                "Airbnb reported %s but that currency is not active in Odoo",
                code)
        return None

    @api.model
    def _airbnb_booking_value(self, money, unit, nights, problems):
        """Decide what to store as the booking value, and in which
        currency.

        Three outcomes, and the distinction matters a great deal:

        1. The email's figure, in the currency Airbnb stated. This is
           the normal case and the only one that reflects any discount
           Airbnb applied.
        2. The email's figure read, but that currency is not usable in
           Odoo. The figure is NOT stored - a 52.00 dollar amount
           written into a Naira field understates the booking roughly
           1,350-fold, and would then be invoiced and settled at that
           value. The list rate is used instead and the record flagged.
        3. No readable money block at all. The unit's list rate stands
           in, in company currency, flagged - Airbnb may have discounted
           away from it.

        Returns (amount, currency_or_None).
        """
        gross = money["room_fee"]
        reported = money["currency"]
        cur = self._airbnb_currency(reported)
        fallback = (unit.nightly_rate or 0.0) * nights if unit else 0.0

        if gross is not None and cur:
            return gross, cur

        if gross is not None and reported:
            problems.append(_(
                "Airbnb reported this booking in %(cur)s, which is not "
                "active in Odoo. The dollar figure has been discarded "
                "rather than recorded as %(company)s - activate %(cur)s "
                "under Accounting > Configuration > Currencies and "
                "re-enter the amount."
            ) % {"cur": reported,
                 "company": self.env.company.currency_id.name})
            return fallback, None

        problems.append(_(
            "Booking amount could not be read from the email. Falling "
            "back to %s's list rate, which Airbnb may have discounted "
            "away from - check before relying on this figure."
        ) % (unit.display_name if unit else "?"))
        return fallback, None

    @api.model
    def _airbnb_build(self, kind, sender, subject, text, raw_body,
                      received, msg_dict, custom_values=None):
        code = self._airbnb_code(subject, text)

        # Idempotency. The SQL constraint on the code is the hard
        # backstop; this keeps repeat emails out of the error log.
        if code:
            # Several messages carrying the same confirmation code can
            # arrive in one fetch. An unflushed insert is invisible to
            # search(), so without this the second reaches create(),
            # trips the unique constraint, and the aborted transaction
            # rolls back the first one too - leaving nothing at all.
            self.env.flush_all()
            dup = self.search(
                [("airbnb_confirmation_code", "=", code)], limit=1)
            if dup:
                _logger.info("Airbnb email for existing booking %s", code)
                return dup

        listing_id = self._airbnb_listing_id(raw_body)
        listing_name = self._airbnb_listing_name(text)
        unit = self._airbnb_match_unit(listing_id, listing_name)
        checkin, checkout = self._airbnb_dates(subject, text, received)
        guest = self._airbnb_guest(subject, text, kind)
        money = self._airbnb_amounts(text)
        payout = money["payout"]
        currency = money["currency"]
        nights = (checkout - checkin).days if (checkin and checkout) else 0

        problems = []
        if not unit:
            problems.append(_(
                "No unit matches Airbnb listing %(id)s (%(name)s). Set the "
                "listing ID on the unit record."
            ) % {"id": listing_id or "?", "name": listing_name or "?"})
        if not checkin or not checkout:
            problems.append(_("Stay dates could not be read."))
        if not code and kind != "inquiry":
            problems.append(_("Confirmation code could not be read."))
        if not guest:
            problems.append(_("Guest name could not be read."))

        # The booking value, and the currency it is denominated in.
        # Appends its own explanation to `problems` where it had to
        # fall back - see _airbnb_booking_value.
        amount, cur = self._airbnb_booking_value(money, unit, nights, problems)

        if kind == "confirmation":
            if not payout:
                problems.append(_("Host payout could not be read or derived."))
            if not money["guest_total"]:
                problems.append(_("Guest total could not be read or derived."))

        # Without a unit or dates there is nothing bookable, and the
        # not-null constraint on unit_id would reject the record anyway.
        if not unit or not checkin or not checkout:
            _logger.warning(
                "Airbnb parse failed: listing=%s dates=%s/%s subject=%s",
                listing_id, checkin, checkout, subject[:70])
            raise ValueError(
                "Could not build a reservation from this email: %s"
                % "; ".join(problems)
            )

        notes = "\n".join("- %s" % p for p in problems) or False

        # The payout figure is Airbnb's, in Airbnb's currency, so it is
        # only meaningful alongside a booking stored in that same
        # currency. On a fallback it would be dollars sitting next to a
        # Naira amount, which is worse than leaving it empty.
        stored_payout = payout if (cur and payout) else 0.0

        # The calendar feed may already have reserved these nights as a
        # placeholder - dates only, no guest, no amount. The email is the
        # richer source, so enrich that record rather than creating a
        # second reservation for the same stay, which would also trip the
        # overlap constraint.
        placeholder = self.search([
            ("unit_id", "=", unit.id),
            ("checkin_date", "=", checkin),
            ("checkout_date", "=", checkout),
            ("from_ical", "=", True),
            ("airbnb_confirmation_code", "=", False),
            ("state", "!=", "cancelled"),
        ], limit=1)

        if placeholder:
            if listing_id and placeholder.airbnb_listing_id \
                    and placeholder.airbnb_listing_id != listing_id:
                placeholder.message_post(body=_(
                    "Warning: the calendar feed placed this stay on "
                    "listing %(old)s but the confirmation email says "
                    "%(new)s. Check the unit is correct."
                ) % {"old": placeholder.airbnb_listing_id,
                     "new": listing_id})

            enrich = {
                "guest_partner_id": self._airbnb_partner(guest).id,
                "gross_amount": amount or placeholder.gross_amount,
                "payout_amount": stored_payout,
                "airbnb_confirmation_code": code,
                "airbnb_listing_id": listing_id
                or placeholder.airbnb_listing_id,
                "airbnb_host_fee": money["host_fee"] or 0.0,
                "airbnb_occupancy_tax": money["occupancy_tax"] or 0.0,
                "airbnb_guest_total": money["guest_total"] or 0.0,
                "source_message_id": msg_dict.get("message_id"),
                "parse_confidence": "low" if problems else "high",
                "parse_notes": notes,
                "raw_email_reference": text[:3000],
                # The feed created it; the email has now identified it.
                "from_ical": False,
            }
            # The feed priced the placeholder in company currency. Once
            # the email supplies a dollar figure the record has to carry
            # the dollar currency with it, or the invoice settles at a
            # Naira value that was never charged.
            if cur:
                enrich["currency_id"] = cur.id
            placeholder.write(enrich)

            placeholder.message_post(body=_(
                "Enriched from the Airbnb confirmation email: guest, "
                "confirmation code and amounts added to a stay the "
                "calendar feed had already reserved."))
            self._airbnb_post_money_note(placeholder, money, cur, unit, nights)

            # The placeholder was created already reserved by the feed,
            # without going through action_reserve(), so it has no sale
            # order - there was no value to put on one until this email
            # arrived. Invoicing refuses to run without it.
            if amount and not placeholder.sale_order_id:
                placeholder._create_sale_order()

            # Now that a real amount exists, raise the invoice the
            # placeholder was too empty to justify.
            if amount and not placeholder.invoice_id:
                try:
                    placeholder.action_create_and_settle_invoice()
                except Exception as e:
                    _logger.exception("Invoicing failed for %s",
                                      placeholder.name)
                    placeholder.message_post(body=_(
                        "Automatic invoicing failed: %s") % e)
            return placeholder

        vals = {
            "unit_id": unit.id,
            "guest_partner_id": self._airbnb_partner(guest).id,
            "source": "airbnb",
            # An inquiry is not a booking yet, so it stays provisional
            # and raises no invoice until someone reserves it.
            "booking_type": "instant" if kind == "confirmation" else "request",
            "state": "inquiry" if kind in ("inquiry", "request") else "hold",
            "checkin_date": checkin,
            "checkout_date": checkout,
            "gross_amount": amount,
            "payout_amount": stored_payout,
            "payout_state": "pending",
            "airbnb_host_fee": money["host_fee"] or 0.0,
            "airbnb_occupancy_tax": money["occupancy_tax"] or 0.0,
            "airbnb_guest_total": money["guest_total"] or 0.0,
            "airbnb_confirmation_code": code,
            "airbnb_listing_id": listing_id,
            "source_message_id": msg_dict.get("message_id"),
            "parse_confidence": "low" if problems else "high",
            "parse_notes": notes,
            "raw_email_reference": text[:3000],
        }

        # The currency must go into vals BEFORE the create. Writing it
        # afterwards would leave every Monetary field already rounded
        # and computed against the company currency.
        if cur:
            vals["currency_id"] = cur.id

        if custom_values:
            vals.update(custom_values)

        rec = self.create(vals)
        self._airbnb_post_money_note(rec, money, cur, unit, nights)

        # An Airbnb confirmation means the guest has already paid
        # Airbnb, so the nights are genuinely gone. A request or an
        # inquiry has not been paid for and stays provisional.
        #
        # Wrapped, because the booking is already saved by this point.
        # An accounting problem downstream - a missing journal, a locked
        # period, an unconfigured exchange account - should cost the
        # invoice, not the reservation.
        if kind == "confirmation" and amount:
            try:
                rec.action_reserve()
            except Exception as e:
                _logger.exception("Could not reserve %s", rec.display_name)
                rec.message_post(body=_(
                    "Automatic reservation failed: %s") % e)

        return rec

    @api.model
    def _airbnb_post_money_note(self, rec, money, cur, unit, nights):
        """Record what Airbnb actually said, on the booking.

        These are the figures the payout reconciliation is checked
        against weeks later, so they belong somewhere durable rather
        than only in the parsed fields - especially the commission,
        which is the difference the clearing account will have to
        explain.
        """
        if not money["guest_total"]:
            return
        if cur:
            rec.message_post(body=_(
                "Airbnb reported this booking in %(cur)s: guest paid "
                "%(total)s, room fee %(room)s, host service fee "
                "%(fee)s, payout %(payout)s. Recorded in %(cur)s; the "
                "%(company)s value is provisional until the payout "
                "reconciles."
            ) % {
                "cur": cur.name,
                "total": money["guest_total"],
                "room": money["room_fee"],
                "fee": money["host_fee"] or 0.0,
                "payout": money["payout"] or 0.0,
                "company": self.env.company.currency_id.name,
            })
        else:
            rec.message_post(body=_(
                "Airbnb's own figures could not be used, so this booking "
                "is priced at %(unit)s's list rate of %(rate)s x "
                "%(nights)s nights in %(company)s. Airbnb may have "
                "discounted away from that rate - check the booking "
                "against the email before the payout is reconciled."
            ) % {
                "unit": unit.display_name if unit else "?",
                "rate": unit.nightly_rate if unit else 0.0,
                "nights": nights,
                "company": self.env.company.currency_id.name,
            })