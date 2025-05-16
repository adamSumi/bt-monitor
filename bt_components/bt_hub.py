#! /usr/bin/env python3
import asyncio
import sys
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Callable, Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice as BleakBLEDevice # Alias for clarity
from bleak.backends.service import BleakGATTServiceCollection

if sys.version_info >= (3, 11):
    from asyncio import timeout
else:
    from asyncio_timeout import timeout # type: ignore

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration Constants (Optional: Move to a config file or class) ---
DEFAULT_SCAN_TIMEOUT_SEC = 5.0
DEFAULT_CONNECT_TIMEOUT_SEC = 10.0
DEFAULT_CONNECT_RETRIES = 3
DEFAULT_CONNECT_RETRY_DELAY_SEC = 2.0
EXAMPLE_TARGET_CHARACTERISTIC_UUID = "00002a37-0000-1000-8000-00805f9b34fb" # Example: Heart Rate Measurement

@dataclass
class ConnectedDevice:
    client: BleakClient
    name: Optional[str] = None
    # Store the actual BleakGATTServiceCollection once discovered
    services: Optional[BleakGATTServiceCollection] = None
    is_connected: bool = False
    # Using default_factory for mutable default like notify_callbacks
    notify_callbacks: Dict[str, Callable[[str, bytes], None]] = field(default_factory=dict)

@dataclass
class ScanResult:
    address: str
    name: Optional[str]
    rssi: int
    advertisement_data: Any # bleak.backends.scanner.AdvertisementData
    device_object: BleakBLEDevice # Store the actual Bleak device object

