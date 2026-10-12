"""Menu bar and NSPopover: CPU, RAM, GPU, history, processes, appearance and actions.

All AppKit calls happen on the main thread (created through AppHelper.callAfter).
metrics.Monitor takes samples in the background; here the latest sample is only read once a second.
The timer is added to NSRunLoopCommonModes, so the numbers update while the menu is open too.
While the panel or the secondary menu is open, Monitor collects processes every second.
"""
import os
import signal
import time
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import AppKit
import objc
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSRunLoop, NSRunLoopCommonModes, NSThread, NSTimer
from PyObjCTools import AppHelper

import cleanup
from metrics import GB, MB, core_types, hardware_info
from prefs import STYLES
from i18n import T, language as current_language, plural_problem

# CGColor from AppKit returns a raw pointer; PyObjC warns about it on every call, the result is correct
warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)

PROC_SLOTS = 10      # how many CPU rows in the submenu
PROC_CPU_ROWS = 6     # CPU rows in the “Processes” zone
PROC_RAM_ROWS = 3     # RAM rows in the “Processes” zone
PROC_GPU_ROWS = 3     # GPU rows in the “Processes” zone
RAM_SLOTS = 5        # how many memory rows
NAME_WIDTH = 22      # width of the name in the submenu (monospaced font, column alignment)
def style_titles():
    return {"both": T("Іконка і цифри"), "icon": T("Лише іконка"), "text": T("Лише цифри")}


def metric_titles():
    return {"cpu": T("Показувати CPU"), "ram": T("Показувати RAM"), "gpu": T("Показувати GPU"), "temp": T("Показувати температуру")}
PANEL_W = 340         # narrow panel
PANEL_PAD = 16        # margin at the edges
INNER_W = PANEL_W - 2 * PANEL_PAD
ZONE_H = 30           # zone row height (Processes / Appearance / Actions)
STYLE_BY_INDEX = ("both", "icon", "text")
ZONE_SYMBOLS = {"ЦП": "cpu", "ГПУ": "display", "ОЗП": "memorychip",
                "Вигляд": "paintpalette", "Дії": "bolt"}   # zone title icons
BLOCK_ORDER = ("cpu", "ram", "gpu")   # order of process lists: as in the snapshot (topCpu, topRam, topGpu)
BLOCK_COUNT = {"cpu": PROC_CPU_ROWS, "ram": PROC_RAM_ROWS, "gpu": PROC_GPU_ROWS}
BLOCK_BASE = {"cpu": 0, "ram": PROC_CPU_ROWS, "gpu": PROC_CPU_ROWS + PROC_RAM_ROWS}   # the first row index
DETAIL_ROW = 16      # detail row height (name and value)
BAR_H = 8            # height of the stacked detail bar
STYLE_INDEX = {style: i for i, style in enumerate(STYLE_BY_INDEX)}
INTERVALS = (1, 2, 5)   # seconds between samples; a segment in the “Appearance” zone
LANGUAGE_CODES = ("uk", "en")   # order of the language segments in the “Appearance” zone
INTERVAL_INDEX = {seconds: i for i, seconds in enumerate(INTERVALS)}
SECURITY_EVERY = 30.0  # seconds between reads of the security summary from the latest report (a file, not every second)


def _security_line(sec):
    """Text and colour of the security state for the panel and the menu bar."""
    level = (sec or {}).get("level", "none")
    if level == "none":
        return T("Безпека: звіту ще немає"), None
    if level == "ok":
        return T("Безпека: усе гаразд"), None
    n = int(sec.get("issues", 0))
    color = AppKit.NSColor.systemRedColor() if level == "critical" else AppKit.NSColor.systemOrangeColor()
    return T("Безпека: {n} {word}", n=n, word=plural_problem(n)), color


def _reduce_motion():
    return bool(AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())


def _fade(view, animate=True):
    if not animate or _reduce_motion():
        view.setAlphaValue_(1.0)
        return
    view.setAlphaValue_(0.0)
    def changes(context):
        context.setDuration_(0.18)
        view.animator().setAlphaValue_(1.0)
    AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(changes, None)


def _label(parent, text, x, y, width, size=12, secondary=False, digits=False, height=22):
    field = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, height))
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setFont_(_font(size) if digits else AppKit.NSFont.systemFontOfSize_(size))
    field.setTextColor_(AppKit.NSColor.secondaryLabelColor() if secondary else AppKit.NSColor.labelColor())
    field.setStringValue_(text)
    field.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    parent.addSubview_(field)
    return field


class _PanelView(AppKit.NSView):
    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, _event):
        # A click on a control works at once, even when the app is not active yet
        return True


class _RootView(_PanelView):
    """The panel root with a solid background: the transparent material mixed in the wallpaper colours and the captions got lost."""

    def drawRect_(self, rect):
        AppKit.NSColor.windowBackgroundColor().setFill()
        AppKit.NSRectFill(rect)


class _Switch(_PanelView):
    """macOS-style switch: a track with a knob. A click changes the state and calls on_change."""
    WIDTH = 34
    HEIGHT = 20
    is_on = False
    active = True
    on_change = None

    def setOn_(self, value):
        self.is_on = bool(value)
        self.setNeedsDisplay_(True)

    def setActive_(self, value):
        self.active = bool(value)
        self.setAlphaValue_(1.0 if self.active else 0.4)

    def drawRect_(self, _rect):
        b = self.bounds()
        track = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, b.size.height / 2, b.size.height / 2)
        (AppKit.NSColor.controlAccentColor() if self.is_on else AppKit.NSColor.tertiaryLabelColor()).setFill()
        track.fill()
        knob = NSMakeRect(b.size.width - 18 if self.is_on else 2, 2, 16, 16)
        AppKit.NSColor.whiteColor().setFill()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(knob).fill()

    def mouseDown_(self, _event):
        if self.active and self.on_change is not None:
            self.on_change()


class _Segments(_PanelView):
    """macOS-style segmented control: a pill with a highlighted option. A click selects and calls on_select(i)."""
    items = ()
    selected = 0
    on_select = None

    def setItems_(self, items):
        self.items = list(items)
        self.setNeedsDisplay_(True)

    def setSelected_(self, index):
        self.selected = index
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        if not self.items:
            return
        b = self.bounds()
        width = b.size.width / len(self.items)
        AppKit.NSColor.quaternaryLabelColor().setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, 7, 7).fill()
        for i, title in enumerate(self.items):
            cell = NSMakeRect(i * width, 0, width, b.size.height)
            chosen = i == self.selected
            if chosen:
                AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.16).setFill()
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AppKit.NSInsetRect(cell, 2, 2), 5, 5).fill()
            attrs = {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(11, 0.3 if chosen else 0.0),
                AppKit.NSForegroundColorAttributeName: (AppKit.NSColor.labelColor() if chosen
                                                        else AppKit.NSColor.secondaryLabelColor()),
            }
            text = AppKit.NSAttributedString.alloc().initWithString_attributes_(title, attrs)
            size = text.size()
            text.drawAtPoint_((cell.origin.x + (width - size.width) / 2, (b.size.height - size.height) / 2))

    def mouseDown_(self, event):
        if not self.items:
            return
        x = self.convertPoint_fromView_(event.locationInWindow(), None).x
        index = min(len(self.items) - 1, max(0, int(x // (self.bounds().size.width / len(self.items)))))
        if index != self.selected:
            self.setSelected_(index)
            if self.on_select is not None:
                self.on_select(index)


class _Card(_PanelView):
    """Rounded backing for the metrics (disk, network, battery)."""

    def drawRect_(self, _rect):
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSInsetRect(self.bounds(), 0.5, 0.5), 10, 10)
        AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.055).setFill()
        path.fill()


class _Tile(_PanelView):
    """Action tile: rounded background, icon and label. Drawn by itself, so it is correct in both themes."""
    title = ""
    on_click = None
    hot = False

    def drawRect_(self, _rect):
        b = self.bounds()
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AppKit.NSInsetRect(b, 0.5, 0.5), 9, 9)
        AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.12 if self.hot else 0.055).setFill()
        path.fill()
        attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(11.5, 0.3),
                 AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor()}
        text = AppKit.NSAttributedString.alloc().initWithString_attributes_(self.title, attrs)
        text.drawAtPoint_((31, (b.size.height - text.size().height) / 2))

    def mouseDown_(self, _event):
        self.hot = True
        self.setNeedsDisplay_(True)
        if self.on_click is not None:
            self.on_click()
        self.hot = False
        self.setNeedsDisplay_(True)


class _ZoneTitle(_PanelView):
    """Zone title with a chevron: points down when the zone is open. A click expands or collapses the zone."""
    title = ""
    opened = False
    on_click = None
    textInset = 12

    def drawRect_(self, _rect):
        b = self.bounds()
        # Rounded plate: the open zone is highlighted with the accent, closed ones are barely visible
        plate = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSInsetRect(b, 0, 1.5), 9, 9)
        if self.opened:
            AppKit.NSColor.controlAccentColor().colorWithAlphaComponent_(0.14).setFill()
        else:
            AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.055).setFill()
        plate.fill()
        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(13, 0.4),
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor(),
        }
        text = AppKit.NSAttributedString.alloc().initWithString_attributes_(self.title, attrs)
        # Inset as in NSTextField: the zone title aligns with the metric text and captions
        text.drawAtPoint_((self.textInset, (b.size.height - text.size().height) / 2))
        cx, cy = b.size.width - 14, b.size.height / 2
        chevron = AppKit.NSBezierPath.bezierPath()
        if self.opened:
            chevron.moveToPoint_((cx - 4, cy - 2))
            chevron.lineToPoint_((cx, cy + 2))
            chevron.lineToPoint_((cx + 4, cy - 2))
        else:
            chevron.moveToPoint_((cx - 2, cy - 4))
            chevron.lineToPoint_((cx + 2, cy))
            chevron.lineToPoint_((cx - 2, cy + 4))
        chevron.setLineWidth_(1.6)
        chevron.setLineCapStyle_(AppKit.NSLineCapStyleRound)
        AppKit.NSColor.secondaryLabelColor().setStroke()
        chevron.stroke()
    def mouseDown_(self, _event):
        if self.on_click is not None:
            self.on_click()


def _percent_text(value):
    """Percent of fixed width: figure spaces (U+2007) align the number, so a change of digits does not shift the card."""
    text = "—" if value is None else f"{value:.0f}%"
    return text.rjust(4, "\u2007")


