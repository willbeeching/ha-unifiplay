#!/usr/bin/env python3
"""Dump everything a UniFi Play device reports over MQTT.

Standalone: needs only `pip install paho-mqtt`. The client certificate is the
one the UniFi Play mobile app uses (bundled with the ha-unifiplay
integration); it is embedded here so the script is a single file.

Usage:
  python3 dump_device.py <DEVICE_IP> [--seconds 20]

Paste the full output into a GitHub issue — it contains the device's
capabilities and current state (device name, volume, sources), nothing
account- or network-sensitive beyond the IP you passed in.
"""

from __future__ import annotations

import argparse
import json
import ssl
import struct
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt is required: pip install paho-mqtt")

MQTT_CERT = """-----BEGIN CERTIFICATE-----
MIIC5jCCAc6gAwIBAgIEZRFUmzANBgkqhkiG9w0BAQsFADAuMQswCQYDVQQGEwJV
UzEfMB0GA1UEAxMWbXF0dC51bmlmaS1wbGF5LnVpLmNvbTAeFw0yMzA5MjUwOTM2
MjdaFw0zMzA5MjUwOTM2MjdaMBcxFTATBgNVBAMTDDY4RDc5QTA1QjQ5NzCCASIw
DQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAKoWEHaxSjmiXQnJVqEuMrZ6ZYhG
4eK5ga6v0Y7UaCzeGgstVpRg321sdASdorD2ELyXSuuis5dttNHt6KCW4KboSBLl
ty5dueq21tpc4tJNUvvodW/5XbsV+qfNWi4KTAqMmbARB1QaRIc4FlVvCSNgpdAI
IHMDfRa51zsiSZFoeNyCElfC26ZtlGr5DEPiGGB6iQWd33vq7B/XFAg9k9IKP2YH
5nmsKL8PwPbFMrJ+6+1aB/ZbJOuCTGuOZ3V3k3+V1OsydU4LD6tXlqw3lloxmetO
m8/zeQ7YRjSM7F6SJIigS+H75RxcYSaNZ/2NqjXhzDWYH2u2SXFMDb2icP0CAwEA
AaMjMCEwHwYDVR0jBBgwFoAU8aD5AZz/fdbn5vlnvGoWWWR0WfUwDQYJKoZIhvcN
AQELBQADggEBAJMspddQa7yeBTDQKuvfltJxa07ye4wdKjMxgGJ9iGZkY2i7pfOz
9Nwc3k1MOx9DAy7cr9HVCScP1lRWAiDzGMyKtxTOuz0Jmb4N+M8a45nMdUOpgVV9
8UsXtEajsz2TqMHAB6DT+wDFdxVY2TcnjmrP4Jt9RccHKWZ958bZRXoFJ/a3DeW4
XsXnwGIoMKBxEhKoSg5/NXTmbPxhIq3di1MsllAkvkncixnZKQq6ZpeV13TLltJO
nwLJnWhQx0qMubqt5qRiofow8esNsYb2m6BGHaHg/hsKGWUoPcv0oNrSfsa8gd1u
SFSzz0jERebSGTePDxqORa9IEe+torcMpmc=
-----END CERTIFICATE-----
"""

