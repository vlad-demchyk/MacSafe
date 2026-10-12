"""Network details for the panel: the local address and the quality of the Wi-Fi link. No root, nothing leaves the Mac.

Wi-Fi comes from CoreWLAN (signal, noise, link rate, channel in a few milliseconds). Without the framework, or on a Mac
without Wi-Fi, only the address is returned. The network name needs the Location permission and is not read.
"""
import socket


def local_ip():
    """The IPv4 address used for outgoing traffic. A UDP socket is connected but no packet is sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def quality(rssi):
    """A word for the signal strength: excellent from -55 dBm, good from -67, fair from -75, otherwise weak."""
    if rssi is None:
        return None
    return "excellent" if rssi >= -55 else "good" if rssi >= -67 else "fair" if rssi >= -75 else "weak"


def wifi():
    """{rssi, noise, rate, channel, quality} of the Wi-Fi interface, or None."""
    try:
        import CoreWLAN
        iface = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
        if iface is None or iface.powerOn() is False:
            return None
        rssi = int(iface.rssiValue())
        if rssi == 0:   # not associated
            return None
        channel = iface.wlanChannel()
        return {"rssi": rssi, "noise": int(iface.noiseMeasurement()), "rate": float(iface.transmitRate()),
                "channel": int(channel.channelNumber()) if channel else None, "quality": quality(rssi)}
    except Exception:
        return None


def read():
    return {"ip": local_ip(), "wifi": wifi()}