METER_NOTE_X = 40  # the note after the CPU, RAM and GPU captions starts here
CARDS_GAP = 10     # space between the last graph and the cards
EXTRA_H = 72       # height of the optional temperature graph that pushes the cards down
TEMP_LOW, TEMP_HIGH = 20.0, 100.0     # the temperature graph spans these °C: 84 °C is the yellow line, 96 °C the red one
POWER_MAX_W = 40.0                    # the power graph spans 0 to this many watts


def _scale_temp(celsius):
    if celsius is None:
        return None
    return max(0.0, min(100.0, (celsius - TEMP_LOW) / (TEMP_HIGH - TEMP_LOW) * 100))


def _scale_power(watts):
    if watts is None:
        return None
    return max(0.0, min(100.0, abs(watts) / POWER_MAX_W * 100))


def _legend_row(view, y, color, title):
    """Detail row: a coloured dot and the name on the left, the value on the right. Returns (name, value)."""
    font = AppKit.NSFont.systemFontOfSize_(11)
    left = _label(view, "", 0, y, 220, 11, height=16)
    text = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(
        "● " if color is not None else "",
        {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color or AppKit.NSColor.labelColor()})
    text.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
        title, {AppKit.NSFontAttributeName: font,
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor()}))
    left.setAttributedStringValue_(text)
    right = _label(view, "", INNER_W - 120, y, 120, 11, digits=True, height=16)
    right.setAlignment_(AppKit.NSTextAlignmentRight)
    return left, right


def _rate(bytes_per_s):
    """Speed for the caption: KB/s or MB/s."""
    kb = bytes_per_s / 1024
    return T("{v:.1f} МБ/с", v=kb / 1024) if kb >= 1024 else T("{v:.0f} КБ/с", v=kb)


def _net_tip(info):
    """Tooltip of the network card: the local address and, on Wi-Fi, the signal, rate and channel."""
    if not info:
        return ""
    lines = []
    if info.get("ip"):
        lines.append(T("Адреса: {ip}", ip=info["ip"]))
    wifi = info.get("wifi")
    if wifi:
        words = {"excellent": T("відмінний"), "good": T("добрий"), "fair": T("посередній"), "weak": T("слабкий")}
        lines.append(T("Wi-Fi: сигнал {rssi} дБм ({word}), шум {noise} дБм", rssi=wifi["rssi"], word=words.get(wifi["quality"], ""), noise=wifi["noise"]))
        lines.append(T("Швидкість лінку {rate:.0f} Мбіт/с, канал {channel}", rate=wifi["rate"], channel=wifi["channel"] or "—"))
    return "\n".join(lines)


def _pct(value):
    return "—" if value is None else f"{value:.0f}%"


def _uptime_text(seconds):
    if seconds is None:
        return "—"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return T("{d} дн. {h} год", d=days, h=hours)
    return T("{h} год {m} хв", h=hours, m=rest // 60)


def _caption(parent, text, y):
    """Section caption: small capitals, a secondary colour."""
    field = _label(parent, text.upper(), 0, y, INNER_W, 10, True, height=14)
    field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(10, 0.4))
    return field


def _symbol(name, description):
    """SF Symbols icon for an action row. None if the symbol is not in the system."""
    image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, description)
    if image is not None:
        image.setTemplate_(True)
    return image


def _metric_color(value):
    if value is not None and value > 95:
        return AppKit.NSColor.systemRedColor()
    if value is not None and value > 80:
        return AppKit.NSColor.systemYellowColor()
    return AppKit.NSColor.systemBlueColor()


class _BarFill(_PanelView):
    def drawRect_(self, _rect):
        _metric_color(self.superview().value).setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), 2.5, 2.5).fill()


class _MeterView(_PanelView):
    """A flat bar and a time scale 0–100%, with gaps for unavailable samples. A click opens the zone of the same kind."""
    on_click = None

    def mouseDown_(self, _event):
        if self.on_click is not None:
            self.on_click()

    def resetCursorRects(self):
        if self.on_click is not None:
            self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())

    def drawRect_(self, _rect):
        width = self.bounds().size.width
        height = self.bounds().size.height
        top, bottom = 2.0, height - 1.0

        def y_of(value):
            return bottom - (bottom - top) * value / 100
        color = _metric_color(self.value)
        AppKit.NSColor.separatorColor().setStroke()
        baseline = AppKit.NSBezierPath.bezierPath()
        baseline.moveToPoint_((0, bottom))
        baseline.lineToPoint_((width, bottom))
        baseline.setLineWidth_(0.5)
        baseline.stroke()
        # Continuous segments between gaps: each is filled under the line and outlined on top
        runs, run = [], []
        for stamp, value in self.samples:
            if value is None:
                if run:
                    runs.append(run)
                run = []
                continue
            run.append((width * (stamp - self.end + 60) / 60, y_of(value)))
        if run:
            runs.append(run)
        fill = color.colorWithAlphaComponent_(0.28)
        for points in runs:
            if len(points) < 2:
                continue
            area = AppKit.NSBezierPath.bezierPath()
            area.moveToPoint_((points[0][0], bottom))
            for x, y in points:
                area.lineToPoint_((x, y))
            area.lineToPoint_((points[-1][0], bottom))
            area.closePath()
            fill.setFill()
            area.fill()
        path = AppKit.NSBezierPath.bezierPath()
        for points in runs:
            for i, (x, y) in enumerate(points):
                if i == 0:
                    path.moveToPoint_((x, y))
                else:
                    path.lineToPoint_((x, y))
        color.setStroke()
        path.setLineWidth_(1.5)
        path.stroke()
        # An optional second series (power) as a thin line in its own colour, on the same 0-100 scale
        second = getattr(self, "samples2", None)
        if second:
            line, started = AppKit.NSBezierPath.bezierPath(), False
            for stamp, value in second:
                if value is None:
                    started = False
                    continue
                point = (width * (stamp - self.end + 60) / 60, y_of(value))
                if started:
                    line.lineToPoint_(point)
                else:
                    line.moveToPoint_(point)
                    started = True
            AppKit.NSColor.systemOrangeColor().setStroke()
            line.setLineWidth_(1.2)
            line.stroke()


class _CoresView(_PanelView):
    """Load of each core: a column per core across the full width, filled from the bottom according to the percentage."""
    loads = ()
    kinds = ()   # “P” or “E” for each core
    on_click = None

    def mouseDown_(self, _event):
        if self.on_click is not None:
            self.on_click()

    def resetCursorRects(self):
        if self.on_click is not None:
            self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())

    def setLoads_(self, loads):
        self.loads = list(loads)
        self.setNeedsDisplay_(True)

    def setKinds_(self, kinds):
        self.kinds = list(kinds)
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        b = self.bounds()
        count = len(self.loads)
        if not count:
            return
        slot = b.size.width / count
        for i, load in enumerate(self.loads):
            x = i * slot + 1.0
            w = max(1.0, slot - 2.0)
            AppKit.NSColor.quaternaryLabelColor().setFill()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, 0, w, b.size.height), 2, 2).fill()
            value = max(0.0, min(100.0, load))
            h = max(1.5, b.size.height * value / 100)   # a zero core is also visible as a line at the bottom
            kind = self.kinds[i] if i < len(self.kinds) else "P"
            # Efficiency cores are teal, performance cores — by load level; a high level turns red in both
            color = _metric_color(value) if value > 80 or kind == "P" else AppKit.NSColor.systemTealColor()
            color.setFill()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, b.size.height - h, w, h), 2, 2).fill()


class _StackBar(_PanelView):
    """Stacked bar: segments (fraction 0–1, colour) on a grey background. The free share stays grey."""
    segments = ()

    def setSegments_(self, segments):
        self.segments = list(segments)
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        b = self.bounds()
        AppKit.NSColor.quaternaryLabelColor().setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, 4, 4).fill()
        x = 0.0
        for fraction, color in self.segments:
            w = b.size.width * max(0.0, min(1.0, fraction))
            if w <= 0:
                continue
            color.setFill()
            AppKit.NSBezierPath.bezierPathWithRect_(NSMakeRect(x, 0, w, b.size.height)).fill()
            x += w


SIDE_W = 240   # width of the second “Summary” menu


class _SidePanel:
    """A second menu on the left of the panel: a short security summary and changes since the previous report. Read-only."""

    def __init__(self, owner):
        self.owner = owner
        self.root = _RootView.alloc().initWithFrame_(NSMakeRect(0, 0, SIDE_W, 120))
        inner = SIDE_W - 2 * PANEL_PAD
        self.title = _label(self.root, T("Коротко"), PANEL_PAD, 12, inner, 13, height=18)
        self.title.setFont_(AppKit.NSFont.systemFontOfSize_weight_(13, 0.4))
        self.status = _label(self.root, "", PANEL_PAD, 36, inner, 12, height=16)
        self.report = _label(self.root, "", PANEL_PAD, 54, inner, 10, True, height=14)
        self.changes_head = _label(self.root, "", PANEL_PAD, 82, inner, 10, True, height=14)
        self.change_rows = [_label(self.root, "", PANEL_PAD, 0, inner, 11, height=16) for _ in range(6)]
        self.controller = AppKit.NSViewController.alloc().init()
        self.controller.setView_(self.root)
        self.popover = AppKit.NSPopover.alloc().init()
        self.popover.setContentViewController_(self.controller)
        self.popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)

    def update(self, summary):
        summary = summary or {}
        text, color = _security_line(summary)
        self.status.setStringValue_(text)
        self.status.setTextColor_(color or AppKit.NSColor.labelColor())
        checked = summary.get("checkedAt")
        self.report.setStringValue_(T("Звіт від {checked}", checked=checked) if checked else T("Звіту ще немає"))
        count = int(summary.get("changeCount", 0))
        self.changes_head.setStringValue_(T("ЗМІНИ ВІД ПОПЕРЕДНЬОГО ЗВІТУ: {count}", count=count) if count else T("ЗМІН НЕМАЄ"))
        changes = summary.get("changes") or []
        y = 102
        for i, row in enumerate(self.change_rows):
            if i < len(changes):
                c = changes[i]
                if c.get("from") is None:
                    detail = T("з'явилась: {to}", to=c.get("to"))
                elif c.get("to") is None:
                    detail = T("зникла")
                else:
                    detail = f"{c.get('from')} → {c.get('to')}"
                row.setStringValue_(f"{c.get('label')}: {detail}")
                row.setFrameOrigin_((PANEL_PAD, y))
                row.setHidden_(False)
                y += 16
            else:
                row.setHidden_(True)
        height = y + 12 if changes else 104
        self.root.setFrame_(NSMakeRect(0, 0, SIDE_W, height))
        self.popover.setContentSize_(NSMakeSize(SIDE_W, height))


