from ast import literal_eval
import logging

_logger = logging.getLogger(__name__)

# Journal Items actions whose default search should group by project.
# Ids vary a little between builds, so every one is optional.
TARGET_ACTIONS = (
    "account.action_account_moves_all",
    "account.action_account_moves_all_a",
    "account.action_account_moves_all_grouped",
)

CTX_KEY = "search_default_group_project"

# The P&L ships with filter_analytic = False, so _init_options_analytic
# returns early and the report cannot be restricted to one project. Turn it
# on, remembering what it was so uninstall can put it back.
PL_REPORT = "account_reports.profit_and_loss"
PARAM_PREV = "seq_project_analytic.pl_filter_analytic_was"


def _set_pl_analytic_filter(env, enable):
    report = env.ref(PL_REPORT, raise_if_not_found=False)
    if not report or "filter_analytic" not in report._fields:
        _logger.info("seq_project_analytic: no P&L report to configure")
        return
    params = env["ir.config_parameter"].sudo()
    if enable:
        params.set_param(PARAM_PREV, "1" if report.filter_analytic else "0")
        report.filter_analytic = True
        _logger.info("seq_project_analytic: enabled filter_analytic on the P&L")
    else:
        if params.get_param(PARAM_PREV) == "0":
            report.filter_analytic = False
            _logger.info("seq_project_analytic: restored filter_analytic = False")
        params.set_param(PARAM_PREV, "")


def _edit_action_contexts(env, add):
    """Add or remove CTX_KEY on each target action's default context."""
    for xmlid in TARGET_ACTIONS:
        action = env.ref(xmlid, raise_if_not_found=False)
        if not action:
            continue
        try:
            ctx = literal_eval(action.context or "{}")
        except (ValueError, SyntaxError):
            _logger.warning(
                "seq_project_analytic: could not parse context on %s, left alone",
                xmlid,
            )
            continue
        if not isinstance(ctx, dict):
            continue
        if add:
            ctx[CTX_KEY] = 1
        else:
            ctx.pop(CTX_KEY, None)
        action.context = repr(ctx)
        _logger.info(
            "seq_project_analytic: %s %s on %s",
            "set" if add else "removed",
            CTX_KEY,
            xmlid,
        )


def post_init_default_groupby(env):
    _edit_action_contexts(env, add=True)
    _set_pl_analytic_filter(env, enable=True)


def uninstall_clear_default_groupby(env):
    _edit_action_contexts(env, add=False)
    _set_pl_analytic_filter(env, enable=False)
