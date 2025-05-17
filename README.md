# bt-monitor
General bluetooth communication code, alongside basic Raspberry Pi sensor monitoring

## Peripheral Setup
- bt_beacon.py script
- Meant to run on a Raspberry Pi (Zero 2W used in testing)

### Dependency Installs
```bash
# Running on RPi
# Start off with system update
sudo apt update
sudo apt full-upgrade -y

# System installs
sudo apt install bluetooth bluez libbluetooth-dev -y
sudo apt install libdbus-1-dev python3-dbus -y
sudo apt install build-essential pkg-config cmake libcairo2-dev libgirepository1.0-dev python3-dev -y

# Starting up/Enabling (start on boot) bluetooth/BlueZ
sudo systemctl enable bluetooth.service
sudo systemctl start bluetooth.service

# Recommended: Set config to LE only
sudo nano /etc/bluetooth/main.conf
...
ControllerMode = le
...

Save & exit nano

sudo systemctl restart bluetooth.service

# Check bluetooth active status -- active: running

# Setting custom hostname (Bluetooth alias)
sudo raspi-config
    -> System Options -> Hostname # Method 1

sudo hostnamectl set-hostname YourDesiredHostname # Method 2

sudo reboot

Activate/Create your venv
#Creating venv in ~/
cd ~
python3 -m venv <rpi_venv>
source <rpi_venv>/bin/activate

# Pip Installs
pip install bluezero dbus-python

# Running beacon script
./bt_beacon.py
    <--uuid, generate a UUID instead of beginning beacon>
```

## Central Hub Setup
- bt_hub.py script
- Meant to run on a Raspberry Pi (testing TBD) OR macOS (tested)

### Dependency Installs
```bash
# Running on macOS OR RPi

# IF RUNNING ON RPi:
# Start off with system update
sudo apt update
sudo apt full-upgrade -y

# System installs
sudo apt install bluetooth bluez libbluetooth-dev -y
sudo apt install libdbus-1-dev python3-dbus -y
sudo apt install build-essential pkg-config cmake libcairo2-dev libgirepository1.0-dev python3-dev -y

# Starting up/Enabling (start on boot) bluetooth/BlueZ
sudo systemctl enable bluetooth.service
sudo systemctl start bluetooth.service


Activate/Create your venv
#Creating venv in ~/
cd ~
python3 -m venv <rpi_venv>
source <rpi_venv>/bin/activate

# Pip Installs
pip install bleak # Optionally, asynio if not already built-in
# Running hub script
./bt_hub.py
```