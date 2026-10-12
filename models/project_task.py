# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ProjectTask(models.Model):
    _inherit = "project.task"

    unit_id = fields.Many2one("airbnb.unit", string="Unit")
    reservation_id = fields.Many2one("airbnb.reservation", string="Related Checkout",
        domain="[('unit_id', '=', unit_id)]")
    consumption_line_ids = fields.One2many("airbnb.turnover.line", "task_id", string="Supplies Used")
    picking_id = fields.Many2one("stock.picking", readonly=True, copy=False, string="Stock Move")

    def action_create_consumption_picking(self):
        for rec in self:
            if rec.unit_id and rec.consumption_line_ids and not rec.picking_id:
                rec._create_consumption_picking()

    def _create_consumption_picking(self):
        # def _create_consumption_picking(self):
        """Move logged quantities from the property's storage location
        to this unit's location - no BOM, just real consumption.
        NOTE: verify 'move.quantity' is the correct field name for
        done-quantity on your 19.0 instance (this has changed across
        Odoo versions). If button_validate() errors, this is the line
        to fix.
        """
        self.ensure_one()
        picking_type = self.env["stock.picking.type"].search(
            [
                ("code", "=", "internal"),
                ("warehouse_id.company_id", "=", self.env.company.id),
            ],
            limit=1,
        )
        if not picking_type:
            raise UserError(
                _("No internal operation type found - configure one under "
                  "Inventory > Configuration > Operation Types.")
            )

        source_location = self.unit_id.property_id.location_id
        dest_location = self.unit_id.location_id

        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": source_location.id,
                "location_dest_id": dest_location.id,
                "origin": self.name,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": line.product_id.name,
                            "product_id": line.product_id.id,
                            "product_uom_qty": line.quantity,
                            "product_uom": line.product_id.uom_id.id,
                            "location_id": source_location.id,
                            "location_dest_id": dest_location.id,
                        },
                    )
                    for line in self.consumption_line_ids
                ],
            }
        )
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.button_validate()
        self.picking_id = picking
        return picking




class AirbnbTurnoverLine(models.Model):
    """One consumable + quantity actually used during a turnover task."""

    _name = "airbnb.turnover.line"
    _description = "Turnover Supply Consumption Line"

    task_id = fields.Many2one(
        "project.task", required=True, ondelete="cascade"
    )
    product_id = fields.Many2one(
        "product.product",
        required=True,
        domain="[('type', '=', 'consu')]",
    )
    quantity = fields.Float(default=1.0, required=True)