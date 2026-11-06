#!/usr/bin/env python3
"""Example: Connect to PdaNet USB or Wi-Fi using the Python API."""

import sys
from pdanet_linux.connection import ConnectOptions, PdaNetConnection


def main():
    # Example options for USB tethering (or Wi-Fi by setting mode='wifi', host='192.168.49.1')
    options = ConnectOptions(
        mode="usb",
        model="Linux Desktop",
        route_all=True,
        privilege="auto",  # pkexec/sudo for TUN helper
        verbose=False,
        log=lambda msg: print(f"[pdanet] {msg}"),
    )

    conn = PdaNetConnection(options)
    print("Connecting to PdaNet...")
    try:
        conn.open()
        print(f"Connected: {conn.description} (TUN: {conn.tun_name})")
        print("Press Ctrl+C to stop.")
        conn.run(on_stats=lambda stats: print(stats.summary()))
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        conn.close()
        print("Closed.")


if __name__ == "__main__":
    main()

