from typing import List

import requests
from datetime import datetime
from .vatsim_pilot import VatsimPilot

class VatsimData:

    def __init__(self, vatsim_data_url="https://data.vatsim.net/v3/vatsim-data.json"):
        self.vatsim_data_url = vatsim_data_url
        self.data = None
        self.pilots: List[VatsimPilot] = []

    def fetch_vatsim_data(self):
        """Fetch VATSIM data from the given URL"""
        # Get current timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # print(f"VATSIM Data Processing - {timestamp}")

        try:
            response = requests.get(self.vatsim_data_url)
            response.raise_for_status()  # Raise an exception for HTTP errors
            self.data = response.json()
            # print(f"Total connected pilots: {len(self.data['pilots'])}\n")
            self.process_pilots()
            return None
        except requests.exceptions.RequestException as e:
            print(f"Error fetching VATSIM data: {e}")
            return None

    def process_pilots(self):
        self.pilots = [VatsimPilot(pilot) for pilot in self.data['pilots']]

    def get_pilot(self, index: int=0):
        return self.pilots[index].callsign, self.pilots[index].get_position()
