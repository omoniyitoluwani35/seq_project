# ops

Maintenance scripts for the Sequoia Odoo 19 database. Not part of the
Airbnb module — Odoo never imports this folder.

Run each from the VPS:

    cd /opt/odoo19E/custom_modules/AirBNB
    git pull
    sudo -u odoo /opt/odoo19E/venv/bin/python3 /opt/odoo19E/odoo/odoo-bin shell \
        -c /etc/odoo19E.conf -d seq_main --no-http < ops/<script>.py

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

Scope is P&L accounts plus the twelve project balance-sheet accounts.
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
