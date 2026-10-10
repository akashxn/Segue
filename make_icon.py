#!/usr/bin/env python3
"""Draws the Segue app icon and writes assets/Segue.icns plus the PNG the page uses.

The mark is a crossfade: one level line easing down (the track leaving) and one
easing up over it (the track coming in). Run again after changing anything here:

    .venv/bin/python make_icon.py && ./build_app.sh
"""
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import AppKit
import Quartz

ASSETS = Path(__file__).parent / "assets"
CANVAS = 1024
PLATE = 824  # macOS icon grid: the rounded plate sits inside a 100 px margin
ICONSET = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]

INDIGO_TOP = (0.478, 0.455, 1.0)
INDIGO_BOTTOM = (0.227, 0.176, 0.769)


def plate_path():
    """A superellipse close to the continuous-corner shape macOS uses for app icons."""
    path = Quartz.CGPathCreateMutable()
    half, centre, power = PLATE / 2, CANVAS / 2, 2 / 5.0
    for step in range(721):
        angle = step / 720 * 2 * math.pi
        c, s = math.cos(angle), math.sin(angle)
        x = centre + half * math.copysign(abs(c) ** power, c)
        y = centre + half * math.copysign(abs(s) ** power, s)
        (Quartz.CGPathMoveToPoint if step == 0 else Quartz.CGPathAddLineToPoint)(path, None, x, y)
    Quartz.CGPathCloseSubpath(path)
    return path


def fade_path(start_y, end_y):
    """A level line that holds, eases to a new level, and holds again."""
    left, right, hold = 262, 762, 84
    middle = (left + right) / 2
    path = Quartz.CGPathCreateMutable()
    Quartz.CGPathMoveToPoint(path, None, left, start_y)
    Quartz.CGPathAddLineToPoint(path, None, left + hold, start_y)
    Quartz.CGPathAddCurveToPoint(path, None, middle, start_y, middle, end_y, right - hold, end_y)
    Quartz.CGPathAddLineToPoint(path, None, right, end_y)
    return path


def gradient(space, stops):
    colours = [c for _, rgba in stops for c in rgba]
    return Quartz.CGGradientCreateWithColorComponents(space, colours, [at for at, _ in stops], len(stops))


def stroke(ctx, path, width):
    Quartz.CGContextSetLineWidth(ctx, width)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    Quartz.CGContextAddPath(ctx, path)
    Quartz.CGContextStrokePath(ctx)


def draw(ctx):
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    plate = plate_path()
    top, bottom = (CANVAS + PLATE) / 2, (CANVAS - PLATE) / 2

    # Plate with the soft drop shadow every macOS app icon carries.
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextSetShadowWithColor(ctx, (0, -10), 22, Quartz.CGColorCreateGenericRGB(0, 0, 0, 0.32))
    Quartz.CGContextAddPath(ctx, plate)
    Quartz.CGContextSetRGBFillColor(ctx, *INDIGO_BOTTOM, 1)
    Quartz.CGContextFillPath(ctx)
    Quartz.CGContextRestoreGState(ctx)

    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextAddPath(ctx, plate)
    Quartz.CGContextClip(ctx)
    Quartz.CGContextDrawLinearGradient(
        ctx, gradient(space, [(0, (*INDIGO_TOP, 1)), (1, (*INDIGO_BOTTOM, 1))]), (0, top), (0, bottom), 0)
    # Light falling from the upper left.
    Quartz.CGContextDrawRadialGradient(
        ctx, gradient(space, [(0, (1, 1, 1, 0.26)), (1, (1, 1, 1, 0))]),
        (330, top - 40), 0, (330, top - 40), 640, 0)
    # A bright rim along the top edge that dies away down the sides.
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextAddPath(ctx, plate)
    Quartz.CGContextSetLineWidth(ctx, 8)
    Quartz.CGContextReplacePathWithStrokedPath(ctx)
    Quartz.CGContextClip(ctx)
    Quartz.CGContextDrawLinearGradient(
        ctx, gradient(space, [(0, (1, 1, 1, 0.5)), (1, (1, 1, 1, 0))]), (0, top), (0, top - 380), 0)
    Quartz.CGContextRestoreGState(ctx)

    high, low, weight = 652, 372, 66
    leaving, arriving = fade_path(high, low), fade_path(low, high)
    Quartz.CGContextSetShadowWithColor(ctx, (0, -8), 20, Quartz.CGColorCreateGenericRGB(0.08, 0.04, 0.4, 0.38))

    # The leaving track: dimmer, and broken where the arriving track passes over it.
    Quartz.CGContextSetAlpha(ctx, 0.46)
    Quartz.CGContextBeginTransparencyLayer(ctx, None)
    Quartz.CGContextSetRGBStrokeColor(ctx, 1, 1, 1, 1)
    stroke(ctx, leaving, weight)
    Quartz.CGContextSetBlendMode(ctx, Quartz.kCGBlendModeDestinationOut)
    stroke(ctx, arriving, weight + 44)
    Quartz.CGContextEndTransparencyLayer(ctx)

    Quartz.CGContextSetAlpha(ctx, 1)
    Quartz.CGContextSetBlendMode(ctx, Quartz.kCGBlendModeNormal)
    Quartz.CGContextSetRGBStrokeColor(ctx, 1, 1, 1, 1)
    stroke(ctx, arriving, weight)
    Quartz.CGContextRestoreGState(ctx)


def render(pixels):
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, pixels, pixels, 8, 0, space, Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextScaleCTM(ctx, pixels / CANVAS, pixels / CANVAS)
    draw(ctx)
    return Quartz.CGBitmapContextCreateImage(ctx)


def save_png(image, path):
    rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
    rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None).writeToFile_atomically_(
        str(path), True)


def main():
    ASSETS.mkdir(exist_ok=True)
    save_png(render(CANVAS), ASSETS / "icon-1024.png")
    save_png(render(256), ASSETS / "icon.png")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Segue.iconset"
        iconset.mkdir()
        for points, scale in ICONSET:
            suffix = "@2x" if scale == 2 else ""
            save_png(render(points * scale), iconset / f"icon_{points}x{points}{suffix}.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(Path(tmp) / "Segue.icns")], check=True)
        shutil.copy2(Path(tmp) / "Segue.icns", ASSETS / "Segue.icns")
    print(f"Wrote {ASSETS}/Segue.icns, icon.png and icon-1024.png")


if __name__ == "__main__":
    main()
