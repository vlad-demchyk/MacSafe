#!/usr/bin/env python3
"""Final macOS icon: a squircle, a thin gradient and three columns without text.

Run: /usr/bin/python3 packaging/make_icon.py <path-to-png>. Needs PyObjC (AppKit).
The same three columns as in the menu bar, so the app icon and the menu glyph look alike.
"""
import math
import os
import sys

import AppKit
from Foundation import NSMakeRect, NSMakeSize

SIZE = 1024
BAR_W = 120
BAR_X = (296, 452, 608)
BAR_BOTTOM = 274
BAR_MAX = 500
BARS = (0.57, 0.91, 0.71)


def _srgb(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def _squircle(inset=64):
    """A superellipse with continuously changing curvature, without straight corner joints."""
    path = AppKit.NSBezierPath.bezierPath()
    radius = SIZE / 2 - inset
    for i in range(513):
        angle = math.tau * i / 512
        c, s = math.cos(angle), math.sin(angle)
        point = (SIZE / 2 + radius * math.copysign(abs(c) ** 0.5, c),
                 SIZE / 2 + radius * math.copysign(abs(s) ** 0.5, s))
        if i == 0:
            path.moveToPoint_(point)
        else:
            path.lineToPoint_(point)
    path.closePath()
    return path


def render(out_path):
    # The bitmap context fixes physical pixels regardless of Retina and the screen.
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0)
    rep.setSize_(NSMakeSize(SIZE, SIZE))
    context = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(context)
    try:
        AppKit.NSColor.clearColor().setFill()
        AppKit.NSRectFillUsingOperation(NSMakeRect(0, 0, SIZE, SIZE), AppKit.NSCompositingOperationCopy)
        bg = _squircle()
        AppKit.NSGraphicsContext.saveGraphicsState()
        shadow = AppKit.NSShadow.alloc().init()
        shadow.setShadowColor_(_srgb(0.02, 0.04, 0.08, 0.24))
        shadow.setShadowBlurRadius_(22)
        shadow.setShadowOffset_(NSMakeSize(0, -12))
        shadow.set()
        _srgb(0.09, 0.16, 0.25).setFill()
        bg.fill()
        AppKit.NSGraphicsContext.restoreGraphicsState()
        grad = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            _srgb(0.075, 0.13, 0.22), _srgb(0.14, 0.23, 0.34))
        grad.drawInBezierPath_angle_(bg, 90)
        _srgb(0.8, 0.9, 1, 0.12).setStroke()
        bg.setLineWidth_(2)
        bg.stroke()
        accent = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            _srgb(0.38, 0.67, 0.90), _srgb(0.65, 0.84, 0.98))
        for x, value in zip(BAR_X, BARS):
            track = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, BAR_BOTTOM, BAR_W, BAR_MAX), 32, 32)
            _srgb(0.72, 0.84, 0.98, 0.055).setFill()
            track.fill()
            h = BAR_MAX * value
            bar = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, BAR_BOTTOM, BAR_W, h), 32, 32)
            accent.drawInBezierPath_angle_(bar, 90)
    finally:
        AppKit.NSGraphicsContext.restoreGraphicsState()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if not data.writeToFile_atomically_(out_path, True):
        raise SystemExit(f"Could not write {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: make_icon.py <path-to-png>")
    render(sys.argv[1])
    print("ICON_OK", sys.argv[1])
