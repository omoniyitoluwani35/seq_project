"""
Sequoia — create the Project analytic plan and retro-tag the imported GL.

    sudo -u odoo /opt/odoo19E/venv/bin/python3 /opt/odoo19E/odoo/odoo-bin shell \
        -c /etc/odoo19E.conf -d seq_main --no-http < setup_analytic.py

DRY_RUN = True first.  Read the report, settle the 51 unallocated lines,
then flip DRY_RUN = False and run again.  Re-runnable: it only ever touches
lines that have no analytic distribution yet, so a second run is a no-op.
"""

DRY_RUN = True

PLAN_NAME = "Project"

# code, name  -- code shows in the analytic account list and in reports
PROJECTS = [
    ("ARU", "Arusha"),
    ("ALB", "Ali Baba"),
    ("JCB", "Jacobs Bay"),
    ("RAY", "Rayyan Garki"),
    ("IKY", "Ikoyi"),
]

# Accounts the accountant reports BELOW the three project blocks, in Note 13
# "Depreciation of property, plant and equipment" (4370 + 4390 =
# 10,459,567.00). They carry no analytic at all, which reproduces his
# presentation: project gross profits first, depreciation after.
NO_ANALYTIC = {"4370", "4390"}

# Accounts naming two projects. Tag to whoever HOLDS the balance:
# 7375/7377 are Arusha's receivables; 9550/9551 are the borrower's payables.
ACCOUNT_OVERRIDE = {
    "7375": "Arusha",       # Loan to Jacobs Bay from Arusha
    "7377": "Arusha",       # Loan to Ali Baba from Arusha
    "9550": "Jacobs Bay",   # Loan from Arusha to Jacobs Bay
    "9551": "Ali Baba",     # Loan from Arusha to Ali Baba

    # Company-level costs the accountant reports inside Note 6,
    # "Operating expenses - Arusha", so they follow him to Arusha.
    "4225": "Arusha",       # Legal, Consultancy & Professional Fees
    "4240": "Arusha",       # Printing and Office Stationery
    "4263": "Arusha",       # Other Subscriptions
}

# Longest / most specific first. "Arusha" last so the loan names above
# never win on a stray match.
NAME_RULES = [
    ("ali baba",   "Ali Baba"),
    ("jacobs bay", "Jacobs Bay"),
    ("rayyan",     "Rayyan Garki"),
    ("ikoyi",      "Ikoyi"),
    ("arusha",     "Arusha"),
]

# Property-specific bank journals — catches bank charges and anything else
# posted from a dedicated account.
JOURNAL_RULES = {
    "MPARU": "Arusha",
    "MPALI": "Ali Baba",
    "SUMAB": "Ali Baba",
    "MPRAY": "Rayyan Garki",
}

# Last resort, applied only after the journal rules. Bank charges raised on
# a property's own account follow that property; charges on the four
# company-level banks have no property, and Note 6 puts them in Arusha.
ACCOUNT_FALLBACK = {
    "4475": "Arusha",       # Bank Charges
}

# Balance-sheet accounts that belong to a project. Banks, equity, tax and
# general control accounts are deliberately out of scope: the expense side of
# a payment carries the analytic, the bank side does not.
PROJECT_BS = {
    "7200", "7375", "7376", "7377", "7378",
    "9300", "9450", "9451", "9452", "9454", "9550", "9551",
}


def in_scope(code):
    return (code and code[0] in "1234") or code in PROJECT_BS


def rule_for(code, name, journal_code):
    if code in ACCOUNT_OVERRIDE:
        return ACCOUNT_OVERRIDE[code], "override"
    low = (name or "").lower()
    for key, proj in NAME_RULES:
        if key in low:
            return proj, "account name"
    if journal_code in JOURNAL_RULES:
        return JOURNAL_RULES[journal_code], "journal"
    if code in ACCOUNT_FALLBACK:
        return ACCOUNT_FALLBACK[code], "note 6 fallback"
    return None, None


print("=" * 74)
print("SEQUOIA ANALYTIC SETUP" + ("   [DRY RUN - nothing written]" if DRY_RUN else "   [WRITING]"))
print("=" * 74)
company = env.company
print(f"Company: {company.name}   Currency: {company.currency_id.name}")

# ---------------------------------------------------------------- plan
Plan = env["account.analytic.plan"]
plan = Plan.search([("name", "=", PLAN_NAME)], limit=1)
if plan:
    print(f"\nPlan '{PLAN_NAME}' already exists (id {plan.id})")
else:
    print(f"\nPlan '{PLAN_NAME}' will be created")
    if not DRY_RUN:
        plan = Plan.create({"name": PLAN_NAME, "default_applicability": "optional"})
        print(f"  created id {plan.id}")

# ------------------------------------------------------------ accounts
AA = env["account.analytic.account"]
proj_id = {}
print("\nAnalytic accounts:")
for code, name in PROJECTS:
    acc = AA.search([("name", "=", name)], limit=1)
    if acc:
        print(f"  exists   {code}  {name}  (id {acc.id})")
        proj_id[name] = acc.id
    elif DRY_RUN:
        print(f"  CREATE   {code}  {name}")
        proj_id[name] = None
    else:
        acc = AA.create({
            "name": name,
            "code": code,
            "plan_id": plan.id,
            "company_id": company.id,
        })
        print(f"  created  {code}  {name}  (id {acc.id})")
        proj_id[name] = acc.id

