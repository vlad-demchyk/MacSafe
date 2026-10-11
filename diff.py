"""Comparison of two security reports: what changed since the previous snapshot.

Checks are matched by name (label). A change: the value or status differs, or the check
appeared or disappeared. The module reads nothing from disk: lists of “security” items are passed in.
"""


from i18n import T

KIND_UA = {"good": "ок", "info": "до відома", "warning": "увага", "critical": "критично"}


def _shown(item):
    """Value together with the status, so a status change without a text change is visible."""
    value = item.get("value")
    kind = item.get("kind")
    return f"{value} · {T(KIND_UA.get(kind, kind))}" if kind else value


def _by_label(items):
    out = {}
    for item in items or []:
        if isinstance(item, dict) and isinstance(item.get("label"), str):
            out[item["label"]] = item
    return out


def security_changes(old_items, new_items):
    """List of changes: {label, from, to}. from None — the check appeared, to None — it disappeared."""
    old = _by_label(old_items)
    new = _by_label(new_items)
    changes = []
    for label, item in new.items():
        prev = old.get(label)
        if prev is None:
            changes.append({"label": label, "from": None, "to": _shown(item)})
        elif (prev.get("kind"), prev.get("value")) != (item.get("kind"), item.get("value")):
            changes.append({"label": label, "from": _shown(prev), "to": _shown(item)})
    for label, prev in old.items():
        if label not in new:
            changes.append({"label": label, "from": _shown(prev), "to": None})
    return changes
