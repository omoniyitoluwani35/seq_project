# ops

Maintenance scripts for the Sequoia Odoo 19 database. Not part of the
Airbnb module — Odoo never imports this folder.

Run each from the VPS:

    cd /opt/odoo19E/custom_modules/AirBNB
    git pull
    sudo -u odoo /opt/odoo19E/.venv/bin/python3 /opt/odoo19E/odoo-bin shell \
        -c /etc/odoo/odoo.conf -d seq_main --no-http < ops/<script>.py

Those three paths are the ones systemd uses — confirm with
`systemctl cat odoo | grep ExecStart` if anything moves. Passing the wrong
`-c` is worse than it looks: Odoo silently falls back to its default addons
path, drops `/opt/odoo19E/custom_modules`, and reports any module there as
`manifest not found` rather than telling you the config was missing.

| Script | What it does | Safe to re-run |
|---|---|---|
| `probe_portal.py` | Read-only. Reports which native Odoo objects exist for the off-plan buyer portal (analytic, project.milestone, payment terms, portal rules) and re-checks 9300/9451/9454 against posted entries. | Yes — writes nothing |
| `setup_analytic.py` | Creates the `Project` analytic plan, the five project analytic accounts, and retro-tags imported journal items. `DRY_RUN = True` by default. | Yes — only touches lines with no analytic distribution |

## setup_analytic.py

Rules follow the accountant's own presentation in the September accounts,
so that Odoo's analytic reports reproduce his per-project P&L.

Applied in order:

1. **No analytic** — 4370 and 4390. He reports these in Note 13,
   "Depreciation of property, plant and equipment" (10,459,567.00),
   *below* the three project blocks. They carry no project.
2. **Account-code override** — the four inter-project loans, tagged to
   whoever holds the balance (7375/7377 → Arusha, 9550 → Jacobs Bay,
   9551 → Ali Baba); plus 4225, 4240 and 4263, which he reports inside
   Note 6, "Operating expenses - Arusha".
3. **Project name in the account name** — the bulk of the chart.
4. **Property-specific bank journal** — MPARU, MPALI, SUMAB, MPRAY.
5. **Note 6 fallback** — 4475 Bank Charges raised on the four
   company-level banks, which have no property. Note 6 puts them in Arusha.

Four analytic accounts: Arusha, Ali Baba, Jacobs Bay, Rayyan Garki.

Ikoyi is deliberately absent. 7376 "Ikoyi Project" is 8,600,009.00 of
advances toward a land-acquisition partnership, reported as "Staff Loans
and Advances / Other Current assets" in Note 15-23 and nowhere in the
P&L. It has no bank account, no journal and no inflows, so 7376 is out of
scope alongside 7400 Staff Loan and 7410 Account Receivables. Add it as a
project once it has funding of its own.

Rayyan Garki stays despite also being balance-sheet only: it has its own
bank account (8440), its own journal (MPRAY), 100,000,000 of subscriber
money in and 99,909,420 spent. Its costs capitalise rather than hitting
the P&L, the same as Jacobs Bay.

Scope is P&L accounts plus the eleven project balance-sheet accounts.
Banks, equity, tax and general control accounts are deliberately excluded:
the expense side of a payment carries the analytic, the bank side does not.

Nothing should come out unallocated. If anything does, add a rule before
the live run rather than leaving it untagged.

## One known divergence from the printed accounts

Note 6 also contains `Rent & Rate - Ali Baba Air Bnb` (20,416,666.66),
so the printed P&L charges Ali Baba's rent against Arusha's gross profit.
Account 4350 names Ali Baba, so rule 3 tags it to Ali Baba and Odoo will
report:

| Project | Printed accounts | Odoo analytic | Shift |
|---|---|---|---|
| Arusha | 64,510,607.26 | 84,927,273.92 | +20,416,666.66 |
| Ali Baba | -130,869,175.42 | -151,285,842.08 | -20,416,666.66 |

Company totals are identical either way (-104,358,568.16, net
-114,818,135.15). Only the split between the two moves. Raise it with the
accountant before anyone compares the two reports.

---

# modules/seq_project_analytic

Odoo module: search, group and sort journal items by project.

