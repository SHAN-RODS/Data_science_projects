import re

EXIT_PREFIX = "Exit"

EXIT_TOKEN = re.compile(rf"^\s*{EXIT_PREFIX}\s*0*(\d+)\b", re.IGNORECASE)


def plan_order(exit_item):
    position = exit_item.get("position")
    if position and len(position) >= 2:
        return (0, round(position[0], 2), round(position[1], 2), exit_item.get("id") or "")
    return (1, 0.0, 0.0, exit_item.get("id") or "")

#Creates readable exit names in the user interface
def exit_names(exits):
    return {e["id"]: f"{EXIT_PREFIX} {n}"
            for n, e in enumerate(sorted(exits, key=plan_order), start=1)}

#Converts ID into readable name
def named(exit_id, names):
    if exit_id is None:
        return None
    return names.get(exit_id, exit_id)

#Converts names back to IFC IDs
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
            match = EXIT_TOKEN.match(token)
            if match:
                hit = by_name.get(f"{EXIT_PREFIX} {int(match.group(1))}".casefold())
        resolved.append(hit or token)
    return resolved

#This replaces the IDs throughout the generated output
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


def scenario_conditions(scenario):
    return (scenario.get("scenario_objective") or {}).get("conditions") or {}


def evacuation_routes(scenario):
    return scenario.get("evacuation_routes") or {}


def block_ids(block, field):
    ids = block.get(f"{field}_ifc_ids")
    if ids is not None:
        return list(ids)
    return list(block.get(field) or [])


def discounted_exit_ids(scenario):
    return block_ids(scenario_conditions(scenario), "exits_discounted")


def available_exit_ids(scenario):
    return block_ids(evacuation_routes(scenario), "exits_available")


def unknown_exit_references(obj):
    names = exit_names(obj.get("exits", []))
    known = set(names) | set(names.values())
    unknown = []
    for scn in obj.get("scenarios", []):
        fire = scn.get("fire_conditions") or {}
        referenced = list(evacuation_routes(scn).get("exits_available") or []) + \
                     list(scenario_conditions(scn).get("exits_discounted") or []) + \
                     list(fire.get("affected_exits") or [])
        missing = sorted({t for t in referenced if isinstance(t, str) and t not in known})
        if missing:
            unknown.append({"scenario": scn.get("id"), "unknown_exits": missing})
    return unknown
