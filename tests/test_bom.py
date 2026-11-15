from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from project_manager.bom import BomError, BomPart, PriceTier, Supply, calculate, natural_key, read_csv, sorted_indices
from project_manager.bom_jlc import DETAIL_URL, fetch_supply, parse_supply
from project_manager.bom_store import SupplyCache, load_state, save_state


def response(code='C25804'):
    return {'code': 200, 'data': {
        'componentCode': code, 'stockCount': 100, 'overseasStockCount': 80,
        'prices': [{'startNumber': 1, 'endNumber': 9, 'productPrice': '0.0022'},
                   {'startNumber': 10, 'endNumber': -1, 'productPrice': '0.00175'}],
        'jlcPrices': None, 'minPurchaseNum': 1, 'componentLibraryType': 'base',
    }}


class BomTests(unittest.TestCase):
    def test_jlc_csv_and_schematic_aliases_aggregate_before_tier_selection(self):
        csv = ('Designator,Value,Footprint,Quantity,LCSC Part #,DNP\n'
               'R1,10k,0603,1,C25804,\nR2,10k,0603,1,C25804,\n'
               'R3,10k,0603,1,C25804,yes\n')
        parts = read_csv(csv)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].references, ('R1', 'R2'))
        supply = parse_supply('C25804', response())
        line = calculate(parts, 5, {'C25804': supply}, {})[0]
        self.assertEqual(line.required, 10)
        self.assertEqual(line.unit_price, Decimal('0.00175'))
        self.assertEqual(line.total, Decimal('0.01750'))
        self.assertEqual(supply.stock, 80)

    def test_excluded_flags_dnp_and_generic_supplier_metadata(self):
        parts = read_csv('Refs,Value,${QUANTITY},${EXCLUDE_FROM_BOM},JLCPCB Part Number,JLCPCB Rotation\n'
                         'R1,10k,1,,C25804,90\nR2,20k,1,1,C123,\n')
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].code, 'C25804')

    def test_conflicting_aliases_are_not_silently_priced(self):
        part = read_csv('Reference,Value,LCSC,JLCPCB\nU1,IC,C123,C456\n')[0]
        self.assertEqual(part.code, '')
        self.assertTrue(part.warning)
        self.assertIsNone(calculate([part], 1, {}, {})[0].total)

    def test_missing_prices_remain_unknown_and_zero_manual_price_is_valid(self):
        part = read_csv('Reference,Value,Footprint\nQ1,MOSFET,SO8\n')[0]
        self.assertIsNone(calculate([part], 1, {}, {})[0].total)
        line = calculate([part], 3, {}, {part.key: {'paid': '0', 'quantity': 1}})[0]
        self.assertEqual(line.total, Decimal(0))

    def test_manual_price_survives_reannotation_refresh_restart_and_cache_loss(self):
        part = read_csv('Reference,Value,Footprint\nQ1,MOSFET,SO8\n')[0]
        reannotated = read_csv('Reference,Value,Footprint\nQ99,MOSFET,SO8\n')[0]
        self.assertEqual(part.key, reannotated.key)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = load_state(root)
            state['overrides'][part.key] = {'paid': '13', 'quantity': 10, 'currency': 'USD', 'seller': 'External'}
            save_state(root, state)
            before = (root / 'kpm-bom.json').read_bytes()
            cache = SupplyCache(root / 'user-data')
            supply = parse_supply('C25804', response())
            cache.write(supply)
            self.assertEqual(cache.read('C25804'), supply)
            (cache.root / 'C25804.json').unlink()
            self.assertIsNone(cache.read('C25804'))
            line = calculate([reannotated], 20, {}, load_state(root)['overrides'])[0]
            self.assertEqual(line.total, Decimal('26.0'))
            self.assertEqual((root / 'kpm-bom.json').read_bytes(), before)

    def test_moq_uses_purchase_quantity_for_tier_and_cost(self):
        part = read_csv('Reference,Value,LCSC\nR1,10k,C25804\n')[0]
        data = response()
        data['data']['minPurchaseNum'] = 10
        line = calculate([part], 1, {'C25804': parse_supply('C25804', data)}, {})[0]
        self.assertEqual((line.required, line.purchase), (1, 10))
        self.assertEqual(line.total, Decimal('0.01750'))

    def test_invalid_quantities_duplicate_references_and_ranges_rejected(self):
        for text in ('Reference,Quantity\nR1,0\n', 'Reference,Quantity\nR1,1.5\n',
                     'Reference,Quantity\nR1,1\nR1,1\n', 'Reference,Value\nR1-R10,10k\n'):
            with self.subTest(text=text), self.assertRaises(BomError):
                read_csv(text)

    def test_corrupted_persistent_state_is_not_silently_reset(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'kpm-bom.json'
            for text in ('{bad json', '{"version":2}',
                         '{"version":1,"overrides":{"x":{"paid":"NaN","quantity":1,"currency":"USD"}}}'):
                path.write_text(text)
                with self.assertRaises(BomError):
                    load_state(root)
                self.assertEqual(path.read_text(), text)

    def test_jlc_parser_rejects_mismatched_ids_and_invalid_tiers(self):
        data = response('C123')
        with self.assertRaises(BomError):
            parse_supply('C25804', data)
        data = response()
        data['data']['prices'][0]['productPrice'] = 'NaN'
        with self.assertRaises(BomError):
            parse_supply('C25804', data)

    def test_empty_prices_keep_stock_and_never_invent_cost(self):
        data = response()
        data['data']['prices'] = []
        supply = parse_supply('C25804', data)
        self.assertEqual(supply.stock, 80)
        self.assertIsNone(supply.unit_price(10))

    def test_jlc_frontend_price_list_preferred_and_raw_precision_preserved(self):
        data = response()
        data['data']['priceList'] = [{'startNumber': 1, 'endNumber': -1, 'productPrice': '0.0001234567'}]
        supply = parse_supply('C25804', data)
        self.assertEqual(supply.unit_price(50000), Decimal('0.0001234567'))

    def test_client_only_calls_jlcpcb_and_timeout_does_not_make_up_price(self):
        with patch('project_manager.bom_jlc.urllib.request.urlopen', side_effect=TimeoutError) as request:
            with self.assertRaises(BomError):
                fetch_supply('C25804')
            self.assertEqual(request.call_args.args[0].full_url, DETAIL_URL + 'C25804')
            self.assertEqual(request.call_args.kwargs['timeout'], 10)

    def test_sort_prices_numerically_and_keep_unpriced_rows_last_in_both_orders(self):
        cells = [(Decimal('9.5'),), (Decimal('100'),), (None,), (Decimal('2'),), (Decimal('0'),)]
        self.assertEqual(sorted_indices(cells, 0), [4, 3, 0, 1, 2])
        self.assertEqual(sorted_indices(cells, 0, True), [1, 0, 3, 4, 2])

    def test_sort_identifiers_naturally_and_dates_chronologically(self):
        identifiers = [(natural_key(value),) for value in ('C100', 'C2', 'C11')]
        self.assertEqual(sorted_indices(identifiers, 0), [1, 2, 0])
        references = [(natural_key(value),) for value in ('R10', 'R2', 'R1')]
        self.assertEqual(sorted_indices(references, 0), [2, 1, 0])
        dates = [(200,), (None,), (100,)]
        self.assertEqual(sorted_indices(dates, 0, True), [0, 2, 1])

    def test_library_classification_is_reported_not_guessed(self):
        for raw, preferred, expected in (('base', None, 'Basic'), ('expand', None, 'Extended'),
                                          ('expand', True, 'Preferred'), ('base', True, 'Basic'),
                                          (None, None, 'Unknown'), ('future-type', None, 'Unknown')):
            with self.subTest(raw=raw, preferred=preferred):
                data = response()
                data['data']['componentLibraryType'] = raw
                data['data']['preferredComponentFlag'] = preferred
                self.assertEqual(parse_supply('C25804', data).library_type, expected)

    def test_library_type_cache_roundtrip_and_old_cache_keeps_its_prices(self):
        with TemporaryDirectory() as folder:
            cache = SupplyCache(Path(folder))
            supply = parse_supply('C25804', response())
            cache.write(supply)
            self.assertEqual(cache.read('C25804').library_type, 'Basic')
            path = cache.root / 'C25804.json'
            old = json.loads(path.read_text())
            old.pop('library_type')
            path.write_text(json.dumps(old))
            restored = cache.read('C25804')
            self.assertEqual(restored.library_type, 'Unknown')
            self.assertEqual(restored.tiers, supply.tiers)


if __name__ == '__main__':
    unittest.main()