class _PopoverPanel:
    """One screen: three metrics on top and three expandable zones below (processes, appearance, actions).

    Only one zone is open; the panel height adjusts to it.
    """

    def __init__(self, owner):
        self.owner = owner
        self.root = _RootView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, 300))
        root = self.root
        title = _label(root, "MacSafe", PANEL_PAD, 12, 160, 15, height=20)
        title.setFont_(AppKit.NSFont.systemFontOfSize_weight_(15, 0.5))
        self.security = _label(root, T("Безпека: звіту ще немає"), PANEL_PAD, 14, INNER_W, 11, True, height=16)
        self.security.setAlignment_(AppKit.NSTextAlignmentRight)
        self.meters = {}
        y = 44
        for key in ("cpu", "ram", "gpu"):
            cap = _label(root, key.upper(), PANEL_PAD, y, 120, 11, height=16)
            cap.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11, 0.5))
            value = _label(root, "—", PANEL_W - PANEL_PAD - 110, y - 7, 110, size=20, digits=True, height=28)
            value.setAlignment_(AppKit.NSTextAlignmentRight)
            # The note sits in the caption's row, so a block is one line shorter
            detail = _label(root, "", PANEL_PAD + METER_NOTE_X, y + 1, INNER_W - METER_NOTE_X - 70, 11, True, height=16)
            graph = _MeterView.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, y + 22, INNER_W, 24))
            graph.value, graph.samples, graph.end = None, (), 0
            graph.setWantsLayer_(True)
            graph.fill = _BarFill.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 5))
            graph.fill.setWantsLayer_(True)
            graph.fill.setHidden_(True)          # the fill bar is removed; only the history line remains
            graph.addSubview_(graph.fill)
            graph.setAccessibilityLabel_(key.upper() + ": " + T("історія за 60 секунд"))
            root.addSubview_(graph)
            self.meters[key] = (value, detail, graph)
            graph.on_click = lambda key=key: self.open_zone(self._block_zone[key])
            if key == "cpu":
                self.cores = _CoresView.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, y + 50, INNER_W, 42))
                self.cores.setKinds_(core_types())
                self.cores.setToolTip_(T("Ядра: синє — продуктивні (P), бірюзове — економні (E)"))
                self.cores.on_click = lambda: self.open_zone(self._block_zone["cpu"])
                root.addSubview_(self.cores)
                y += 44
            y += 58
        # System metrics row: disk, network, battery
        self.infos = {}
        self._cards_base = y - 2     # the end of the graphs: the optional temperature graph goes here, the cards under it
        self._card_views = []
        y += CARDS_GAP
        col = INNER_W / 3
        for i, (key, caption) in enumerate((("disk", T("ДИСК")), ("net", T("МЕРЕЖА")), ("battery", T("БАТАРЕЯ")))):
            x0 = PANEL_PAD + i * col
            card = _Card.alloc().initWithFrame_(NSMakeRect(x0, y - 8, col - 6, 58))
            root.addSubview_(card)
            self._card_views.append(card)
            cap = _label(root, caption, x0 + 10, y - 3, col - 26, 10, True, height=14)
            cap.setFont_(AppKit.NSFont.systemFontOfSize_weight_(10, 0.5))
            top = _label(root, "—", x0 + 10, y + 10, col - 26, 13, digits=True, height=18)
            sub = _label(root, "", x0 + 10, y + 28, col - 26, 10, True, height=14)
            self.infos[key] = (top, sub)
            self._card_views += [cap, top, sub]
        self._card_frames = [v.frame() for v in self._card_views]
        y += 66
        # Optional graph above the cards: temperature and power. It is added or removed in the Appearance zone
        self.extra = {}
        self._extra_top = self._cards_base + 4
        self.extra["temp"] = self._make_extra_meter(root, "temp", T("ТЕМПЕРАТУРА · W"), self._extra_top)
        self.divider = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, y - 8, INNER_W, 1))
        self.divider.setBoxType_(AppKit.NSBoxSeparator)
        self.divider.setHidden_(True)   # cards and zone plates separate the blocks without lines
        root.addSubview_(self.divider)
        self._zones_base = y - 2
        self.zones_top = self._zones_base   # zones are counted from the end of the metrics block (plus the optional graphs)
        self.zones = []
        self.process_rows = []
        self.switches = {}
        self.style_seg = None
        self.interval_seg = None
        self.auto = None
        hw = hardware_info()
        self.chip = hw.get("chip") or "—"
        self.gpu_cores = hw.get("gpuCores")
        self._blocks, self.details = {}, {}
        self._block_zone = {"cpu": 0, "gpu": 1, "ram": 2}   # indices of the CPU, GPU and RAM zones in self.zones
        for title, fill in (("ЦП", self._fill_cpu), ("ГПУ", self._fill_gpu), ("ОЗП", self._fill_ram),
                            ("Вигляд", self._fill_appearance), ("Дії", self._fill_actions)):
            self._zone(title, fill)
        self.process_rows = [self._blocks[key]["rows"] for key in BLOCK_ORDER]
        self.footer_line = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, 0, INNER_W, 1))
        self.footer_line.setBoxType_(AppKit.NSBoxSeparator)
        root.addSubview_(self.footer_line)
        self.footer_open = self._footer_button(T("Відкрити додаток"), "openWindow:", 110, AppKit.NSTextAlignmentLeft, "doc.text")
        self.footer_quit = self._footer_button(T("Вийти"), "quit:", 80, AppKit.NSTextAlignmentLeft, "power")
        self.footer_side = self._footer_button("", "sideReport:", 22, AppKit.NSTextAlignmentLeft, "sidebar.left")
        self.footer_side.setToolTip_(T("Коротко: безпека і зміни від попереднього звіту"))
        # Status line in one row: long text is truncated instead of taking a second row at the bottom
        self.note = _label(root, "", PANEL_PAD, 0, INNER_W, 10, True, height=14)
        self.note.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        # The panel scrolls when it is taller than the screen
        self.scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, 300))
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        self.scroll.setDrawsBackground_(False)
        self.scroll.setBorderType_(AppKit.NSNoBorder)
        self.scroll.setDocumentView_(root)
        self.controller = AppKit.NSViewController.alloc().init()
        self.controller.setView_(self.scroll)
        self.popover = AppKit.NSPopover.alloc().init()
        self.popover.setContentViewController_(self.controller)
        self.popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        self.popover.setDelegate_(owner._targets)
        self.armed = None            # (row index, time) after the first click on “×”
        self.kill_targets = {}       # row index → process group from the latest snapshot
        self.selected = None         # index of the row selected by clicking the name (pins the list)
        self.pinned = None           # groups by row at the moment of pinning; None — the list is live
        self.slot_names = [[], [], []]   # names by row of CPU, RAM and GPU: the order is stable between updates
        self._lit = None             # the highlighted row: we change the background only when the selection changes
        self._icon_paths = {}        # which app is already shown in the row icon: we change it only when it changes
        self.apply_extra()
        self._layout()
        self.sync()

    # ---------- optional graphs: temperature and power ----------
    def _make_extra_meter(self, root, key, caption, y):
        cap = _label(root, caption, PANEL_PAD, y, 160, 11, height=16)
        cap.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11, 0.5))
        value = _label(root, "—", PANEL_W - PANEL_PAD - 200, y - 7, 200, size=20, digits=True, height=28)
        value.setAlignment_(AppKit.NSTextAlignmentRight)
        detail = _label(root, T("Очікування даних"), PANEL_PAD, y + 18, INNER_W, 11, True, height=16)
        graph = _MeterView.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, y + 34, INNER_W, 24))
        graph.value, graph.samples, graph.end = None, (), 0
        graph.setWantsLayer_(True)
        graph.fill = _BarFill.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 5))
        graph.fill.setWantsLayer_(True)
        graph.fill.setHidden_(True)
        graph.addSubview_(graph.fill)
        graph.setAccessibilityLabel_(caption + ": " + T("історія за 60 секунд"))
        root.addSubview_(graph)
        return {"views": (cap, value, detail, graph), "value": value, "detail": detail, "graph": graph}

    def apply_extra(self):
        """Shows the temperature and power graph when it is on, and moves the zones below by the height it takes."""
        on = bool(self.owner._settings.get("graphTemp"))
        for view in self.extra["temp"]["views"]:
            view.setHidden_(not on)
        shift = EXTRA_H if on else 0
        for view, frame in zip(self._card_views, self._card_frames):
            view.setFrame_(NSMakeRect(frame.origin.x, frame.origin.y + shift, frame.size.width, frame.size.height))
        self.zones_top = self._zones_base + shift
        if getattr(self, "zones", None):
            self._layout(animate=False)

    def _update_extra(self, snap):
        temp = snap.get("temp")
        power = snap.get("power") or {}
        sys_w, bat_w = power.get("sysW"), power.get("batW")
        meter = self.extra["temp"]
        t_text = "—" if temp is None else f"{temp:.0f} °C"
        w_text = "—" if sys_w is None else f"{sys_w:.1f} W"
        meter["value"].setStringValue_(f"{t_text}  ·  {w_text}")
        if temp is None and sys_w is None:
            meter["detail"].setStringValue_(T("Датчики недоступні"))
        elif bat_w is not None and bat_w > 0:
            meter["detail"].setStringValue_(T("заряд +{w:.1f} W", w=bat_w))
        elif bat_w is not None and bat_w < -0.5 and (snap.get("battery") or {}).get("plugged"):
            # On the adapter and the battery still gives power: that is slow charging only if the adapter is below the load.
            # Right after plugging in the battery current lags behind the adapter reading: say nothing then
            adapter = power.get("adapterW")
            if not adapter:
                meter["detail"].setStringValue_(T("від мережі, батарея −{w:.1f} W", w=-bat_w))
            elif adapter < (sys_w or 0):
                meter["detail"].setStringValue_(T("повільно: блок {a} W, батарея −{w:.1f} W", a=adapter, w=-bat_w))
            else:
                meter["detail"].setStringValue_("")
        else:
            meter["detail"].setStringValue_("")
        graph = meter["graph"]
        graph.value = _scale_temp(temp)
        graph.samples = [(s[0], s[1].get("temp")) for s in self.owner._history]
        graph.samples2 = [(s[0], s[1].get("power")) for s in self.owner._history]
        graph.end = self.owner._history[-1][0] if self.owner._history else 0
        graph.setNeedsDisplay_(True)

    # ---------- zones ----------
    def _zone(self, title, fill):
        index = len(self.zones)
        head = _ZoneTitle.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, 0, INNER_W, ZONE_H))
        head.title = T(title)
        symbol = ZONE_SYMBOLS.get(title)
        if symbol is not None:
            icon = AppKit.NSImageView.alloc().initWithFrame_(NSMakeRect(10, (ZONE_H - 14) / 2, 14, 14))
            icon.setImage_(_symbol(symbol, title))
            icon.setContentTintColor_(AppKit.NSColor.secondaryLabelColor())
            head.addSubview_(icon)
            head.textInset = 32
        head.on_click = lambda index=index: self.toggle_zone(index)
        self.root.addSubview_(head)
        content = _PanelView.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, 0, INNER_W, 0))
        content.setHidden_(True)
        self.root.addSubview_(content)
        zone = {"head": head, "content": content, "open": False, "height": 0}
        zone["height"] = fill(content)
        self.zones.append(zone)

    def toggle_zone(self, index):
        for i, zone in enumerate(self.zones):
            zone["open"] = i == index and not zone["open"]
        self._layout()

    def open_zone(self, index):
        """A click on a graph: the zone of that kind opens (a second click closes it) and is scrolled into view."""
        self.toggle_zone(index)
        zone = self.zones[index]
        if zone["open"]:
            head = zone["head"].frame()
            self.root.scrollRectToVisible_(NSMakeRect(0, head.origin.y, PANEL_W, ZONE_H + 6 + zone["height"] + 18))

    def _layout(self, animate=True):
        y = self.zones_top
        for zone in self.zones:
            zone["head"].setFrame_(NSMakeRect(PANEL_PAD, y, INNER_W, ZONE_H))
            zone["head"].opened = zone["open"]
            zone["head"].setNeedsDisplay_(True)
            y += ZONE_H
            if zone["open"]:
                zone["content"].setFrame_(NSMakeRect(PANEL_PAD, y + 6, INNER_W, zone["height"]))
                zone["content"].setHidden_(False)
                if animate and not zone.get("shown") and not _reduce_motion():
                    # The zone's content fades in as it opens
                    zone["content"].setAlphaValue_(0.0)
                    AppKit.NSAnimationContext.beginGrouping()
                    AppKit.NSAnimationContext.currentContext().setDuration_(0.28)
                    zone["content"].animator().setAlphaValue_(1.0)
                    AppKit.NSAnimationContext.endGrouping()
                else:
                    zone["content"].setAlphaValue_(1.0)
                zone["shown"] = True
                y += zone["height"] + 18
            else:
                zone["content"].setHidden_(True)
                zone["shown"] = False
        # A note above the footer, and the buttons at the very bottom with the same margin
        self.note.setFrame_(NSMakeRect(PANEL_PAD, y, INNER_W, 14))
        y += 22
        self.footer_line.setFrame_(NSMakeRect(PANEL_PAD, y, INNER_W, 1))
        for button in (self.footer_open, self.footer_quit):
            button.sizeToFit()
        open_w = self.footer_open.frame().size.width
        quit_w = self.footer_quit.frame().size.width
        self.footer_side.setFrame_(NSMakeRect(PANEL_PAD, y + 8, 22, 22))
        self.footer_open.setFrame_(NSMakeRect(PANEL_PAD + 30, y + 8, open_w, 22))
        self.footer_quit.setFrame_(NSMakeRect(PANEL_W - PANEL_PAD - quit_w, y + 8, quit_w, 22))
        y += 8 + 22 + 10
        self.root.setFrame_(NSMakeRect(0, 0, PANEL_W, y))
        y = min(y, self._max_height())
        # Resizing an open NSPopover shifts its content, so we call it only when the height really changed
        if (PANEL_W, y) != getattr(self, "_size", None):
            self._size = (PANEL_W, y)
            # Per-second height changes from the process lists are without animation, otherwise the panel would sway side to side
            self.popover.setAnimates_(animate and not _reduce_motion())
            self.popover.setContentSize_(NSMakeSize(PANEL_W, y))

    def _max_height(self):
        """The tallest the panel may be: the visible height of the screen under the menu bar icon minus the arrow and margins."""
        button = getattr(self.owner, "_button", None)
        window = button.window() if button is not None else None
        screen = (window.screen() if window is not None else None) or AppKit.NSScreen.mainScreen()
        if screen is None:
            return 10000
        return max(300, screen.visibleFrame().size.height - 90)

    def freeze(self):
        """Pins the rows as they are visible now: positions do not change while there is no selection or “Sure?”."""
        if self.pinned is None:
            total = sum(len(rows) for rows in self.process_rows)
            self.pinned = [self.kill_targets.get(i) for i in range(total)]

    def toggle_select(self, index):
        """A click on the name: selects the row, a second click on it clears the selection."""
        self.freeze()
        self.selected = None if self.selected == index else index
        self._release()

    def clear_selection(self):
        self.selected = None
        self.armed = None
        self.pinned = None

    def _release(self):
        """Releases the pin when there is neither a selection nor a “Sure?” countdown."""
        if self.selected is None and (self.armed is None or time.monotonic() - self.armed[1] > 3):
            self.pinned = None

    def _fill_block(self, view, key, detail_rows):
        """Zone: details (stacked bar and rows) on top, below the top processes of this type."""
        y = 0
        bar = _StackBar.alloc().initWithFrame_(NSMakeRect(0, y, INNER_W, BAR_H))
        view.addSubview_(bar)
        y += BAR_H + 10
        legend = []
        for color, title in detail_rows:
            legend.append(_legend_row(view, y, color, title))
            y += DETAIL_ROW
        self.details[key] = {"bar": bar, "legend": legend, "h": y}
        caption = _label(view, T("ТОП ПРОЦЕСІВ"), 0, y + 6, 110, 10, True)
        heads = []
        for text, x, w in (("CPU", 116, 52), ("GPU", 168, 50), (T("ПАМʼЯТЬ"), 220, 62)):
            head = _label(view, text, x, y + 6, w, 9, True, height=14)
            head.setAlignment_(AppKit.NSTextAlignmentRight)
            heads.append(head)
        self.heads = getattr(self, "heads", {})
        self.heads[key] = heads
        rows = [self._proc_row(view, 0, BLOCK_BASE[key] + i) for i in range(BLOCK_COUNT[key])]
        self._blocks[key] = {"caption": caption, "rows": rows}
        # At first the list is hidden: rows appear with the data
        for item in [caption] + heads + [it for row in rows for it in row]:
            item.setHidden_(True)
        return y + 6 + 16 + BLOCK_COUNT[key] * 18 + 8

    def _fill_cpu(self, view):
        blue, red = AppKit.NSColor.systemBlueColor(), AppKit.NSColor.systemRedColor()
        return self._fill_block(view, "cpu", [
            (red, T("Система")), (blue, T("Користувач")), (AppKit.NSColor.tertiaryLabelColor(), T("Вільно")),
            (AppKit.NSColor.systemTealColor(), T("Енергоефективні ядра")), (blue, T("Продуктивні ядра")),
            (None, T("Час роботи")), (None, T("Навантаження 1 · 5 · 15 хв")),
        ])

    def _fill_gpu(self, view):
        return self._fill_block(view, "gpu", [
            (None, T("Модель")), (None, T("Ядра")), (AppKit.NSColor.systemBlueColor(), T("Використання")),
            (AppKit.NSColor.systemTealColor(), "Render"), (AppKit.NSColor.systemPurpleColor(), "Tiler"),
        ])

    def _fill_ram(self, view):
        return self._fill_block(view, "ram", [
            (None, T("Використовується")), (AppKit.NSColor.systemBlueColor(), T("Програми")),
            (AppKit.NSColor.systemOrangeColor(), T("Постійна")), (AppKit.NSColor.systemRedColor(), T("Стиснена")),
            (AppKit.NSColor.tertiaryLabelColor(), T("Вільна")), (None, "Swap"),
        ])

    def _set_details(self, key, segments, values):
        """The bar and the detail values of the zone. Values change together, without intermediate digits."""
        detail = self.details[key]
        detail["bar"].setSegments_(segments)
        for (_, right), text in zip(detail["legend"], values):
            right.setStringValue_(text)

    def _update_details(self, snap):
        times = snap.get("cpuTimes") or {}
        user, system, idle = (times.get(k, 0.0) for k in ("user", "system", "idle"))
        load = snap.get("loadavg") or (0.0, 0.0, 0.0)
        blue = AppKit.NSColor.systemBlueColor()
        self._set_details("cpu", [(system / 100, AppKit.NSColor.systemRedColor()), (user / 100, blue)], [
            _pct(system), _pct(user), _pct(idle), _pct(snap.get("eAvg")), _pct(snap.get("pAvg")),
            _uptime_text(snap.get("uptime")), f"{load[0]:.2f} · {load[1]:.2f} · {load[2]:.2f}",
        ])
        gpu = snap.get("gpuDetail") or {}
        self._set_details("gpu", [((gpu.get("device") or 0) / 100, blue)], [
            self.chip, str(self.gpu_cores or "—"), _pct(gpu.get("device")),
            _pct(gpu.get("renderer")), _pct(gpu.get("tiler")),
        ])
        mem = snap.get("memory") or {}
        total = mem.get("totalGb") or 1.0
        app, wired, comp = (mem.get(k, 0.0) for k in ("appGb", "wiredGb", "compressedGb"))
        free = mem.get("freeGb", 0.0)
        self._set_details("ram", [(app / total, blue),
                                  (wired / total, AppKit.NSColor.systemOrangeColor()),
                                  (comp / total, AppKit.NSColor.systemRedColor())], [
            T("{used:.1f} ГБ з {total:.0f} ГБ", used=total - free, total=total), T("{v:.1f} ГБ", v=app), T("{v:.1f} ГБ", v=wired),
            T("{v:.1f} ГБ", v=comp), T("{v:.1f} ГБ", v=free), T("{v:.1f} ГБ", v=mem.get("swapGb", 0.0)),
        ])

    def _set_icon(self, icon, idx, path):
        """The system app icon in the row. We set the image only when the app has changed."""
        key = path or ""   # without an app — the default icon, the key is empty
        if self._icon_paths.get(idx) != key:
            icon.setImage_(_app_icon(path) if path else _generic_icon())
            self._icon_paths[idx] = key
        icon.setHidden_(False)

    def _place_processes(self, entries, sizes, ready, armed):
        """The top processes of this type under the details of each zone. An empty list takes no space.

        Returns the height of each zone: details plus the list.
        """
        flat = 0
        heights = {}
        for key, size in zip(BLOCK_ORDER, sizes):
            block = self._blocks[key]
            top = self.details[key]["h"] + 6
            filled = [i for i in range(size) if entries[flat + i][0] is not None]
            block["caption"].setHidden_(not filled)
            for head in self.heads.get(key, ()):
                head.setHidden_(not filled)
                head.setFrameOrigin_((head.frame().origin.x, top))
            row_y = top
            if filled:
                block["caption"].setFrameOrigin_((0, top))
                row_y = top + 16
            for i, (name, cpu, gpu, memory, kill, bg, icon) in enumerate(block["rows"]):
                idx = flat + i
                group, stale = entries[idx]
                shown = group is not None
                for item in (name, cpu, gpu, memory, kill, bg):
                    item.setHidden_(not shown)
                if not shown:
                    self.kill_targets.pop(idx, None)
                    icon.setHidden_(True)
                    continue
                bg.setFrameOrigin_((-6, row_y - 3))
                name.setFrameOrigin_((18, row_y - 2))
                cpu.setFrameOrigin_((116, row_y))
                gpu.setFrameOrigin_((168, row_y))
                memory.setFrameOrigin_((220, row_y))
                kill.setFrameOrigin_((INNER_W - 22, row_y - 4))
                icon.setFrameOrigin_((0, row_y - 1))
                self._set_icon(icon, idx, group.get("bundle"))
                row_y += 18
                title = group["name"] + (f" ×{group['count']}" if group["count"] > 1 else "")
                name.setTitle_(title)
                name.setToolTip_(title)
                if not ready:
                    cpu_text, gpu_text = "…", "…"     # after priming, CPU and GPU are not yet counted
                else:
                    cpu_text = "—" if stale else f"{group['cpu']:.1f}%"
                    gpu_value = group.get("gpu")
                    gpu_text = "—" if stale or gpu_value is None else f"{gpu_value:.0f}%"
                cpu.setStringValue_(cpu_text)
                gpu.setStringValue_(gpu_text)
                memory.setStringValue_(_mem(group["rss"]))
                kill.setTag_(idx)
                if stale:
                    kill.setHidden_(True)
                    self.kill_targets.pop(idx, None)
                else:
                    self.kill_targets[idx] = group
                    kill.setHidden_(not group.get("killable"))
                kill.setTitle_(T("Так?") if armed and armed[0] == idx else "×")
            heights[key] = row_y + 8 if filled else top + 4
            flat += size
        return heights

    def _proc_row(self, view, y, index):
        bg = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(-6, y - 3, INNER_W + 12, 20))
        bg.setWantsLayer_(True)
        bg.layer().setCornerRadius_(4)
        view.addSubview_(bg)
        icon = AppKit.NSImageView.alloc().initWithFrame_(NSMakeRect(0, y - 1, 14, 14))
        icon.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        icon.setHidden_(True)
        view.addSubview_(icon)
        name = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(18, y - 2, 98, 16))
        name.setBordered_(False)
        name.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        name.setAlignment_(AppKit.NSTextAlignmentLeft)
        name.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        name.setTitle_("—")
        name.setTarget_(self.owner._targets)
        name.setAction_("selectRow:")   # a click on the name selects and pins the row
        name.setTag_(index)
        view.addSubview_(name)
        cpu = _label(view, "", 116, y, 52, 11, digits=True)
        gpu = _label(view, "", 168, y, 50, 11, digits=True)
        mem = _label(view, "", 220, y, 62, 11, digits=True)
        for field in (cpu, gpu, mem):
            field.setAlignment_(AppKit.NSTextAlignmentRight)
        kill = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(INNER_W - 22, y - 4, 22, 20))
        kill.setBordered_(False)
        kill.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        kill.setTitle_("×")
        kill.setTarget_(self.owner._targets)
        kill.setAction_("killRow:")
        kill.setTag_(index)
        kill.setHidden_(True)
        view.addSubview_(kill)
        return (name, cpu, gpu, mem, kill, bg, icon)

    def _fill_appearance(self, view):
        y = 0
        _caption(view, T("Показники в барі"), y)
        y += 22
        for key, title in (("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"), ("temp", T("Температура, °C"))):
            _label(view, title, 0, y, 200, 12, height=20)
            sw = _Switch.alloc().initWithFrame_(NSMakeRect(INNER_W - _Switch.WIDTH, y, _Switch.WIDTH, _Switch.HEIGHT))
            sw.on_change = lambda key=key: self.owner.toggle_metric(key)
            view.addSubview_(sw)
            self.switches[key] = sw
            y += 28
        y += 10
        _caption(view, T("Графіки в панелі"), y)
        y += 22
        for key, title in (("graphTemp", T("Температура і W")),):
            _label(view, title, 0, y, 200, 12, height=20)
            sw = _Switch.alloc().initWithFrame_(NSMakeRect(INNER_W - _Switch.WIDTH, y, _Switch.WIDTH, _Switch.HEIGHT))
            sw.on_change = lambda key=key: self.owner.toggle_graph(key)
            view.addSubview_(sw)
            self.switches[key] = sw
            y += 28
        y += 10
        _caption(view, T("Автоматично"), y)
        y += 22
        for key, title in (("alerts", T("Сповіщення")), ("alertMemoryDisk", T("  · памʼять і диск")), ("alertBattery", T("  · батарея")),
                           ("alertLoad", T("  · перегрів і навантаження")), ("dailyCheck", T("Щоденна перевірка"))):
            _label(view, title, 0, y, 200, 12, height=20)
            sw = _Switch.alloc().initWithFrame_(NSMakeRect(INNER_W - _Switch.WIDTH, y, _Switch.WIDTH, _Switch.HEIGHT))
            sw.on_change = lambda key=key: self.owner.toggle_flag(key)
            view.addSubview_(sw)
            self.switches[key] = sw
            y += 28
        y += 10
        _caption(view, T("Стиль в барі"), y)
        y += 22
        self.style_seg = _Segments.alloc().initWithFrame_(NSMakeRect(0, y, INNER_W, 26))
        self.style_seg.setItems_((T("Обидва"), T("Іконка"), T("Цифри")))
        self.style_seg.on_select = lambda i: self.owner.set_style(STYLE_BY_INDEX[i])
        view.addSubview_(self.style_seg)
        y += 26 + 14
        _caption(view, T("Оновлення даних"), y)
        y += 22
        self.interval_seg = _Segments.alloc().initWithFrame_(NSMakeRect(0, y, INNER_W, 26))
        self.interval_seg.setItems_([T("{s} с", s=seconds) for seconds in INTERVALS])
        self.interval_seg.on_select = lambda i: self.owner.set_interval(INTERVALS[i])
        view.addSubview_(self.interval_seg)
        y += 26 + 14
        _caption(view, T("Мова"), y)
        y += 22
        self.lang_seg = _Segments.alloc().initWithFrame_(NSMakeRect(0, y, INNER_W, 26))
        self.lang_seg.setItems_(("Українська", "English"))
        self.lang_seg.on_select = lambda i: self.owner.set_language(LANGUAGE_CODES[i])
        view.addSubview_(self.lang_seg)
        return y + 26

    def _fill_actions(self, view):
        tile_w = (INNER_W - 8) / 2
        for n, (title, selector, symbol) in enumerate(((T("Швидке очищення"), "quickClean:", "sparkles"),
                                                       (T("Перевірка"), "runCheck:", "checkmark.shield"),
                                                       (T("Детальне очищення"), "cleanup:", "trash"),
                                                       (T("Звільнити памʼять"), "purge:", "memorychip"))):
            view.addSubview_(self._tile(title, selector, symbol, (n % 2) * (tile_w + 8), (n // 2) * 42, tile_w))
        y = 92
        _label(view, T("Запускати при вході"), 0, y + 2, 240, 12, height=20)
        self.auto = _Switch.alloc().initWithFrame_(NSMakeRect(INNER_W - _Switch.WIDTH, y + 2, _Switch.WIDTH, _Switch.HEIGHT))
        self.auto.on_change = lambda: self.owner.toggle_autostart()
        view.addSubview_(self.auto)
        return y + 26

    def _tile(self, title, selector, symbol, x, y, width):
        tile = _Tile.alloc().initWithFrame_(NSMakeRect(x, y, width, 36))
        tile.title = title
        tile.setAccessibilityLabel_(title)
        tile.setAccessibilityRole_(AppKit.NSAccessibilityButtonRole)
        icon = AppKit.NSImageView.alloc().initWithFrame_(NSMakeRect(10, 10, 16, 16))
        image = _symbol(symbol, title)
        if image is not None:
            icon.setImage_(image)
            icon.setContentTintColor_(AppKit.NSColor.controlAccentColor())
            tile.addSubview_(icon)
        method = selector.replace(":", "_")
        tile.on_click = lambda: getattr(self.owner._targets, method)(None)
        return tile

    def _footer_button(self, title, selector, width, alignment, symbol):
        button = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, width, 22))
        button.setBordered_(False)
        button.setAlignment_(alignment)
        button.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        button.setTitle_(title)
        image = _symbol(symbol, title)
        if image is not None:
            button.setImage_(image)
            button.setImagePosition_(AppKit.NSImageLeading)
        button.setTarget_(self.owner._targets)
        button.setAction_(selector)
        self.root.addSubview_(button)
        return button

    # ---------- state and updates ----------
    def sync(self):
        for key, control in self.switches.items():
            control.setOn_(self.owner._settings[key])
        self.style_seg.setSelected_(STYLE_INDEX.get(self.owner._settings["style"], 0))
        self.interval_seg.setSelected_(INTERVAL_INDEX.get(self.owner._settings["interval"], 0))
        self.lang_seg.setSelected_(LANGUAGE_CODES.index(current_language()) if current_language() in LANGUAGE_CODES else 0)
        self.auto.setOn_(self.owner._autostart_on)
        self.auto.setActive_(not self.owner._auto_busy)

    def update(self, snap):
        self.ends = {"cpu": snap["cpu"], "ram": snap["ram"]["percent"], "gpu": snap["gpu"]}
        self.meters["cpu"][1].setStringValue_(T("{cores} ядер", cores=snap["cores"]))
        ram = snap["ram"]
        self.meters["ram"][1].setStringValue_(
            T("{used:.1f}/{total:.0f} ГБ · swap {swap:.1f}", used=ram["usedGb"], total=ram["totalGb"], swap=ram["swapGb"]))
        self.meters["gpu"][1].setStringValue_(T("Дані недоступні") if snap["gpu"] is None else "")
        self.cores.setLoads_(snap.get("perCore") or [])
        self._update_infos(snap)
        self._update_extra(snap)
        for key, (_, _, graph) in self.meters.items():
            graph.samples = [(s[0], s[1].get(key)) for s in self.owner._history]
            graph.end = self.owner._history[-1][0]
        self._release()
        armed = self.armed if self.armed and time.monotonic() - self.armed[1] <= 3 else None
        sizes = [len(rows) for rows in self.process_rows]
        keys = ("topCpu", "topRam", "topGpu")
        live = {g["name"]: g for g in snap.get("groups") or
                list(snap["topCpu"]) + list(snap["topRam"]) + list(snap.get("topGpu") or [])}
        if self.pinned is None:
            self.slot_names = [_stable_names(self.slot_names[i], snap.get(key) or [], size)
                               for i, (key, size) in enumerate(zip(keys, sizes))]
            names = [name for group_names in self.slot_names for name in group_names]
            entries = [(live.get(name) if name is not None else None, False) for name in names]
        else:
            entries = _pinned_groups(self.pinned, live)
            self.pinned = [g for g, _ in entries]
        rows_flat = [row for rows in self.process_rows for row in rows]
        if self._lit != self.selected:
            if self._lit is not None and self._lit < len(rows_flat):
                rows_flat[self._lit][5].layer().setBackgroundColor_(None)
            if self.selected is not None:
                rows_flat[self.selected][5].layer().setBackgroundColor_(_highlight_color())
            self._lit = self.selected
        heights = self._place_processes(entries, sizes, snap.get("procReady", True) is not False, armed)
        resize = False
        for key, height in heights.items():
            zone = self.zones[self._block_zone[key]]
            if zone["height"] != height:
                zone["height"] = height
                resize = resize or zone["open"]   # a collapsed zone does not change the panel size
        if resize:
            self._layout(animate=False)
        self._update_details(snap)
        self._draw_values()

    def _update_infos(self, snap):
        disk, net, bat = snap.get("disk"), snap.get("net"), snap.get("battery")
        top, sub = self.infos["disk"]
        top.setStringValue_("—" if not disk else T("{free:.0f} ГБ", free=disk["freeGb"]))
        io = snap.get("diskIo")
        if disk and io and max(io) >= 100 * 1024:   # while the disk is busy the line shows its speed, otherwise the size
            sub.setStringValue_(T("ч {r} · з {w}", r=_rate(io[0]), w=_rate(io[1])))
        else:
            sub.setStringValue_("" if not disk else T("з {total:.0f} ГБ", total=disk["totalGb"]))
        low = bool(disk) and disk["freeGb"] < 10
        top.setTextColor_(AppKit.NSColor.systemRedColor() if low else AppKit.NSColor.labelColor())
        top, sub = self.infos["net"]
        top.setStringValue_("—" if not net else "↓ " + _rate(net[0]))
        sub.setStringValue_("" if not net else "↑ " + _rate(net[1]))
        tip = _net_tip(snap.get("netinfo"))
        top.setToolTip_(tip)
        sub.setToolTip_(tip)
        top, sub = self.infos["battery"]
        top.setStringValue_("—" if not bat else f"{bat['percent']:.0f}%")
        sub.setStringValue_(T("немає батареї") if not bat else (T("від мережі") if bat["plugged"] else T("від батареї")))
        flat = bool(bat) and not bat["plugged"] and bat["percent"] <= 15
        top.setTextColor_(AppKit.NSColor.systemRedColor() if flat else AppKit.NSColor.labelColor())

    def _draw_values(self):
        """The value and the graph change at once, together. No intermediate digits, the number does not “roll” to the final one."""
        for key, (label, _, graph) in self.meters.items():
            value = self.ends[key]
            graph.value = None if value is None else max(0, min(100, value))
            label.setStringValue_(_percent_text(value))
            graph.setNeedsDisplay_(True)


def _font(size=12.0):
    return AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0)


