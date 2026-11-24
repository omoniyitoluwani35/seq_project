{
    "name": "Sequoia Project Analytic Filter",
    "version": "19.0.2.0.1",
    "summary": "Search, group and sort journal items by project analytic account",
    "description": """
Sequoia Project Analytic Filter
===============================

``analytic_distribution`` is a JSON field, so Odoo can search it but cannot
group or sort by it. This module adds a stored, indexed Many2one derived from
the distribution, which behaves like any other field in the search panel.

Adds:

- ``account.move.line.project_analytic_id``, stored, indexed and computed
  from ``analytic_distribution``
- Project as a searchable field and a Group By option on Journal Items
- A No Project filter, for lines carrying no analytic distribution
- Project as a column on the Journal Items list
- A default Group By Project on the Journal Items action, applied on install
  and removed again on uninstall
- A Project search on Journal Entries, matched through the entry's lines
- Profit and Loss and Download P&L buttons on the analytic account form
  and list, opening or exporting the P&L restricted to that one project

Journal Entries get a search field only, no column and no Group By. One
entry often spans several projects, so the project belongs to the line.
""",
    "author": "NIVIC Tech",
    "license": "LGPL-3",
    "category": "Accounting",
    "depends": ["account", "analytic", "account_reports"],
    "data": [
        "data/account_report_data.xml",
        "views/account_move_line_views.xml",
        "views/account_move_views.xml",
        "views/account_analytic_account_views.xml",
    ],
    "post_init_hook": "post_init_default_groupby",
    "uninstall_hook": "uninstall_clear_default_groupby",
    "installable": True,
    "application": False,
    "auto_install": False,
}
