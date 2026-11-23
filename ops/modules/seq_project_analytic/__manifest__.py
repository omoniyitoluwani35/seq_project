{
    "name": "Sequoia Project Analytic Filter",
    "version": "19.0.1.0.0",
    "summary": "Search, group and sort journal items by project analytic account",
    "description": """
Sequoia Project Analytic Filter
===============================

``analytic_distribution`` is a JSON field, so Odoo can search it but cannot
group or sort by it. This module adds a stored, indexed Many2one derived from
the distribution, which behaves like any other field in the search panel.

Adds:
- ``account.move.line.project_analytic_id`` — stored, indexed, computed from
  ``analytic_distribution``
- Project as a searchable field and a Group By option on Journal Items
- Project as an optional column on the Journal Items list
- Project as a searchable field on Journal Entries, matched through its lines
- A default Group By Project on the Journal Items action, applied on install
  and removed again on uninstall
""",
    "author": "NIVIC Tech",
    "license": "LGPL-3",
    "category": "Accounting",
    "depends": ["account", "analytic"],
    "data": [
        "views/account_move_line_views.xml",
        "views/account_move_views.xml",
    ],
    "post_init_hook": "post_init_default_groupby",
    "uninstall_hook": "uninstall_clear_default_groupby",
    "installable": True,
    "application": False,
    "auto_install": False,
}
