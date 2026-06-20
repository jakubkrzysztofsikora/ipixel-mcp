# Exécuter le serveur WebSocket

## Démarrer le serveur WebSocket

Un serveur WebSocket est inclus pour permettre le contrôle en temps réel de votre appareil. Pour démarrer le serveur, utilisez la commande suivante :

```bash
python -m pypixelcolor.websocket -a <MAC_ADDRESS>
```

Par défaut, le serveur écoute sur `localhost:4444`. Vous pouvez spécifier un hôte et un port différents en utilisant les options `--host` et `--port` :

```bash
python -m pypixelcolor.websocket -a <MAC_ADDRESS> --host 0.0.0.0 --port 4444
```

## Envoyer des commandes via WebSocket

En utilisant un client WebSocket (par exemple, [WebSocket King](https://websocketking.com/)), connectez-vous au serveur à l'hôte et au port spécifiés (par défaut `ws://localhost:4444`).
Une fois connecté, vous pouvez envoyer des commandes au format JSON. Par exemple, pour envoyer un message texte avec des paramètres d'animation et de vitesse, vous pouvez utiliser la charge utile JSON suivante :

```json
{
  "command": "send_text",
  "params": [
    "text=Bonjour depuis WebSocket",
    "animation=1",
    "speed=100"
  ]
}
```

Pour plus d'informations sur les commandes disponibles, consultez la page [Commandes](../commands/content.fr.md).
