# NRF-INTERCOM

A battery-powered BLE module on an apartment intercom handset: it detects
ringing and, per a mode set from Home Assistant, does nothing, rejects the call
or opens the door. Firmware, wiring and protocol: [`firmware/README.md`](firmware/README.md).

## Home Assistant integration

Install with [HACS](https://hacs.xyz): *HACS → ⋮ → Custom repositories*, add
`https://github.com/n0n3m4/nrf-intercom` as an *Integration*, install
**BLE Intercom**, restart. The module is discovered over Bluetooth (a local
adapter or an ESPHome Bluetooth proxy); otherwise add it from
*Settings → Devices & services → Add integration → BLE Intercom*.

| Entity | What it is |
|---|---|
| Mode (select) | Do nothing / Reject calls / Open the door / Open for a while / Open a number of times |
| Allow for, Allow rings (number) | Length and count used when a timed mode is picked |
| Ringing (binary sensor) | On while the ring lasts |
| Doorbell (event) | `ring`, `opened`, `denied` — one per occurrence, even several in a second |
| Battery, Battery voltage (sensor) | % (linear 3.3–4.2 V) and mV |

The mode select shows what the module reports. The module boots in "Do nothing"
and keeps no settings, so Home Assistant remembers the chosen mode and writes it
back whenever the module disagrees — after a reboot, or after a battery swap.
A timed mode runs down on the module itself and falls back to "Do nothing",
also while Home Assistant is down.

State and events come from passive adverts; the only connection is the short
write that sets the mode.

Example automation (rename the entity to match your device):

```yaml
triggers:
  - trigger: state
    entity_id: event.ble_intercom_doorbell
conditions:
  - condition: state
    entity_id: event.ble_intercom_doorbell
    attribute: event_type
    state: ring
actions:
  - action: notify.notify
    data:
      message: Someone is at the door
```

Self-check of the protocol and mode logic, no Home Assistant needed:

```sh
python3 custom_components/nrf_intercom/protocol.py
```
