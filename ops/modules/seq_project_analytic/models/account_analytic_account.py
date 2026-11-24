from odoo import _, models
from odoo.exceptions import UserError

PL_REPORT = "account_reports.profit_and_loss"
PL_ACTION = "account_reports.action_account_report_pl"


class AccountAnalyticAccount(models.Model):
    _inherit = "account.analytic.account"

    def _pl_report(self):
        """The P&L report, or a UserError explaining why it cannot be used."""
        report = self.env.ref(PL_REPORT, raise_if_not_found=False)
        if not report:
            raise UserError(_(
                "The Profit and Loss report is not available. Check that "
                "Accounting Reports is installed."
            ))
        if not report.filter_analytic:
            raise UserError(_(
                "The Profit and Loss report has its analytic filter switched "
                "off, so it cannot be restricted to one project. Turn on "
                "Analytic for that report, or upgrade this module, which "
                "sets it."
            ))
        if not self.env.user.has_group("analytic.group_analytic_accounting"):
            raise UserError(_(
                "You need the Analytic Accounting access right for the report "
                "to filter by project. Without it the report ignores the "
                "filter and shows every project together, which would be "
                "misleading."
            ))
        return report

    def action_open_pnl(self):
        """Open the P&L restricted to this project."""
        self.ensure_one()
        self._pl_report()
        action = self.env["ir.actions.actions"]._for_xml_id(PL_ACTION)
        # _init_options_analytic reads previous_options['analytic_accounts'],
        # so seeding that key is what restricts the report to this project.
        # ignore_session stops a filter the user left set earlier from
        # overriding it.
        action["params"] = {
            "options": {"analytic_accounts": self.ids},
            "ignore_session": True,
        }
        action["display_name"] = _("Profit and Loss: %s", self.name)
        return action

    def action_download_pnl(self):
        """Download the P&L for this project as xlsx."""
        self.ensure_one()
        report = self._pl_report()
        options = report.get_options({"analytic_accounts": self.ids})
        return report.export_file(options, "export_to_xlsx")
