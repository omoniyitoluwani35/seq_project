# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from datetime import timedelta

BLOCKING_STATES = ("reserved", "done")
PROVISIONAL_STATES = ("inquiry", "hold")
STATE_COLOURS = {
    "inquiry": 4,     # light blue
    "hold": 3,        # yellow
    "reserved": 10,   # green
    "done": 7,        # teal
    "cancelled": 1,   # red
}



class AirbnbReservation(models.Model):
    """Core booking record and state machine.

    State flow:
        pending    -> confirmed  (action_confirm)
        pending    -> cancelled  (action_cancel)
        confirmed  -> cancelled  (action_cancel)
        confirmed  -> done       (action_complete, after checkout date)

    Design notes tied directly to the hiccups already identified:
      - #7 (Instant Book vs Request-to-Book): a 'request' booking_type
        lands in 'pending' and does NOT touch the calendar or create an
        invoice until action_confirm() runs. An 'instant' or 'direct'
        booking is auto-confirmed on creation.
      - #8 (payout != booking value): gross_amount is what the guest
        was charged and what the sale order is built from. payout_amount
        is populated later, separately, from the payout notification and
        used only for bank reconciliation - the two are never conflated.
      - Idempotency: airbnb_confirmation_code carries a SQL uniqueness
        constraint so a duplicate/repeat confirmation email can never
        create a second sale order for the same booking.
      - Overlap protection: a python constraint blocks two CONFIRMED
        reservations on the same unit from having overlapping dates.
        Pending/cancelled reservations are excluded from this check on
        purpose - a still-pending request shouldn't block the calendar.
    """

    _name = "airbnb.reservation"
    _description = "Airbnb Reservation"
    _inherit = ["mail.thread"]
    _order = "checkin_date desc"

    _confirmation_code_uniq = models.Constraint(
        "unique(airbnb_confirmation_code)",
        "A reservation with this Airbnb confirmation code already exists - "
        "this looks like a duplicate or repeat confirmation email rather "
        "than a new booking.",
    )

    name = fields.Char(compute="_compute_name", store=True)

    unit_id = fields.Many2one(
        "airbnb.unit", required=True, tracking=True, ondelete="restrict"
    )
    property_id = fields.Many2one(
        related="unit_id.property_id", store=True, string="Property"
    )
    guest_partner_id = fields.Many2one(
        "res.partner", string="Guest", tracking=True
    )

    from_ical = fields.Boolean(
        string="From Calendar Feed",
        readonly=True,
        copy=False,
        help="Created from Airbnb's iCal feed rather than a parsed booking "
        "email, so it has dates but no guest details or amount yet.",
    )

    source = fields.Selection(
        [("airbnb", "Airbnb"), ("direct", "Direct / Physical"), ("other", "Other")],
        required=True,
        default="direct",
        tracking=True,
    )
    booking_type = fields.Selection(
        [("instant", "Instant Book"), ("request", "Request to Book")],
        help="Only meaningful when source = Airbnb. Direct/physical "
        "bookings are treated the same as Instant Book.",
    )

    state = fields.Selection(
        [
            ("inquiry", "Inquiry"),
            ("hold", "On Hold"),
            ("reserved", "Reserved"),
            ("done", "Completed"),
            ("cancelled", "Cancelled"),
        ],
        string="Status", default="hold", required=True,
        copy=False, index=True, tracking=True,
        help="Inquiry and On Hold leave the nights open to anyone else - "
             "several guests may hold the same dates at once. Only "
             "Reserved, which means invoiced and paid, takes the unit "
             "off the market.",
    )

    hold_expiry = fields.Datetime(
        string="Hold Expires", copy=False, tracking=True,
        help="An unpaid hold lapses automatically at this time. Blank "
             "means it never lapses.",
    )

    is_blocking = fields.Boolean(
        string="Blocks the Calendar",
        compute="_compute_is_blocking",
        store=True,
        help="True once the stay is paid for and the nights are no "
             "longer sellable. Used by the calendar and the overlap "
             "check.",
    )

    competing_hold_count = fields.Integer(
        string="Competing Holds",
        compute="_compute_competing_hold_count",
        help="Other provisional bookings wanting the same nights. More "
             "than zero means whoever pays first takes the unit.",
    )

    airbnb_host_fee = fields.Monetary(
        string="Airbnb Commission", readonly=True,
        help="Host service fee Airbnb deducts before paying out.")
    airbnb_occupancy_tax = fields.Monetary(
        string="Occupancy Tax", readonly=True,
        help="Collected and remitted by Airbnb. Not company revenue.")
    airbnb_guest_total = fields.Monetary(
        string="Guest Paid", readonly=True,
        help="Total charged to the guest, including tax Airbnb remits.")

    checkin_date = fields.Date(required=True, tracking=True, 
            default=fields.Date.context_today)

    checkout_date = fields.Date(required=True, tracking=True, 
            default=lambda self: fields.Date.context_today(self) + timedelta(days=1))

    # Idempotency key - globally unique across all units since Airbnb
    # confirmation codes are unique platform-wide. Left blank for
    # direct/physical bookings, which have no such code.
    airbnb_confirmation_code = fields.Char(copy=False)
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id
    )

    discount_type = fields.Selection(
        [("fixed", "Fixed Amount"), ("percent", "Percentage")],
        string="Discount Type",
        help="Type of discount applied to the booking",
    )
    discount_percent = fields.Float(
        string = "Percentage",
        help = ""
    )


    discount = fields.Monetary(
        string = "Discount",
        help= "Discount value for the customer"  
         )

    room_value = fields.Monetary(
        store=True, 
        compute="_onchange_default_price",
        string="Room Rate",
        help="Value for the room"
    )
    gross_amount = fields.Monetary(
        store=True, 
        compute="_onchange_default_price",
        string="Amt Total",
        help="What the guest was charged, taken from the booking "
        "confirmation email. This is what the sale order is built from.",
    )

    payout_amount = fields.Monetary(
        string="Airbnb Payout",
        help="Net amount Airbnb actually pays out, taken from the "
        "separate payout notification email. Used for bank "
        "reconciliation only - never used as the invoice amount.",
    )
    payout_state = fields.Selection(
        [
            ("pending", "Awaiting Payout"),
            ("received", "Received"),
            ("reconciled", "Reconciled"),
        ],
        default="pending",
        tracking=True,
    )

    sale_order_id = fields.Many2one("sale.order", readonly=True, copy=False)

    # Stable identifier for the iCal VEVENT UID - deterministic and
    # never regenerated, so repeated feed rebuilds don't create
    # duplicate or orphaned events on the Airbnb side (hiccup #3).
    ical_uid = fields.Char(readonly=True, copy=False)

    raw_email_reference = fields.Text(
        help="Optional: store the parsed email's Message-ID or raw "
        "snippet here for debugging when a parse looks wrong."
    )
    notes = fields.Text()
    nights = fields.Integer(compute="_compute_nights", store=True)

    color_index = fields.Integer(
        string="Colour",
        compute="_compute_color_index",
        store=True,
        help="Drives the pill colour on the calendar, derived from the "
             "state.",
    )

    @api.depends("state")
    def _compute_color_index(self):
        for rec in self:
            rec.color_index = STATE_COLOURS.get(rec.state, 0)

    @api.depends("state")
    def _compute_is_blocking(self):
        for rec in self:
            rec.is_blocking = rec.state in BLOCKING_STATES

    @api.depends("unit_id", "checkin_date", "checkout_date", "state")
    def _compute_competing_hold_count(self):
        for rec in self:
            if rec.state == "cancelled" or not rec._has_stay():
                rec.competing_hold_count = 0
                continue
            rec.competing_hold_count = self.search_count(
                rec._overlap_domain(states=PROVISIONAL_STATES))

    def _has_stay(self):
        self.ensure_one()
        return bool(self.unit_id and self.checkin_date and self.checkout_date)

    def _overlap_domain(self, states):
        """Stays on the same unit sharing at least one night.

        Checkout is exclusive: a stay ending on the 20th and one
        starting on the 20th do not overlap, because the room is empty
        by midday and ready for the next guest.
        """
        self.ensure_one()
        domain = [
            ("unit_id", "=", self.unit_id.id),
            ("checkin_date", "<", self.checkout_date),
            ("checkout_date", ">", self.checkin_date),
        ]
        if self.id:
            domain.insert(0, ("id", "!=", self.id))
        if states:
            domain.append(("state", "in", list(states)))
        return domain

    @api.model
    def _default_hold_expiry(self):
        hours = int(self.env["ir.config_parameter"].sudo().get_param(
            "airbnb.hold_hours", 48))
        if hours <= 0:
            return False
        return fields.Datetime.now() + timedelta(hours=hours)

    @api.depends("checkin_date", "checkout_date")
    def _compute_nights(self):
        for rec in self:
            if rec.checkin_date and rec.checkout_date:
                rec.nights = (rec.checkout_date - rec.checkin_date).days
            else:
                rec.nights = 0

    @api.depends("unit_id", "checkin_date", "checkout_date", "discount", "room_value", "discount_type", "discount_percent")
    def _onchange_default_price(self):
        """Fill the amount from the unit's rate when the user has not
        set one. Only fires on manual entry - parsed Airbnb bookings
        carry their own figures and must not be overwritten."""
        for rec in self:
            if rec.source == "airbnb":
                rec.room_value = rec.room_value or 0.0
                rec.gross_amount = rec.gross_amount or 0.0
                continue
            
            nights = (rec.checkout_date - rec.checkin_date).days
            rate = rec.unit_id.nightly_rate
            if not rate:
                continue

            rec.currency_id = rec.unit_id.currency_id
            rec.room_value = rate * nights
            if rec.discount_type in ("","fixed", False ):
                rec.gross_amount = rec.room_value - rec.discount
            if rec.discount_type == "percent" :
                discounted = rec.room_value  * rec.discount_percent/100
                rec.gross_amount = rec.room_value - discounted
                
    

    @api.depends("unit_id.display_name", "checkin_date", "checkout_date")
    def _compute_name(self):
        for rec in self:
            if rec.unit_id and rec.checkin_date and rec.checkout_date:
                rec.name = (
                    f"{rec.unit_id.display_name}: "
                    f"{rec.checkin_date} -> {rec.checkout_date}"
                )
            else:
                rec.name = _("New Reservation")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.ical_uid:
                rec.ical_uid = f"resa-{rec.id}@airbnb-sync.dbsoft"
            # Direct bookings and Instant Book are auto-confirmed;
            # Request-to-Book sits in pending until action_confirm().
            if rec.state == "pending" and rec.booking_type != "request":
                rec.action_confirm()
        return records

    def write(self, vals):
        """A confirmed booking that already carries an invoice must not
        have its value quietly changed underneath it. The invoice is what
        the guest owes; if the booking genuinely changes, the invoice has
        to be credited and reissued rather than the two drifting apart.
        """
        financial = {"gross_amount", "checkin_date", "checkout_date", "unit_id"}
        if financial & set(vals):
            for rec in self:
                if rec.invoice_id and rec.invoice_id.state == "posted":
                    raise UserError(_(
                        "%(name)s has already been invoiced (%(inv)s). Cancel "
                        "or credit that invoice before changing the booking."
                    ) % {"name": rec.name, "inv": rec.invoice_id.name})
        return super().write(vals)

    @api.constrains("checkin_date", "checkout_date")
    def _check_dates(self):
        for rec in self:
            if rec.checkin_date and rec.checkout_date and rec.checkin_date >= rec.checkout_date:
                raise ValidationError(_("Check-out date must be after check-in date."))


    @api.constrains("unit_id", "state", "checkin_date", "checkout_date")
    def _check_unit_bookable(self):
        """Stop new confirmed bookings on a unit that's out of service.

        Only checks on confirm - a reservation that was already confirmed
        before maintenance was scheduled is left alone, since cancelling
        an existing guest's booking should be a deliberate decision, not
        a side effect of flipping a status field.
        """
        for rec in self:
            if rec.state != "confirmed" or not rec.unit_id:
                continue
            unit = rec.unit_id
            if unit.state == "inactive":
                raise ValidationError(
                    _("%s is not bookable.") % unit.display_name
                )
            if unit.state == "maintenance":
                # If maintenance has dates, only block overlapping stays.
                if unit.maintenance_from and unit.maintenance_to:
                    overlaps = (
                        rec.checkin_date < unit.maintenance_to
                        and rec.checkout_date > unit.maintenance_from
                    )
                    if not overlaps:
                        continue
                raise ValidationError(
                    _("%(unit)s is under maintenance%(reason)s and cannot "
                      "be booked for these dates.") % {
                        "unit": unit.display_name,
                        "reason": f" ({unit.maintenance_reason})" if unit.maintenance_reason else "",
                    }
                )
    @api.constrains("unit_id", "checkin_date", "checkout_date", "state")
    def _check_no_overlap(self):
        """A unit can only be SOLD once for a given night.

        Holds and inquiries are exempt by design. Several guests may be
        holding the same nights at the same time; the first to pay takes
        them, and this constraint is what stops a second one reaching
        that point.
        """
        for rec in self:
            if rec.state not in BLOCKING_STATES or not rec._has_stay():
                continue
            clash = self.search(
                rec._overlap_domain(states=BLOCKING_STATES), limit=1)
            if clash:
                raise ValidationError(_(
                    "%(unit)s is already reserved for those nights.\n\n"
                    "%(who)s has it from %(start)s to %(end)s, paid for "
                    "on %(ref)s.\n\n"
                    "Cancel that reservation first, or put this one on "
                    "hold instead - a hold does not take the unit off "
                    "the market, and whoever pays first gets it."
                ) % {
                    "unit": rec.unit_id.display_name,
                    "who": clash.guest_partner_id.name or _("Another guest"),
                    "start": clash.checkin_date,
                    "end": clash.checkout_date,
                    "ref": clash.invoice_id.name or clash.display_name,
                })

    def action_hold(self):
        """Write the booking down without taking the unit off sale."""
        for rec in self:
            if rec.state in BLOCKING_STATES:
                raise UserError(_(
                    "%s has already been paid for. Cancel it instead - it "
                    "cannot go back to being a hold."
                ) % rec.display_name)
            rec.write({
                "state": "hold",
                "hold_expiry": rec.hold_expiry or rec._default_hold_expiry(),
            })
        return True

    def action_reserve(self):
        """Take the money, then take the unit off the market.

        The order matters. The state is written FIRST so the overlap
        constraint fires before any invoice exists - otherwise a clash
        leaves an invoice behind for nights that were never sellable.
        """
        for rec in self:
            if rec.state == "cancelled":
                raise UserError(_(
                    "%s is cancelled. Duplicate it to rebook those nights."
                ) % rec.display_name)
            if not rec._has_stay():
                raise UserError(_("Set the unit and both dates before reserving."))
            if not rec.gross_amount:
                raise UserError(_(
                    "%s has no booking value. Set the amount, or check "
                    "the nightly rate on %s, before reserving."
                ) % (rec.display_name, rec.unit_id.display_name))

            rec.write({"state": "reserved", "hold_expiry": False})

            if not rec.sale_order_id:
                rec._create_sale_order()
            if not rec.invoice_id:
                rec.action_create_and_settle_invoice()

            rec._release_competing_holds()
        return True

    def _release_competing_holds(self):
        """Cancel the holds this reservation has just made impossible.

        Leaving them on hold would be a lie the calendar keeps telling:
        the nights are gone and those guests cannot have them. Each one
        is cancelled with the reason on its chatter, so whoever follows
        it up has something to tell the guest.
        """
        for rec in self:
            if not rec._has_stay():
                continue
            losers = self.search(
                rec._overlap_domain(states=PROVISIONAL_STATES))
            for loser in losers:
                loser.message_post(body=_(
                    "Released. %(unit)s was paid for by another guest for "
                    "%(start)s to %(end)s, so these nights are no longer "
                    "available."
                ) % {
                    "unit": rec.unit_id.display_name,
                    "start": rec.checkin_date,
                    "end": rec.checkout_date,
                })
                loser.state = "cancelled"
            if losers:
                rec.message_post(body=_(
                    "%(n)s competing hold(s) released: %(who)s."
                ) % {
                    "n": len(losers),
                    "who": ", ".join(
                        l.guest_partner_id.name or l.display_name
                        for l in losers),
                })
        if len(self) == 1 and self.invoice_id \
                and self.invoice_id.payment_state in ("not_paid", "partial"):
            return self.action_register_payment()
        return True
    # def action_confirm(self):
    #     for rec in self:
    #         if rec.state == "confirmed":
    #             continue
    #         if rec.state == "cancelled":
    #             raise UserError(_("Cannot confirm a cancelled reservation."))
    #         rec.state = "confirmed"
    #         if not rec.sale_order_id:
    #             rec._create_sale_order()

    def action_register_payment(self):
        """Open Odoo's Register Payment wizard on this booking's invoice."""
        self.ensure_one()
        if not self.invoice_id:
            raise UserError(_(
                "There is no invoice to pay yet - reserve the booking "
                "first."))
        if self.invoice_id.state == "draft":
            self.invoice_id.action_post()
        return self.invoice_id.action_register_payment()

        
    def action_cancel(self):
        for rec in self:
            if rec.state == "done":
                raise UserError(_("Cannot cancel a completed stay."))
            rec.state = "cancelled"
            # Intentionally do NOT delete the sale order - cancel it
            # instead, so accounting keeps a record of the cancellation
            # rather than the booking silently vanishing.
            if rec.sale_order_id and rec.sale_order_id.sudo().state not in ("cancel",):
                rec.sale_order_id.sudo().action_cancel()

    def action_complete(self):
        """Intended to be called by a daily cron for confirmed
        reservations whose checkout_date has passed."""
        for rec in self:
            if rec.state == "confirmed":
                rec.state = "done"


    def _create_sale_order(self):
        """Build a sale order from gross_amount (booking value) -
        never from payout_amount. See class docstring on #8."""
        self.ensure_one()
        if not self.guest_partner_id:
            raise UserError(
                _("A guest must be set on the reservation before it can be confirmed.")
            )
        product = self.unit_id.property_id.get_or_create_product()
        order = self.env["sale.order"].sudo().create(
            {
                "partner_id": self.guest_partner_id.id,
                "origin": self.name,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "product_uom_qty": 1,
                            "price_unit": self.gross_amount,
                            # "analytic_distribution": (
                            #     {str(self.unit_id.analytic_account_id.id): 100}
                            #     if self.unit_id.analytic_account_id
                            #     else {}
                            # ),
                        },
                    )
                ],
            }
        )
        order.action_confirm()
        self.sale_order_id = order
        return order
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("state", "hold") == "hold" \
                    and not vals.get("hold_expiry"):
                vals["hold_expiry"] = self._default_hold_expiry()
        return super().create(vals_list)

    @api.model
    def _cron_lapse_holds(self):
        """Cancel holds nobody paid for."""
        stale = self.search([
            ("state", "=", "hold"),
            ("hold_expiry", "!=", False),
            ("hold_expiry", "<", fields.Datetime.now()),
        ])
        for rec in stale:
            rec.message_post(body=_(
                "Hold lapsed - it was not paid for by %s.") % rec.hold_expiry)
        stale.write({"state": "cancelled"})
        return len(stale)

    @api.model
    def _cron_complete_past_stays(self):
        """REPLACES the existing version, which looked for 'confirmed'."""
        past = self.search([
            ("state", "=", "reserved"),
            ("checkout_date", "<", fields.Date.context_today(self)),
        ])
        past.write({"state": "done"})
        return len(past)