`analytic_distribution` is a JSON field, so Odoo can search it but cannot
group or sort by it. This adds `account.move.line.project_analytic_id` —
a stored, indexed Many2one computed from the distribution — which then
behaves like any other field in the search panel.

What you get:

- **Journal Items** — Project as a searchable field, a Group By Project
  option, a "No Project" filter, and Project as a column on the list
- Group By Project applied as the default on the Journal Items action

- **Journal Entries** — a Project search, matched through the entry's lines
- **Analytic Accounts** — *Profit and Loss* and *Download P&L* buttons on
  both the form and the list, opening or exporting the P&L restricted to
  that one project

## The P&L buttons

`account_reports.profit_and_loss` ships with **`filter_analytic = False`**,
so `_init_options_analytic` returns early and the report cannot be
restricted to a project at all. The install hook turns it on and records
the previous value in `ir.config_parameter`, so uninstall restores it.

`_init_options_analytic` reads `previous_options['analytic_accounts']`, so
the Open button returns the `account_reports.action_account_report_pl`
client action with:

    action["params"] = {"options": {"analytic_accounts": self.ids},
                        "ignore_session": True}

`ignore_session` stops a filter the user left set earlier from overriding
it. Download goes through `report.export_file(options, "export_to_xlsx")`.

That same method also early-returns unless the user holds
`analytic.group_analytic_accounting`. Without it the report would silently
ignore the filter and show every project at once, so both buttons raise a
UserError instead of producing a wrong report.

The export is Odoo's **standard** P&L layout — Operating Income, Cost of
Revenue, Expenses — not the accountant's note structure of Revenue, Cost
of Sales, Personnel, Operating expenses, Finance cost. If a project P&L
ever goes to a buyer or an investor, build it to match the September
accounts rather than sending this.

Journal Entries get a search field only. One entry often spans several
projects — a bank charge accrual hits four at once — so there is no correct
Project column or Group By there: the entry would have to appear under
several groups or pick one arbitrarily. Group by project on Journal Items,
where the project actually lives.

## Editing the search view

A search view's RELAX NG schema enforces element **order**: every `<field>`
must precede the filters, separators, `<group>` and `<searchpanel>`. So
anchor each new node after a real sibling of its own kind
(`//field[@name='account_id']`, `//filter[@name='pl_accounts']`) rather than
using `position="inside"` on `//search`, which appends at the end and fails
with `RELAXNG_ERR_EXTRACONTENT: Element search has extra content: field`.

`<group>` in a search view takes **no** `string` attribute — the stock view
uses a bare `<group>`. Add group-by filters inside that existing group;
a new `<group string="...">` fails with `RELAXNG_ERR_INVALIDATTR`.

Neither error reaches the UI. Odoo reports only `Invalid view ... definition`
with `-no context-`; the real reason needs
`--log-handler odoo.tools.convert:DEBUG`.

## Installing

The addons path is `/opt/odoo19E/custom_modules`, and this repo's root is
itself the AirBNB module, so a module nested here is not a sibling and Odoo
will not find it. Symlink it in once:

    ln -s /opt/odoo19E/custom_modules/AirBNB/ops/modules/seq_project_analytic \
          /opt/odoo19E/custom_modules/seq_project_analytic

Then install it, and it stays current with every `git pull`:

    sudo systemctl stop odoo
    sudo -u odoo /opt/odoo19E/.venv/bin/python3 /opt/odoo19E/odoo-bin \
        -c /etc/odoo/odoo.conf -d seq_main -i seq_project_analytic \
        --stop-after-init --no-http --logfile=
    sudo systemctl start odoo

Run `setup_analytic.py` first. The stored field computes on install, so
installing before the lines are tagged leaves it empty until the next
upgrade recomputes it.

## Two things to know

The Many2one holds **one** analytic account. Every distribution written by
`setup_analytic.py` is 100% to a single project, so it is exact today. If
anyone later splits a line 60/40 the field shows the larger share only —
fine as a filter, wrong as an amount. For split amounts use Analytic Items
(`account.analytic.line`), which is the real table behind the distribution
and carries one row per project per line.

The default Group By changes the Journal Items view for **everyone**,
including whoever is reconciling. The uninstall hook removes it again, or
delete `post_init_hook` from the manifest and upgrade to keep the filter
without the default.
