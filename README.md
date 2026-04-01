# UniFi Play for Home Assistant

[![CI](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-unifiplay/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-unifiplay?include_prereleases)](https://github.com/willbeeching/ha-unifiplay/releases)
[![License](https://img.shields.io/github/license/willbeeching/ha-unifiplay)](LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)

A Home Assistant custom integration for **UniFi Play** devices (PowerAmp, In-Wall, etc.) managed by a UniFi OS Console (UDM Pro, Cloud Gateway, etc.).

## Features

- **Media player** — volume, mute, source select, now-playing metadata
- **Switches** — loudness, equalizer, subwoofer
- **Number controls** — balance, volume limit, screen & LED brightness
- **Buttons** — locate (flash LEDs), restart
- **Sensors** — firmware update status
- **Real-time state** via direct MQTT connection to each device
- **Source selection** — Line In, Bluetooth, AirPlay, Spotify, HDMI eARC, Optical

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
