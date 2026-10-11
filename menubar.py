"""Статус-бар і NSPopover: CPU, RAM, GPU, історія, процеси, вигляд і дії.

Усі виклики AppKit відбуваються на головному потоці (створення через AppHelper.callAfter).
Вибірку робить metrics.Monitor у фоні; тут лише читається остання вибірка раз на секунду.
Таймер додано в NSRunLoopCommonModes, тому цифри оновлюються і поки меню відкрите.
Поки панель або резервне меню відкрите, Monitor збирає процеси щосекунди.
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

# CGColor з AppKit повертає сирий вказівник; PyObjC попереджає про це при кожному виклику, результат коректний
warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)

PROC_SLOTS = 10      # скільки рядків за CPU у підменю
PROC_CPU_ROWS = 6     # рядків за CPU у зоні «Процеси»
PROC_RAM_ROWS = 3     # рядків за RAM у зоні «Процеси»
PROC_GPU_ROWS = 3     # рядків за GPU у зоні «Процеси»
RAM_SLOTS = 5        # скільки рядків за памʼяттю
NAME_WIDTH = 22      # ширина назви в підменю (моноширинний шрифт, вирівнювання стовпчиків)
STYLE_TITLES = {"both": "Іконка і цифри", "icon": "Лише іконка", "text": "Лише цифри"}
METRIC_TITLES = {"cpu": "Показувати CPU", "ram": "Показувати RAM", "gpu": "Показувати GPU"}
PANEL_W = 340         # вузька панель
PANEL_PAD = 16        # відступ по краях
INNER_W = PANEL_W - 2 * PANEL_PAD
ZONE_H = 30           # висота рядка зони (Процеси / Вигляд / Дії)
STYLE_BY_INDEX = ("both", "icon", "text")
ZONE_SYMBOLS = {"ЦП": "cpu", "ГПУ": "display", "ОЗП": "memorychip",
                "Вигляд": "paintpalette", "Дії": "bolt"}   # іконки заголовків зон
BLOCK_ORDER = ("cpu", "ram", "gpu")   # порядок списків процесів: як у snapshot (topCpu, topRam, topGpu)
BLOCK_COUNT = {"cpu": PROC_CPU_ROWS, "ram": PROC_RAM_ROWS, "gpu": PROC_GPU_ROWS}
BLOCK_BASE = {"cpu": 0, "ram": PROC_CPU_ROWS, "gpu": PROC_CPU_ROWS + PROC_RAM_ROWS}   # перший індекс рядка
DETAIL_ROW = 16      # висота рядка деталей (назва і значення)
BAR_H = 8            # висота складеної смуги деталей
STYLE_INDEX = {style: i for i, style in enumerate(STYLE_BY_INDEX)}
INTERVALS = (1, 2, 5)   # секунд між вибірками; сегмент у зоні «Вигляд»
INTERVAL_INDEX = {seconds: i for i, seconds in enumerate(INTERVALS)}
SECURITY_EVERY = 30.0  # секунд між читаннями підсумку безпеки з останнього звіту (файл, не щосекунди)


def _plural_problems(n):
    """Українська множина: 1 проблема, 2 проблеми, 5 проблем."""
    if n % 10 == 1 and n % 100 != 11:
        return "проблема"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "проблеми"
    return "проблем"


def _security_line(sec):
    """Текст і колір стану безпеки для панелі й статус-бару."""
    level = (sec or {}).get("level", "none")
    if level == "none":
        return "Безпека: звіту ще немає", None
    if level == "ok":
        return "Безпека: усе гаразд", None
    n = int(sec.get("issues", 0))
    color = AppKit.NSColor.systemRedColor() if level == "critical" else AppKit.NSColor.systemOrangeColor()
    return f"Безпека: {n} {_plural_problems(n)}", color


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
        # Клік по контролу відразу спрацьовує, навіть коли застосунок ще не активний
        return True


class _RootView(_PanelView):
    """Корінь панелі з суцільним фоном: прозорий матеріал підмішував кольори шпалер, і підписи гублялись."""

    def drawRect_(self, rect):
        AppKit.NSColor.windowBackgroundColor().setFill()
        AppKit.NSRectFill(rect)


class _Switch(_PanelView):
    """Перемикач у стилі macOS: доріжка з кружечком. Клік змінює стан і викликає on_change."""
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
    """Сегментований вибір у стилі macOS: пігулка з підсвіченим варіантом. Клік вибирає і викликає on_select(i)."""
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
    """Заокруглена підкладка під показники (диск, мережа, батарея)."""

    def drawRect_(self, _rect):
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSInsetRect(self.bounds(), 0.5, 0.5), 10, 10)
        AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.055).setFill()
        path.fill()


class _Tile(_PanelView):
    """Плитка дії: заокруглений фон, іконка і підпис. Малюється сама, тож коректна в обох темах."""
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
    """Заголовок зони з шевроном: дивиться вниз, коли зона відкрита. Клік розгортає або згортає зону."""
    title = ""
    opened = False
    on_click = None
    textInset = 12

    def drawRect_(self, _rect):
        b = self.bounds()
        # Заокруглена плашка: відкрита зона підсвічена акцентом, закриті ледь помітні
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
        # Відступ як у NSTextField: заголовок зони вирівнюється з текстом показників і підписів
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
    """Відсоток фіксованої ширини: цифрові пробіли (U+2007) вирівнюють число, тож зміна цифр не зсуває картку."""
    text = "—" if value is None else f"{value:.0f}%"
    return text.rjust(4, "\u2007")


def _legend_row(view, y, color, title):
    """Рядок деталей: кольорова крапка і назва зліва, значення справа. Повертає (назва, значення)."""
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
    """Швидкість для підпису: КБ/с або МБ/с."""
    kb = bytes_per_s / 1024
    return f"{kb / 1024:.1f} МБ/с" if kb >= 1024 else f"{kb:.0f} КБ/с"


def _pct(value):
    return "—" if value is None else f"{value:.0f}%"


def _uptime_text(seconds):
    if seconds is None:
        return "—"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} дн. {hours} год"
    return f"{hours} год {rest // 60} хв"


def _caption(parent, text, y):
    """Підпис секції: малі великі літери, другорядний колір."""
    field = _label(parent, text.upper(), 0, y, INNER_W, 10, True, height=14)
    field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(10, 0.4))
    return field


def _symbol(name, description):
    """Іконка SF Symbols для рядка дій. None, якщо символу немає в системі."""
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
    """Плоский бар і часова шкала 0–100%, з розривами для недоступних вибірок."""

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
        # Неперервні ділянки між розривами: кожна заповнюється під лінією і обводиться зверху
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


class _CoresView(_PanelView):
    """Навантаження кожного ядра: стовпчик на ядро на всю ширину, заповнений знизу відповідно до відсотка."""
    loads = ()
    kinds = ()   # «P» або «E» для кожного ядра

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
            h = max(1.5, b.size.height * value / 100)   # нульове ядро теж видно лінією внизу
            kind = self.kinds[i] if i < len(self.kinds) else "P"
            # Економні ядра бірюзові, продуктивні — за рівнем навантаження; високий рівень червоніє в обох
            color = _metric_color(value) if value > 80 or kind == "P" else AppKit.NSColor.systemTealColor()
            color.setFill()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, b.size.height - h, w, h), 2, 2).fill()


class _StackBar(_PanelView):
    """Складена смуга: сегменти (частка 0–1, колір) на сірому тлі. Вільна частка лишається сірою."""
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


SIDE_W = 240   # ширина другого меню «Коротко»


class _SidePanel:
    """Друге меню зліва від панелі: короткий підсумок безпеки і зміни від попереднього звіту. Тільки читання."""

    def __init__(self, owner):
        self.owner = owner
        self.root = _RootView.alloc().initWithFrame_(NSMakeRect(0, 0, SIDE_W, 120))
        inner = SIDE_W - 2 * PANEL_PAD
        self.title = _label(self.root, "Коротко", PANEL_PAD, 12, inner, 13, height=18)
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
        self.report.setStringValue_(f"Звіт від {checked}" if checked else "Звіту ще немає")
        count = int(summary.get("changeCount", 0))
        self.changes_head.setStringValue_(f"ЗМІНИ ВІД ПОПЕРЕДНЬОГО ЗВІТУ: {count}" if count else "ЗМІН НЕМАЄ")
        changes = summary.get("changes") or []
        y = 102
        for i, row in enumerate(self.change_rows):
            if i < len(changes):
                c = changes[i]
                if c.get("from") is None:
                    detail = f"з'явилась: {c.get('to')}"
                elif c.get("to") is None:
                    detail = "зникла"
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
    """Один екран: три показники зверху і три розгортні зони знизу (процеси, вигляд, дії).

    Відкрита лише одна зона, висота панелі підлаштовується під неї.
    """

    def __init__(self, owner):
        self.owner = owner
        self.root = _RootView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, 300))
        root = self.root
        title = _label(root, "MacSafe", PANEL_PAD, 12, 160, 15, height=20)
        title.setFont_(AppKit.NSFont.systemFontOfSize_weight_(15, 0.5))
        self.security = _label(root, "Безпека: звіту ще немає", PANEL_PAD, 14, INNER_W, 11, True, height=16)
        self.security.setAlignment_(AppKit.NSTextAlignmentRight)
        self.meters = {}
        y = 44
        for key in ("cpu", "ram", "gpu"):
            cap = _label(root, key.upper(), PANEL_PAD, y, 120, 11, height=16)
            cap.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11, 0.5))
            value = _label(root, "—", PANEL_W - PANEL_PAD - 110, y - 7, 110, size=20, digits=True, height=28)
            value.setAlignment_(AppKit.NSTextAlignmentRight)
            detail = _label(root, "Очікування даних", PANEL_PAD, y + 18, INNER_W, 11, True, height=16)
            graph = _MeterView.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, y + 34, INNER_W, 24))
            graph.value, graph.samples, graph.end = None, (), 0
            graph.setWantsLayer_(True)
            graph.fill = _BarFill.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 5))
            graph.fill.setWantsLayer_(True)
            graph.fill.setHidden_(True)          # смуга-заливка прибрана; лишилась лише лінія історії
            graph.addSubview_(graph.fill)
            graph.setAccessibilityLabel_(key.upper() + ": історія за 60 секунд")
            root.addSubview_(graph)
            self.meters[key] = (value, detail, graph)
            if key == "cpu":
                self.cores = _CoresView.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, y + 62, INNER_W, 42))
                self.cores.setKinds_(core_types())
                self.cores.setToolTip_("Ядра: синє — продуктивні (P), бірюзове — економні (E)")
                root.addSubview_(self.cores)
                y += 44
            y += 70
        # Рядок системних показників: диск, мережа, батарея
        self.infos = {}
        col = INNER_W / 3
        for i, (key, caption) in enumerate((("disk", "ДИСК"), ("net", "МЕРЕЖА"), ("battery", "БАТАРЕЯ"))):
            x0 = PANEL_PAD + i * col
            card = _Card.alloc().initWithFrame_(NSMakeRect(x0, y - 8, col - 6, 58))
            root.addSubview_(card)
            cap = _label(root, caption, x0 + 10, y - 3, col - 26, 10, True, height=14)
            cap.setFont_(AppKit.NSFont.systemFontOfSize_weight_(10, 0.5))
            top = _label(root, "—", x0 + 10, y + 10, col - 26, 13, digits=True, height=18)
            sub = _label(root, "", x0 + 10, y + 28, col - 26, 10, True, height=14)
            self.infos[key] = (top, sub)
        y += 66
        self.divider = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, y - 8, INNER_W, 1))
        self.divider.setBoxType_(AppKit.NSBoxSeparator)
        self.divider.setHidden_(True)   # картки й плашки зон відокремлюють блоки без ліній
        root.addSubview_(self.divider)
        self.zones_top = y - 2   # зони рахуються від кінця блоку метрик, а не від фіксованого 252
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
        self._block_zone = {"cpu": 0, "gpu": 1, "ram": 2}   # індекси зон ЦП, ГПУ, ОЗП у self.zones
        for title, fill in (("ЦП", self._fill_cpu), ("ГПУ", self._fill_gpu), ("ОЗП", self._fill_ram),
                            ("Вигляд", self._fill_appearance), ("Дії", self._fill_actions)):
            self._zone(title, fill)
        self.process_rows = [self._blocks[key]["rows"] for key in BLOCK_ORDER]
        self.footer_line = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, 0, INNER_W, 1))
        self.footer_line.setBoxType_(AppKit.NSBoxSeparator)
        root.addSubview_(self.footer_line)
        self.footer_open = self._footer_button("Звіти", "openWindow:", 110, AppKit.NSTextAlignmentLeft, "doc.text")
        self.footer_quit = self._footer_button("Вийти", "quit:", 80, AppKit.NSTextAlignmentLeft, "power")
        self.footer_side = self._footer_button("", "sideReport:", 22, AppKit.NSTextAlignmentLeft, "sidebar.left")
        self.footer_side.setToolTip_("Коротко: безпека і зміни від попереднього звіту")
        # Рядок статусу в один рядок: довгий текст обрізається, а не займає другий рядок внизу
        self.note = _label(root, "", PANEL_PAD, 0, INNER_W, 10, True, height=14)
        self.note.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        self.controller = AppKit.NSViewController.alloc().init()
        self.controller.setView_(root)
        self.popover = AppKit.NSPopover.alloc().init()
        self.popover.setContentViewController_(self.controller)
        self.popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        self.popover.setDelegate_(owner._targets)
        self.armed = None            # (індекс рядка, час) після першого кліку по «×»
        self.kill_targets = {}       # індекс рядка → група процесу з останнього знімка
        self.selected = None         # індекс рядка, вибраного кліком по назві (фіксує список)
        self.pinned = None           # групи по рядках на момент фіксації; None — список живий
        self.slot_names = [[], [], []]   # назви по рядках CPU, RAM і GPU: порядок стабільний між оновленнями
        self._lit = None             # рядок із підсвіткою: фон міняємо лише при зміні вибору
        self._icon_paths = {}        # який застосунок уже показано в іконці рядка: міняємо лише при зміні
        self._layout()
        self.sync()

    # ---------- зони ----------
    def _zone(self, title, fill):
        index = len(self.zones)
        head = _ZoneTitle.alloc().initWithFrame_(NSMakeRect(PANEL_PAD, 0, INNER_W, ZONE_H))
        head.title = title
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
                y += zone["height"] + 18
            else:
                zone["content"].setHidden_(True)
        # Нотатка над футером, а кнопки стоять у самого низу з однаковим відступом
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
        # Зміна розміру відкритого NSPopover зсуває вміст, тож викликаємо її лише коли висота справді змінилась
        if (PANEL_W, y) != getattr(self, "_size", None):
            self._size = (PANEL_W, y)
            # Щосекундні зміни висоти від списків процесів — без анімації, інакше панель ходить ліво-право
            self.popover.setAnimates_(animate and not _reduce_motion())
            self.popover.setContentSize_(NSMakeSize(PANEL_W, y))

    def freeze(self):
        """Фіксує рядки такими, якими їх видно зараз: позиції не міняються, поки немає вибору або «Так?»."""
        if self.pinned is None:
            total = sum(len(rows) for rows in self.process_rows)
            self.pinned = [self.kill_targets.get(i) for i in range(total)]

    def toggle_select(self, index):
        """Клік по назві: вибирає рядок, повторний клік по ньому знімає вибір."""
        self.freeze()
        self.selected = None if self.selected == index else index
        self._release()

    def clear_selection(self):
        self.selected = None
        self.armed = None
        self.pinned = None

    def _release(self):
        """Знімає фіксацію, коли немає ні вибору, ні відліку «Так?»."""
        if self.selected is None and (self.armed is None or time.monotonic() - self.armed[1] > 3):
            self.pinned = None

    def _fill_block(self, view, key, detail_rows):
        """Зона: деталі (складена смуга і рядки) зверху, нижче топ процесів цього типу."""
        y = 0
        bar = _StackBar.alloc().initWithFrame_(NSMakeRect(0, y, INNER_W, BAR_H))
        view.addSubview_(bar)
        y += BAR_H + 10
        legend = []
        for color, title in detail_rows:
            legend.append(_legend_row(view, y, color, title))
            y += DETAIL_ROW
        self.details[key] = {"bar": bar, "legend": legend, "h": y}
        caption = _label(view, "ТОП ПРОЦЕСІВ", 0, y + 6, 110, 10, True)
        heads = []
        for text, x, w in (("CPU", 116, 52), ("GPU", 168, 50), ("ПАМʼЯТЬ", 220, 62)):
            head = _label(view, text, x, y + 6, w, 9, True, height=14)
            head.setAlignment_(AppKit.NSTextAlignmentRight)
            heads.append(head)
        self.heads = getattr(self, "heads", {})
        self.heads[key] = heads
        rows = [self._proc_row(view, 0, BLOCK_BASE[key] + i) for i in range(BLOCK_COUNT[key])]
        self._blocks[key] = {"caption": caption, "rows": rows}
        # Спочатку список сховано: рядки з'являються з даними
        for item in [caption] + heads + [it for row in rows for it in row]:
            item.setHidden_(True)
        return y + 6 + 16 + BLOCK_COUNT[key] * 18 + 8

    def _fill_cpu(self, view):
        blue, red = AppKit.NSColor.systemBlueColor(), AppKit.NSColor.systemRedColor()
        return self._fill_block(view, "cpu", [
            (red, "Система"), (blue, "Користувач"), (AppKit.NSColor.tertiaryLabelColor(), "Вільно"),
            (AppKit.NSColor.systemTealColor(), "Енергоефективні ядра"), (blue, "Продуктивні ядра"),
            (None, "Час роботи"), (None, "Навантаження 1 · 5 · 15 хв"),
        ])

    def _fill_gpu(self, view):
        return self._fill_block(view, "gpu", [
            (None, "Модель"), (None, "Ядра"), (AppKit.NSColor.systemBlueColor(), "Використання"),
            (AppKit.NSColor.systemTealColor(), "Render"), (AppKit.NSColor.systemPurpleColor(), "Tiler"),
        ])

    def _fill_ram(self, view):
        return self._fill_block(view, "ram", [
            (None, "Використовується"), (AppKit.NSColor.systemBlueColor(), "Програми"),
            (AppKit.NSColor.systemOrangeColor(), "Постійна"), (AppKit.NSColor.systemRedColor(), "Стиснена"),
            (AppKit.NSColor.tertiaryLabelColor(), "Вільна"), (None, "Swap"),
        ])

    def _set_details(self, key, segments, values):
        """Смуга і значення деталей зони. Значення міняються разом, без проміжних цифр."""
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
            f"{total - free:.1f} ГБ з {total:.0f} ГБ", f"{app:.1f} ГБ", f"{wired:.1f} ГБ",
            f"{comp:.1f} ГБ", f"{free:.1f} ГБ", f"{mem.get('swapGb', 0.0):.1f} ГБ",
        ])

    def _set_icon(self, icon, idx, path):
        """Системна іконка застосунку в рядку. Картинку ставимо лише коли застосунок змінився."""
        key = path or ""   # без застосунку — типова іконка, ключ порожній
        if self._icon_paths.get(idx) != key:
            icon.setImage_(_app_icon(path) if path else _generic_icon())
            self._icon_paths[idx] = key
        icon.setHidden_(False)

    def _place_processes(self, entries, sizes, ready, armed):
        """Топ процесів цього типу під деталями кожної зони. Порожній список не займає місця.

        Повертає висоту кожної зони: деталі плюс список.
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
                    cpu_text, gpu_text = "…", "…"     # після праймінгу CPU і GPU ще не порахувані
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
                kill.setTitle_("Так?" if armed and armed[0] == idx else "×")
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
        name.setAction_("selectRow:")   # клік по назві вибирає і фіксує рядок
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
        _caption(view, "Показники в барі", y)
        y += 22
        for key, title in (("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU")):
            _label(view, title, 0, y, 200, 12, height=20)
            sw = _Switch.alloc().initWithFrame_(NSMakeRect(INNER_W - _Switch.WIDTH, y, _Switch.WIDTH, _Switch.HEIGHT))
            sw.on_change = lambda key=key: self.owner.toggle_metric(key)
            view.addSubview_(sw)
            self.switches[key] = sw
            y += 28
        y += 10
        _caption(view, "Автоматично", y)
        y += 22
        for key, title in (("alerts", "Сповіщення"), ("dailyCheck", "Щоденна перевірка")):
            _label(view, title, 0, y, 200, 12, height=20)
            sw = _Switch.alloc().initWithFrame_(NSMakeRect(INNER_W - _Switch.WIDTH, y, _Switch.WIDTH, _Switch.HEIGHT))
            sw.on_change = lambda key=key: self.owner.toggle_flag(key)
            view.addSubview_(sw)
            self.switches[key] = sw
            y += 28
        y += 10
        _caption(view, "Стиль в барі", y)
        y += 22
        self.style_seg = _Segments.alloc().initWithFrame_(NSMakeRect(0, y, INNER_W, 26))
        self.style_seg.setItems_(("Обидва", "Іконка", "Цифри"))
        self.style_seg.on_select = lambda i: self.owner.set_style(STYLE_BY_INDEX[i])
        view.addSubview_(self.style_seg)
        y += 26 + 14
        _caption(view, "Оновлення даних", y)
        y += 22
        self.interval_seg = _Segments.alloc().initWithFrame_(NSMakeRect(0, y, INNER_W, 26))
        self.interval_seg.setItems_([f"{seconds} с" for seconds in INTERVALS])
        self.interval_seg.on_select = lambda i: self.owner.set_interval(INTERVALS[i])
        view.addSubview_(self.interval_seg)
        return y + 26

    def _fill_actions(self, view):
        tile_w = (INNER_W - 8) / 2
        for n, (title, selector, symbol) in enumerate((("Швидке очищення", "quickClean:", "sparkles"),
                                                       ("Перевірка", "runCheck:", "checkmark.shield"),
                                                       ("Детальне очищення", "cleanup:", "trash"),
                                                       ("Звільнити памʼять", "purge:", "memorychip"))):
            view.addSubview_(self._tile(title, selector, symbol, (n % 2) * (tile_w + 8), (n // 2) * 42, tile_w))
        y = 92
        _label(view, "Запускати при вході", 0, y + 2, 240, 12, height=20)
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

    # ---------- стан і оновлення ----------
    def sync(self):
        for key, control in self.switches.items():
            control.setOn_(self.owner._settings[key])
        self.style_seg.setSelected_(STYLE_INDEX.get(self.owner._settings["style"], 0))
        self.interval_seg.setSelected_(INTERVAL_INDEX.get(self.owner._settings["interval"], 0))
        self.auto.setOn_(self.owner._autostart_on)
        self.auto.setActive_(not self.owner._auto_busy)

    def update(self, snap):
        self.ends = {"cpu": snap["cpu"], "ram": snap["ram"]["percent"], "gpu": snap["gpu"]}
        self.meters["cpu"][1].setStringValue_(f"{snap['cores']} ядер · загальне навантаження")
        ram = snap["ram"]
        self.meters["ram"][1].setStringValue_(
            f"{ram['usedGb']:.1f} з {ram['totalGb']:.1f} ГБ · swap {ram['swapGb']:.1f} ГБ")
        self.meters["gpu"][1].setStringValue_("Дані недоступні" if snap["gpu"] is None else "Навантаження графіки")
        self.cores.setLoads_(snap.get("perCore") or [])
        self._update_infos(snap)
        for key, (_, _, graph) in self.meters.items():
            graph.samples = [(s[0], s[1][key]) for s in self.owner._history]
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
                resize = resize or zone["open"]   # згорнута зона не змінює розмір панелі
        if resize:
            self._layout(animate=False)
        self._update_details(snap)
        self._draw_values()

    def _update_infos(self, snap):
        disk, net, bat = snap.get("disk"), snap.get("net"), snap.get("battery")
        top, sub = self.infos["disk"]
        top.setStringValue_("—" if not disk else f"{disk['freeGb']:.0f} ГБ")
        sub.setStringValue_("" if not disk else f"з {disk['totalGb']:.0f} ГБ")
        low = bool(disk) and disk["freeGb"] < 10
        top.setTextColor_(AppKit.NSColor.systemRedColor() if low else AppKit.NSColor.labelColor())
        top, sub = self.infos["net"]
        top.setStringValue_("—" if not net else "↓ " + _rate(net[0]))
        sub.setStringValue_("" if not net else "↑ " + _rate(net[1]))
        top, sub = self.infos["battery"]
        top.setStringValue_("—" if not bat else f"{bat['percent']:.0f}%")
        sub.setStringValue_("немає батареї" if not bat else ("від мережі" if bat["plugged"] else "від батареї"))
        flat = bool(bat) and not bat["plugged"] and bat["percent"] <= 15
        top.setTextColor_(AppKit.NSColor.systemRedColor() if flat else AppKit.NSColor.labelColor())

    def _draw_values(self):
        """Значення і графік міняються одразу, разом. Проміжних цифр немає, число не «перекочує» до кінцевого."""
        for key, (label, _, graph) in self.meters.items():
            value = self.ends[key]
            graph.value = None if value is None else max(0, min(100, value))
            label.setStringValue_(_percent_text(value))
            graph.setNeedsDisplay_(True)


def _font(size=12.0):
    return AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0)


BAR_FONT = 10.5   # шрифт тексту в статус-барі: менший, щоб значок був вужчим


def _attr(text, size=12.0):
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSFontAttributeName: _font(size)})


