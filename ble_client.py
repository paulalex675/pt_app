"""BLE connection to a Polar H10 (or any standard BLE Heart Rate Monitor).

Uses the standard GATT Heart Rate Service (0x180D) / Heart Rate Measurement
characteristic (0x2A37), which the H10 implements alongside Polar's own
proprietary streaming protocol. This gives HR + RR intervals, which is
enough for zone tracking; move to Polar's PMD service later if raw ECG is
ever needed.
"""
import asyncio
import json
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


async def scan_for_straps(timeout: float = 5.0) -> list[BLEDevice]:
    """Scan for nearby devices advertising the Heart Rate service."""
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    straps = []
    for device, adv in devices.values():
        if HR_SERVICE_UUID in (adv.service_uuids or []):
            straps.append(device)
        elif device.name and "polar" in device.name.lower():
            straps.append(device)
    return straps


def _parse_hr_measurement(data: bytes) -> tuple[int, list[float]]:
    """Parses the Heart Rate Measurement characteristic per the BLE spec.

    Returns (heart_rate_bpm, rr_intervals_ms).
    """
    flags = data[0]
    hr_16bit = flags & 0x01
    rr_present = flags & 0x10

    offset = 1
    if hr_16bit:
        hr = int.from_bytes(data[offset:offset + 2], "little")
        offset += 2
    else:
        hr = data[offset]
        offset += 1

    # Energy expended field, if present, sits between HR and RR intervals.
    energy_present = flags & 0x08
    if energy_present:
        offset += 2

    rr_intervals = []
    if rr_present:
        while offset + 1 < len(data):
            raw = int.from_bytes(data[offset:offset + 2], "little")
            rr_intervals.append(round(raw / 1024.0 * 1000, 1))  # spec: 1/1024s units -> ms
            offset += 2

    return hr, rr_intervals


class HeartRateStream:
    """Manages one live BLE connection and forwards samples to a callback.

    on_sample(hr: int, rr_intervals_ms: list[float]) is called on every
    notification from the strap. Runs inside the asyncio loop bleak needs,
    so the caller should schedule it with asyncio.create_task or run it in
    a dedicated thread with its own loop (see main.py for the Flet wiring).
    """

    def __init__(self, device_address: str, on_sample: Callable[[int, list[float]], None]):
        self.device_address = device_address
        self.on_sample = on_sample
        self._client: BleakClient | None = None
        self._connected = False

    def _handle_notification(self, _sender, data: bytearray):
        hr, rr = _parse_hr_measurement(bytes(data))
        self.on_sample(hr, rr)

    async def connect(self):
        self._client = BleakClient(self.device_address)
        await self._client.connect()
        await self._client.start_notify(HR_MEASUREMENT_UUID, self._handle_notification)
        self._connected = True

    async def disconnect(self):
        if self._client and self._connected:
            await self._client.stop_notify(HR_MEASUREMENT_UUID)
            await self._client.disconnect()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and (self._client.is_connected if self._client else False)


def rr_to_json(rr_intervals: list[float]) -> str | None:
    return json.dumps(rr_intervals) if rr_intervals else None
