from typing import List

import requests
from datetime import datetime
from .pilot import Pilot

class VatsimData:

    def __init__(self, vatsim_data_url="https://data.vatsim.net/v3/vatsim-data.json"):
        self.vatsim_data_url = vatsim_data_url
        self.data = None
        self.pilots: List[Pilot] = []
        self.timestamp = None

    def fetch_vatsim_data(self):
        """Fetch VATSIM data from the given URL"""
        # Get current timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # print(f"VATSIM Data Processing - {timestamp}")

        try:
            response = requests.get(self.vatsim_data_url)
            response.raise_for_status()  # Raise an exception for HTTP errors
            self.data = response.json()
            #print(f"Total connected pilots: {len(self.data['pilots'])}\n")
            update_time = self.data['general']['update_timestamp']  # Example: "2026-06-24T11:29:42.8324524Z"
            self.timestamp = datetime.fromisoformat(update_time.replace('Z', '+00:00'))
            self.process_pilots()
            return None
        except requests.exceptions.RequestException as e:
            print(f"Error fetching VATSIM data: {e}")
            return None

    def process_pilots(self):
        self.pilots = [Pilot(pilot) for pilot in self.data['pilots']]

    def get_pilot(self, index: int=0):
        return self.pilots[index].callsign, self.pilots[index].get_position()

    def get_update_time(self):
        return self.timestamp.strftime('%Y-%m-%dT%H:%M:%S.%f') + '+00:00'
