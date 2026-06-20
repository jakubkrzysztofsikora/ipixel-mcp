# Installation

## Prérequis

Avant d'installer `pypixelcolor`, assurez-vous d'avoir les prérequis suivants :

- Python 3.10 ou supérieur
- pip (installateur de paquets Python)
- Adaptateur Bluetooth sur votre machine

## Installation via pip

Vous pouvez installer `pypixelcolor` en utilisant pip. Ouvrez votre terminal et exécutez la commande suivante :

```bash
pip install pypixelcolor
```

Cette commande téléchargera et installera la dernière version de `pypixelcolor` ainsi que ses dépendances.

Pour vérifier que l'installation a réussi, vous pouvez vérifier la version de `pypixelcolor` installée en exécutant :

```bash
pypixelcolor --version
```

## Installation depuis la source

Si vous préférez installer `pypixelcolor` à partir du code source, suivez ces étapes :

- Clonez le dépôt depuis GitHub :

  ```bash
  git clone https://github.com/lucagoc/pypixelcolor.git
  ```

- Naviguez vers le répertoire cloné :

  ```bash
  cd pypixelcolor
  ```

- Installez le paquet en utilisant pip :

  ```bash
  pip install .
  ```

## Post-installation

Après l'installation, vous voudrez peut-être configurer votre adaptateur Bluetooth pour vous assurer qu'il fonctionne correctement avec `pypixelcolor`. Assurez-vous que votre Bluetooth est activé et que votre appareil est détectable.
Vous pouvez maintenant commencer à utiliser `pypixelcolor` pour contrôler vos appareils iPixel Color LED matrix !

[Utilisation du CLI](cli.fr.md){ .md-button .md-button--primary }
[Utilisation de WebSocket](websocket.fr.md){ .md-button .md-button--primary }
[Utilisation de la bibliothèque Python](library.fr.md){ .md-button .md-button--primary }
