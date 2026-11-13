"""Durable project prices and separate disposable, per-part supplier cache."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import time

from .bom import BomError, PriceTier, Supply, amount, part_number, positive_integer


STATE_FILE = 'kpm-bom.json'
CACHE_TTL = 15 * 60


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(root: Path) -> dict:
    path = root / STATE_FILE
    try:
        state = json.loads(path.read_text('utf-8'))
        if not isinstance(state, dict) or state.get('version') != 1:
            raise ValueError('Unsupported BOM settings version.')
        positive_integer(state.get('boards', 1))
        if not isinstance(state.get('source', ''), str) or not isinstance(state.get('overrides', {}), dict):
            raise ValueError('Invalid BOM settings.')
        for value in state.get('overrides', {}).values():
            if not isinstance(value, dict) or value.get('currency') != 'USD':
                raise ValueError('Invalid manual USD price.')
            amount(value['paid'])
            positive_integer(value['quantity'])
            if not isinstance(value.get('seller', ''), str):
                raise ValueError('Invalid seller.')
        return state
    except FileNotFoundError:
        return {'version': 1, 'boards': 1, 'source': '', 'overrides': {}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BomError(f'Could not read {STATE_FILE}: {exc}. Repair that file before saving BOM settings.') from exc


def save_state(root: Path, state: dict) -> None:
    atomic_json(root / STATE_FILE, state)


class SupplyCache:
    def __init__(self, root: Path):
        self.root = root / 'bom-cache'

    def read(self, code: str) -> Supply | None:
        try:
            value = json.loads((self.root / (part_number(code) + '.json')).read_text('utf-8'))
            if value.get('version') != 1 or value['code'] != code:
                return None
            stock = value['stock']
            if stock is not None and (type(stock) is not int or stock < 0):
                return None
            tiers = tuple(PriceTier(positive_integer(t['minimum']),
                                    None if t['maximum'] is None else positive_integer(t['maximum']),
                                    amount(t['price'])) for t in value['tiers'])
            fetched = float(amount(value['fetched_at']))
            if not math.isfinite(fetched) or fetched > time.time() + 300:
                return None
            if any(t.maximum is not None and t.maximum < t.minimum for t in tiers):
                return None
            return Supply(code, stock, tiers, fetched, positive_integer(value.get('minimum_order', 1)))
        except (OSError, ValueError, KeyError, TypeError):
            # Supplier data is disposable; user prices never go through this path.
            return None

    def write(self, supply: Supply) -> None:
        atomic_json(self.root / (part_number(supply.code) + '.json'), {
            'version': 1, 'code': supply.code, 'stock': supply.stock,
            'fetched_at': supply.fetched_at, 'minimum_order': supply.minimum_order,
            'tiers': [{'minimum': t.minimum, 'maximum': t.maximum, 'price': str(t.price)} for t in supply.tiers],
        })
