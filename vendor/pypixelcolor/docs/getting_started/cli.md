# Getting started with CLI

## Scanning for devices

Find your device's MAC address by scanning for nearby Bluetooth devices:

```bash
pypixelcolor --scan
```

![Scan for devices](../assets/gifs/scan.gif)

If your device is found, take note of its MAC address (e.g., `30:E1:AF:BD:5F:D0`).

```txt
% pypixelcolor --scan
ℹ️ [2025-11-18 21:07:35] [pypixelcolor.cli] Scanning for Bluetooth devices...
ℹ️ [2025-11-18 21:07:40] [pypixelcolor.cli] Found 1 device(s):
ℹ️ [2025-11-18 21:07:40] [pypixelcolor.cli]   - LED_BLE_E1BD5C80 (30:E1:AF:BD:5F:D0)
```

> If your device is not found, ensure it is powered, in range and not connected to another device.

See [troubleshooting](../troubleshooting/bluetooth_connection.md) for more help.

## Sending a command

CLI commands are sent using the `-c` option, along with the `-a` or `--address` option to specify the target device's MAC address.

For instance, to send a text message to your device, use the following command, replacing the MAC address with your device's MAC address:

```bash
pypixelcolor -a <MAC_ADDRESS> -c send_text "Hello pypixelcolor"
```

You can also add optional parameters to customize the display:

```bash
pypixelcolor -a <MAC_ADDRESS> -c send_text "Hello pypixelcolor" animation=1 speed=100
```

For more information on available commands, refer to the [Commands](../commands/content.md) page.

## Advanced usage

You can execute multiple commands in a single call. For example, to clear the display, set the brightness to 0, and switch to clock mode, you can run:

```bash
pypixelcolor -a <MAC_ADDRESS> -c clear -c set_brightness 0 -c set_clock_mode
```