BAR_FONT = 10.5   # text font in the menu bar: smaller, so the icon is narrower


def _attr(text, size=12.0):
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSFontAttributeName: _font(size)})


def _stable_names(prev, top, n):
    """Names by rows. A group already in the list and still in the top stays in its place; new ones take only free slots.

    This way rows do not reorder every second: a click lands where the user was looking. Empty slots — None.
    """
    top_names = [g["name"] for g in top[:n]]
    kept = [name if name in top_names else None for name in list(prev[:n]) + [None] * (n - len(prev[:n]))]
    free = [name for name in top_names if name not in kept]
    return [name if name is not None else (free.pop(0) if free else None) for name in kept]


def _pinned_groups(pinned, live):
    """Pinned rows: fresh data if the process still exists; otherwise the last known, marked stale."""
    out = []
    for g in pinned:
        if g is None:
            out.append((None, False))
            continue
        fresh = live.get(g["name"])
        out.append((fresh, False) if fresh is not None else (g, True))
    return out


_ICONS = {}


def _app_icon(path):
    """The system app icon from the macOS cache: we do not draw it ourselves. None if there is no path."""
    if not path:
        return None
    if path not in _ICONS:
        _ICONS[path] = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(path)
    return _ICONS[path]


def _generic_icon():
    """The default system executable icon for processes without an app (from the macOS cache)."""
    if "generic" not in _ICONS:
        _ICONS["generic"] = AppKit.NSWorkspace.sharedWorkspace().iconForFileType_("public.unix-executable")
    return _ICONS["generic"]


