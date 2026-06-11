#!/usr/bin/env python3
import sys, json
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from daily_cycle import _fetch_closed_trades_from_alpaca, _build_setup_map_from_journal, _compute_setup_rotation_from_journal, load_policy

# Test fetch
activities = _fetch_closed_trades_from_alpaca()
print(f'Fills from Alpaca: {len(activities)}')
if activities:
    print(f'Sample: {json.dumps(activities[0], indent=2)[:300]}')

# Test setup map
smap = _build_setup_map_from_journal()
print(f'Symbols in journal setup map: {list(smap.keys())[:10]}')

# Test full rotation
policy = load_policy()
result = _compute_setup_rotation_from_journal(policy)
print(f'Rotation result: {json.dumps(result, indent=2)[:800]}')
