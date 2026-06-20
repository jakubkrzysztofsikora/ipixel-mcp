# Bien démarrer avec le CLI

## Scanner les appareils

Trouvez l'adresse MAC de votre appareil en scannant les appareils Bluetooth à proximité :

```bash
pypixelcolor --scan
```

![Scan for devices](../assets/gifs/scan.gif)

Si votre appareil est trouvé, notez son adresse MAC (par exemple, `30:E1:AF:BD:5F:D0`).

```txt
% pypixelcolor --scan
ℹ️ [2025-11-18 21:07:35] [pypixelcolor.cli] Scanning for Bluetooth devices...
ℹ️ [2025-11-18 21:07:40] [pypixelcolor.cli] Found 1 device(s):
ℹ️ [2025-11-18 21:07:40] [pypixelcolor.cli]   - LED_BLE_E1BD5C80 (30:E1:AF:BD:5F:D0)
```

> Si votre appareil n'est pas trouvé, assurez-vous qu'il est allumé, à portée et non connecté à un autre appareil.

Voir [dépannage](../troubleshooting/bluetooth_connection.fr.md) pour plus d'aide.

## Envoyer une commande

Les commandes CLI sont envoyées en utilisant l'option `-c`, avec l'option `-a` ou `--address` pour spécifier l'adresse MAC de l'appareil cible.

Par exemple, pour envoyer un message texte à votre appareil, utilisez la commande suivante, en remplaçant l'adresse MAC par celle de votre appareil :

```bash
pypixelcolor -a <MAC_ADDRESS> -c send_text "Bonjour pypixelcolor"
```

Vous pouvez également ajouter des paramètres optionnels pour personnaliser l'affichage :

```bash
pypixelcolor -a <MAC_ADDRESS> -c send_text "Bonjour pypixelcolor" animation=1 speed=100
```

Pour plus d'informations sur les commandes disponibles, consultez la page [Commandes](../commands/content.fr.md).

## Utilisation avancée

Vous pouvez exécuter plusieurs commandes en un seul appel. Par exemple, pour effacer l'affichage, régler la luminosité à 0 et passer en mode horloge, vous pouvez exécuter :

```bash
pypixelcolor -a <MAC_ADDRESS> -c clear -c set_brightness 0 -c set_clock_mode
```
