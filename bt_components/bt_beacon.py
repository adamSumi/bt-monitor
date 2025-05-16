#! /usr/bin/env python3
import uuid
import time
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--name', '-n', help="Beacon Name", type=str)
parser.add_argument('--uuid', action='store_true')

def generate_uuid():
    return uuid.uuid4()

if __name__ == "__main__":
    args = parser.parse_args()
    print(generate_uuid())