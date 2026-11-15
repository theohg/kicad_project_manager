"""Native BOM pricing and stock page; supplier IO never runs on the UI thread."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import queue
import threading
import time

import wx

from .bom import BomError, amount, calculate, load_bom, natural_key, positive_integer, sorted_indices
from .bom_jlc import fetch_supply
from .bom_store import CACHE_TTL, STATE_FILE, SupplyCache, load_state, save_state
from .preferences import data_directory


COLUMNS = (
    ('part', 'JLCPCB part', 105), ('library_type', 'Type', 90),
    ('references', 'References', 135), ('value', 'Value', 160),
    ('quantity', '/ board', 65), ('required', 'Needed / buy', 100),
    ('stock', 'JLC stock', 100), ('unit_price', 'USD / pc', 90),
    ('total', 'USD total', 95), ('source', 'Price source', 140),
    ('checked', 'Checked', 130), ('status', 'Status', 260),
)
COLUMN_INDEX = {key: index for index, (key, _, _) in enumerate(COLUMNS)}


def money(value: Decimal | None, digits: int = 4) -> str:
    return '—' if value is None else f'{value:,.{digits}f}'


class PriceDialog(wx.Dialog):
    def __init__(self, parent, part, current):
        super().__init__(parent, title='Component price')
        self.current = current
        layout = wx.BoxSizer(wx.VERTICAL)
        label = part.code or part.value or ', '.join(part.references)
        layout.Add(wx.StaticText(self, label=label), 0, wx.ALL, 14)
        grid = wx.FlexGridSizer(cols=2, vgap=10, hgap=12)
        grid.AddGrowableCol(1)
        self.fields = {}
        for key, title, value in (
                ('paid', 'Total paid (USD)', current.get('paid', '')),
                ('quantity', 'Pieces purchased', str(current.get('quantity', 1))),
                ('seller', 'Seller / note', current.get('seller', ''))):
            grid.Add(wx.StaticText(self, label=title), 0, wx.ALIGN_CENTER_VERTICAL)
            field = wx.TextCtrl(self, value=value, size=(280, -1))
            self.fields[key] = field
            grid.Add(field, 1, wx.EXPAND)
            field.Bind(wx.EVT_TEXT, self._preview)
        layout.Add(grid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        self.preview = wx.StaticText(self, label='')
        layout.Add(self.preview, 0, wx.ALL, 14)
        note = wx.StaticText(self, label='This saves a unit price, not an inventory balance.\nJLCPCB stock checks continue for assigned parts.')
        layout.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.restore_button = wx.Button(self, label='Use JLCPCB price' if part.code else 'Clear paid price')
        self.restore_button.Enable(bool(part.code or current))
        self.restore_button.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_REVERT_TO_SAVED))
        actions.Add(self.restore_button, 0, wx.RIGHT, 12)
        actions.AddStretchSpacer()
        actions.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0)
        layout.Add(actions, 0, wx.EXPAND | wx.ALL, 14)
        self.FindWindow(wx.ID_OK).SetLabel('Save')
        self.SetSizerAndFit(layout)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self._preview()
        self.CentreOnParent()

    def record(self):
        return {'paid': str(amount(self.fields['paid'].GetValue())),
                'quantity': positive_integer(self.fields['quantity'].GetValue()),
                'seller': self.fields['seller'].GetValue().strip(), 'currency': 'USD',
                'saved_at': datetime.now(timezone.utc).isoformat()}

    def _preview(self, event=None):
        if self.current and not self.fields['paid'].GetValue().strip():
            self.preview.SetLabel('Save to clear this paid-price override.')
            return
        try:
            record = self.record()
            value = amount(record['paid']) / record['quantity']
            self.preview.SetLabel(f'USD {money(value, 6)} per component')
        except BomError:
            self.preview.SetLabel('Example: USD 13 for 10 pieces = USD 1.30 each')

    def _accept(self, event):
        if self.current and not self.fields['paid'].GetValue().strip():
            self.EndModal(wx.ID_REVERT_TO_SAVED)
            return
        try:
            self.record()
        except BomError as exc:
            wx.MessageBox(str(exc), 'Manual price', wx.OK | wx.ICON_INFORMATION, self)
            return
        self.EndModal(wx.ID_OK)


class BomStockPanel(wx.Panel):
    def __init__(self, parent, storage: Path | None = None):
        super().__init__(parent)
        self.context = None
        self.root = None
        self.source = None
        self.parts = []
        self.supplies = {}
        self.errors = {}
        self.stock_changes = {}
        self.state = {'version': 1, 'boards': 1, 'source': '', 'overrides': {}}
        self.state_error = ''
        self.save_error = ''
        self.cache = SupplyCache(storage or data_directory())
        self._generation = 0
        self._closed = False
        self._loading = False
        self._refreshing = False
        self._last_refresh_started = 0.0
        self._cancel = threading.Event()
        self._visible_keys = []
        self._sort_column = None
        self._sort_descending = False
        layout = wx.BoxSizer(wx.VERTICAL)
        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.Add(wx.StaticText(self, label='Boards'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.boards = wx.SpinCtrl(self, min=1, max=1_000_000, initial=1, size=(100, -1))
        self.boards.Bind(wx.EVT_SPINCTRL, self._quantity_changed)
        self.boards.Bind(wx.EVT_TEXT, self._quantity_typed)
        controls.Add(self.boards, 0, wx.RIGHT, 16)
        self.reload_button = wx.Button(self, label='Reload BOM')
        self.reload_button.Bind(wx.EVT_BUTTON, lambda event: self.reload_bom())
        controls.Add(self.reload_button, 0, wx.RIGHT, 8)
        self.import_button = wx.Button(self, label='Import CSV…')
        self.import_button.Bind(wx.EVT_BUTTON, self._import_csv)
        controls.Add(self.import_button, 0, wx.RIGHT, 8)
        self.refresh_button = wx.Button(self, label='Refresh JLCPCB')
        self.refresh_button.Bind(wx.EVT_BUTTON, lambda event: self.refresh_supply(force=True))
        controls.Add(self.refresh_button, 0, wx.RIGHT, 12)
        self.auto = wx.CheckBox(self, label='Auto refresh (15 min)')
        self.auto.SetValue(True)
        controls.Add(self.auto, 0, wx.ALIGN_CENTER_VERTICAL)
        layout.Add(controls, 0, wx.EXPAND | wx.ALL, 14)
        self.source_label = wx.StaticText(self, label='Open a saved project to load its BOM.', style=wx.ST_ELLIPSIZE_MIDDLE)
        layout.Add(self.source_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        totals = wx.BoxSizer(wx.HORIZONTAL)
        self.total = wx.StaticText(self, label='BOM total —')
        font = self.total.GetFont()
        font.SetPointSize(font.GetPointSize() + 3)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.total.SetFont(font)
        totals.Add(self.total, 1, wx.ALIGN_CENTER_VERTICAL)
        self.per_board = wx.StaticText(self, label='Per board —')
        totals.Add(self.per_board, 0, wx.ALIGN_CENTER_VERTICAL)
        layout.Add(totals, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.status = wx.StaticText(self, label='JLCPCB component prices in USD.', style=wx.ST_ELLIPSIZE_END)
        layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.table = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for _, label, width in COLUMNS:
            self.table.InsertColumn(self.table.GetColumnCount(), label, width=width)
        self.table.SetToolTip('Click any column header to sort; click it again to reverse the order.')
        self.table.Bind(wx.EVT_LIST_COL_CLICK, self._sort_by_column)
        self.table.Bind(wx.EVT_LIST_ITEM_SELECTED, self._selection_changed)
        self.table.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._selection_changed)
        self.table.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._edit_price)
        layout.Add(self.table, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.price_button = wx.Button(self, label='Set paid price…')
        self.price_button.Bind(wx.EVT_BUTTON, self._edit_price)
        actions.Add(self.price_button, 0, wx.RIGHT, 8)
        self.restore_button = wx.Button(self, label='Use JLCPCB price')
        self.restore_button.Bind(wx.EVT_BUTTON, self._restore_price)
        actions.Add(self.restore_button, 0, wx.RIGHT, 8)
        self.schematic_button = wx.Button(self, label='Use saved schematic')
        self.schematic_button.Bind(wx.EVT_BUTTON, self._use_schematic)
        actions.Add(self.schematic_button, 0)
        layout.Add(actions, 0, wx.EXPAND | wx.ALL, 14)
        self.details = wx.TextCtrl(self, value='Select a component to inspect its price and stock status.',
                                   style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        layout.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        self.SetSizer(layout)
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self._timer)
        self._timer.Start(60_000)
        self._quantity_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._quantity_changed, self._quantity_timer)
        self._update_buttons()

    def close(self):
        self._closed = True
        self._generation += 1
        self._cancel.set()
        self._timer.Stop()
        self._quantity_timer.Stop()

    def _post(self, function, *args):
        if not self._closed:
            wx.CallAfter(function, *args)

    def set_context(self, context):
        self.context = context
        self._generation += 1
        self._cancel.set()
        self._cancel = threading.Event()
        self._loading = self._refreshing = False
        self._last_refresh_started = 0.0
        self.parts, self.supplies, self.errors, self.stock_changes = [], {}, {}, {}
        self.root = Path(context.project_file or context.board_file).parent if context else None
        self.source = None
        self.state_error = ''
        self.save_error = ''
        self.state = {'version': 1, 'boards': 1, 'source': '', 'overrides': {}}
        if self.root:
            try:
                self.state = load_state(self.root)
            except BomError as exc:
                self.state_error = str(exc)
            self.boards.SetValue(min(1_000_000, int(self.state.get('boards', 1))))
            selected = self.state.get('source', '')
            if selected:
                self.source = Path(selected)
                if not self.source.is_absolute():
                    self.source = self.root / self.source
            else:
                self.source = self._schematic()
        self._render()
        if self.source:
            self.reload_bom()
        else:
            self._message('No saved schematic found. Import a BOM CSV.' if self.root else 'Open a saved project, then click Refresh.')

    def _schematic(self):
        if not self.context:
            return None
        anchor = self.context.project_file or self.context.board_file
        candidate = Path(anchor).with_suffix('.kicad_sch')
        return candidate if candidate.is_file() else None

    def on_shown(self):
        if not self._closed and self.parts:
            self.refresh_supply()

    def _message(self, value):
        self.status.SetLabel(value)
        self.status.SetToolTip(value)

    def _save(self, state):
        if self.state_error:
            self._message(self.state_error)
            return False
        try:
            if load_state(self.root) != self.state:
                raise BomError('BOM settings changed on disk. Use the main Refresh button before saving.')
            save_state(self.root, state)
        except (OSError, ValueError) as exc:
            self.save_error = f'Could not save {STATE_FILE}: {exc}'
            self._message(self.save_error)
            return False
        self.state = state
        self.save_error = ''
        return True

    def _remember_source(self):
        try:
            source = str(self.source.relative_to(self.root))
        except ValueError:
            source = str(self.source)
        state = dict(self.state, source=source)
        return self._save(state)

    def reload_bom(self):
        if self._closed or not self.source:
            return
        self._generation += 1
        generation = self._generation
        self._cancel.set()
        self._cancel = threading.Event()
        self._loading, self._refreshing = True, False
        self.parts, self.supplies, self.errors, self.stock_changes = [], {}, {}, {}
        source = self.source
        self._render()
        self._message('Reading saved BOM…')

        def worker():
            try:
                parts = load_bom(source)
                supplies = {code: supply for code in {p.code for p in parts if p.code}
                            if (supply := self.cache.read(code)) is not None}
                error = ''
            except (OSError, ValueError, UnicodeError) as exc:
                parts, supplies, error = [], {}, str(exc)
            except Exception:
                parts, supplies, error = [], {}, 'Could not load this BOM. Try importing a CSV.'
            self._post(self._loaded, generation, parts, supplies, error)

        threading.Thread(target=worker, daemon=True, name='bom-import').start()

    def _loaded(self, generation, parts, supplies, error):
        if self._closed or generation != self._generation:
            return
        self._loading = False
        self.parts, self.supplies = parts, supplies
        self._render()
        if error:
            self._message(error)
        elif self.IsShownOnScreen():
            self.refresh_supply()

    def refresh_supply(self, force=False):
        if self._closed or self._loading or self._refreshing or not self.parts:
            return
        now = time.time()
        codes = list(dict.fromkeys(p.code for p in self.parts if p.code and
                    (force or p.code not in self.supplies or self.supplies[p.code].library_type == 'Unknown'
                     or now - self.supplies[p.code].fetched_at >= CACHE_TTL)))
        if not codes:
            self._render()
            return
        self._refreshing = True
        self._last_refresh_started = now
        self._completed = 0
        generation, cancel = self._generation, self._cancel
        self._render()
        self._message(f'Refreshing JLCPCB: 0 / {len(codes)} components…')

        def worker():
            pending = queue.Queue()
            for code in codes:
                pending.put(code)
            halt, lock = threading.Event(), threading.Lock()
            failures = 0

            def lookup():
                nonlocal failures
                while not cancel.is_set() and not halt.is_set():
                    try:
                        code = pending.get_nowait()
                    except queue.Empty:
                        return
                    supply, error = None, ''
                    try:
                        supply = fetch_supply(code)
                        if cancel.is_set():
                            return
                        try:
                            self.cache.write(supply)
                        except OSError:
                            error = 'Live data loaded; could not save supplier cache.'
                        with lock:
                            failures = 0
                    except Exception as exc:
                        error = str(exc) if isinstance(exc, (BomError, OSError)) else 'JLCPCB lookup failed.'
                        with lock:
                            failures += 1
                            if failures >= 3:
                                halt.set()
                    self._post(self._supply_loaded, generation, code, supply, error, 0, len(codes))
                    if 'rate limit' in error.lower():
                        halt.set()
                    if cancel.wait(0.15):
                        return

            # Bounded daemon workers keep first-open refresh short without making
            # plugin shutdown wait for a blocked network request.
            workers = [threading.Thread(target=lookup, daemon=True, name='bom-jlc-part')
                       for _ in range(min(3, len(codes)))]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join()
            self._post(self._supply_finished, generation)

        threading.Thread(target=worker, daemon=True, name='bom-jlcpcb').start()

    def _supply_loaded(self, generation, code, supply, error, index, count):
        if self._closed or generation != self._generation:
            return
        if supply:
            previous = self.supplies.get(code)
            if previous and previous.stock is not None and supply.stock is not None:
                self.stock_changes[code] = supply.stock - previous.stock
            self.supplies[code] = supply
        if error:
            self.errors[code] = error
        else:
            self.errors.pop(code, None)
        self._completed += 1
        self._render()
        self._message(f'Refreshing JLCPCB: {self._completed} / {count} components…' + (' ' + error if error else ''))

    def _supply_finished(self, generation):
        if self._closed or generation != self._generation:
            return
        self._refreshing = False
        self._render()

    def _tick(self, event):
        if (self.IsShownOnScreen() and self.auto.GetValue()
                and time.time() - self._last_refresh_started >= CACHE_TTL):
            self.refresh_supply()

    def _quantity_typed(self, event):
        self._quantity_timer.StartOnce(250)

    def _quantity_changed(self, event=None):
        self._quantity_timer.Stop()
        if not self.root:
            return
        try:
            boards = positive_integer(self.boards.GetTextValue())
        except BomError:
            self._message('Board quantity must be a positive whole number.')
            return
        if boards > 1_000_000:
            self._message('Choose at most 1,000,000 boards.')
            return
        if boards != self.state.get('boards'):
            # Keep the calculation responsive even on a read-only project.
            self._save(dict(self.state, boards=boards))
        self._render()

    def _selected(self):
        index = self.table.GetFirstSelected()
        key = self._visible_keys[index] if 0 <= index < len(self._visible_keys) else None
        return next((part for part in self.parts if part.key == key), None)

    def _sort_by_column(self, event):
        column = event.GetColumn()
        if not 0 <= column < len(COLUMNS):
            return
        self._sort_descending = not self._sort_descending if column == self._sort_column else False
        self._sort_column = column
        self._render()

    def _update_buttons(self):
        selected = self._selected()
        self.boards.Enable(bool(self.root))
        self.reload_button.Enable(bool(self.source) and not self._loading)
        self.import_button.Enable(bool(self.root) and not self._loading)
        self.schematic_button.Enable(bool(self._schematic()) and not self._loading)
        self.refresh_button.Enable(bool(self.parts) and not self._loading and not self._refreshing)
        self.price_button.Enable(bool(selected) and not self.state_error)
        self.restore_button.Enable(bool(selected and selected.key in self.state.get('overrides', {})) and not self.state_error)

    def _render(self):
        if self._closed:
            return
        selected = self._selected()
        key = selected.key if selected else None
        self.source_label.SetLabel(str(self.source) if self.source else 'No BOM loaded')
        self.source_label.SetToolTip(str(self.source or ''))
        lines = calculate(self.parts, self.boards.GetValue(), self.supplies, self.state.get('overrides', {}))
        total = sum((line.total for line in lines if line.total is not None), Decimal(0))
        missing = sum(line.total is None for line in lines)
        suffix = f' · incomplete ({missing} unpriced)' if missing else ''
        self.total.SetLabel(f'{"Known subtotal" if missing else "BOM total"}  USD {money(total, 2)}' if lines else 'BOM total —')
        self.per_board.SetLabel((f'USD {money(total / self.boards.GetValue(), 2)} / board' + (' · partial' if missing else '')) if lines else 'Per board —')
        shortage = sum(line.stock is not None and line.stock < line.purchase for line in lines if line.part.code)
        stale = sum(time.time() - supply.fetched_at >= CACHE_TTL for supply in self.supplies.values())
        message = f'{len(lines)} purchasing parts · {shortage} shortages · {stale} stale · {len(self.errors)} lookup errors{suffix}'
        self._message(self.state_error or self.save_error or message)
        rows = []
        for line in lines:
            part = line.part
            supply = self.supplies.get(part.code)
            notes = [part.warning] if part.warning else []
            if line.total is None:
                notes.append('Price unavailable')
            if part.code:
                if line.stock is None:
                    notes.append('Stock unknown')
                elif line.stock < line.purchase:
                    notes.append(f'Short by {line.purchase - line.stock:,}')
            else:
                notes.append('Stock not tracked')
            if supply and time.time() - supply.fetched_at >= CACHE_TTL:
                notes.append('Stale')
            if part.code in self.errors:
                notes.append(self.errors[part.code])
            if part.code in self.stock_changes:
                notes.append(f'Stock change {self.stock_changes[part.code]:+,}')
            need = f'{line.required:,}' + (f' / {line.purchase:,}' if line.purchase != line.required else '')
            library_type = supply.library_type if supply else 'Unknown'
            status = ' · '.join(notes) or 'Available'
            references = ', '.join(part.references)
            values = (part.code or '—', library_type if part.code else '—', references, part.value,
                      str(part.quantity), need, '—' if line.stock is None else f'{line.stock:,}',
                      money(line.unit_price, 6), money(line.total), line.source,
                      datetime.fromtimestamp(supply.fetched_at).strftime('%d %b %H:%M') if supply else '—', status)
            sort_values = (natural_key(part.code) if part.code else None,
                           natural_key(library_type) if library_type != 'Unknown' else None,
                           natural_key(references), natural_key(part.value) if part.value else None,
                           part.quantity, (line.required, line.purchase), line.stock,
                           line.unit_price, line.total, natural_key(line.source),
                           supply.fetched_at if supply else None, natural_key(status))
            rows.append((line, values, sort_values))
        order = (range(len(rows)) if self._sort_column is None else
                 sorted_indices([row[2] for row in rows], self._sort_column, self._sort_descending))
        self.table.Freeze()
        try:
            self._visible_keys = []
            self.table.DeleteAllItems()
            for position in order:
                line, values, _ = rows[position]
                part = line.part
                index = self.table.InsertItem(self.table.GetItemCount(), values[0])
                self._visible_keys.append(part.key)
                for column, value in enumerate(values[1:], 1):
                    self.table.SetItem(index, column, value)
                if line.stock is not None and line.stock < line.purchase:
                    self.table.SetItemTextColour(index, wx.Colour(185, 55, 45))
                if part.key == key:
                    self.table.Select(index)
            for column, (_, label, _) in enumerate(COLUMNS):
                header = self.table.GetColumn(column)
                arrow = (' ▼' if self._sort_descending else ' ▲') if column == self._sort_column else ''
                header.SetText(label + arrow)
                self.table.SetColumn(column, header)
        finally:
            self.table.Thaw()
        self._update_buttons()
        self.Layout()

    def _selection_changed(self, event=None):
        part = self._selected()
        if part:
            self.details.ChangeValue(f'{", ".join(part.references)}\n{part.value} · {part.footprint}\n' +
                                     (self.errors.get(part.code) or part.warning or
                                      ('JLCPCB stock is shared supplier availability; it is not reserved for this build.'
                                       if part.code else 'Assign a JLCPCB number in the schematic to monitor supplier stock.')))
        else:
            self.details.ChangeValue('Select a component to inspect its price and stock status.')
        self._update_buttons()

    def _edit_price(self, event=None):
        part = self._selected()
        if not part or self.state_error:
            return
        with PriceDialog(self, part, self.state.get('overrides', {}).get(part.key, {})) as dialog:
            result = dialog.ShowModal()
            if result == wx.ID_OK:
                overrides = dict(self.state.get('overrides', {}))
                overrides[part.key] = dialog.record()
                if self._save(dict(self.state, overrides=overrides)):
                    self._render()
            elif result == wx.ID_REVERT_TO_SAVED:
                self._clear_override(part.key)

    def _restore_price(self, event=None):
        part = self._selected()
        if not part or self.state_error:
            return
        self._clear_override(part.key)

    def _clear_override(self, key):
        overrides = dict(self.state.get('overrides', {}))
        overrides.pop(key, None)
        if self._save(dict(self.state, overrides=overrides)):
            self._render()

    def _import_csv(self, event):
        with wx.FileDialog(self, 'Import BOM CSV', defaultDir=str(self.root),
                           wildcard='BOM CSV (*.csv;*.tsv)|*.csv;*.tsv|All files|*',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.source = Path(dialog.GetPath())
                self._remember_source()
                self.reload_bom()

    def _use_schematic(self, event=None):
        source = self._schematic()
        if source:
            self.source = source
            self._remember_source()
            self.reload_bom()