# ------------------------------------------------------- applicability
# Make the plan mandatory on P&L accounts so nothing new slips through
# untagged. Best effort: the model's selection values vary by build.
print("\nApplicability (make Project mandatory on P&L accounts):")
if DRY_RUN or not plan:
    print("  would add rules for account prefixes 1, 2, 3, 4")
else:
    App = env.get("account.analytic.applicability")
    if App is None:
        print("  account.analytic.applicability not present - set it in the UI instead")
    else:
        for prefix in ("1", "2", "3", "4"):
            try:
                existing = App.search([
                    ("analytic_plan_id", "=", plan.id),
                    ("account_prefix", "=", prefix),
                ], limit=1)
                if existing:
                    print(f"  prefix {prefix}: already set")
                    continue
                App.create({
                    "analytic_plan_id": plan.id,
                    "account_prefix": prefix,
                    "business_domain": "general",
                    "applicability": "mandatory",
                })
                print(f"  prefix {prefix}: mandatory")
            except Exception as e:
                print(f"  prefix {prefix}: skipped ({type(e).__name__}: {e})")
                env.cr.rollback()

# ------------------------------------------------------------- tagging
AML = env["account.move.line"]
lines = AML.search([("analytic_distribution", "=", False)])
print(f"\nJournal items with no analytic distribution: {len(lines):,}")

tagged = {}
todo = {}
below = {}
skipped_scope = 0
failed = []

for ln in lines:
    code = ln.account_id.code or ""
    if not in_scope(code):
        skipped_scope += 1
        continue
    val = ln.debit + ln.credit
    if code in NO_ANALYTIC:
        e = below.setdefault((code, ln.account_id.name), [0, 0.0])
        e[0] += 1
        e[1] += val
        continue
    proj, how = rule_for(code, ln.account_id.name, ln.journal_id.code)
    if not proj:
        key = (code, ln.account_id.name)
        e = todo.setdefault(key, [0, 0.0])
        e[0] += 1
        e[1] += val
        continue
    e = tagged.setdefault(proj, [0, 0.0, {}])
    e[0] += 1
    e[1] += val
    e[2][how] = e[2].get(how, 0) + 1
    if not DRY_RUN:
        aid = proj_id.get(proj)
        if not aid:
            continue
        try:
            ln.analytic_distribution = {str(aid): 100.0}
        except Exception as ex:
            failed.append((ln.id, ln.move_id.name, code, f"{type(ex).__name__}: {ex}"))
            env.cr.rollback()

print(f"Out of scope (banks, equity, tax, control): {skipped_scope:,}")

print("\n" + "-" * 74)
print(f"{'PROJECT':<16}{'LINES':>8}{'VALUE':>24}   MATCHED BY")
print("-" * 74)
tot_l = tot_v = 0
for proj in sorted(tagged, key=lambda p: -tagged[p][1]):
    n, v, how = tagged[proj]
    tot_l += n
    tot_v += v
    print(f"{proj:<16}{n:>8,}{v:>24,.2f}   " + ", ".join(f"{k} x{c}" for k, c in how.items()))
print("-" * 74)
print(f"{'TAGGED':<16}{tot_l:>8,}{tot_v:>24,.2f}")

if below:
    n = sum(v[0] for v in below.values())
    v = sum(v[1] for v in below.values())
    print(f"{'NO ANALYTIC':<16}{n:>8,}{v:>24,.2f}   below the project blocks (Note 13)")
    for (code, name), (n, v) in sorted(below.items(), key=lambda kv: -kv[1][1]):
        print(f"  {code:<7}{(name or '')[:46]:<48}{n:>7,}{v:>18,.2f}")

if todo:
    n = sum(v[0] for v in todo.values())
    v = sum(v[1] for v in todo.values())
    print(f"{'UNALLOCATED':<16}{n:>8,}{v:>24,.2f}")
    print("\nNo rule matched these — add one before the live run:")
    print(f"  {'CODE':<7}{'ACCOUNT':<48}{'LINES':>7}{'VALUE':>18}")
    for (code, name), (n, v) in sorted(todo.items(), key=lambda kv: -kv[1][1]):
        if v == 0:
            continue
        print(f"  {code:<7}{(name or '')[:46]:<48}{n:>7,}{v:>18,.2f}")

if failed:
    print(f"\nFAILED TO WRITE ({len(failed)}):")
    for lid, move, code, err in failed[:20]:
        print(f"  line {lid} move {move} acct {code}: {err}")
    print("  (posted entries normally still accept analytic distribution;")
    print("   if these all failed, reset the moves to draft and re-run)")

if DRY_RUN:
    print("\nDRY RUN — nothing written. Set DRY_RUN = False to apply.")
else:
    env.cr.commit()
    print("\nCommitted.")
print("=" * 74)
