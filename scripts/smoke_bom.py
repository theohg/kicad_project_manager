"""Exercise BOM quantity changes, paid-price dialogs, stock refresh and project isolation."""
from __future__ import annotations

import argparse
from decimal import Decimal
import faulthandler
from pathlib import Path
import threading
import time
import traceback
from tempfile import TemporaryDirectory
from unittest.mock import patch

import wx

from project_manager.bom import BomError, PriceTier, Supply
from project_manager.bom_store import load_state, save_state
from project_manager.bom_ui import COLUMNS, COLUMN_INDEX, PriceDialog
from project_manager.context import ProjectContext
from project_manager.ui import ProjectManagerFrame
from scripts.capture_screenshots import capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.dev/bom-stock.png'))
    args = parser.parse_args()
    app = wx.App(False)
    failures, requests = [], []
    mode = {'stock': 100, 'fail': False, 'wait': False}
    release = threading.Event()

    def supplier(code):
        requests.append(code)
        if mode['wait']:
            release.wait(3)
        if mode['fail']:
            raise BomError('Synthetic network interruption')
        return Supply(code, mode['stock'], (PriceTier(1, 9, Decimal('0.01')),
                                           PriceTier(10, None, Decimal('0.005'))), time.time(), library_type='Basic')

    with TemporaryDirectory(prefix='kpm-bom-smoke-') as folder:
        root = Path(folder)
        project = root / 'project'
        project.mkdir()
        (project / 'sample.kicad_pro').write_text('{}')
        (project / 'sample.kicad_pcb').write_text('(kicad_pcb)')
        (project / 'bom.csv').write_text('Reference,Value,Footprint,LCSC\nR1,10k,0603,C25804\nR2,10k,0603,C25804\nQ1,MOSFET,SO8,\n')
        save_state(project, {'version': 1, 'boards': 1, 'source': 'bom.csv', 'overrides': {}})
        context = ProjectContext('10.0.7', 'sample', str(project), str(project / 'sample.kicad_pro'),
                                 str(project / 'sample.kicad_pcb'), True, True, False)
        frame = ProjectManagerFrame(lambda: context, root / 'settings')
        frame.SetSize((1280, 820))
        frame.Show()
        panel = frame.bom_panel
        phase, started = 0, time.monotonic()

        def click_column(column):
            event = wx.ListEvent(wx.EVT_LIST_COL_CLICK.typeId, panel.table.GetId())
            event.SetColumn(column)
            panel.table.GetEventHandler().ProcessEvent(event)

        def row_for(part):
            return panel._visible_keys.index(part.key)

        def set_price(part, reset=None):
            panel.table.Select(row_for(part))
            def edit():
                dialog = next(w for w in wx.GetTopLevelWindows() if isinstance(w, PriceDialog))
                if reset == 'empty':
                    dialog.fields['paid'].SetValue('')
                    dialog.fields['quantity'].SetValue('')
                    with patch.object(dialog, 'record', side_effect=AssertionError('Reset validated manual price')):
                        dialog._accept(None)
                elif reset == 'button':
                    dialog.fields['paid'].SetValue('invalid')
                    dialog.fields['quantity'].SetValue('invalid')
                    with patch.object(dialog, 'record', side_effect=AssertionError('Reset validated manual price')):
                        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, dialog.restore_button.GetId())
                        dialog.restore_button.GetEventHandler().ProcessEvent(event)
                else:
                    dialog.fields['paid'].SetValue('20')
                    dialog.fields['quantity'].SetValue('10')
                    dialog._accept(None)
            wx.CallLater(60, edit)
            panel._edit_price()

        def check():
            nonlocal phase
            try:
                if time.monotonic() - started > 20:
                    raise AssertionError('BOM smoke timed out')
                if frame._busy or panel._loading or panel._refreshing:
                    wx.CallLater(80, check)
                    return
                if phase == 0:
                    frame.notebook.SetSelection(4)
                    phase = 1
                elif phase == 1:
                    assert len(panel.parts) == 2
                    if 'C25804' not in panel.supplies:
                        wx.CallLater(80, check)
                        return
                    assert 'incomplete' in panel.status.GetLabel()
                    assert 'partial' in panel.per_board.GetLabel()
                    count = len(requests)
                    panel.boards.SetValue(5)
                    panel._quantity_changed()
                    assert len(requests) == count, 'Quantity change made a network request'
                    assert panel.table.GetItemText(0, COLUMN_INDEX['unit_price']) == '0.005000'
                    assert panel.table.GetItemText(0, COLUMN_INDEX['library_type']) == 'Basic'
                    panel.table.Select(1)

                    def enter_price():
                        dialog = next(w for w in wx.GetTopLevelWindows() if isinstance(w, PriceDialog))
                        dialog.fields['paid'].SetValue('13')
                        dialog.fields['quantity'].SetValue('10')
                        dialog.fields['seller'].SetValue('External purchase')
                        dialog._accept(None)

                    wx.CallLater(80, enter_price)
                    panel._edit_price()
                    assert load_state(project)['overrides'][panel.parts[1].key]['paid'] == '13'
                    assert panel.table.GetItemText(1, COLUMN_INDEX['unit_price']) == '1.300000'
                    assert 'incomplete' not in panel.status.GetLabel()
                    assert 'USD 6.55' in panel.total.GetLabel(), panel.total.GetLabel()
                    manual, coded = panel.parts[1], panel.parts[0]
                    for column in range(len(COLUMNS)):
                        for descending in (False, True):
                            click_column(column)
                            assert panel._sort_descending == descending
                            assert panel._selected().key == manual.key, 'Sorting changed the selected component'
                    click_column(COLUMN_INDEX['total'])
                    click_column(COLUMN_INDEX['total'])
                    assert panel._visible_keys[0] == manual.key, 'Expensive component is not first'
                    # Paid-price reset works after sorting and preserves other overrides.
                    for reset in ('empty', 'button', 'toolbar'):
                        set_price(coded)
                        assert panel.table.GetItemText(row_for(coded), COLUMN_INDEX['unit_price']) == '2.000000'
                        assert panel.table.GetItemText(row_for(coded), COLUMN_INDEX['library_type']) == 'Basic'
                        if reset == 'toolbar':
                            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, panel.restore_button.GetId())
                            panel.restore_button.GetEventHandler().ProcessEvent(event)
                        else:
                            set_price(coded, reset)
                        assert coded.key not in load_state(project)['overrides']
                        assert manual.key in load_state(project)['overrides'], 'Reset changed another component'
                        assert panel.table.GetItemText(row_for(coded), COLUMN_INDEX['unit_price']) == '0.005000'
                        assert panel._selected().key == coded.key
                    assert 'USD 6.55' in panel.total.GetLabel()
                    assert panel._sort_column == COLUMN_INDEX['total'] and panel._sort_descending
                    mode['stock'] = 5
                    panel.refresh_supply(force=True)
                    phase = 2
                elif phase == 2:
                    assert '1 shortages' in panel.status.GetLabel(), panel.status.GetLabel()
                    assert panel.stock_changes['C25804'] == -95
                    assert panel.table.GetItemText(row_for(panel.parts[1]), COLUMN_INDEX['unit_price']) == '1.300000'
                    assert panel._visible_keys[0] == panel.parts[1].key, 'Refresh discarded the sort order'
                    capture(frame, args.output)
                    frame.SetSize((860, 640))
                    phase = 21
                elif phase == 21:
                    assert panel.auto.GetScreenRect().right <= panel.GetScreenRect().right
                    assert panel.details.GetScreenRect().bottom <= panel.GetScreenRect().bottom
                    assert panel.table.GetSize().height > 100
                    capture(frame, args.output.with_name(args.output.stem + '-small.png'))
                    mode['fail'] = True
                    panel.refresh_supply(force=True)
                    phase = 3
                elif phase == 3:
                    assert 'Synthetic' in panel.errors['C25804']
                    assert panel.supplies['C25804'].stock == 5, 'Failure discarded last known stock'
                    assert 'USD 6.55' in panel.total.GetLabel()
                    generation = panel._generation
                    # A result delivered after a context change cannot populate the new project.
                    panel.set_context(None)
                    panel._supply_loaded(generation, 'C25804', supplier('C25804') if not mode['fail'] else None,
                                         'Late result', 1, 1)
                    assert not panel.supplies and not panel.parts and not panel.errors
                    assert not panel.price_button.IsEnabled()
                    # Closing a page while a worker runs must not touch destroyed controls.
                    mode['fail'], mode['wait'] = False, True
                    panel.set_context(context)
                    phase = 4
                elif phase == 4:
                    panel.refresh_supply(force=True)
                    mode['wait'] = False
                    frame.Close()
                    release.set()
                    assert panel._closed
                    return
                wx.CallLater(100, check)
            except Exception as exc:
                traceback.print_exc()
                failures.append(str(exc) or type(exc).__name__)
                release.set()
                panel.close()
                frame.Destroy()

        with patch('project_manager.bom_ui.fetch_supply', side_effect=supplier):
            wx.CallLater(150, check)
            faulthandler.dump_traceback_later(25, exit=True)
            app.MainLoop()
            faulthandler.cancel_dump_traceback_later()
        release.set()
    if failures:
        raise SystemExit('; '.join(failures))
    print('BOM GUI: all-column sorting, stable selection, library type, all three paid-price reset paths, '
          'cached quantities, shortages, failure retention and stale-result isolation passed')


if __name__ == '__main__':
    main()
