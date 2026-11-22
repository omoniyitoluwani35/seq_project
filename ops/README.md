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

Tagging rules, in order: account-code override for the four inter-project
loan accounts (7375/7377 → Arusha, 9550 → Jacobs Bay, 9551 → Ali Baba),
then project name found in the account name, then the property-specific
bank journals (MPARU, MPALI, SUMAB, MPRAY).

Scope is P&L accounts plus the twelve project balance-sheet accounts.
Banks, equity, tax and general control accounts are deliberately excluded:
the expense side of a payment carries the analytic, the bank side does not.

Expect ~99.7% of in-scope value to tag automatically. The ~51 lines that
don't are shared costs — depreciation, bank charges on non-property
journals, legal fees, stationery — and need an allocation decision before
the second run.
