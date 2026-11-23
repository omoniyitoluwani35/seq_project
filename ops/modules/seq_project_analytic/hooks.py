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


def uninstall_clear_default_groupby(env):
    _edit_action_contexts(env, add=False)