class BLEHub:
    def __init__(self):
        """
        Initialize the BLE Hub
        """
        self.connected_devices: Dict[str, ConnectedDevice] = {}
        self._disconnect_callbacks: Dict[str, Callable[[str], None]] = {}
        # Stores ScanResult objects, keyed by device address
        self._scan_results: Dict[str, ScanResult] = {}
        self._scan_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock() # Lock for connect/disconnect operations

    async def scan_devices(self, timeout_sec: float = DEFAULT_SCAN_TIMEOUT_SEC) -> List[Dict[str, Any]]:
        """
        Scan for nearby BLE devices.

        Args:
            timeout_sec: How long to scan (in seconds).

        Returns:
            List of discovered devices as dictionaries (for external use),
            but also populates internal _scan_results with richer ScanResult objects.
        """
        logger.info(f"Scanning for BLE devices for {timeout_sec} seconds...")
        async with self._scan_lock: # Ensure only one scan operation at a time
            self._scan_results.clear()
            discovered_devices_list: List[Dict[str, Any]] = []

            def detection_callback(device: BleakBLEDevice, advertisement_data: Any):
                if device.address not in self._scan_results: # Add only new devices during this scan pass
                    name = device.name or advertisement_data.local_name or "Unknown"
                    # Ensure RSSI is an int; provide a default if None (though Bleak usually provides int)
                    rssi_value = device.rssi if isinstance(device.rssi, int) else -127

                    scan_entry = ScanResult(
                        address=device.address,
                        name=name,
                        rssi=rssi_value,
                        advertisement_data=advertisement_data,
                        device_object=device
                    )
                    self._scan_results[device.address] = scan_entry
                    discovered_devices_list.append({
                        "address": device.address,
                        "name": name,
                        "rssi": scan_entry.rssi,
                    })
                    logger.debug(f"Discovered: {name} ({device.address}), RSSI: {scan_entry.rssi}")

            scanner = BleakScanner(detection_callback=detection_callback)
            try:
                await scanner.start()
                await asyncio.sleep(timeout_sec)
            except BleakError as e:
                logger.error(f"BleakError during scanning: {e}")
            except Exception as e:
                logger.error(f"Unexpected error during scanning: {e}")
            finally:
                try:
                    # Check if scanner has a backend and if it's scanning before stopping
                    if hasattr(scanner, '_backend') and scanner._backend and hasattr(scanner._backend, 'is_scanning') and scanner._backend.is_scanning:
                         await scanner.stop()
                    elif not hasattr(scanner, '_backend'): # For cases where scanner init might have failed partially
                        logger.warning("Scanner has no backend, stop might not be needed or possible.")
                except BleakError as e:
                     logger.error(f"BleakError stopping scanner: {e}")
                except AttributeError:
                    logger.warning("Scanner backend or scanning status not available for stop check (AttributeError).")


        logger.info(f"Scan complete. Found {len(self._scan_results)} unique BLE devices.")
        return discovered_devices_list

    async def connect_device(
        self,
        device_address: str,
        disconnect_callback: Optional[Callable[[str], None]] = None,
        timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
        retries: int = DEFAULT_CONNECT_RETRIES,
        retry_delay_sec: float = DEFAULT_CONNECT_RETRY_DELAY_SEC
    ) -> bool:
        """
        Connect to a BLE device with retries.
        """
        async with self._connection_lock:
            if device_address in self.connected_devices and self.connected_devices[device_address].is_connected:
                logger.warning(f"Already connected to {device_address}")
                return True

            scan_result_entry = self._scan_results.get(device_address)
            device_name = scan_result_entry.name if scan_result_entry else "Unknown"
            ble_device_identifier = scan_result_entry.device_object if scan_result_entry else device_address

            logger.info(f"Attempting to connect to {device_name} ({device_address}), {retries} retries...")

            client = BleakClient(ble_device_identifier)

            for attempt in range(retries):
                logger.info(f"Connection attempt {attempt + 1}/{retries} to {device_address}")
                try:
                    async with timeout(timeout_sec):
                        await client.connect()

                    self.connected_devices[device_address] = ConnectedDevice(
                        client=client,
                        name=device_name,
                        is_connected=True
                    )

                    if disconnect_callback:
                        self._disconnect_callbacks[device_address] = disconnect_callback

                    client.set_disconnected_callback(
                        lambda disconnected_client: self._on_device_disconnected(device_address, disconnected_client)
                    )

                    logger.info(f"Successfully connected to {device_name} ({device_address})")
                    return True
                except asyncio.TimeoutError:
                    logger.error(f"Connection to {device_address} timed out on attempt {attempt + 1} after {timeout_sec} seconds")
                except BleakError as e:
                    logger.error(f"BleakError on attempt {attempt + 1} for {device_address}: {str(e)}")
                except Exception as e:
                    logger.error(f"Unexpected error on attempt {attempt + 1} for {device_address}: {str(e)}")

                if attempt < retries - 1:
                    logger.info(f"Waiting {retry_delay_sec} seconds before next attempt...")
                    await asyncio.sleep(retry_delay_sec)
                else:
                    if client.is_connected:
                        logger.warning(f"Client for {device_address} still connected after failed attempts, forcing disconnect.")
                        try:
                            await client.disconnect()
                        except Exception as e_disc:
                            logger.error(f"Error forcing disconnect for {device_address} after failed connect: {e_disc}")


            logger.error(f"Failed to connect to {device_address} after {retries} attempts.")
            return False

    def _on_device_disconnected(self, device_address: str, client: Optional[BleakClient]): # Client can be None
        """
        Internal handler for device disconnection events from Bleak.
        """
        client_addr = client.address if client else "N/A"
        logger.info(f"Device {device_address} (client: {client_addr}) disconnected event received.")

        self._cleanup_disconnected_device_state(device_address)

        user_disconnect_callback = self._disconnect_callbacks.pop(device_address, None)
        if user_disconnect_callback:
            try:
                logger.info(f"Executing user disconnect callback for {device_address}.")
                user_disconnect_callback(device_address)
            except Exception as e:
                logger.error(f"Error in user disconnect callback for {device_address}: {str(e)}")

    def _cleanup_disconnected_device_state(self, device_address: str):
        """Helper to centralize state cleanup for a disconnected device."""
        connected_device_obj = self.connected_devices.pop(device_address, None)
        if connected_device_obj:
            connected_device_obj.is_connected = False
            connected_device_obj.notify_callbacks.clear()
            logger.debug(f"Internal state for {device_address} cleaned (connected_devices, notify_callbacks).")
        # User disconnect callback is handled by _on_device_disconnected or disconnect_device


    async def disconnect_device(self, device_address: str) -> bool:
        """
        Disconnect from a BLE device.
        """
        logger.info(f"Attempting to manually disconnect from {device_address}...")
        connected_device_obj = self.connected_devices.get(device_address)

        if not connected_device_obj:
            logger.warning(f"Device {device_address} not found in connected_devices for manual disconnect.")
            self._cleanup_disconnected_device_state(device_address) # Ensure full cleanup if somehow missed
            return True

        try:
            if connected_device_obj.client.is_connected:
                 await connected_device_obj.client.disconnect()
                 logger.info(f"Successfully sent disconnect command to {device_address}.")
            else:
                logger.info(f"Client for {device_address} was already disconnected (based on client.is_connected).")

            # This cleanup will be called. If Bleak's callback also fires, pop is safe.
            self._cleanup_disconnected_device_state(device_address)
            return True
        except BleakError as e:
            logger.error(f"BleakError manually disconnecting from {device_address}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error manually disconnecting from {device_address}: {str(e)}")

        logger.warning(f"Ensuring cleanup for {device_address} after manual disconnection error/attempt.")
        self._cleanup_disconnected_device_state(device_address)
        return False


    async def discover_services(self, device_address: str) -> Optional[BleakGATTServiceCollection]:
        """
        Discover services and characteristics for a connected device.
        """
        connected_device_obj = self.connected_devices.get(device_address)
        if not connected_device_obj or not connected_device_obj.is_connected:
            logger.error(f"Not connected to {device_address} for service discovery.")
            return None

        try:
            logger.info(f"Discovering services for {device_address}...")
            svcs = await connected_device_obj.client.get_services()
            connected_device_obj.services = svcs
            logger.info(f"Discovered {len(svcs) if svcs else 0} services for {device_address}.")
            return svcs
        except BleakError as e:
            logger.error(f"BleakError discovering services for {device_address}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error discovering services for {device_address}: {str(e)}")
        return None

    async def read_characteristic(
        self,
        device_address: str,
        characteristic_uuid: str
    ) -> Optional[bytes]:
        connected_device_obj = self.connected_devices.get(device_address)
        if not connected_device_obj or not connected_device_obj.is_connected:
            logger.error(f"Not connected to {device_address} for reading char {characteristic_uuid}.")
            return None
        try:
            value = await connected_device_obj.client.read_gatt_char(characteristic_uuid)
            logger.debug(f"Read from {device_address} char {characteristic_uuid}: {value.hex() if value else 'None'}")
            return value
        except BleakError as e:
            logger.error(f"BleakError reading char {characteristic_uuid} from {device_address}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error reading char {characteristic_uuid} from {device_address}: {str(e)}")
        return None

    async def write_characteristic(
        self,
        device_address: str,
        characteristic_uuid: str,
        data: bytes,
        response: bool = True
    ) -> bool:
        connected_device_obj = self.connected_devices.get(device_address)
        if not connected_device_obj or not connected_device_obj.is_connected:
            logger.error(f"Not connected to {device_address} for writing to char {characteristic_uuid}.")
            return False
        try:
            await connected_device_obj.client.write_gatt_char(characteristic_uuid, data, response=response)
            logger.debug(f"Wrote to {device_address} char {characteristic_uuid}: {data.hex()}")
            return True
        except BleakError as e:
            logger.error(f"BleakError writing to char {characteristic_uuid} on {device_address}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error writing to char {characteristic_uuid} on {device_address}: {str(e)}")
        return False

    async def start_notify(
        self,
        device_address: str,
        characteristic_uuid: str,
        callback: Callable[[str, bytes], None]
    ) -> bool:
        connected_device_obj = self.connected_devices.get(device_address)
        if not connected_device_obj or not connected_device_obj.is_connected:
            logger.error(f"Not connected to {device_address} for starting notify on {characteristic_uuid}.")
            return False

        try:
            connected_device_obj.notify_callbacks[characteristic_uuid] = callback

            def bleak_notification_handler(sender: Any, data: bytearray):
                try:
                    logger.debug(f"Notification: sender={sender}, data={data.hex()} for {device_address} char {characteristic_uuid}")
                    callback(device_address, bytes(data))
                except Exception as e_inner:
                    logger.error(f"Error in user notification callback for {characteristic_uuid} on {device_address}: {e_inner}")

            await connected_device_obj.client.start_notify(characteristic_uuid, bleak_notification_handler)
            logger.info(f"Started notifications for {characteristic_uuid} on {device_address}")
            return True
        except BleakError as e:
            logger.error(f"BleakError starting notify for {characteristic_uuid} on {device_address}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error starting notify for {characteristic_uuid} on {device_address}: {str(e)}")
        return False

    async def stop_notify(self, device_address: str, characteristic_uuid: str) -> bool:
        connected_device_obj = self.connected_devices.get(device_address)
        if not connected_device_obj or not connected_device_obj.is_connected:
            logger.warning(f"Not connected to {device_address}, cannot actively stop notify for {characteristic_uuid}.")
            if connected_device_obj and characteristic_uuid in connected_device_obj.notify_callbacks:
                del connected_device_obj.notify_callbacks[characteristic_uuid]
            return True

        try:
            await connected_device_obj.client.stop_notify(characteristic_uuid)
            if characteristic_uuid in connected_device_obj.notify_callbacks:
                del connected_device_obj.notify_callbacks[characteristic_uuid]
            logger.info(f"Stopped notifications for {characteristic_uuid} on {device_address}")
            return True
        except BleakError as e:
            logger.error(f"BleakError stopping notify for {characteristic_uuid} on {device_address}: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error stopping notify for {characteristic_uuid} on {device_address}: {str(e)}")
        return False

    async def shutdown(self):
        logger.info("Shutting down BLE Hub...")
        device_addresses_to_disconnect = list(self.connected_devices.keys())

        tasks = [self.disconnect_device(addr) for addr in device_addresses_to_disconnect]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for addr, result in zip(device_addresses_to_disconnect, results):
            if isinstance(result, Exception):
                logger.error(f"Exception during shutdown disconnect for {addr}: {result}")
            else:
                logger.info(f"Shutdown disconnect for {addr} completed with result: {result}")

        # Ensure all collections are clear after individual disconnections
        self.connected_devices.clear()
        self._disconnect_callbacks.clear()
        self._scan_results.clear()
        logger.info("BLE Hub shutdown complete.")


