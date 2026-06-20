# Running WebSocket Server

## Starting the WebSocket server

A WebSocket server is included to allow real-time control of your device. To start the server, use the following command:

```bash
python -m pypixelcolor.websocket -a <MAC_ADDRESS>
```

By default, the server listens on `localhost:4444`. You can specify a different host and port using the `--host` and `--port` options:

```bash
python -m pypixelcolor.websocket -a <MAC_ADDRESS> --host 0.0.0.0 --port 4444
```

## Sending commands via WebSocket

Using a WebSocket client (for example, [WebSocket King](https://websocketking.com/)), connect to the server at the specified host and port (by default `ws://localhost:4444`).
Once connected, you can send commands in JSON format. For example, to send a text message with animation and speed settings, you can use the following JSON payload:

```json
{
  "command": "send_text",
  "params": [
    "text=Hello from WebSocket",
    "animation=1",
    "speed=100"
  ]
}
```

For more information on available commands, refer to the [Commands](../commands/content.md) page.
