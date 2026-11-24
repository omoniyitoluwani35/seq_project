"""
Probe the account_reports engine so the P&L buttons can be written against
the real API instead of a guess.

    cd /opt/odoo19E/custom_modules/AirBNB && git pull
    sudo -u odoo /opt/odoo19E/.venv/bin/python3 /opt/odoo19E/odoo-bin shell \
        -c /etc/odoo/odoo.conf -d seq_main --no-http < ops/probe_reports.py

Read-only. Output is kept short on purpose.
"""
import inspect
import re


def hits(obj, pattern, limit=14, label=""):
    """Print only the source lines of `obj` matching `pattern`."""
    try:
        src = inspect.getsource(obj)
    except Exception as exc:
        print(f"  <no source: {type(exc).__name__}>")
        return
    found = [l.strip() for l in src.splitlines() if re.search(pattern, l)]
    if not found:
        print(f"  <no match for {pattern}>")
    for l in found[:limit]:
        print(f"  {l[:118]}")
    if len(found) > limit:
        print(f"  ... {len(found) - limit} more")


print("=" * 72)
print("1. MODULES")
mods = env["ir.module.module"].search([
    ("name", "in", ["account_reports", "account_accountant", "analytic",
                    "account_asset", "web"]),
])
for m in mods.sorted("name"):
    print(f"  {m.state:<12} {m.name}")

if "account.report" not in env:
    print("\naccount.report not present - account_reports is not installed.")
    print("Stop here and tell Claude; the buttons need a different approach.")
else:
    print("\n" + "=" * 72)
    print("2. P&L REPORT RECORDS")
    reports = env["account.report"].search([])
    print(f"  {len(reports)} account.report record(s)")
    for r in reports:
        xmlid = r.get_external_id().get(r.id) or "-"
        if "profit" in (r.name or "").lower() or "loss" in (r.name or "").lower() \
                or "profit" in xmlid or "pl" == xmlid.split(".")[-1]:
            print(f"  id={r.id:<4} {xmlid:<46} {r.name}")
            for f in ("filter_analytic", "filter_analytic_groupby",
                      "filter_date_range", "filter_journals", "root_report_id"):
                if f in r._fields:
                    print(f"       {f:<26} = {r[f]}")

    print("\n" + "=" * 72)
    print("3. ACTIONS POINTING AT A P&L REPORT")
    for model in ("ir.actions.client", "ir.actions.act_window"):
        acts = env[model].search([("name", "ilike", "profit")])
        for a in acts:
            xmlid = a.get_external_id().get(a.id) or "-"
            print(f"  [{model}] {xmlid}")
            print(f"       context = {str(a.context)[:110]}")

    print("\n" + "=" * 72)
    print("4. HOW OPTIONS ARE SEEDED FROM CONTEXT")
    AR = env["account.report"]
    for meth in ("_get_options", "_init_options_analytic",
                 "_init_options_date", "get_options"):
        if hasattr(AR, meth):
            print(f"  --- {meth} : lines mentioning context/analytic ---")
            hits(getattr(AR, meth), r"context|analytic")

    print("\n" + "=" * 72)
    print("5. METHODS FOR OPENING AND EXPORTING")
    names = [m for m in dir(AR)
             if re.search(r"(action_|export|print|xlsx|download)", m)
             and not m.startswith("_abstract")]
    for n in sorted(set(names)):
        if n.startswith("__"):
            continue
        try:
            sig = str(inspect.signature(getattr(AR, n)))
        except Exception:
            sig = "(?)"
        print(f"  {n}{sig[:80]}")

    print("\n" + "=" * 72)
    print("6. ANALYTIC OPTION KEYS ACTUALLY PRODUCED")
    pl = env.ref("account_reports.profit_and_loss", raise_if_not_found=False)
    if not pl:
        pl = reports.filtered(lambda r: "profit" in (r.name or "").lower())[:1]
    if pl:
        try:
            opts = pl.get_options({}) if hasattr(pl, "get_options") \
                else pl._get_options({})
            keys = [k for k in opts if "analytic" in k or "date" == k]
            for k in keys:
                print(f"  {k} = {str(opts[k])[:90]}")
            if not keys:
                print("  <no analytic keys in options - filter_analytic may be off>")
        except Exception as exc:
            print(f"  <get_options failed: {type(exc).__name__}: {exc}>")

print("\n" + "=" * 72)
print("7. ANALYTIC ACCOUNTS AND THEIR VIEWS")
for a in env["account.analytic.account"].search([]):
    print(f"  id={a.id:<4} {a.code or '-':<5} {a.name}")
for xmlid in ("analytic.view_account_analytic_account_form",
              "analytic.view_account_analytic_account_list",
              "analytic.view_account_analytic_account_tree"):
    v = env.ref(xmlid, raise_if_not_found=False)
    print(f"  {'OK ' if v else '-- '} {xmlid}")

print("=" * 72)
print("DONE - nothing written")