def _highlight_color():
    """Background of the selected row: the system accent colour, semi-transparent."""
    return AppKit.NSColor.controlAccentColor().colorWithAlphaComponent_(0.18).CGColor()


def _mem(rss):
    if rss >= GB:
        return T("{v:.1f} ГБ", v=rss / GB)
    return T("{v:.0f} МБ", v=rss / MB)


BAR_MAX_PX = 14   # the maximum column height of the icon, in pixels


def _bar_px(value):
    """Column height in whole pixels; None — no data (grey minimum). A change smaller than a pixel does not touch the icon."""
    if value is None:
        return None
    return max(2, round(BAR_MAX_PX * min(value, 100) / 100.0))


def _bars_icon(levels):
    """Columns of the selected metrics (heights from _bar_px). Template image: macOS tints it for the light and dark menu bar itself."""
    def draw(_rect):
        for i, px in enumerate(levels):
            x = 1.5 + i * 5.0
            color = AppKit.NSColor.blackColor() if px is not None else AppKit.NSColor.blackColor().colorWithAlphaComponent_(0.35)
            color.setFill()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, 1.0, 3.2, 2 if px is None else px), 1.2, 1.2).fill()
        return True

    img = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(16, 16), False, draw)
    img.setTemplate_(True)
    return img


def _state(on):
    return AppKit.NSControlStateValueOn if on else AppKit.NSControlStateValueOff