async def example_usage():
    hub = BLEHub()
    char_to_notify_uuid = EXAMPLE_TARGET_CHARACTERISTIC_UUID

    def robust_notification_handler(address: str, data: bytes):
        try:
            hex_data = data.hex()
            decoded_data_str = ""
            try:
                decoded_data_str = data.decode('utf-8')
                decoded_data_str = f"(UTF-8: '{decoded_data_str}')"
            except UnicodeDecodeError:
                decoded_data_str = "(Could not decode as UTF-8)"

            print(f"INFO: Notification from {address} | Char: {char_to_notify_uuid} | Data: {hex_data} {decoded_data_str}")
        except Exception as e:
            logger.error(f"Error in example_usage notification_handler for {address}: {e}")

    def example_disconnect_handler(address: str):
        print(f"INFO: Device {address} disconnected (handled by example_usage disconnect_handler)")

    try:
        print("--- Starting BLE Hub Example ---")
        devices_found_dicts = await hub.scan_devices(timeout_sec=DEFAULT_SCAN_TIMEOUT_SEC)

        if not devices_found_dicts:
            print("INFO: No devices found during scan.")
            return

        print("--- Discovered Devices ---")
        for i, device_dict in enumerate(devices_found_dicts):
            print(f"  {i}: {device_dict.get('name')} ({device_dict.get('address')}) RSSI: {device_dict.get('rssi')}")

        target_device_dict = devices_found_dicts[0]
        target_address = target_device_dict['address']
        target_name = target_device_dict['name']
        print(f"\n--- Attempting to connect to: {target_name} ({target_address}) ---")

        if await hub.connect_device(
            target_address,
            disconnect_callback=example_disconnect_handler,
        ):
            print(f"INFO: Successfully connected to {target_name} ({target_address})")

            services_collection = await hub.discover_services(target_address)
            if services_collection:
                print(f"\n--- Services on {target_name} ({target_address}) ---")
                for service in services_collection:
                    print(f"  Service: {service.uuid} (Handle: {service.handle})")
                    for char in service.characteristics:
                        print(f"    Characteristic: {char.uuid} (Handle: {char.handle}), Properties: {char.properties}")

                readable_char = next((char for service in services_collection for char in service.characteristics if "read" in char.properties), None)

                if readable_char:
                    print(f"\n--- Reading from characteristic: {readable_char.uuid} ---")
                    value = await hub.read_characteristic(target_address, readable_char.uuid)
                    print(f"  Read value: {value.hex() if value else 'N/A'} (Decoded: '{value.decode(errors='ignore') if value else ''}')")
                else:
                    print("\nINFO: No readable characteristic found.")

                characteristic_to_notify_obj = services_collection.get_characteristic(char_to_notify_uuid)

                if characteristic_to_notify_obj and "notify" in characteristic_to_notify_obj.properties:
                    print(f"\n--- Starting notifications for: {char_to_notify_uuid} ---")
                    if await hub.start_notify(target_address, char_to_notify_uuid, robust_notification_handler):
                        print(f"  Notifications started. Waiting for 10 seconds...")
                        await asyncio.sleep(10)
                        print(f"\n--- Stopping notifications for {char_to_notify_uuid} ---")
                        await hub.stop_notify(target_address, char_to_notify_uuid)
                    else:
                        print(f"  Failed to start notifications for {char_to_notify_uuid}")
                else:
                    print(f"\nINFO: Characteristic {char_to_notify_uuid} not found or does not support 'notify'.")
            else:
                print(f"ERROR: Could not discover services for {target_address}")

            print(f"\n--- Disconnecting from {target_name} ({target_address}) ---")
            await hub.disconnect_device(target_address)
        else:
            print(f"ERROR: Could not connect to {target_name} ({target_address}) after retries.")

    except BleakError as e:
        logger.error(f"A BleakError occurred in example_usage: {str(e)}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in example_usage: {str(e)}", exc_info=True)
    finally:
        print("\n--- Shutting down BLE Hub Example ---")
        await hub.shutdown()
        print("--- Example Finished ---")

if __name__ == "__main__":
    try:
        asyncio.run(example_usage())
    except KeyboardInterrupt:
        logger.info("Program terminated by user (KeyboardInterrupt).")
        print("\nProgram terminated by user.") # Also print to console for immediate feedback
    except Exception as e:
        logger.critical(f"Unhandled exception in __main__: {e}", exc_info=True)
        print(f"CRITICAL ERROR in main: {e}")