"""GPU load through IOKit directly (ctypes), without running ioreg every second.

Reads the PerformanceStatistics property → “Device Utilization %” (or “GPU Activity(%)”) of the services
IOAccelerator. No root needed. If the library or key is missing, returns None (shown as “n/a”).
One call takes microseconds, not tens of milliseconds for a fork+exec of ioreg.
"""
import ctypes
import ctypes.util
import re

GPU_KEYS = ("Device Utilization %", "GPU Activity(%)")
_KCF_STRING_ENCODING_UTF8 = 0x08000100
_KCF_NUMBER_SINT64 = 4

try:
    _iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
    _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    _ok = True
except (OSError, TypeError):
    _ok = False

if _ok:
    _vp = ctypes.c_void_p
    _iokit.IOServiceMatching.restype = _vp
    _iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    _iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
    _iokit.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, _vp, ctypes.POINTER(ctypes.c_uint32)]
    _iokit.IOIteratorNext.restype = ctypes.c_uint32
    _iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
    _iokit.IORegistryEntryCreateCFProperty.restype = _vp
    _iokit.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, _vp, _vp, ctypes.c_uint32]
    _iokit.IOObjectRelease.restype = ctypes.c_int
    _iokit.IORegistryEntryGetChildIterator.restype = ctypes.c_int
    _iokit.IORegistryEntryGetChildIterator.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)]
    _iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
    _cf.CFStringCreateWithCString.restype = _vp
    _cf.CFStringCreateWithCString.argtypes = [_vp, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFDictionaryGetValue.restype = _vp
    _cf.CFDictionaryGetValue.argtypes = [_vp, _vp]
    _cf.CFNumberGetValue.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [_vp, ctypes.c_int, ctypes.c_void_p]
    _cf.CFRelease.restype = None
    _cf.CFRelease.argtypes = [_vp]
    _cf.CFGetTypeID.restype = ctypes.c_ulong
    _cf.CFGetTypeID.argtypes = [_vp]
    _cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
    _cf.CFDictionaryGetTypeID.argtypes = []
    _cf.CFNumberGetTypeID.restype = ctypes.c_ulong
    _cf.CFNumberGetTypeID.argtypes = []
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [_vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _cf.CFArrayGetCount.restype = ctypes.c_long
    _cf.CFArrayGetCount.argtypes = [_vp]
    _cf.CFArrayGetValueAtIndex.restype = _vp
    _cf.CFArrayGetValueAtIndex.argtypes = [_vp, ctypes.c_long]
    _cf.CFArrayGetTypeID.restype = ctypes.c_ulong
    _cf.CFArrayGetTypeID.argtypes = []
    _cf.CFStringGetTypeID.restype = ctypes.c_ulong
    _cf.CFStringGetTypeID.argtypes = []


def _cfstr(text):
    return _cf.CFStringCreateWithCString(None, text.encode("utf-8"), _KCF_STRING_ENCODING_UTF8)


def _number(value_ref):
    out = ctypes.c_int64(0)
    if _cf.CFGetTypeID(value_ref) != _cf.CFNumberGetTypeID():
        return None
    if not _cf.CFNumberGetValue(value_ref, _KCF_NUMBER_SINT64, ctypes.byref(out)):
        return None
    return int(out.value)


def _max_stats(groups):
    """Maximum across GPUs for each key group: the first existing key of the group in PerformanceStatistics.

    groups — {name: (keys…)}. Returns {name: value 0–100 or None}. One pass over the services for all groups.
    """
    if not _ok:
        return None
    iterator = ctypes.c_uint32(0)
    matching = _iokit.IOServiceMatching(b"IOAccelerator")
    if not matching or _iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)) != 0:
        return None
    key_stats = _cfstr("PerformanceStatistics")
    group_keys = {name: [_cfstr(k) for k in keys] for name, keys in groups.items()}
    values = {name: [] for name in groups}
    try:
        while True:
            service = _iokit.IOIteratorNext(iterator.value)
            if not service:
                break
            try:
                stats = _iokit.IORegistryEntryCreateCFProperty(service, key_stats, None, 0)
                if not stats:
                    continue
                try:
                    if _cf.CFGetTypeID(stats) != _cf.CFDictionaryGetTypeID():
                        continue
                    for name, keys in group_keys.items():
                        for key in keys:
                            val = _cf.CFDictionaryGetValue(stats, key)
                            if val:
                                num = _number(val)
                                if num is not None:
                                    values[name].append(min(max(num, 0), 100))
                                    break
                finally:
                    _cf.CFRelease(stats)
            finally:
                _iokit.IOObjectRelease(service)
    finally:
        _iokit.IOObjectRelease(iterator.value)
        _cf.CFRelease(key_stats)
        for keys in group_keys.values():
            for k in keys:
                _cf.CFRelease(k)
    return {name: (max(vals) if vals else None) for name, vals in values.items()}


def gpu_percent_iokit():
    """Maximum GPU load across GPUs, 0–100, or None if IOKit is unavailable or the key is missing."""
    stats = _max_stats({"device": GPU_KEYS})
    return stats["device"] if stats else None