def _stable_names(prev, top, n):
    """Назви по рядках. Група, що вже в списку і досі в топі, лишається на своєму місці; нові займають лише вільні.

    Так рядки не переставляються щосекунди: клік потрапляє туди, куди користувач дивився. Порожні місця — None.
    """
    top_names = [g["name"] for g in top[:n]]
    kept = [name if name in top_names else None for name in list(prev[:n]) + [None] * (n - len(prev[:n]))]
    free = [name for name in top_names if name not in kept]
    return [name if name is not None else (free.pop(0) if free else None) for name in kept]


def _pinned_groups(pinned, live):
    """Зафіксовані рядки: свіжі дані, якщо процес іще є; інакше останні відомі, зі позначкою stale."""
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
    """Системна іконка застосунку з кешу macOS: не малюємо її самі. None, якщо шляху немає."""
    if not path:
        return None
    if path not in _ICONS:
        _ICONS[path] = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(path)
    return _ICONS[path]


def _generic_icon():
    """Типова системна іконка виконуваного файлу для процесів без застосунку (з кешу macOS)."""
    if "generic" not in _ICONS:
        _ICONS["generic"] = AppKit.NSWorkspace.sharedWorkspace().iconForFileType_("public.unix-executable")
    return _ICONS["generic"]


def _highlight_color():
    """Фон вибраного рядка: акцентний колір системи, напівпрозорий."""
    return AppKit.NSColor.controlAccentColor().colorWithAlphaComponent_(0.18).CGColor()


