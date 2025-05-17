#! /usr/bin/env python3
import asyncio
import sys
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Callable, Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice as BleakBLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTServiceCollection, BleakGATTCharacteristic

if sys.version_info >= (3, 11):
    from asyncio import timeout
else:
    from asyncio_timeout import timeout # type: ignore

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration Constants ---
DEFAULT_SCAN_TIMEOUT_SEC = 5.0
DEFAULT_CONNECT_TIMEOUT_SEC = 15.0
DEFAULT_CONNECT_RETRIES = 3
DEFAULT_CONNECT_RETRY_DELAY_SEC = 3.0
EXAMPLE_TARGET_CHARACTERISTIC_UUID = '1dc7211f-fd34-43e1-ade8-4b9544c9d999'

@dataclass
class ConnectedDevice:
    client: BleakClient
    name: Optional[str] = None
    services: Optional[BleakGATTServiceCollection] = None
    is_connected: bool = False
    notify_callbacks: Dict[str, Callable[[str, bytes], None]] = field(default_factory=dict)

@dataclass
class ScanResult:
    address: str
    name: Optional[str]
    rssi: int
    advertisement_data: AdvertisementData
    device_object: BleakBLEDevice

class BLEHub:
    def __init__(self):
        self.connected_devices: Dict[str, ConnectedDevice] = {}
        self._disconnect_callbacks: Dict[str, Callable[[str], None]] = {}
        self._scan_results: Dict[str, ScanResult] = {}
        self._scan_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()

    async def scan_devices(self, timeout_sec: float = DEFAULT_SCAN_TIMEOUT_SEC) -> List[Dict[str, Any]]:
        logger.info(f"Scanning for BLE devices for {timeout_sec} seconds...")
        async with self._scan_lock:
            self._scan_results.clear()
            discovered_devices_list: List[Dict[str, Any]] = []

            def detection_callback(device: BleakBLEDevice, advertisement_data: AdvertisementData):
                if device.address not in self._scan_results:
                    name = device.name or advertisement_data.local_name or "Unknown"
                    rssi_value = advertisement_data.rssi if advertisement_data.rssi is not None else \
                                 (device.rssi if isinstance(device.rssi, int) else -127)

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
            finally:
                try:
                    if hasattr(scanner, '_backend') and scanner._backend and hasattr(scanner._backend, 'is_scanning') and scanner._backend.is_scanning:
                        await scanner.stop()
                except BleakError as e:
                    logger.error(f"BleakError stopping scanner: {e}")
                except AttributeError:
                    logger.warning("Scanner backend or scanning attribute not found, stop might not be needed.")

        logger.info(f"Scan complete. Found {len(self._scan_results)} unique BLE devices.")
        return discovered_devices_list

    async def connect_device(
        self,
        device_address: str,
        user_disconnect_callback: Optional[Callable[[str], None]] = None,
        timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
        retries: int = DEFAULT_CONNECT_RETRIES,
        retry_delay_sec: float = DEFAULT_CONNECT_RETRY_DELAY_SEC
    ) -> bool:
        async with self._connection_lock:
            if device_address in self.connected_devices and self.connected_devices[device_address].is_connected:
                logger.warning(f"Already connected to {device_address}")
                return True

            scan_result_entry = self._scan_results.get(device_address)
            device_name = scan_result_entry.name if scan_result_entry else "Unknown"
            ble_device_identifier = scan_result_entry.device_object if scan_result_entry else device_address

            logger.info(f"Attempting to connect to {device_name} ({device_address}), {retries} retries...")

            def internal_bleak_disconnect_callback(client_instance: BleakClient):
                self._on_device_disconnected(device_address, client_instance)

            client = BleakClient(ble_device_identifier, disconnected_callback=internal_bleak_disconnect_callback)

            for attempt in range(retries):
                logger.info(f"Connection attempt {attempt + 1}/{retries} to {device_address}")
                try:
                    async with timeout(timeout_sec):
                        await client.connect()

                    # Services are discovered during connect, store them now.
                    discovered_services = client.services
                    self.connected_devices[device_address] = ConnectedDevice(
                        client=client,
                        name=device_name,
                        is_connected=True,
                        services=discovered_services # Store discovered services
                    )
                    if user_disconnect_callback:
                        self._disconnect_callbacks[device_address] = user_disconnect_callback

                    logger.info(f"Successfully connected to {device_name} ({device_address})")
                    num_services = len(discovered_services.services) if discovered_services and hasattr(discovered_services, 'services') else 0
                    logger.info(f"Discovered {num_services} services for {device_name} upon connection.")
                    return True
                except asyncio.TimeoutError:
                    logger.error(f"Connection to {device_address} timed out on attempt {attempt + 1}")
                except BleakError as e:
                    logger.error(f"BleakError on attempt {attempt + 1} for {device_address}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error on attempt {attempt + 1} for {device_address}: {e}")

                if attempt < retries - 1:
                    await asyncio.sleep(retry_delay_sec)
                else:
                    if client.is_connected: # Should ideally not be needed if connect failed
                        logger.warning(f"Client for {device_address} still connected after failed attempts, forcing disconnect.")
                        try:
                            await client.disconnect()
                        except Exception as e_disc:
                            logger.error(f"Error forcing disconnect for {device_address}: {e_disc}")

            logger.error(f"Failed to connect to {device_address} after {retries} attempts.")
            return False

    def _on_device_disconnected(self, device_address: str, client: Optional[BleakClient]):
        client_addr = client.address if client else "N/A (client object None)"
        logger.info(f"Device {device_address} (client: {client_addr}) disconnected event received.")
        self._cleanup_disconnected_device_state(device_address)

        user_callback = self._disconnect_callbacks.pop(device_address, None)
        if user_callback:
            try:
                user_callback(device_address)
            except Exception as e:
                logger.error(f"Error in user disconnect callback for {device_address}: {e}")

    def _cleanup_disconnected_device_state(self, device_address: str):
        connected_device_obj = self.connected_devices.pop(device_address, None)
        if connected_device_obj:
            connected_device_obj.is_connected = False
            connected_device_obj.notify_callbacks.clear()
            logger.debug(f"Internal state for {device_address} cleaned.")

    async def disconnect_device(self, device_address: str) -> bool:
        logger.info(f"Attempting to manually disconnect from {device_address}...")
        connected_device_obj = self.connected_devices.get(device_address)

        if not connected_device_obj:
            logger.warning(f"Device {device_address} not found for manual disconnect.")
            self._cleanup_disconnected_device_state(device_address)
            return True
        try:
            if connected_device_obj.client.is_connected:
                await connected_device_obj.client.disconnect()
                logger.info(f"Disconnect command sent to {device_address}.")
            else:
                logger.info(f"Client for {device_address} already disconnected.")
            self._cleanup_disconnected_device_state(device_address)
            return True
        except BleakError as e:
            logger.error(f"BleakError manually disconnecting {device_address}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error manually disconnecting {device_address}: {e}")
        self._cleanup_disconnected_device_state(device_address)
        return False

    async def discover_services(self, device_address: str, force_rediscovery: bool = False) -> Optional[BleakGATTServiceCollection]:
        connected_device_obj = self.connected_devices.get(device_address)
        if not connected_device_obj or not connected_device_obj.is_connected:
            logger.error(f"Not connected to {device_address} for service retrieval.")
            return None

        if not force_rediscovery and connected_device_obj.services:
            num_services = len(connected_device_obj.services.services) if hasattr(connected_device_obj.services, 'services') else 0
            logger.info(f"Returning stored {num_services} services for {device_address}.")
            return connected_device_obj.services

        logger.info(f"Performing explicit service discovery for {device_address} (force_rediscovery={force_rediscovery}).")
        try:
            # This explicit call to get_services() will perform a new discovery.
            # It might still show a FutureWarning if bleak has stricter intentions for its use.
            svcs = await connected_device_obj.client.get_services()
            connected_device_obj.services = svcs # Update stored services
            num_services = len(svcs.services) if svcs and hasattr(svcs, 'services') and svcs.services is not None else 0
            logger.info(f"Discovered {num_services} services for {device_address} via explicit call.")
            return svcs
        except BleakError as e:
            logger.error(f"BleakError during explicit service discovery for {device_address}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during explicit service discovery for {device_address}: {e}")
        return None

    async def read_characteristic(self, device_address: str, characteristic_uuid: str) -> Optional[bytes]:
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

    async def write_characteristic(self, device_address: str, characteristic_uuid: str, data: bytes, response: bool = True) -> bool:
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

    async def start_notify(self, device_address: str, characteristic_uuid: str, callback: Callable[[str, bytes], None]) -> bool:
        connected_device_obj = self.connected_devices.get(device_address)
        if not connected_device_obj or not connected_device_obj.is_connected:
            logger.error(f"Not connected to {device_address} for starting notify on {characteristic_uuid}.")
            return False
        try:
            connected_device_obj.notify_callbacks[characteristic_uuid] = callback
            def bleak_notification_handler(sender: Any, data: bytearray): # sender can be int or BleakGATTCharacteristic
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
        # print("--- Discovered Devices ---")
        # for i, device_dict in enumerate(devices_found_dicts):
        #     print(f"  {i}: {device_dict.get('name')} ({device_dict.get('address')}) RSSI: {device_dict.get('rssi')}")

        print("--- Discovered Devices (Named Only) ---")

        named_devices = [d for d in devices_found_dicts if d.get('name') != "Unknown"]
        target_device_dict = None
        if not named_devices:
            print("\nINFO: No named devices found to connect to. Exiting example.")
            return

        for i, device_dict in enumerate(named_devices):
            print(f"  {i}: {device_dict.get('name')} ({device_dict.get('address')}) RSSI: {device_dict.get('rssi')}")
            if device_dict["name"] == "RaspPiBeacon":
                target_device_dict = device_dict
        if target_device_dict is None:
            target_device_dict = named_devices[0]
        target_address = target_device_dict['address']
        target_name = target_device_dict['name']
        print(f"\n--- Attempting to connect to: {target_name} ({target_address}) ---")

        if await hub.connect_device(
            target_address,
            user_disconnect_callback=example_disconnect_handler,
        ):
            print(f"INFO: Successfully connected to {target_name} ({target_address})")

            # Use the services discovered on connect, or force a re-discovery if needed for some reason
            services_collection = await hub.discover_services(target_address) # force_rediscovery=False by default

            if services_collection:
                print(f"\n--- Services on {target_name} ({target_address}) ---")
                # BleakGATTServiceCollection is iterable
                for service_obj in services_collection: # service_obj is BleakGATTService
                    print(f"  Service: {service_obj.uuid} (Handle: {service_obj.handle})")
                    # BleakGATTService has a 'characteristics' attribute (list of BleakGATTCharacteristic)
                    for char_obj in service_obj.characteristics: # char_obj is BleakGATTCharacteristic
                         print(f"    Characteristic: {char_obj.uuid} (Handle: {char_obj.handle}), Properties: {char_obj.properties}")

                readable_char_obj: Optional[BleakGATTCharacteristic] = None
                for service_obj in services_collection:
                    for char_obj in service_obj.characteristics:
                        if "read" in char_obj.properties:
                            readable_char_obj = char_obj
                            break
                    if readable_char_obj:
                        break

                if readable_char_obj:
                    print(f"\n--- Reading from characteristic: {readable_char_obj.uuid} ---")
                    value = await hub.read_characteristic(target_address, readable_char_obj.uuid) # Pass UUID string
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
                print(f"ERROR: Could not retrieve services for {target_address}")

            print(f"\n--- Disconnecting from {target_name} ({target_address}) ---")
            await hub.disconnect_device(target_address)
        else:
            print(f"ERROR: Could not connect to {target_name} ({target_address}) after retries.")

    except BleakError as e:
        logger.error(f"A BleakError occurred in example_usage: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred in example_usage: {e}", exc_info=True)
    finally:
        print("\n--- Shutting down BLE Hub Example ---")
        await hub.shutdown()
        print("--- Example Finished ---")

