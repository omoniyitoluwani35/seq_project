# -*- coding: utf-8 -*-
from odoo import api, fields, models


class AirbnbProperty(models.Model):
    """A physical building that contains one or more bookable units.

    Kept deliberately separate from ``airbnb.unit`` because P&L, ownership,
    and depreciation (if the client owns the building) are tracked at the
    property level, while bookings/calendars/parsing all happen at the
    individual unit level.
    """

    _name = "airbnb.property"
    _description = "Airbnb Property (Building)"
    _inherit = ["mail.thread"]

    name = fields.Char(required=True, tracking=True)
    street = fields.Char()
    city = fields.Char()
    active = fields.Boolean(default=True)

    unit_ids = fields.One2many("airbnb.unit", "property_id", string="Units")
    unit_count = fields.Integer(compute="_compute_unit_count")
    sale_journal_id = fields.Many2one(
        "account.journal", string="Sales Journal",
        domain="[('type', '=', 'sale')]",
        help="Invoices for this property are posted here, so the property "
             "gets its own number series. Left empty, Odoo's default sales "
             "journal is used.")
    bank_journal_id = fields.Many2one(
        "account.journal", string="Bank Journal",
        domain="[('type', 'in', ('bank', 'cash'))]",
        help="Pre-selected when registering a guest payment against an "
             "invoice for this property.")

    # Parent analytic account for building-level roll-up reporting.
    # Each unit gets its own child analytic account so per-unit P&L is
    # still available, but everything rolls up here for the "whole
    # building" view the client will want on the P&L report.
    analytic_account_id = fields.Many2one(
        "account.analytic.account",
        string="Analytic Account",
        help="Building-level analytic account. Unit-level analytic "
        "accounts should be created as children of this one so that "
        "reporting can roll up or drill down as needed.",
    )

    # Auto-provisioned parent stock location for this property. Every
    # unit's own location is created as a child of this one, so the
    # inventory tree mirrors the Property -> Unit hierarchy without
    # anyone having to manually create matching locations by hand.
    location_id = fields.Many2one(
        "stock.location",
        string="Stock Location",
        readonly=True,
        copy=False,
        help="Auto-created when the property is saved. Unit stock "
        "locations are created as children of this one.",
    )

    income_account_id = fields.Many2one(
        "account.account",
        string="Revenue Account",
        domain="[('account_type', 'in', ('income', 'income_other'))]",
        help="Where booking revenue for this property posts - e.g. "
        "1010 ARUSHA AIR BNB REVENUE. Set this before taking bookings, "
        "otherwise revenue lands in the default income account.",
    )
    product_id = fields.Many2one(
        "product.product",
        string="Booking Product",
        readonly=True,
        copy=False,
        help="Auto-created on first booking. One per property, shared by "
        "all its units - the unit is identified on the reservation itself.",
    )

    def get_or_create_product(self):
        """One accommodation product per property, not per unit.

        A sale order line needs a product, but a product per unit would
        clutter the catalogue without adding anything - unit-level revenue
        is already available from the reservation records. Per-property is
        the useful granularity because it matches how the chart of
        accounts splits revenue.
        """
        self.ensure_one()
        if self.product_id:
            return self.product_id

        vals = {
            "name": f"Accommodation - {self.name}",
            "type": "service",
            "invoice_policy": "order",
            "sale_ok": True,
            "purchase_ok": False,
        }
        if self.income_account_id:
            vals["property_account_income_id"] = self.income_account_id.id

        self.product_id = self.env["product.product"].create(vals)
        return self.product_id

    @api.depends("unit_ids")
    def _compute_unit_count(self):
        for rec in self:
            rec.unit_count = len(rec.unit_ids)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if not rec.location_id:
                rec._create_stock_location()
            if not rec.analytic_account_id:
                rec._create_analytic_account()
        return records

    def _create_analytic_account(self):
        """One analytic account per property, for costs that no account
        code identifies on its own - repairs, maintenance, and anything
        shared that needs attributing to a building.

        Revenue is already split by account code (1010 Arusha, 1020 Ali
        Baba), so this is not for bookings.
        """
        self.ensure_one()
        plan = self._analytic_plan()
        self.analytic_account_id = self.env["account.analytic.account"].create({
            "name": self.name,
            "plan_id": plan.id,
            "company_id": self.env.company.id,
        })
        return self.analytic_account_id

    def _analytic_plan(self):
        """Group the property accounts under one plan so reporting can
        filter to properties rather than every analytic account in the
        system."""
        Plan = self.env["account.analytic.plan"]
        plan = Plan.search([("name", "=", "Properties")], limit=1)
        return plan or Plan.create({"name": "Properties"})

    def write(self, vals):
        res = super().write(vals)
        if "name" in vals:
            for rec in self:
                if rec.location_id and rec.location_id.name != rec.name:
                    rec.location_id.sudo().name = rec.name
        return res

    def _create_stock_location(self):
        """Auto-provision a stock.location for this property, parented
        under the company's default warehouse stock location so it
        sits inside the existing Inventory hierarchy rather than
        floating on its own.
        """
        self.ensure_one()
        warehouse = self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1
        )
        parent_location = warehouse.lot_stock_id if warehouse else False
        location = self.env["stock.location"].create(
            {
                "name": self.name,
                "usage": "internal",
                "location_id": parent_location.id if parent_location else False,
            }
        )
        self.location_id = location

        
    @api.ondelete(at_uninstall=False)
    def _unlink_clean_analytic(self):
        """Remove the analytic account with the property, but only when
        nothing has been posted against it. An account carrying journal
        items is part of the accounting record and must not disappear
        with the property that happened to create it.
        """
        for rec in self:
            acct = rec.analytic_account_id
            if not acct:
                continue
            posted = self.env["account.analytic.line"].search_count(
                [("auto_account_id", "=", acct.id)])
            if posted:
                raise UserError(_(
                    "%(prop)s cannot be deleted: %(acct)s has %(n)s posted "
                    "entries against it. Archive the property instead."
                ) % {"prop": rec.name, "acct": acct.name, "n": posted})
            acct.unlink()