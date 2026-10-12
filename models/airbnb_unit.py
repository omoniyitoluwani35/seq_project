# -*- coding: utf-8 -*-
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class AirbnbUnit(models.Model):
    """A single bookable unit = a single Airbnb listing with its own
    calendar. This is the level at which parsing-matching, iCal export,
    and iCal import all operate — never at the Property level, since a
    booking on one unit must never block a sibling unit's calendar.
    """

    _name = "airbnb.unit"
    _description = "Airbnb Unit / Listing"
    _inherit = ["mail.thread"]
    _rec_name = "display_name"

    name = fields.Char(
        required=True,
        help="Internal short name, e.g. '2BR-A', '1BR-C'.",
    )
    property_id = fields.Many2one(
        "airbnb.property", required=True, ondelete="cascade", string="Property"
    )
    bedrooms = fields.Integer(default=1)
    active = fields.Boolean(default=True)

    # This MUST be copied verbatim from the client's Airbnb host
    # dashboard. The email parser matches on this exact string, so an
    # approximate/paraphrased title here will cause silent
    # mis-attribution of bookings to the wrong unit (see hiccup #6).
    airbnb_listing_name = fields.Char(
        required=True,
        tracking=True,
        help="Exact listing title as it appears on Airbnb. Used as the "
        "anchor string for matching confirmation emails to this unit.",
    )

    # Stable, unguessable token used to build this unit's public iCal
    # export URL (Odoo -> Airbnb) and to identify this unit inbound
    # if you build a webhook alternative to Gmail OAuth later.
    ical_token = fields.Char(
        default=lambda self: uuid.uuid4().hex,
        copy=False,
        readonly=True,
        help="Do not expose this in list views or logs shared outside "
        "the team - it is effectively the security for the iCal feed URL.",
    )

    # The Airbnb-hosted export URL for this listing, pasted in by hand.
    # Used as the secondary, more reliable read source (date blocks
    # only, no guest details) to cross-check the Gmail-parsed data.
    airbnb_ical_import_url = fields.Char(
        string="Airbnb Export iCal URL",
        help="From this listing's Airbnb Calendar > Availability > "
        "Export Calendar setting. Used as a cross-check source, not "
        "the primary booking data source.",
    )

    analytic_account_id = fields.Many2one(
        "account.analytic.account",
        string="Analytic Account",
        help="Should be created as a child of the property's analytic "
        "account for building-level roll-up.",
    )

    # product_id = fields.Many2one(
    #     "product.product",
    #     string="Booking Product",
    #     help="The service product used on sale order lines for stays "
    #     "in this unit. Auto-created on first reservation if left blank.",
    # )

    reservation_ids = fields.One2many("airbnb.reservation", "unit_id")

    display_name = fields.Char(compute="_compute_display_name", store=True)

    # Auto-provisioned stock location, created as a child of the
    # parent property's location. Turnover kit deliveries are
    # validated against this location, giving per-unit consumption
    # tracking without a second, manually-maintained location tree.
    location_id = fields.Many2one(
        "stock.location",
        string="Stock Location",
        readonly=True,
        copy=False,
        help="Auto-created when the unit is saved, as a child of the "
        "property's stock location.",
    )

    state = fields.Selection(
        [
            ("available", "Available"),
            ("maintenance", "Under Maintenance"),
            ("inactive", "Not Bookable"),
        ],
        default="available",
        required=True,
        tracking=True,
        help="Maintenance blocks new bookings in Odoo and, if the "
        "maintenance dates are set, pushes a block to Airbnb via the "
        "calendar feed.",
    )
    maintenance_from = fields.Date(
        help="Optional. Set both dates to push this outage to Airbnb's "
        "calendar so guests can't book the unit while it's out."
    )
    maintenance_to = fields.Date()
    maintenance_reason = fields.Char()

    setup_state = fields.Selection(
        [("ready", "Ready"), ("incomplete", "Needs Setup")],
        compute="_compute_setup_state",
        help="Whether this unit is fully configured for calendar sync "
        "and booking.",
    )
    setup_issues = fields.Text(compute="_compute_setup_state")
    
    ical_registered_with_airbnb = fields.Boolean(
        string="Feed Registered on Airbnb",
        help="Tick once the Odoo Export URL above has been pasted into "
        "this listing's Calendar > Availability > Import Calendar on "
        "Airbnb. Until then, physical bookings will NOT block the unit "
        "on Airbnb.",
    )
    nightly_rate = fields.Monetary(
        string="Nightly Rate",
        currency_field="currency_id",
        help="Default rate per night for this unit. Used for direct and "
        "walk-in bookings, and as a fallback where an amount cannot be "
        "read from an Airbnb email.",
    )
    airbnb_listing_id = fields.Char(
        string="Airbnb Listing ID",
        index=True,
        copy=False,
        help="Numeric ID from the listing's URL on Airbnb "
        "(airbnb.com/rooms/1453778350406796026). Matching on this is exact "
        "and survives the listing being renamed, which matching on the "
        "title does not.",
    )

    _listing_id_uniq = models.Constraint(
        "unique(airbnb_listing_id)",
        "Another unit is already using this Airbnb listing ID.",
    )
    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
        required=True,
        help="Currency the nightly rate is quoted in.",
    )

    
    @api.depends(
        "airbnb_listing_name", "airbnb_ical_import_url", "location_id",
        "property_id.income_account_id", "last_ical_import", "ical_import_error", "state",
    )
    def _compute_setup_state(self):
        for rec in self:
            issues = []


            if not rec.ical_registered_with_airbnb:
                issues.append(
                    "Odoo's export feed has not been registered on Airbnb. "
                    "Physical bookings will not block this unit there."
                )
            # The single most expensive thing to get wrong: the parser and
            # the calendar matcher both key off this exact string.
            if not rec.airbnb_listing_name:
                issues.append(
                    "No Airbnb listing name. Copy it verbatim from the "
                    "Airbnb dashboard - do not retype it."
                )

            if not rec.airbnb_ical_import_url:
                issues.append(
                    "Airbnb's export calendar URL is not set. Get it from "
                    "the listing's Calendar > Availability > Export Calendar."
                )
            elif not rec.last_ical_import:
                issues.append(
                    "Airbnb calendar has never imported. Check the URL is "
                    "correct and reachable."
                )
            elif rec.ical_import_error:
                issues.append(f"Last calendar import failed: {rec.ical_import_error[:150]}")

            if not rec.location_id:
                issues.append(
                    "No stock location. Set up a warehouse under Inventory, "
                    "then re-save this unit."
                )

            if not rec.property_id.income_account_id:
                issues.append(
                    "The property has no revenue account set. Booking "
                    "revenue will post to the default income account."
                )

            if rec.state == "maintenance":
                issues.append("Unit is under maintenance and cannot be booked.")

            rec.setup_issues = "\n".join(f"• {i}" for i in issues)
            rec.setup_state = "incomplete" if issues else "ready"

    @api.constrains("maintenance_from", "maintenance_to")
    def _check_maintenance_dates(self):
        for rec in self:
            if rec.maintenance_from and rec.maintenance_to:
                if rec.maintenance_from >= rec.maintenance_to:
                    raise ValidationError(
                        _("Maintenance end date must be after the start date.")
                    )

    def write(self, vals):
        res = super().write(vals)
        if "name" in vals:
            for rec in self:
                if rec.location_id and rec.location_id.name != rec.name:
                    rec.location_id.sudo().name = rec.name
        return res


    @api.ondelete(at_uninstall=False)
    def _block_delete_with_history(self):
        for rec in self:
            if rec.reservation_ids:
                raise UserError(_(
                    "%s has reservations against it and cannot be deleted. "
                    "Archive it instead so its history is preserved."
                ) % rec.display_name
            )

    def action_set_maintenance(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Put Unit Under Maintenance"),
            "res_model": "airbnb.maintenance.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_id": self.id},
        }

    def action_set_available(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Return Unit to Service"),
            "res_model": "airbnb.maintenance.end.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_id": self.id},
        }


    @api.depends("name", "property_id.name")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = (
                f"{rec.property_id.name} / {rec.name}" if rec.property_id else rec.name
            )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.location_id:
                rec._create_stock_location()
        return records

    def _create_stock_location(self):
        self.ensure_one()
        parent = self.property_id.location_id
        if not parent:
            # Safety net - shouldn't normally happen since Property.create()
            # provisions its own location first, but covers edge cases like
            # data imports that bypass the normal create flow.
            self.property_id._create_stock_location()
            parent = self.property_id.location_id
        location = self.env["stock.location"].create(
            {
                "name": self.name,
                "usage": "internal",
                "location_id": parent.id,
            }
        )
        self.location_id = location

    # def get_or_create_product(self):
    #     """Return this unit's booking product, creating a simple
    #     service product on first use so reservations always have
    #     something to put on the sale order line.
    #     """
    #     self.ensure_one()
    #     if self.product_id:
    #         return self.product_id
    #     product = self.env["product.product"].create(
    #         {
    #             "name": f"Stay - {self.display_name}",
    #             "type": "service",
    #             "invoice_policy": "order",
    #             "sale_ok": True,
    #             "purchase_ok": False,
    #         }
    #     )
    #     self.product_id = product
    #     return product
