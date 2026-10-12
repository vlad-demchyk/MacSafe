"""Temperatures without root: the HID thermal sensors of Apple Silicon, read through IOKit (ctypes).

`powermetrics` needs root, but the same sensors (PMU tdie for the processor, NAND for the SSD, gas gauge for the
battery) are readable by an ordinary user through IOHIDEventSystemClient. If the library or the sensors are missing
(Intel Mac, a future macOS), read() returns None and the report says "no data" as before.

    python3 thermal_hid.py      prints: cpu=54.1 ssd=48.0 battery=36.0 (only the values that were found)
"""
import ctypes
import ctypes.util
import threading

_UTF8 = 0x08000100
_SINT32 = 9
_TEMPERATURE_EVENT = 15          # kIOHIDEventTypeTemperature
_PAGE, _USAGE = 0xFF00, 5        # the vendor page of the temperature sensors
_WANTED = ("tdie", "NAND", "gas gauge")   # what summarize() uses


def summarize(readings):
    """[(sensor name, °C)] → {"cpu", "ssd", "battery"} in °C. Nonsense values (the unset -22 of PMU tdev) are dropped."""
    good = [(name, c) for name, c in readings if isinstance(c, (int, float)) and 0 < c < 130]

    def pick(match, how):
        values = [c for name, c in good if match(name)]
        return round(how(values), 1) if values else None

    out = {
        "cpu": pick(lambda n: "tdie" in n, lambda v: sum(v) / len(v)),
        "ssd": pick(lambda n: "NAND" in n, max),
        "battery": pick(lambda n: "gas gauge" in n, lambda v: sum(v) / len(v)),
    }
    return {k: v for k, v in out.items() if v is not None}


class _Sensors:
    """The IOKit client, the matched services and their names, created once and reused.

    Building the bindings and the client on every read cost about 50 ms and leaked the CF objects; now a read
    is only IOHIDServiceClientCopyEvent for each sensor.
    """

    def __init__(self):
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        vp, i32, i64 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_int64
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFNumberCreate.restype = vp
        cf.CFNumberCreate.argtypes = [vp, i32, vp]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [vp]
        cf.CFArrayGetValueAtIndex.restype = vp
        cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [vp]
        iokit.IOHIDEventSystemClientCreate.restype = vp
        iokit.IOHIDEventSystemClientCreate.argtypes = [vp]
        iokit.IOHIDEventSystemClientSetMatching.argtypes = [vp, vp]
        iokit.IOHIDEventSystemClientCopyServices.restype = vp
        iokit.IOHIDEventSystemClientCopyServices.argtypes = [vp]
        iokit.IOHIDServiceClientCopyEvent.restype = vp
        iokit.IOHIDServiceClientCopyEvent.argtypes = [vp, i64, i32, i64]
        iokit.IOHIDServiceClientCopyProperty.restype = vp
        iokit.IOHIDServiceClientCopyProperty.argtypes = [vp, vp]
        iokit.IOHIDEventGetFloatValue.restype = ctypes.c_double
        iokit.IOHIDEventGetFloatValue.argtypes = [vp, i32]
        self.iokit, self.cf = iokit, cf
        self.services = []          # [(service ref, name)]; the refs live as long as self._array

        def cfstr(text):
            return cf.CFStringCreateWithCString(None, text.encode(), _UTF8)

        def cfnum(value):
            box = i32(value)
            return cf.CFNumberCreate(None, _SINT32, ctypes.byref(box))

        key_strs = [cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage")]
        val_nums = [cfnum(_PAGE), cfnum(_USAGE)]
        keys, vals = (vp * 2)(*key_strs), (vp * 2)(*val_nums)
        key_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        val_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        match = cf.CFDictionaryCreate(None, keys, vals, 2, ctypes.byref(key_cb), ctypes.byref(val_cb))
        self._client = iokit.IOHIDEventSystemClientCreate(None)
        self._array = None
        if self._client:
            iokit.IOHIDEventSystemClientSetMatching(self._client, match)
            self._array = iokit.IOHIDEventSystemClientCopyServices(self._client)
        product = cfstr("Product")
        for i in range(cf.CFArrayGetCount(self._array) if self._array else 0):
            service = cf.CFArrayGetValueAtIndex(self._array, i)
            name_ref = iokit.IOHIDServiceClientCopyProperty(service, product)
            name = ""
            if name_ref:
                buf = ctypes.create_string_buffer(128)
                if cf.CFStringGetCString(name_ref, buf, 128, _UTF8):
                    name = buf.value.decode(errors="replace")
                cf.CFRelease(name_ref)
            if any(word in name for word in _WANTED):   # the other sensors are never used: skip reading them
                self.services.append((service, name))
        for ref in key_strs + val_nums + [match, product]:   # only the client and the array are kept
            cf.CFRelease(ref)

    def read(self):
        readings = []
        for service, name in self.services:
            event = self.iokit.IOHIDServiceClientCopyEvent(service, _TEMPERATURE_EVENT, 0, 0)
            if event:
                readings.append((name, self.iokit.IOHIDEventGetFloatValue(event, _TEMPERATURE_EVENT << 16)))
                self.cf.CFRelease(event)
        return readings


_sensors = None
_guard = threading.Lock()


def _raw_readings():
    global _sensors
    with _guard:
        if _sensors is None:
            _sensors = _Sensors()
        return _sensors.read()


def read():
    """{"cpu", "ssd", "battery"} in °C (only what was found) or None when the sensors cannot be read."""
    try:
        values = summarize(_raw_readings())
    except (OSError, AttributeError, ValueError):
        return None
    return values or None


if __name__ == "__main__":
    found = read() or {}
    print(" ".join(f"{k}={v}" for k, v in found.items()))
