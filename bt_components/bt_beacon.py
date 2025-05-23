#! /usr/bin/env python3
import uuid
import argparse
import logging
import socket
import struct # For packing integers into bytes

from dataclasses import dataclass
from bluezero import adapter
from bluezero import peripheral

# --- Configuration ---
# Define your custom UUIDs
MY_SERVICE_UUID = 'da28c736-042f-4b45-bfb8-265185ce2cbb'
MY_CHARACTERISTIC_UUID = '1dc7211f-fd34-43e1-ade8-4b9544c9d999'

@dataclass
class SensorInfo:
    sensor_name: str
    air_temp: float
    humidity: float
    soil_moisture: int

test_data = SensorInfo(sensor_name="plant_1",
                        air_temp=78.2,
                        humidity=54.3,
                        soil_moisture=65)
# --- Logging Setup ---
# You can enable debug logging for bluezero if needed
# logging.basicConfig(level=logging.DEBUG)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MyBeacon:
    def __init__(self, name=socket.gethostname()):
        # Get the Bluetooth adapter
        try:
            self.dongle = adapter.Adapter() # Gets the first available adapter, e.g., hci0
            logger.info(f"Using adapter: {self.dongle.address}")
        except Exception as e:
            logger.error(f"Error initializing Bluetooth adapter: {e}")
            logger.error("Ensure Bluetooth is enabled and accessible. Try 'rfkill unblock bluetooth'.")
            exit(1)

        # Create a peripheral object
        self.local_name = name
        self.ble_peripheral = peripheral.Peripheral(self.dongle.address, local_name=self.local_name)

        # Add your custom service
        self.ble_peripheral.add_service(
            srv_id=1, # An arbitrary ID for this service within this peripheral
            uuid=MY_SERVICE_UUID,
            primary=True
        )

        # Add your custom characteristic to the service
        # This characteristic will be readable and support notifications
        self.ble_peripheral.add_characteristic(
            srv_id=1, # Must match the srv_id of the service it belongs to
            chr_id=1, # An arbitrary ID for this characteristic within this service
            uuid=MY_CHARACTERISTIC_UUID,
            value=[], # Initial value (empty or placeholder)
            notifying=False, # Start with notifications off
            flags=['read', 'notify'],
            read_callback=self.read_data_callback,
            write_callback=None, # No write functionality needed for this example
            notify_callback=self.notify_data_callback
        )

        self.data_to_send_bytes = self.pack_values(test_data)
        logger.info(f"Prepared data: {test_data} as bytes: {self.data_to_send_bytes.hex()}")

    def pack_values(self, sensor_info: SensorInfo):
        """
        Packs a list of 3 integers into a byte array.
        Using <h for signed short (2 bytes each), little-endian.
        Adjust format string if different integer sizes/types are needed.
        Example: 3 shorts = 6 bytes.
        """
        # '<' for little-endian, 'h' for short (2 bytes). Use 'i' for 4-byte int etc.
        # This will create a 6-byte array for three 2-byte integers.
        try:
            pack_format = f'<ffH{len(sensor_info.sensor_name)}s'
            return struct.pack(pack_format,
                                sensor_info.air_temp,
                                sensor_info.humidity,
                                sensor_info.soil_moisture,
                                sensor_info.sensor_name.encode('utf-8'))
        except struct.error as e:
            logger.error(f"Error packing values {sensor_info}: {e}")
            logger.error("Ensure integers are within the range of a signed short (-32768 to 32767).")
            # Fallback or re-raise
            return b'\x00\x00\x00\x00\x00\x00' # Default 6-byte zero value

    def read_data_callback(self):
        """
        Called when a client reads the characteristic.
        Returns the current canned data.
        """
        logger.info("Read request received. Sending canned data.")
        return self.data_to_send_bytes

    def notify_data_callback(self, notifying, characteristic):
        """
        Called when a client subscribes or unsubscribes to notifications.
        'notifying' is True if a client started notifications, False otherwise.
        """
        if notifying:
            logger.info("Client subscribed for notifications. Sending canned data.")
            # When a client subscribes, update the characteristic's value.
            # bluezero will then send the notification with this new value.
            characteristic.set_value(self.data_to_send_bytes)
        else:
            logger.info("Client unsubscribed from notifications.")

    def start(self):
        """
        Starts the BLE peripheral advertising and event loop.
        """
        logger.info(f"Starting BLE peripheral '{self.local_name}'...")
        logger.info(f"  Service UUID: {MY_SERVICE_UUID}")
        logger.info(f"  Characteristic UUID: {MY_CHARACTERISTIC_UUID} (for sensor data)")
        logger.info("Advertising started. Waiting for connections...")
        self.ble_peripheral.publish() # This registers services and starts advertising/processing connections
        logger.info("Peripheral stopped.")


parser = argparse.ArgumentParser()
parser.add_argument('--uuid', action='store_true', help="Generate a random UUID")

def generate_uuid():
    return uuid.uuid4()

if __name__ == "__main__":
    args = parser.parse_args()
    if args.uuid:
        print(generate_uuid())
    else:
        my_beacon_app = MyBeacon()
        my_beacon_app.start()