MQTT_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAqhYQdrFKOaJdCclWoS4ytnpliEbh4rmBrq/RjtRoLN4aCy1W
lGDfbWx0BJ2isPYQvJdK66Kzl2200e3ooJbgpuhIEuW3Ll256rbW2lzi0k1S++h1
b/lduxX6p81aLgpMCoyZsBEHVBpEhzgWVW8JI2Cl0AggcwN9FrnXOyJJkWh43IIS
V8Lbpm2UavkMQ+IYYHqJBZ3fe+rsH9cUCD2T0go/Zgfmeawovw/A9sUysn7r7VoH
9lsk64JMa45ndXeTf5XU6zJ1TgsPq1eWrDeWWjGZ606bz/N5DthGNIzsXpIkiKBL
4fvlHFxhJo1n/Y2qNeHMNZgfa7ZJcUwNvaJw/QIDAQABAoIBAQCG+DdcWNfT4HoA
GBt8kBKCJ6KGf+kkZ5z3KGIc+4UnhaAZGoNH/4+NX7C5MPY3gyxI264CSvxEwDPr
GuWs+c2P5We8RzoTuyCblzfr1CXSSLX/XLpEfxfYLFrZ6eUT/+vTuzsCMqEkJiwX
OXTPmusffcRWzvwhCLWs4RBgxpamoXr1Ppk0W44pPoWZvLOeP7qVQ9jhbDHQmMDi
cZcAwhefG18hc0zW+oGvDSQRYph4UtLikZD+LDISG6kSkUBByTNWlLAkniqIZGPy
rwTQ/q8HIIIcnUTMWrgG0eLqGhUhjz88eGvBrdct77flQv0AXyGjoH8IwvoZq8LZ
abtkx9CBAoGBAN1rKYJJICnK0f9eDdy5VzFebpU2STzCqpxsVY+TCY/c3XyErVbk
gLTMCaUp2nplo2SvlO8G3zB3t46inFcaTs41GJC4uYfoeimOxDa65ALHDWqLBVfW
dK/JRnSzu8onWOhsqqd9kAI7qcYOtEHIgFrsD4eFlwzcQhinl7aQmEktAoGBAMSm
gt/YqVFh8IzoSK1bF8LDer9b9h4MXCzfm/lEam1QLWPhtL/pF4X6S5bXRUPobBEf
rmqpBwO68/x1C3M7/htO1zd9m6em4hbMv11iBlyj+AEeYv5SrH6onGSqpLyEuMDe
5IA4n3hL8ePziRQTa7aqy0t27DdtQMH07lpunwkRAoGACfJGaxPd3gK+bDpNZRzu
TclwLkPCBni4MU6siUaRp2TEjlNndf/NyFFiHYlDxzvJmzxH9HTakdLO7Blh7IfC
AoFgGSAzNWe8FSHUrqC2nWlTsPWNx+RaWYsxHwzz4qDh3Y8EG4IIdhE4Dy2Z61qW
aX8xM2VM48cBMRpWNl1IegECgYAkr3OG0uJzXjQD9Wlpfa7nFJSXkk5NuLyRWn28
eLjp/6UYFwkjLBbJVbI4R5ySWI+geiqNl07JsVzG4gbqzmxPJ9wabAJXulg/LJ8e
iqTpL2Wav9Jz43RuhIH4faURziixQmOaT/Xf+Tr87XfLGPxlLWOThnH2vRjxlgHJ
OQ3OIQKBgBO1sO2SPhybth+hp1swOesEjy90YZDRoFhJIlHuj5LNZSBRxbQVpylf
STzHNwjQN83ru4MPqj0OOqynNONrmCh+mpLxpbY7WosUyaCPsJ6i4GJzBAV3k1xL
VSIXKWqHXpN2gLHUqyfD38uVvNvE8tI7UU7xT0Tp2aXjo4Zf5ytB
-----END RSA PRIVATE KEY-----
"""

BINME_TYPE_HEADER = 0x01
BINME_TYPE_BODY = 0x02
BINME_FORMAT_JSON = 0x01


def encode_binme(header: dict, body: dict) -> bytes:
    buf = bytearray()
    for part_type, data in (
        (BINME_TYPE_HEADER, json.dumps(header).encode()),
        (BINME_TYPE_BODY, json.dumps(body).encode()),
    ):
        buf += bytes([part_type, BINME_FORMAT_JSON, 0, 0])
        buf += struct.pack(">I", len(data))
        buf += data
    return bytes(buf)


def decode_binme(payload: bytes) -> dict:
    pos, parts = 0, {}
    while pos + 8 <= len(payload):
        ptype, pfmt, compressed = payload[pos], payload[pos + 1], payload[pos + 2]
        pos += 4
        length = struct.unpack(">I", payload[pos : pos + 4])[0]
        pos += 4
        data = payload[pos : pos + length]
        pos += length
        if compressed:
            data = zlib.decompress(data)
        label = "header" if ptype == BINME_TYPE_HEADER else "body"
        if pfmt == BINME_FORMAT_JSON:
            try:
                parts[label] = json.loads(data.decode())
            except (ValueError, UnicodeDecodeError):
                parts[label] = f"<{len(data)} bytes, not JSON>"
        else:
            parts[label] = f"<{len(data)} bytes, format {pfmt}>"
    return parts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ip", help="Device IP address")
    parser.add_argument("--seconds", type=int, default=20, help="Listen duration")
    args = parser.parse_args()

    certdir = Path(tempfile.mkdtemp(prefix="uplay-dump-"))
    cert_file = certdir / "cert.pem"
    key_file = certdir / "key.pem"
    cert_file.write_text(MQTT_CERT)
    key_file.write_text(MQTT_KEY)

    client_uuid = uuid.uuid4().hex[:12]
    action_topic = f"UPL-MOB/{client_uuid}/action"
    info_sent = False

    def on_connect(c, userdata, flags, rc, properties=None):
        print(f"CONNECTED to {args.ip}:8883 rc={rc}")
        c.subscribe("#")

    def on_message(c, userdata, msg):
        parsed = decode_binme(msg.payload)
        header = parsed.get("header", {})
        name = "?"
        if isinstance(header, dict):
            name = header.get("name", header.get("action", "?"))
        retained = " [retained]" if msg.retain else ""
        print(f"\n--- {msg.topic}{retained} event={name} ---")
        print(json.dumps(parsed, indent=2, default=str))

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"uplay-dump-{client_uuid}"
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.tls_set(
        certfile=str(cert_file),
        keyfile=str(key_file),
        cert_reqs=ssl.CERT_NONE,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.tls_insecure_set(True)
    print(f"connecting to {args.ip}:8883 ...")
    client.connect(args.ip, 8883, 30)

    start = time.time()
    while time.time() - start < args.seconds:
        client.loop(0.5)
        if not info_sent and client.is_connected() and time.time() - start > 1:
            header = {
                "id": str(uuid.uuid4()),
                "type": "request",
                "timestamp": int(time.time() * 1000),
                "action": "info",
            }
            client.publish(action_topic, encode_binme(header, {}))
            print("\n>>> sent 'info' request")
            info_sent = True

    client.disconnect()
    print(f"\nDone ({args.seconds}s). Paste this whole output into the issue.")


if __name__ == "__main__":
    main()
