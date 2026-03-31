# UniFi Play for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for **UniFi Play** devices (PowerAmp, In-Wall, etc.) managed by a UniFi OS Console (UDM Pro, Cloud Gateway, etc.).

## Features

- **Media player entity** for each UniFi Play device
- **Real-time state** via direct MQTT connection to devices
- **Volume control** (set, step, mute)
- **Source selection** (Line In, Bluetooth, AirPlay, Spotify, HDMI eARC, Optical)
- **Now-playing metadata** (song, artist, album) when streaming
- **Device info** (firmware, EQ, loudness, balance, volume limit)

## Requirements

- A UniFi OS Console (UDM Pro, UDM SE, Cloud Gateway Ultra, etc.)
- One or more UniFi Play devices (PowerAmp, In-Wall) on the same network
- An API key from UniFi OS Settings

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add `https://github.com/willbeeching/ha-unifiplay` as an **Integration**
4. Install **UniFi Play**
5. Restart Home Assistant

### Manual

Copy the `custom_components/unifi_play` folder into your Home Assistant `config/custom_components/` directory and restart.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **UniFi Play**
3. Enter your UDM Pro IP address (e.g. `10.0.0.1`) and API key
4. Devices will be discovered automatically

## How it works

The integration uses two communication channels:

- **REST API** on the UDM Pro (`/proxy/apollo/api/v1/`) for device discovery
- **MQTT** (port 8883, TLS) directly to each device for real-time state updates and control

All communication stays local on your network.
