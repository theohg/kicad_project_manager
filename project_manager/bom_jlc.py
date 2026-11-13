"""Small anonymous JLCPCB website client. No LCSC service or API keys."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.request

from .bom import BomError, PriceTier, Supply, amount, part_number, positive_integer


DETAIL_URL = 'https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode='


def tls_context():
    context = ssl.create_default_context()
    # KiCad's relocated macOS Python can retain a nonexistent python.org CA path.
    # Use macOS's trusted bundle when that interpreter has no default roots.
    # Environment-provided trust and normal platform defaults take precedence.
    bundle = Path('/etc/ssl/cert.pem')
    if sys.platform == 'darwin' and not context.get_ca_certs() and bundle.is_file():
        context.load_verify_locations(cafile=str(bundle))
    return context


def parse_supply(code: str, response: dict, fetched_at: float | None = None) -> Supply:
    code = part_number(code)
    if not isinstance(response, dict) or response.get('code') != 200:
        raise BomError('JLCPCB did not return component data.')
    data = response.get('data')
    if not isinstance(data, dict) or data.get('componentCode') != code:
        raise BomError('JLCPCB returned no matching component.')
    # These are the active catalog bands used by JLCPCB's component-page UI.
    # priceList is preferred there; legacy responses expose prices or jlcPrices.
    # All data comes from JLCPCB. Never query LCSC or use a guessed generic price.
    raw_tiers = data.get('priceList') or data.get('jlcPrices') or data.get('prices') or []
    tiers = []
    if not isinstance(raw_tiers, list):
        raise BomError('JLCPCB returned an unsupported price format.')
    for entry in raw_tiers:
        try:
            lower = positive_integer(entry['startNumber'])
            upper = entry.get('endNumber')
            upper = None if upper in (None, -1, '-1') else positive_integer(upper)
            if upper is not None and upper < lower:
                raise BomError('Invalid price range.')
            tiers.append(PriceTier(lower, upper, amount(entry['productPrice'])))
        except (KeyError, TypeError, ValueError) as exc:
            raise BomError('JLCPCB returned an unsupported price band.') from exc
    stock = data.get('overseasStockCount')
    if stock is None:
        stock = data.get('stockCount')
    if stock is not None:
        try:
            number = amount(stock)
            if number != number.to_integral_value():
                raise BomError('Invalid stock count.')
            stock = int(number)
        except ValueError as exc:
            raise BomError('JLCPCB returned an unsupported stock count.') from exc
    minimum = data.get('minPurchaseNum')
    minimum = positive_integer(minimum) if minimum is not None and amount(minimum) > 0 else 1
    return Supply(code, stock, tuple(tiers), time.time() if fetched_at is None else fetched_at, minimum)


def fetch_supply(code: str) -> Supply:
    code = part_number(code)
    request = urllib.request.Request(DETAIL_URL + code, headers={
        'Accept': 'application/json', 'User-Agent': 'KiCad-Project-Manager/1.1 BOM',
    })
    try:
        with urllib.request.urlopen(request, timeout=10, context=tls_context()) as response:
            # Bound malformed/changed responses; only one component is requested.
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise BomError('JLCPCB returned an unexpectedly large response.')
        result = json.loads(raw, parse_float=Decimal)
        return parse_supply(code, result)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise BomError('JLCPCB rate limit reached. Try Refresh later.') from None
        raise BomError(f'JLCPCB request failed (HTTP {exc.code}).') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BomError('Could not reach JLCPCB. Last known data is retained.') from None
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, BomError):
            raise
        raise BomError('Could not read the JLCPCB response.') from exc