def gpu_detail_iokit():
    """GPU load: device (main), renderer and tiler. Each is 0–100 or None."""
    return _max_stats({
        "device": GPU_KEYS,
        "renderer": ("Renderer Utilization %",),
        "tiler": ("Tiler Utilization %",),
    })


_PID_RE = re.compile(r"^pid (\d+)")


def _string(ref):
    buf = ctypes.create_string_buffer(256)
    if ref and _cf.CFGetTypeID(ref) == _cf.CFStringGetTypeID() \
            and _cf.CFStringGetCString(ref, buf, 256, _KCF_STRING_ENCODING_UTF8):
        return buf.value.decode("utf-8", "replace")
    return ""


def _add_client(service, keys, totals):
    """Adds the GPU client's AppUsage to totals[pid]. Clients without a PID in the creator are skipped."""
    key_creator, key_usage, key_time = keys
    creator = _iokit.IORegistryEntryCreateCFProperty(service, key_creator, None, 0)
    try:
        match = _PID_RE.match(_string(creator))
    finally:
        if creator:
            _cf.CFRelease(creator)
    if not match:
        return
    usage = _iokit.IORegistryEntryCreateCFProperty(service, key_usage, None, 0)
    if not usage:
        return
    try:
        if _cf.CFGetTypeID(usage) != _cf.CFArrayGetTypeID():
            return
        pid = int(match.group(1))
        for i in range(_cf.CFArrayGetCount(usage)):
            entry = _cf.CFArrayGetValueAtIndex(usage, i)
            if not entry or _cf.CFGetTypeID(entry) != _cf.CFDictionaryGetTypeID():
                continue
            raw = _cf.CFDictionaryGetValue(entry, key_time)
            value = _number(raw) if raw else None
            if value is not None:
                totals[pid] = totals.get(pid, 0) + max(value, 0)
    finally:
        _cf.CFRelease(usage)


def gpu_time_by_pid():
    """Accumulated GPU time in nanoseconds per process PID, or None if IOKit is unavailable.

    macOS keeps an AppUsage counter (accumulatedGPUTime) for each GPU client; clients are child nodes
    accelerator, and the PID is in IOUserClientCreator (“pid 172, WindowServer”). The sum over the process's clients —
    its GPU use. No root is needed.
    """
    if not _ok:
        return None
    accel_it = ctypes.c_uint32(0)
    matching = _iokit.IOServiceMatching(b"IOAccelerator")
    if not matching or _iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(accel_it)) != 0:
        return None
    keys = (_cfstr("IOUserClientCreator"), _cfstr("AppUsage"), _cfstr("accumulatedGPUTime"))
    totals = {}
    try:
        while True:
            accel = _iokit.IOIteratorNext(accel_it.value)
            if not accel:
                break
            try:
                child_it = ctypes.c_uint32(0)
                if _iokit.IORegistryEntryGetChildIterator(accel, b"IOService", ctypes.byref(child_it)) != 0:
                    continue
                try:
                    while True:
                        child = _iokit.IOIteratorNext(child_it.value)
                        if not child:
                            break
                        try:
                            _add_client(child, keys, totals)
                        finally:
                            _iokit.IOObjectRelease(child)
                finally:
                    _iokit.IOObjectRelease(child_it.value)
            finally:
                _iokit.IOObjectRelease(accel)
    finally:
        _iokit.IOObjectRelease(accel_it.value)
        for k in keys:
            _cf.CFRelease(k)
    return totals


def battery_iokit():
    """{"load": SystemLoad mW, "volt": mV, "amp": mA (negative while discharging), "pct"} of AppleSmartBattery.

    Only the values that were found. None if IOKit is unavailable or there is no battery. Replaces a fork of ioreg.
    """
    if not _ok:
        return None
    matching = _iokit.IOServiceMatching(b"AppleSmartBattery")
    iterator = ctypes.c_uint32(0)
    if not matching or _iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator)) != 0:
        return None
    out = {}
    try:
        service = _iokit.IOIteratorNext(iterator.value)
        if not service:
            return None
        try:
            for key, name in (("volt", "Voltage"), ("amp", "Amperage"), ("pct", "CurrentCapacity")):
                k = _cfstr(name)
                ref = _iokit.IORegistryEntryCreateCFProperty(service, k, None, 0)
                _cf.CFRelease(k)
                if ref:
                    value = _number(ref)
                    _cf.CFRelease(ref)
                    if value is not None:
                        out[key] = value
            k = _cfstr("PowerTelemetryData")
            ref = _iokit.IORegistryEntryCreateCFProperty(service, k, None, 0)
            _cf.CFRelease(k)
            if ref:
                if _cf.CFGetTypeID(ref) == _cf.CFDictionaryGetTypeID():
                    k = _cfstr("SystemLoad")
                    inner = _cf.CFDictionaryGetValue(ref, k)
                    _cf.CFRelease(k)
                    value = _number(inner) if inner else None
                    if value is not None:
                        out["load"] = value
                _cf.CFRelease(ref)
        finally:
            _iokit.IOObjectRelease(service)
    finally:
        _iokit.IOObjectRelease(iterator.value)
    return out or None
