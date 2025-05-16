#! /usr/bin/env python3
import uuid
import bluetooth
import time
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--name', '-n', help="Beacon Name", type=str)

def generate_uuid():
    return uuid.uuid4()

class BTBeacon:
    """
    CLASS: BTBeacon

    Functions as peripheral, will advertise itself for a client/hub to accept.
    """
    def __init__(self, device_name, service_name = "RPI_BTComms", service_uuid = "9fac58bd-90bd-48d0-9d42-efe9dd52ce7c"):
        self.service_uuid = service_uuid
        self.service_name = service_name

        self.name = device_name


        self.beacon_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
        sock, _ = self.connect_sequence()
        self.bt_socket = sock

    def connect_sequence(self):
        self.beacon_sock.bind(bluetooth.PORT_ANY)
        self.beacon_sock.listen(1)

        port = self.beacon_sock.getsockname()[1]
        print(f"Server listening on RFCOMM channel {port}")
        print(f"Advertising service: '{self.service_name}' with UUID: {self.service_uuid}")
        print("Waiting to connect...")

        try:
            bluetooth.advertise_service(
                self.beacon_sock,
                self.service_name,
                service_id=self.service_uuid,
                service_classes=[self.service_uuid, bluetooth.SERIAL_PORT_CLASS],
                profiles=[bluetooth.SERIAL_PORT_PROFILE],
            )
        except Exception as e:
            print(f"Warning: Could not advertise service (this might be okay if client connects by MAC/port directly after discovery): {e}")

        hub_sock, hub_info = self.beacon_sock.accept()
        print(f"Accepted connection from: {hub_info[0]} (MAC Address), Channel: {hub_info[1]}")
        print(f"Client Info:\n{hub_info}")

        return hub_sock, hub_info

    def begin_monitoring(self):
        try:
            while True:
                data = self.bt_socket.recv(1024)
                if not data:
                    print("Client disconnected.")
                    break
                received_message = data.decode('utf-8')
                print(f"Client says: [{received_message}]")

                response_message = f"RPi received: '{received_message}'"
                # hub_sock.send(response_message.encode('utf-8'))
                print(f"{response_message}")
                time.sleep(0.1)

        except bluetooth.btcommon.BluetoothError as bt_err:
            print(f"Bluetooth Error during communication: {bt_err}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
        finally:
            print("Closing hub socket.")
            self.hub_sock.close()
            print("Closing beacon socket.")
            self.beacon_sock.close()
            print("Raspberry Pi Bluetooth Beacon session ended.")


if __name__ == "__main__":
    args = parser.parse_args()

    while True:
        try:
            beacon = BTBeacon(args.name)
            print("Starting new beacon instance...")
            beacon.begin_monitoring()
            print("Monitoring instance finished, Awaiting new connection...")
            time.sleep(1)
        except KeyboardInterrupt:
            print("\nServer execution stopped by user (Ctrl+C).")
            break
        except bluetooth.btcommon.BluetoothError as e:
            print(f"Critical Bluetooth setup error: {e}")
            print("Ensure Bluetooth is enabled and the adapter is up (sudo hciconfig hci0 up).")
            print("Retrying in 10 seconds...")
            time.sleep(10)
        except Exception as e:
            print(f"An unexpected error occurred in the main loop: {e}")
            print("Retrying in 10 seconds...")
            time.sleep(10)
        finally:
            print("Cleaning up before potential restart or exit.")

