from odoo import api, fields, models


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    project_analytic_id = fields.Many2one(
        comodel_name="account.analytic.account",
        string="Project",
        compute="_compute_project_analytic_id",
        store=True,
        index=True,
        help="Project this line is charged to, read from its analytic "
             "distribution. Where a line is split across several analytic "
             "accounts this shows the one carrying the largest share, so "
             "treat it as a filter and use Analytic Items when you need the "
             "split amounts themselves.",
    )

    @api.depends("analytic_distribution")
    def _compute_project_analytic_id(self):
        for line in self:
            best_id = False
            best_pct = -1.0
            for key, pct in (line.analytic_distribution or {}).items():
                try:
                    pct = float(pct)
                except (TypeError, ValueError):
                    continue
                if pct <= best_pct:
                    continue
                # A key is one analytic account id ("7"), or one per plan
                # joined by commas ("7,12") when several plans apply.
                ids = [int(p) for p in str(key).split(",") if p.strip().isdigit()]
                if ids:
                    best_id = ids[0]
                    best_pct = pct
            line.project_analytic_id = best_id or False