class _Targets(NSObject):
    """Action targets of the menu and the timer (AppKit selectors). The logic is in StatusBar, through the owner field."""

    def tick_(self, _timer):
        self.owner.on_tick()

    def togglePanel_(self, _sender):
        self.owner.toggle_panel()

    def killRow_(self, sender):
        self.owner.kill_row(sender.tag())

    def selectRow_(self, sender):
        self.owner.select_row(sender.tag())

    def cleanup_(self, _sender):
        self.owner.actions["cleanup"]()

    def styleSegment_(self, sender):
        self.owner.set_style(STYLE_BY_INDEX[sender.selectedSegment()])

    def intervalSegment_(self, sender):
        self.owner.set_interval(INTERVALS[sender.selectedSegment()])

    def popoverDidClose_(self, _notification):
        self.owner._monitor.set_detail(False)
        self.owner.close_side()
        if self.owner._panel is not None:
            self.owner._panel.clear_selection()

    def menuWillOpen_(self, menu):
        self.owner.on_menu_open(menu)

    def menuDidClose_(self, menu):
        self.owner.on_menu_close(menu)

    def openWindow_(self, _sender):
        self.owner.actions["show"]()

    def openReport_(self, _sender):
        self.owner.actions["reports"]()

    def quickClean_(self, _sender):
        self.owner.quick_clean()

    def sideReport_(self, _sender):
        self.owner.toggle_side()

    def runCheck_(self, _sender):
        self.owner._work(self.owner.actions["check"])

    def purge_(self, _sender):
        self.owner.actions["purge"]()

    def autostart_(self, sender):
        self.owner.toggle_autostart()

    def toggleCpu_(self, _sender):
        self.owner.toggle_metric("cpu")

    def toggleRam_(self, _sender):
        self.owner.toggle_metric("ram")

    def toggleGpu_(self, _sender):
        self.owner.toggle_metric("gpu")

    def toggleTemp_(self, _sender):
        self.owner.toggle_metric("temp")

    def styleBoth_(self, _sender):
        self.owner.set_style("both")

    def styleIcon_(self, _sender):
        self.owner.set_style("icon")

    def styleText_(self, _sender):
        self.owner.set_style("text")

    def quit_(self, _sender):
        self.owner.actions["quit"]()


