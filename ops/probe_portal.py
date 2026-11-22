"""
Probe what Odoo 19 already provides for the Sequoia buyer portal.
Run on the VPS:

    sudo -u odoo /opt/odoo19E/venv/bin/python3 /opt/odoo19E/odoo/odoo-bin shell \
        -c /etc/odoo19E.conf -d seq_main --no-http < probe_portal.py

Read-only. Nothing is written.
"""

def line(t=""):
    print(t)

def head(t):
    line()
    line("=" * 72)
    line(t)
    line("=" * 72)

def has_model(name):
    return name in env

def fields_of(model, want):
    """Report which of `want` exist on `model`, with their type."""
    if not has_model(model):
        line(f"  MODEL MISSING: {model}")
        return {}
    f = env[model]._fields
    out = {}
    for w in want:
        if w in f:
            fld = f[w]
            rel = getattr(fld, "comodel_name", None)
            out[w] = f"{fld.type}" + (f" -> {rel}" if rel else "")
        else:
            out[w] = None
    for k, v in out.items():
        line(f"  {'OK ' if v else '-- '} {model}.{k:<28} {v or 'not present'}")
    return out


head("1. VERSION AND INSTALLED MODULES")
line(f"  Odoo version: {odoo.release.version}")
line(f"  Database:     {env.cr.dbname}")
mods = env["ir.module.module"].search([
    ("name", "in", [
        "account", "account_accountant", "account_followup", "analytic",
        "sale", "sale_management", "project", "sale_project",
        "documents", "portal", "website", "sale_subscription",
        "account_asset", "stock",
    ])
])
for m in mods.sorted("name"):
    line(f"  {m.state:<12} {m.name}")


head("2. ANALYTIC ACCOUNTING  (the project dimension)")
fields_of("account.move.line", ["analytic_distribution", "analytic_line_ids"])
line()
if has_model("account.analytic.plan"):
    plans = env["account.analytic.plan"].search([])
    line(f"  Analytic plans: {len(plans)}")
    for p in plans:
        line(f"    - {p.name} (applicability: {getattr(p, 'default_applicability', '?')})")
if has_model("account.analytic.account"):
    accs = env["account.analytic.account"].search([])
    line(f"  Analytic accounts: {len(accs)}")
    for a in accs[:25]:
        line(f"    - {a.name}")
line()
line("  -> If 0 plans/accounts, we create plan 'Project' with:")
line("     Arusha, Ali Baba, Jacobs Bay, Rayyan Garki, Ikoyi")


head("3. PROJECT + MILESTONES  (construction progress)")
fields_of("project.project", [
    "account_id", "analytic_account_id", "partner_id", "privacy_visibility",
    "allow_milestones", "milestone_ids", "date_start", "date", "stage_id",
])
line()
fields_of("project.milestone", [
    "name", "project_id", "deadline", "is_reached", "reached_date",
    "sale_line_id", "quantity_percentage", "message_ids",
])
line()
line("  -> 'progress %' is the likely gap. If absent we add one integer field.")
line("  -> Photos ride on ir.attachment / message_ids, no model needed.")


head("4. PAYMENT TERMS  (the instalment schedule)")
fields_of("account.payment.term", ["name", "line_ids", "early_discount"])
line()
fields_of("account.payment.term.line", [
    "value", "value_amount", "nb_days", "delay_type", "days_next_month",
])
line()
if has_model("account.payment.term"):
    terms = env["account.payment.term"].search([])
    line(f"  Existing payment terms: {len(terms)}")
    for t in terms:
        line(f"    - {t.name}  ({len(t.line_ids)} line(s))")
    multi = terms.filtered(lambda t: len(t.line_ids) > 1)
    line(f"  Multi-line terms (= instalment plans): {len(multi)}")
line()
line("  -> A 6-line term gives 6 receivable lines each with its own")
line("     date_maturity. That is the buyer payment schedule, natively.")


head("5. INVOICE STATE  (paid / outstanding / overdue, for free)")
fields_of("account.move", [
    "amount_total", "amount_residual", "payment_state", "invoice_date_due",
    "invoice_payment_term_id", "state", "access_url", "access_token",
])
line()
fields_of("account.move.line", ["date_maturity", "amount_residual", "full_reconcile_id"])


head("6. SALE ORDER  (the purchase agreement)")
fields_of("sale.order", [
    "partner_id", "amount_total", "client_order_ref", "state",
    "payment_term_id", "access_url", "project_id", "analytic_account_id",
])


head("7. PORTAL  (what buyers can already see with zero code)")
grp = env.ref("base.group_portal", raise_if_not_found=False)
line(f"  group_portal exists: {bool(grp)}")
if grp:
    users = env["res.users"].search([("groups_id", "in", grp.ids)]) \
        if "groups_id" in env["res.users"]._fields \
        else env["res.users"].search([("group_ids", "in", grp.ids)])
    line(f"  Existing portal users: {len(users)}")
line()
line("  Record rules already protecting portal access:")
rules = env["ir.rule"].search([("name", "ilike", "portal")])
for r in rules[:30]:
    line(f"    - [{r.model_id.model}] {r.name}")
line()
line("  Native portal routes expected: /my/orders  /my/invoices  /my/projects")


head("8. UNITS  (the one genuine gap)")
fields_of("product.template", [
    "name", "type", "list_price", "property_account_income_id", "categ_id",
])
line()
cats = env["product.category"].search([])
line(f"  Product categories: {len(cats)}")
for c in cats[:15]:
    line(f"    - {c.complete_name}")
line()
line("  -> Plan: one product.product per unit, one category per project,")
line("     income account pointed at 9300 Unearned Income.")


head("9. THE ACCOUNTS THIS HAS TO RECONCILE TO")
for code in ["9300", "9451", "9454", "7376", "7410", "5100"]:
    acc = env["account.account"].search([("code", "=", code)], limit=1)
    if not acc:
        line(f"  {code}: NOT FOUND")
        continue
    env.cr.execute("""
        SELECT COALESCE(SUM(l.debit - l.credit), 0), COUNT(*)
        FROM account_move_line l
        JOIN account_move m ON m.id = l.move_id
        WHERE l.account_id = %s AND m.state = 'posted'
    """, (acc.id,))
    bal, n = env.cr.fetchone()
    line(f"  {code} {acc.name[:42]:<42} {bal:>20,.2f}  ({n} posted lines)")
line()
env.cr.execute("SELECT state, COUNT(*) FROM account_move GROUP BY state")
line("  account.move by state: " + ", ".join(f"{s}={c}" for s, c in env.cr.fetchall()))

head("DONE - nothing was written")
