# Installation

## Prerequisites

Before installing `pypixelcolor`, ensure you have the following prerequisites:

- Python 3.10 or higher
- pip (Python package installer)
- Bluetooth adapter on your machine

## Installation via pip

You can install `pypixelcolor` using pip. Open your terminal and run the following command:

```bash
pip install pypixelcolor
```

This command will download and install the latest version of `pypixelcolor` along with its dependencies.

To verify that the installation was successful, you can check the version of `pypixelcolor` installed by running:

```bash
pypixelcolor --version
```

## Installation from source

If you prefer to install `pypixelcolor` from the source code, follow these steps:

- Clone the repository from GitHub:

  ```bash
  git clone https://github.com/lucagoc/pypixelcolor.git
  ```

- Navigate to the cloned directory:

  ```bash
  cd pypixelcolor
  ```

- Install the package using pip:

  ```bash
  pip install .
  ```

## Post-installation

After installation, you may want to set up your Bluetooth adapter to ensure it works correctly with `pypixelcolor`. Make sure your Bluetooth is enabled and that your device is discoverable.
You can now start using `pypixelcolor` to control your iPixel Color LED matrix devices!

[Using CLI](cli.md){ .md-button .md-button--primary }
[Using WebSocket](websocket.md){ .md-button .md-button--primary }
[Using Python library](library.md){ .md-button .md-button--primary }