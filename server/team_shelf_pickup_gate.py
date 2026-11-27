"""Conservative admission gate for a shared synthetic pickup spur."""
import json
import sqlite3
from server.order_demo_map import read_map
from server.team_shelf_order_slots import TeamShelfOrderSlots


def pickup_is_clear(store, runtime, shared_zones=frozenset()):
    if type(store) is not TeamShelfOrderSlots or not isinstance(shared_zones, (set, frozenset)):
        return False
    try:
        orders = store.inspect()['orders']
        # A robot that has not confirmed pickup may later enter or reverse out
        # of the source spur even if no current segment holds it yet.
        if any(row['state'] in ('reserved', 'picking', 'unknown') for row in orders):
            return False
        occupancy = read_map(runtime)['occupancy']
        for row in occupancy:
            kind, resource = row['kind'], row['resource_id']
            if kind == 'node' and resource in ('D01', 'N01'):
                return False
            if kind == 'zone' and resource in shared_zones:
                return False
            if kind == 'edge':
                endpoints = json.loads(resource)
                if not isinstance(endpoints, list) or len(endpoints) != 2:
                    return False
                if set(endpoints) & {'D01', 'N01'}:
                    return False
        return True
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error):
        return False
