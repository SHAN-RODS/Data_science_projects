"""Plain names for the final exits — "Exit 1" instead of "3QFdhgjZ16g8zXqCOuoml1".

An exit's only stable identifier in an IFC is its GlobalId, and the human-facing name the authoring
tool leaves behind is a type string ("UO-2:UO-2.10+4x21:1402305"). Neither tells a reader which door
is meant, yet both were what the output referred to: the AI's prose, the routes, the per-room goals
and the exported record all quoted GlobalIds.

So every final exit is given a plain name here, numbered across the plan (model +X, then +Y) rather
than in IFC file order, so the numbering runs along the building instead of following whatever order
the file happened to store the doors in.

The GlobalId is never dropped — it is what an egress simulator keys on, so it travels beside the name
everywhere. The name is what the AI is asked to write with and what the app displays; the pairing is
resolved back to GlobalIds deterministically by ``resolve_exit_ids``.
"""

import re

EXIT_PREFIX = "Exit"

# "Exit 3", "exit3", "Exit 03 (1.4 m)" — anything the model might write around the number
_EXIT_TOKEN_RE = re.compile(rf"^\s*{EXIT_PREFIX}\s*0*(\d+)\b", re.IGNORECASE)


def _plan_order(exit_item):
    """Sort key placing exits across the plan (+X, then +Y); positionless ones last, by id."""
    position = exit_item.get("position")
    if position and len(position) >= 2:
        return (0, round(position[0], 2), round(position[1], 2), exit_item.get("id") or "")
    return (1, 0.0, 0.0, exit_item.get("id") or "")


def exit_names(exits):
    return {e["id"]: f"{EXIT_PREFIX} {n}"
            for n, e in enumerate(sorted(exits, key=_plan_order), start=1)}


def named(exit_id, names):
    if exit_id is None:
        return None
    return names.get(exit_id, exit_id)


def resolve_exit_ids(tokens, names):
    by_name = {name.casefold(): exit_id for exit_id, name in names.items()}
    resolved = []
    for token in tokens or []:
        if not isinstance(token, str):
            continue
        if token in names:                                  
            resolved.append(token)
            continue
        hit = by_name.get(token.strip().casefold())
        if hit is None:
            match = _EXIT_TOKEN_RE.match(token)
            if match:
                hit = by_name.get(f"{EXIT_PREFIX} {int(match.group(1))}".casefold())
        resolved.append(hit or token)
    return resolved


def name_exit_ids(value, names):
    if isinstance(value, str):
        for exit_id, name in sorted(names.items(), key=lambda kv: len(kv[0]), reverse=True):
            value = re.sub(rf"(?i)\bexit\s+{re.escape(exit_id)}", name, value)
            value = value.replace(exit_id, name)
        return value
    if isinstance(value, list):
        return [name_exit_ids(v, names) for v in value]
    if isinstance(value, dict):
        return {k: name_exit_ids(v, names) for k, v in value.items()}
    return value


def _conditions_ids(scenario, field):
    conditions = scenario.get("conditions") or {}
    ids = conditions.get(f"{field}_ifc_ids")
    if ids is not None:
        return list(ids)
    return list(conditions.get(field) or [])


def discounted_exit_ids(scenario):
    return _conditions_ids(scenario, "exits_discounted")


def available_exit_ids(scenario):
    return _conditions_ids(scenario, "exits_available")


def unknown_exit_references(obj):
    names = exit_names(obj.get("exits", []))
    known = set(names) | set(names.values())
    unknown = []
    for scn in obj.get("scenarios", []):
        conditions = scn.get("conditions") or {}
        referenced = list(conditions.get("exits_available") or []) + \
                     list(conditions.get("exits_discounted") or [])
        missing = sorted({t for t in referenced if isinstance(t, str) and t not in known})
        if missing:
            unknown.append({"scenario": scn.get("id"), "unknown_exits": missing})
    return unknown