class StatusBar:
    """The icon in the menu bar and menu. Keep a reference to the instance, otherwise the icon disappears."""

    _quick = None   # quick clean: (cache ids, time of the first click)
    _side = None    # the second “Summary” menu, created on first opening

    def __init__(self, monitor, actions, autostart_on, prefs):
        self._monitor = monitor
        self.actions = actions
        self._prefs = prefs
        self._settings = {key: prefs.get(key) for key in ("cpu", "ram", "gpu", "temp", "style", "interval", "alerts", "dailyCheck", "graphTemp",
                                                                  "alertMemoryDisk", "alertBattery", "alertLoad")}
        self._monitor.set_interval(self._settings["interval"])
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit-settings")
        self._closed = False
        self._auto_busy = False
        self._autostart_on = bool(autostart_on)
        self._history = deque(maxlen=61)
        self._panel = None
        self._note = ""
        self._menu_open = False    # the main menu is open: then its items update every second
        self._bar_key = None       # the last menu bar state: we update only when it changes
        self._icon_key = None      # the last column heights of the icon (pixels)
        self._bar_text = None      # the last text and security level in the menu bar
        self._alert_log = deque(maxlen=5)   # (time, title, text) of the notifications shown, newest first
        self._security = None      # security summary from the latest report, updated once per SECURITY_EVERY
        self._security_at = 0.0
        self._last = None
        self._targets = _Targets.alloc().init()
        self._targets.owner = self

        self._status = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        self._button = self._status.button()
        self._button.setImagePosition_(AppKit.NSImageLeading)
        self._button.setToolTip_(T("MacSafe: CPU · RAM · GPU, відсотки зайнятості"))

        menu = AppKit.NSMenu.alloc().initWithTitle_("MacSafe")
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._targets)
        self._menu = menu

        self._cpu_item = self._info(menu)
        self._ram_item = self._info(menu)
        self._gpu_item = self._info(menu)
        self._note_item = self._info(menu)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        procs = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(T("Процеси"), None, "")
        sub = AppKit.NSMenu.alloc().initWithTitle_(T("Процеси"))
        sub.setAutoenablesItems_(False)
        sub.setDelegate_(self._targets)
        self._info(sub, T("За CPU (% одного ядра)"))
        self._cpu_rows = [self._info(sub) for _ in range(PROC_SLOTS)]
        sub.addItem_(AppKit.NSMenuItem.separatorItem())
        self._info(sub, T("За памʼяттю (RSS)"))
        self._ram_rows = [self._info(sub) for _ in range(RAM_SLOTS)]
        procs.setSubmenu_(sub)
        menu.addItem_(procs)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Appearance: metric switches and the menu bar style
        view = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(T("Вигляд"), None, "")
        vsub = AppKit.NSMenu.alloc().initWithTitle_(T("Вигляд"))
        vsub.setAutoenablesItems_(False)
        self._metric_items = {}
        for key, action in (("cpu", "toggleCpu:"), ("ram", "toggleRam:"), ("gpu", "toggleGpu:"), ("temp", "toggleTemp:")):
            item = self._action(metric_titles()[key], action)
            self._metric_items[key] = item
            vsub.addItem_(item)
        vsub.addItem_(AppKit.NSMenuItem.separatorItem())
        self._style_items = {}
        for style, action in (("both", "styleBoth:"), ("icon", "styleIcon:"), ("text", "styleText:")):
            item = self._action(style_titles()[style], action)
            self._style_items[style] = item
            vsub.addItem_(item)
        view.setSubmenu_(vsub)
        menu.addItem_(view)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._alerts_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(T("Тривоги"), None, "")
        self._alerts_menu = AppKit.NSMenu.alloc().initWithTitle_(T("Тривоги"))
        self._alerts_menu.setAutoenablesItems_(False)
        self._alerts_item.setSubmenu_(self._alerts_menu)
        menu.addItem_(self._alerts_item)
        self._fill_alerts()
        menu.addItem_(self._action(T("Звіти"), "openReport:"))
        menu.addItem_(self._action(T("Перевірити зараз"), "runCheck:"))
        menu.addItem_(self._action(T("Звільнити памʼять…"), "purge:"))
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        auto = self._action(T("Запускати при вході"), "autostart:")
        self._auto_item = auto
        auto.setState_(_state(autostart_on))
        menu.addItem_(auto)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = self._action(T("Вийти з MacSafe"), "quit:")
        quit_item.setKeyEquivalent_("q")
        menu.addItem_(quit_item)

        self._status.setMenu_(menu)
        # The menu is already ready if any step of creating the popover failed.
        try:
            self._panel = _PopoverPanel(self)
            self._status.setMenu_(None)
            self._button.setTarget_(self._targets)
            self._button.setAction_("togglePanel:")
        except Exception as exc:
            self._panel = None
            self._status.setMenu_(menu)
            self.set_note(T("Панель недоступна, використовуйте меню: {exc}", exc=exc))

        # The timer in common modes: fires also while the menu is open (NSEventTrackingRunLoopMode)
        self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self._targets, "tick:", None, True)
        NSRunLoop.mainRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)
        self._sync_view_items()
        self._update_bar(None, None, None)
        self.refresh()

    def close(self):
        """Removes the icon and the timer. Call from the main thread on exit."""
        if self._closed:
            return
        self._closed = True
        self._timer.invalidate()
        if self._panel is not None:
            self._panel.popover.setDelegate_(None)
            self._panel.popover.close()
        self._monitor.set_detail(False)
        self._menu.setDelegate_(None)
        self._worker.shutdown(wait=False, cancel_futures=True)
        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self._status)

    def _work(self, action, completion=None):
        """File operations run in sequence; AppKit only in callAfter."""
        if self._closed:
            return
        def finished(future):
            def deliver():
                if self._closed:
                    return
                try:
                    result = future.result()
                except Exception as exc:
                    self.set_note(T("Не вдалося виконати дію: {exc}", exc=exc))
                    if completion is not None:
                        completion(None)
                else:
                    if completion is not None:
                        completion(result)
            AppHelper.callAfter(deliver)
        self._worker.submit(action).add_done_callback(finished)

    def toggle_side(self):
        """A second menu to the left of the panel. Shows the current security summary from the report files."""
        if self._panel is None:
            return
        if self._side is None:
            self._side = _SidePanel(self)
        if self._side.popover.isShown():
            self._side.popover.performClose_(None)
            return
        self._side.update(self.actions["security"]())
        app = AppKit.NSApplication.sharedApplication()
        if hasattr(app, "activate"):
            app.activate()
        else:
            app.activateIgnoringOtherApps_(True)
        self._side.popover.showRelativeToRect_ofView_preferredEdge_(
            self._panel.footer_side.bounds(), self._panel.footer_side, AppKit.NSMinXEdge)

    def close_side(self):
        if self._side is not None and self._side.popover.isShown():
            self._side.popover.performClose_(None)

    def quick_clean(self):
        """Quick clean of safe caches: the first click counts the size, the second cleans (like “×” in processes)."""
        if self._quick is not None and time.monotonic() - self._quick[1] <= 10:
            ids = self._quick[0]
            self._quick = None
            self.set_note(T("Очищую: npm, pip, Homebrew, Xcode…"))
            self._work(lambda: self.actions["cache_clean"](ids), self._quick_done)
            return
        self._quick = None
        self.set_note(T("Рахую розмір безпечних кешів…"))   # the reaction is visible at once, the counting runs in the background
        self._work(self.actions["cache_scan"], self._quick_ready)

    def _quick_ready(self, items):
        candidates = [i for i in (items or []) if i.get("sizeMb", 0) > 0]
        if not candidates:
            self.set_note(T("Безпечних кешів для очищення немає"))
            return
        total = sum(i["sizeMb"] for i in candidates)
        self._quick = ([i["id"] for i in candidates], time.monotonic())
        size = T("{v:.1f} ГБ", v=total / 1024) if total >= 1024 else T("{v:.0f} МБ", v=total)
        names = ", ".join(i["title"] for i in candidates)
        self.set_note(T("Ще раз: очистити {size} старше {days} днів: {names}", size=size, days=cleanup.OLD_DAYS, names=names))

    def _quick_done(self, result):
        if not result or not result.get("ok"):
            self.set_note((result or {}).get("error") or T("Не вдалося очистити кеші"))
            return
        delta = (result.get("memory") or {}).get("delta")
        mem = T(". RAM: доступно {avail:+d} МБ, swap {swap:+d} МБ", avail=delta["availableMb"], swap=delta["swapMb"]) if delta else ""
        self.set_note(T("Звільнено на диску: {freed:.0f} МБ{mem}", freed=result.get("freedMb", 0), mem=mem))

    def toggle_autostart(self):
        if self._auto_busy:
            return
        self._auto_busy = True
        self._sync_view_items()
        def finished(on):
            self._auto_busy = False
            if on is not None:
                self._autostart_on = bool(on)
            self._sync_view_items()
        self._work(self.actions["autostart"], finished)

    def toggle_panel(self):
        if self._panel.popover.isShown():
            self._panel.popover.performClose_(None)
            return
        try:
            self._panel.popover.setAnimates_(not _reduce_motion())
            self._sync_view_items()
            self._monitor.set_detail(True)
            self.refresh()
            # The app must become active before showing, otherwise the first click inside the panel
            # activates it, and NSPopover treats this as a click outside the panel and closes it
            app = AppKit.NSApplication.sharedApplication()
            if hasattr(app, "activate"):
                app.activate()
            else:
                app.activateIgnoringOtherApps_(True)
            self._panel.popover.showRelativeToRect_ofView_preferredEdge_(
                self._button.bounds(), self._button, AppKit.NSMinYEdge)
            if self._last is not None:
                self._panel.update(self._last)
            window = self._panel.popover.contentViewController().view().window()
            if window is not None:   # there is no window if the icon is hidden by the system: then the menu is shown
                window.makeKeyWindow()
        except Exception as exc:
            self._panel.popover.close()
            self._panel = None
            self._monitor.set_detail(False)
            self._status.setMenu_(self._menu)
            self._button.setAction_(None)
            self.set_note(T("Панель недоступна, використовуйте меню: {exc}", exc=exc))

    # ---------- events ----------
    def on_tick(self):
        if self._closed:
            return
        self.actions["tick"]()   # check for a request to show the window from a second launch
        self.refresh()

    def on_menu_open(self, menu):
        if menu is self._menu:
            self._menu_open = True
            self._monitor.set_detail(True)   # the process list every second while the menu is open
        self._sync_view_items()
        self.refresh()

    def on_menu_close(self, menu):
        if menu is self._menu:
            self._menu_open = False
            self._monitor.set_detail(False)

    def toggle_metric(self, key):
        # The three cannot all be turned off together with visible text: the menu bar would be empty then
        now = self._settings[key]
        if now and sum(1 for k in ("cpu", "ram", "gpu", "temp") if self._settings[k]) == 1:
            self.set_note(T("Має лишитися увімкненим хоча б один показник"))
            self._sync_view_items()
            return
        self._save_setting(key, not now)
        self._last = None
        self._sync_view_items()
        self.refresh()

    def toggle_graph(self, key):
        """The temperature and power graph of the panel: on or off, the panel closes the gap at once."""
        self._save_setting(key, not self._settings[key])
        self._sync_view_items()
        if self._panel is not None:
            self._panel.apply_extra()

    def toggle_flag(self, key):
        """The “Notifications” and “Daily check” switches."""
        self._save_setting(key, not self._settings[key])
        self._sync_view_items()

    def set_language(self, code):
        # The language applies at launch: the agent restarts itself (without a window)
        if code in LANGUAGE_CODES:
            self.actions["set_language"](code)

    def set_interval(self, seconds):
        if seconds not in INTERVALS:
            return
        self._monitor.set_interval(seconds)
        self._save_setting("interval", seconds)

    def set_style(self, style):
        if style not in STYLES:
            return
        self._save_setting("style", style)
        self._last = None
        self._sync_view_items()
        self.refresh()

    def _sync_view_items(self):
        for key, item in self._metric_items.items():
            item.setState_(_state(self._settings[key]))
        current = self._settings["style"]
        for style, item in self._style_items.items():
            item.setState_(_state(style == current))
        self._auto_item.setState_(_state(self._autostart_on))
        self._auto_item.setEnabled_(not self._auto_busy)
        if self._panel is not None:
            self._panel.sync()

    def _save_setting(self, key, value):
        self._settings[key] = value
        def save():
            self._prefs.set(key, value)
            return True
        def finished(ok):
            if ok is None and self._settings[key] == value:
                self._settings[key] = self._prefs.get(key)
                self._last = None
                self._sync_view_items()
                self.refresh()
        self._work(save, finished)

    # ---------- construction ----------
    def _info(self, menu, title=""):
        """An item without an action (text only). Returns an NSMenuItem whose title is changed later."""
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setEnabled_(False)
        menu.addItem_(item)
        return item

    def _action(self, title, selector):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
        item.setTarget_(self._targets)
        return item

    def _mark_kill(self, index):
        """“Sure?” on row index, on the others “×” at once, not on the next panel update. None — reset all."""
        flat = 0   # running index, as in update: CPU rows first, then RAM
        for rows in self._panel.process_rows:
            for _, _, _, _, kill, _, _ in rows:
                kill.setTitle_(T("Так?") if index is not None and flat == index else "×")
                flat += 1

    def select_row(self, index):
        """A click on a process name: the row is selected and pinned, positions do not change. A second click clears the selection."""
        panel = self._panel
        group = panel.kill_targets.get(index) if panel is not None else None
        if group is None:
            return
        panel.toggle_select(index)
        self.set_note(T("Зафіксовано: {name}. Ще раз по назві — зняти вибір", name=group["name"]) if panel.selected == index
                      else T("Вибір знято"))
        snap = self._monitor.latest()
        if snap is not None:
            panel.update(snap)

    def kill_row(self, index):
        """The cross in a process row: the first click asks for confirmation, the second quits the process (SIGTERM).

        Processes of the group that belong to the current user are quit. MacSafe does not quit itself.
        """
        panel = self._panel
        group = panel.kill_targets.get(index) if panel is not None else None
        if not group or not group.get("killable"):
            return
        now = time.monotonic()
        armed = panel.armed
        if armed is None or armed[0] != index or now - armed[1] > 3:
            # Rows are pinned already on the first click: “Sure?” will quit exactly the group that was clicked
            panel.freeze()
            panel.armed = (index, now, group)
            self._mark_kill(index)
            self.set_note(T("Натисніть «Так?», щоб завершити {name}", name=group["name"]))
            return
        group = armed[2]
        panel.clear_selection()   # after the attempt, the selection and pin are released
        self._mark_kill(None)
        sent, denied = 0, 0
        for pid in group.get("pids", []):
            try:
                os.kill(pid, signal.SIGTERM)
                sent += 1
            except ProcessLookupError:
                pass
            except PermissionError:
                denied += 1
        if denied:
            self.set_note(T("Немає доступу, щоб завершити {name}", name=group["name"]))
        elif sent:
            self.set_note(T("Завершено: {name}", name=group["name"]))
        else:
            self.set_note(T("{name} вже не працює", name=group["name"]))

    def set_note(self, text):
        """A short line about the last action (for example, the cleaning result). Call from the main thread."""
        if not NSThread.isMainThread():
            AppHelper.callAfter(self.set_note, text)
            return
        if self._closed:
            return
        self._note = str(text)
        self._note_item.setAttributedTitle_(_attr(self._note))
        if self._panel is not None:
            self._panel.note.setStringValue_(self._note)
            self._panel.note.setToolTip_(self._note)

    # ---------- updates ----------
    def refresh(self):
        if self._closed:
            return
        snap = self._monitor.latest()
        if snap is None or snap is self._last:
            return
        self._last = snap
        cpu, gpu, ram = snap["cpu"], snap["gpu"], snap["ram"]
        ram_pct = ram["percent"]
        stamp = float(snap["time"])
        sample = {"cpu": cpu, "ram": ram_pct, "gpu": gpu,
                  "temp": _scale_temp(snap.get("temp")), "power": _scale_power((snap.get("power") or {}).get("sysW"))}
        if not self._history or stamp > self._history[-1][0]:
            # A gap in the line only when samples are skipped, not at the normal interval with timer jitter
            gap = max(2.0, 2.5 * self._monitor.interval)
            if self._history and stamp - self._history[-1][0] > gap:
                self._history.append((stamp - 0.001, {"cpu": None, "ram": None, "gpu": None, "temp": None, "power": None}))
            self._history.append((stamp, sample))
        elif stamp < self._history[-1][0]:
            self._history.clear()
            self._history.append((stamp, sample))
        while self._history and self._history[0][0] < stamp - 60:
            self._history.popleft()
        self._update_security()
        if self._settings["alerts"]:
            self.actions["alerts"](snap, self._security)
        # We redraw the menu bar only when the rounded value changed: the background needs almost no AppKit work
        temp = snap.get("temp")
        key = (round(cpu), round(ram_pct), None if gpu is None else round(gpu), None if temp is None else round(temp), self._settings["style"],
               self._settings["cpu"], self._settings["ram"], self._settings["gpu"], self._settings["temp"],
               (self._security or {}).get("level"))
        if key != self._bar_key:
            self._bar_key = key
            self._update_bar(cpu, ram_pct, gpu, temp)

        # We update the menu items and the panel only when they are open: closed ones do not spend AppKit time
        if self._menu_open:
            self._cpu_item.setAttributedTitle_(_attr(f"CPU   {cpu:5.1f}%   " + T("{cores} ядер", cores=snap["cores"])))
            self._ram_item.setAttributedTitle_(_attr(
                f"RAM   {ram_pct:5.1f}%   " + T("{used:.1f} з {total:.1f} ГБ, swap {swap:.1f} ГБ", used=ram["usedGb"], total=ram["totalGb"], swap=ram["swapGb"])))
            self._gpu_item.setAttributedTitle_(_attr(f"GPU   {T('н/д') if gpu is None else f'{gpu:.0f}%'}"))
            self._fill(self._cpu_rows, snap["topCpu"], lambda g: f"{g['cpu']:6.1f}%  {_mem(g['rss'])}")
            self._fill(self._ram_rows, snap["topRam"], lambda g: _mem(g["rss"]))
        if self._panel is not None and self._panel.popover.isShown():
            self._panel.update(snap)
            if not self._note:
                self._panel.note.setStringValue_("")

    def _update_bar(self, cpu, ram_pct, gpu, temp=None):
        """Menu bar: only the selected metrics, in the selected style. Fixed digit width (monospaced)."""
        shown = []
        if self._settings["cpu"]:
            shown.append(("cpu", cpu))
        if self._settings["ram"]:
            shown.append(("ram", ram_pct))
        if self._settings["gpu"]:
            shown.append(("gpu", gpu))
        if self._settings["temp"]:
            shown.append(("temp", temp))   # °C, 0–99 fits the two positions
        style = self._settings["style"]

        if style in ("both", "icon") or not shown:
            levels = tuple(_bar_px(v) for _, v in shown) or (_bar_px(cpu),)
            if levels != self._icon_key:
                self._icon_key = levels
                self._button.setImage_(_bars_icon(list(levels)))
        elif self._icon_key is not None:
            self._icon_key = None
            self._button.setImage_(None)

        if style == "icon" or not shown:
            text = ""
        else:
            # Each value is exactly two positions (0–99), equal spaces on both sides of the dot: distances are equal,
            # the width does not change. A figure space (U+2007) on the left for a single digit.
            parts = []
            for key, value in shown:
                text = "–" if value is None else str(min(99, int(round(value))))
                parts.append(text.rjust(2, "\u2007"))
            text = " " + " · ".join(parts)
        title_key = (text, (self._security or {}).get("level"))   # the security level changes the dot colour
        if title_key != self._bar_text:
            self._bar_text = title_key
            self._button.setAttributedTitle_(self._bar_title(text))

    def add_alert(self, title, text):
        """Remember a notification that was shown: the “Alerts” menu keeps the last five, the newest first."""
        self._alert_log.appendleft((time.strftime("%H:%M"), title, text))
        self._fill_alerts()

    def _fill_alerts(self):
        self._alerts_menu.removeAllItems()
        if not self._alert_log:
            empty = self._action(T("Немає тривог"), None)
            empty.setEnabled_(False)
            self._alerts_menu.addItem_(empty)
            return
        for stamp, title, text in self._alert_log:
            item = self._action(f"{stamp}  {title}", "openReport:")   # a click opens the report window
            item.setToolTip_(text)
            self._alerts_menu.addItem_(item)

    def refresh_security(self):
        """The next update of the security indicator reads the report at once (after a check)."""
        self._security_at = 0.0

    def _update_security(self):
        """Reads the security summary no more often than once every SECURITY_EVERY seconds and updates the panel."""
        now = time.monotonic()
        if self._security is None or now - self._security_at >= SECURITY_EVERY:
            self._security_at = now
            try:
                self._security = self.actions["security"]()
            except Exception:
                self._security = {"level": "none"}
            if self._panel is not None:
                text, color = _security_line(self._security)
                self._panel.security.setStringValue_(text)
                self._panel.security.setTextColor_(color or AppKit.NSColor.secondaryLabelColor())
                sec = self._security or {}
                checked = sec.get("checkedAt") or T("звіту ще немає")
                changed = int(sec.get("changeCount", 0))
                tip = T("Останній звіт: {checked}. Змін з попереднього звіту: {changed}. Деталі: «Звіти»", checked=checked, changed=changed)
                self._panel.security.setToolTip_(tip)

    def _bar_title(self, text):
        """Menu bar text with a dot in front of it if there are security issues (orange — warning, red — critical)."""
        title = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(_attr(text, BAR_FONT))
        _, color = _security_line(self._security)
        if color is not None:
            dot = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    "●", {AppKit.NSFontAttributeName: _font(BAR_FONT), AppKit.NSForegroundColorAttributeName: color}))
            dot.appendAttributedString_(title)
            return dot
        return title

    @staticmethod
    def _fill(rows, groups, value):
        for i, row in enumerate(rows):
            if i < len(groups):
                g = groups[i]
                name = g["name"][:NAME_WIDTH].ljust(NAME_WIDTH)
                row.setAttributedTitle_(_attr(f"{name}  {value(g)}"))
            else:
                row.setAttributedTitle_(_attr(""))


def install(monitor, actions, autostart_on, prefs):
    """Creates the icon in the menu bar. Call from the main thread. Returns StatusBar (keep a reference)."""
    return StatusBar(monitor, actions, autostart_on, prefs)