async def example_connect_to_multiple(hub: BLEHub):
    target_device_names = ["MyDevice1", "MyDevice2", "AnotherSensor"] # Example
    # Or target_addresses = ["XX:XX:XX:XX:XX:X1", "YY:YY:YY:YY:YY:Y2"]

    devices_found_dicts = await hub.scan_devices()
    if not devices_found_dicts:
        print("No devices found.")
        return

    connect_tasks = []
    actual_targets_found = []

    for device_dict in devices_found_dicts:
        if device_dict.get('name') in target_device_names: # Or check address
            actual_targets_found.append(device_dict)
            print(f"Target device found: {device_dict.get('name')}. Queuing connection.")
            # Note: You'd pass a unique disconnect handler or a generic one
            connect_tasks.append(hub.connect_device(device_dict['address'], user_disconnect_callback=lambda addr: print(f"Device {addr} disconnected.")))

    if not connect_tasks:
        print("None of the specified target devices were found.")
        return

    # Attempt to connect to all found target devices concurrently
    connection_results = await asyncio.gather(*connect_tasks, return_exceptions=True)

    successful_connections = []
    for i, result in enumerate(connection_results):
        target_device = actual_targets_found[i]
        if isinstance(result, Exception):
            logger.error(f"Failed to connect to {target_device.get('name')}: {result}")
        elif result is True:
            logger.info(f"Successfully connected to {target_device.get('name')}")
            successful_connections.append(target_device['address'])
        else:
            logger.warning(f"Connection to {target_device.get('name')} did not succeed (returned False).")


    # Now you can interact with devices in `successful_connections`
    # For example, discover services for all of them
    if successful_connections:
        print("\n--- Interacting with connected devices ---")
        for addr in successful_connections:
            device_name = hub.connected_devices[addr].name
            print(f"\n--- {device_name} ({addr}) ---")
            services = await hub.discover_services(addr)
            if services:
                for service in services:
                    print(f"  Service: {service.uuid}")
                    for char_obj in service.characteristics:
                        print(f"    Char: {char_obj.uuid}, Properties: {char_obj.properties}")
            # Add read/write/notify logic here for each device as needed
            # Be mindful of doing too many operations in a simple loop if they are long-running;
            # for many parallel long operations, create more tasks for asyncio.gather.

        # Example: Keep connections open for a while
        print("\nConnections active. Waiting for 30 seconds...")
        await asyncio.sleep(30)

if __name__ == "__main__":
    try:
        asyncio.run(example_usage())
    except KeyboardInterrupt:
        logger.info("Program terminated by user (KeyboardInterrupt).")
        print("\nProgram terminated by user.")
    except Exception as e:
        logger.critical(f"Unhandled exception in __main__: {e}", exc_info=True)
        print(f"CRITICAL ERROR in main: {e}")