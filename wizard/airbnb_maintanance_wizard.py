# -*- coding: utf-8 -*-
from odoo import _, fields, models
from markupsafe import Markup


class AirbnbMaintenanceWizard(models.TransientModel):
    """Captures the reason and expected dates when a unit is taken out
    of service, so there's an audit trail in the chatter rather than a
    status field silently flipping with no explanation.
    """

    _name = "airbnb.maintenance.wizard"
    _description = "Put Unit Under Maintenance"

    unit_id = fields.Many2one(
        "airbnb.unit",
        required=True,
        readonly=True,
        default=lambda self: self.env.context.get("active_id"),
    )
    reason = fields.Text(
        required=True,
        string="Reason",
        help="What is wrong, and what needs doing. This is logged to the "
        "unit's chatter permanently.",
    )
    maintenance_from = fields.Date(
        required=True, default=fields.Date.context_today
    )
    maintenance_to = fields.Date(
        help="Optional. If set, these dates are also blocked on Airbnb "
        "via the calendar feed so guests can't book during the outage."
    )

    def action_confirm(self):
        self.ensure_one()
        unit = self.unit_id

        unit.write({
            "state": "maintenance",
            "maintenance_reason": self.reason,
            "maintenance_from": self.maintenance_from,
            "maintenance_to": self.maintenance_to,
        })

        body = Markup(
            "<p><strong>%(title)s</strong></p>"
            "<ul>"
            "<li><strong>%(reason_label)s:</strong> %(reason)s</li>"
            "<li><strong>%(from_label)s:</strong> %(start)s</li>"
            "<li><strong>%(to_label)s:</strong> %(end)s</li>"
            "</ul>"
        ) % {
            "title": _("Unit placed under maintenance"),
            "reason_label": _("Reason"),
            "reason": self.reason,
            "from_label": _("From"),
            "start": self.maintenance_from,
            "to_label": _("Until"),
            "end": self.maintenance_to or _("open-ended"),
        }
        unit.message_post(body=body, subject=_("Maintenance started"))
        
        return {"type": "ir.actions.act_window_close"}


class AirbnbMaintenanceEndWizard(models.TransientModel):
    """Captures what was actually done when the unit comes back into
    service - the other half of the audit trail."""

    _name = "airbnb.maintenance.end.wizard"
    _description = "Return Unit to Service"

    unit_id = fields.Many2one(
        "airbnb.unit",
        required=True,
        readonly=True,
        default=lambda self: self.env.context.get("active_id"),
    )
    original_reason = fields.Char(
        related="unit_id.maintenance_reason", readonly=True
    )
    resolution = fields.Text(
        required=True,
        string="Work Done",
        help="What was actually fixed or completed.",
    )

    def action_confirm(self):
        self.ensure_one()
        unit = self.unit_id
        original = unit.maintenance_reason

        body = Markup(
            "<p><strong>%(title)s</strong></p>"
            "<ul>"
            "<li><strong>%(orig_label)s:</strong> %(original)s</li>"
            "<li><strong>%(res_label)s:</strong> %(resolution)s</li>"
            "</ul>"
        ) % {
            "title": _("Unit returned to service"),
            "orig_label": _("Original issue"),
            "original": original or _("not recorded"),
            "res_label": _("Work done"),
            "resolution": self.resolution,
        }
        unit.message_post(body=body, subject=_("Maintenance completed"))

        unit.write({
            "state": "available",
            "maintenance_reason": False,
            "maintenance_from": False,
            "maintenance_to": False,
        })
        return {"type": "ir.actions.act_window_close"}