def _mem(rss):
    if rss >= GB:
        return f"{rss / GB:.1f} ГБ"
    return f"{rss / MB:.0f} МБ"


BAR_MAX_PX = 14   # найвища висота стовпчика значка, пікселів


def _bar_px(value):
    """Висота стовпчика в цілих пікселях; None — немає даних (сірий мінімум). Зміна менша за піксель значок не чіпає."""
    if value is None:
        return None
    return max(2, round(BAR_MAX_PX * min(value, 100) / 100.0))


def _bars_icon(levels):
    """Стовпчики обраних показників (висоти з _bar_px). Шаблонне зображення: macOS сам фарбує його під світлу і темну панель."""
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
    """Цілі дій меню і таймера (селектори AppKit). Логіка в StatusBar, через поле owner."""

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

    def styleBoth_(self, _sender):
        self.owner.set_style("both")

    def styleIcon_(self, _sender):
        self.owner.set_style("icon")

    def styleText_(self, _sender):
        self.owner.set_style("text")

    def quit_(self, _sender):
        self.owner.actions["quit"]()


class StatusBar:
    """Іконка в статус-барі і меню. Тримати посилання на екземпляр, інакше іконка зникне."""

    _quick = None   # швидке очищення: (id кешів, час першого кліку)
    _side = None    # друге меню «Коротко», створюється при першому відкритті

    def __init__(self, monitor, actions, autostart_on, prefs):
        self._monitor = monitor
        self.actions = actions
        self._prefs = prefs
        self._settings = {key: prefs.get(key) for key in ("cpu", "ram", "gpu", "style", "interval", "alerts", "dailyCheck")}
        self._monitor.set_interval(self._settings["interval"])
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit-settings")
        self._closed = False
        self._auto_busy = False
        self._autostart_on = bool(autostart_on)
        self._history = deque(maxlen=61)
        self._panel = None
        self._note = ""
        self._menu_open = False    # головне меню відкрите: тоді його пункти оновлюються щосекунди
        self._bar_key = None       # останній стан статус-бару: оновлюємо лише при зміні
        self._icon_key = None      # останні висоти стовпчиків значка (пікселі)
        self._bar_text = None      # останній текст і рівень безпеки в статус-барі
        self._security = None      # підсумок безпеки з останнього звіту, оновлюється раз на SECURITY_EVERY
        self._security_at = 0.0
        self._last = None
        self._targets = _Targets.alloc().init()
        self._targets.owner = self

        self._status = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        self._button = self._status.button()
        self._button.setImagePosition_(AppKit.NSImageLeading)
        self._button.setToolTip_("MacSafe: CPU · RAM · GPU, відсотки зайнятості")

        menu = AppKit.NSMenu.alloc().initWithTitle_("MacSafe")
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self._targets)
        self._menu = menu

        self._cpu_item = self._info(menu)
        self._ram_item = self._info(menu)
        self._gpu_item = self._info(menu)
        self._note_item = self._info(menu)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        procs = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Процеси", None, "")
        sub = AppKit.NSMenu.alloc().initWithTitle_("Процеси")
        sub.setAutoenablesItems_(False)
        sub.setDelegate_(self._targets)
        self._info(sub, "За CPU (% одного ядра)")
        self._cpu_rows = [self._info(sub) for _ in range(PROC_SLOTS)]
        sub.addItem_(AppKit.NSMenuItem.separatorItem())
        self._info(sub, "За памʼяттю (RSS)")
        self._ram_rows = [self._info(sub) for _ in range(RAM_SLOTS)]
        procs.setSubmenu_(sub)
        menu.addItem_(procs)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Вигляд: перемикачі показників і стиль статус-бару
        view = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Вигляд", None, "")
        vsub = AppKit.NSMenu.alloc().initWithTitle_("Вигляд")
        vsub.setAutoenablesItems_(False)
        self._metric_items = {}
        for key, action in (("cpu", "toggleCpu:"), ("ram", "toggleRam:"), ("gpu", "toggleGpu:")):
            item = self._action(METRIC_TITLES[key], action)
            self._metric_items[key] = item
            vsub.addItem_(item)
        vsub.addItem_(AppKit.NSMenuItem.separatorItem())
        self._style_items = {}
        for style, action in (("both", "styleBoth:"), ("icon", "styleIcon:"), ("text", "styleText:")):
            item = self._action(STYLE_TITLES[style], action)
            self._style_items[style] = item
            vsub.addItem_(item)
        view.setSubmenu_(vsub)
        menu.addItem_(view)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        menu.addItem_(self._action("Звіти", "openReport:"))
        menu.addItem_(self._action("Перевірити зараз", "runCheck:"))
        menu.addItem_(self._action("Звільнити памʼять…", "purge:"))
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        auto = self._action("Запускати при вході", "autostart:")
        self._auto_item = auto
        auto.setState_(_state(autostart_on))
        menu.addItem_(auto)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = self._action("Вийти з MacSafe", "quit:")
        quit_item.setKeyEquivalent_("q")
        menu.addItem_(quit_item)

        self._status.setMenu_(menu)
        # Меню вже готове, якщо будь-який крок створення popover не вдався.
        try:
            self._panel = _PopoverPanel(self)
            self._status.setMenu_(None)
            self._button.setTarget_(self._targets)
            self._button.setAction_("togglePanel:")
        except Exception as exc:
            self._panel = None
            self._status.setMenu_(menu)
            self.set_note(f"Панель недоступна, використовуйте меню: {exc}")

        # Таймер у common modes: спрацьовує і під час відкритого меню (NSEventTrackingRunLoopMode)
        self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self._targets, "tick:", None, True)
        NSRunLoop.mainRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)
        self._sync_view_items()
        self._update_bar(None, None, None)
        self.refresh()

    def close(self):
        """Прибирає іконку і таймер. Викликати з головного потоку при виході."""
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
        """Файлові операції виконуються послідовно; AppKit лише в callAfter."""
        if self._closed:
            return
        def finished(future):
            def deliver():
                if self._closed:
                    return
                try:
                    result = future.result()
                except Exception as exc:
                    self.set_note(f"Не вдалося виконати дію: {exc}")
                    if completion is not None:
                        completion(None)
                else:
                    if completion is not None:
                        completion(result)
            AppHelper.callAfter(deliver)
        self._worker.submit(action).add_done_callback(finished)

    def toggle_side(self):
        """Друге меню ліворуч від панелі. Показує поточний підсумок безпеки з файлів звітів."""
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
        """Швидке очищення безпечних кешів: перший клік рахує розмір, другий очищує (як «×» у процесах)."""
        if self._quick is not None and time.monotonic() - self._quick[1] <= 10:
            ids = self._quick[0]
            self._quick = None
            self.set_note("Очищую: npm, pip, Homebrew, Xcode…")
            self._work(lambda: self.actions["cache_clean"](ids), self._quick_done)
            return
        self._quick = None
        self.set_note("Рахую розмір безпечних кешів…")   # одразу видно реакцію, підрахунок іде у фоні
        self._work(self.actions["cache_scan"], self._quick_ready)

    def _quick_ready(self, items):
        candidates = [i for i in (items or []) if i.get("sizeMb", 0) > 0]
        if not candidates:
            self.set_note("Безпечних кешів для очищення немає")
            return
        total = sum(i["sizeMb"] for i in candidates)
        self._quick = ([i["id"] for i in candidates], time.monotonic())
        size = f"{total / 1024:.1f} ГБ" if total >= 1024 else f"{total:.0f} МБ"
        names = ", ".join(i["title"] for i in candidates)
        self.set_note(f"Ще раз: очистити {size} старше {cleanup.OLD_DAYS} днів: {names}")

    def _quick_done(self, result):
        if not result or not result.get("ok"):
            self.set_note((result or {}).get("error") or "Не вдалося очистити кеші")
            return
        delta = (result.get("memory") or {}).get("delta")
        mem = f". Памʼять: доступно {delta['availableMb']:+d} МБ, swap {delta['swapMb']:+d} МБ" if delta else ""
        self.set_note(f"Звільнено на диску: {result.get('freedMb', 0):.0f} МБ{mem}")

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
            # Застосунок має стати активним до показу, інакше перший клік усередині панелі
            # активує його і NSPopover сприймає це як клік поза панеллю та закриває її
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
            if window is not None:   # вікна немає, якщо значок сховано системою: тоді покажеться меню
                window.makeKeyWindow()
        except Exception as exc:
            self._panel.popover.close()
            self._panel = None
            self._monitor.set_detail(False)
            self._status.setMenu_(self._menu)
            self._button.setAction_(None)
            self.set_note(f"Панель недоступна, використовуйте меню: {exc}")

    # ---------- події ----------
    def on_tick(self):
        if self._closed:
            return
        self.actions["tick"]()   # перевірка запиту на показ вікна від другого запуску
        self.refresh()

    def on_menu_open(self, menu):
        if menu is self._menu:
            self._menu_open = True
            self._monitor.set_detail(True)   # список процесів щосекунди, поки меню відкрите
        self._sync_view_items()
        self.refresh()

    def on_menu_close(self, menu):
        if menu is self._menu:
            self._menu_open = False
            self._monitor.set_detail(False)

    def toggle_metric(self, key):
        # Не можна вимкнути всі три разом з видимим текстом: тоді в статус-барі буде порожньо
        now = self._settings[key]
        if now and sum(1 for k in ("cpu", "ram", "gpu") if self._settings[k]) == 1:
            self.set_note("Має лишитися увімкненим хоча б один показник")
            self._sync_view_items()
            return
        self._save_setting(key, not now)
        self._last = None
        self._sync_view_items()
        self.refresh()

    def toggle_flag(self, key):
        """Перемикачі «Сповіщення» і «Щоденна перевірка»."""
        self._save_setting(key, not self._settings[key])
        self._sync_view_items()

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

    # ---------- побудова ----------
    def _info(self, menu, title=""):
        """Пункт без дії (лише текст). Повертає NSMenuItem, у якого далі міняється заголовок."""
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setEnabled_(False)
        menu.addItem_(item)
        return item

    def _action(self, title, selector):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, "")
        item.setTarget_(self._targets)
        return item

    def _mark_kill(self, index):
        """«Так?» на рядку index, на решті «×» одразу, а не на наступному оновленні панелі. None — скинути всі."""
        flat = 0   # наскрізний індекс, як у update: спершу рядки CPU, потім RAM
        for rows in self._panel.process_rows:
            for _, _, _, _, kill, _, _ in rows:
                kill.setTitle_("Так?" if index is not None and flat == index else "×")
                flat += 1

    def select_row(self, index):
        """Клік по назві процесу: рядок вибирається і фіксується, позиції не міняються. Повторний клік знімає вибір."""
        panel = self._panel
        group = panel.kill_targets.get(index) if panel is not None else None
        if group is None:
            return
        panel.toggle_select(index)
        self.set_note(f"Зафіксовано: {group['name']}. Ще раз по назві — зняти вибір" if panel.selected == index
                      else "Вибір знято")
        snap = self._monitor.latest()
        if snap is not None:
            panel.update(snap)

    def kill_row(self, index):
        """Хрестик у рядку процесу: перший клік просить підтвердження, другий завершує процес (SIGTERM).

        Завершуються процеси групи, які належать поточному користувачу. MacSafe себе не завершує.
        """
        panel = self._panel
        group = panel.kill_targets.get(index) if panel is not None else None
        if not group or not group.get("killable"):
            return
        now = time.monotonic()
        armed = panel.armed
        if armed is None or armed[0] != index or now - armed[1] > 3:
            # Рядки фіксуються ще на першому кліку: «Так?» завершить саме ту групу, по якій клікнули
            panel.freeze()
            panel.armed = (index, now, group)
            self._mark_kill(index)
            self.set_note(f"Натисніть «Так?», щоб завершити {group['name']}")
            return
        group = armed[2]
        panel.clear_selection()   # після спроби вибір і фіксація знімаються
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
            self.set_note(f"Немає доступу, щоб завершити {group['name']}")
        elif sent:
            self.set_note(f"Завершено: {group['name']}")
        else:
            self.set_note(f"{group['name']} вже не працює")

    def set_note(self, text):
        """Короткий рядок про останню дію (наприклад, результат очищення). Викликати з головного потоку."""
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

    # ---------- оновлення ----------
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
        if not self._history or stamp > self._history[-1][0]:
            # Розрив лінії лише при пропуску вибірок, а не при звичайному інтервалі з тремтінням таймера
            gap = max(2.0, 2.5 * self._monitor.interval)
            if self._history and stamp - self._history[-1][0] > gap:
                self._history.append((stamp - 0.001, {"cpu": None, "ram": None, "gpu": None}))
            self._history.append((stamp, {"cpu": cpu, "ram": ram_pct, "gpu": gpu}))
        elif stamp < self._history[-1][0]:
            self._history.clear()
            self._history.append((stamp, {"cpu": cpu, "ram": ram_pct, "gpu": gpu}))
        while self._history and self._history[0][0] < stamp - 60:
            self._history.popleft()
        self._update_security()
        if self._settings["alerts"]:
            self.actions["alerts"](snap, self._security)
        # Статус-бар перемальовуємо лише коли змінилось округлене значення: фон майже без роботи AppKit
        key = (round(cpu), round(ram_pct), None if gpu is None else round(gpu), self._settings["style"],
               self._settings["cpu"], self._settings["ram"], self._settings["gpu"],
               (self._security or {}).get("level"))
        if key != self._bar_key:
            self._bar_key = key
            self._update_bar(cpu, ram_pct, gpu)

        # Пункти меню і панель оновлюємо лише коли вони відкриті: закриті не витрачають AppKit
        if self._menu_open:
            self._cpu_item.setAttributedTitle_(_attr(f"CPU   {cpu:5.1f}%   {snap['cores']} ядер"))
            self._ram_item.setAttributedTitle_(_attr(
                f"RAM   {ram_pct:5.1f}%   {ram['usedGb']:.1f} з {ram['totalGb']:.1f} ГБ, swap {ram['swapGb']:.1f} ГБ"))
            self._gpu_item.setAttributedTitle_(_attr(f"GPU   {'н/д' if gpu is None else f'{gpu:.0f}%'}"))
            self._fill(self._cpu_rows, snap["topCpu"], lambda g: f"{g['cpu']:6.1f}%  {_mem(g['rss'])}")
            self._fill(self._ram_rows, snap["topRam"], lambda g: _mem(g["rss"]))
        if self._panel is not None and self._panel.popover.isShown():
            self._panel.update(snap)
            if not self._note:
                self._panel.note.setStringValue_("")

    def _update_bar(self, cpu, ram_pct, gpu):
        """Статус-бар: лише обрані показники, у обраному стилі. Фіксована ширина цифр (моноширинні)."""
        shown = []
        if self._settings["cpu"]:
            shown.append(("cpu", cpu))
        if self._settings["ram"]:
            shown.append(("ram", ram_pct))
        if self._settings["gpu"]:
            shown.append(("gpu", gpu))
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
            # Кожне значення — рівно дві позиції (0–99), однакові пробіли з боків крапки: відстані рівні,
            # ширина не змінюється. Цифровий пробіл (U+2007) зліва для однієї цифри.
            parts = []
            for key, value in shown:
                text = "–" if value is None else str(min(99, int(round(value))))
                parts.append(text.rjust(2, "\u2007"))
            text = " " + " · ".join(parts)
        title_key = (text, (self._security or {}).get("level"))   # рівень безпеки міняє колір крапки
        if title_key != self._bar_text:
            self._bar_text = title_key
            self._button.setAttributedTitle_(self._bar_title(text))

    def refresh_security(self):
        """Наступне оновлення індикатора безпеки прочитає звіт одразу (після перевірки)."""
        self._security_at = 0.0

    def _update_security(self):
        """Читає підсумок безпеки не частіше ніж раз на SECURITY_EVERY секунд і оновлює панель."""
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
                checked = sec.get("checkedAt") or "звіту ще немає"
                changed = int(sec.get("changeCount", 0))
                tip = f"Останній звіт: {checked}. Змін з попереднього звіту: {changed}. Деталі: «Звіти»"
                self._panel.security.setToolTip_(tip)

    def _bar_title(self, text):
        """Текст статус-бару з крапкою перед ним, якщо є проблеми безпеки (orange — увага, red — критично)."""
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
    """Створює іконку в статус-барі. Викликати з головного потоку. Повертає StatusBar (тримати посилання)."""
    return StatusBar(monitor, actions, autostart_on, prefs)
