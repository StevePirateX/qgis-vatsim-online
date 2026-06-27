class Pilot:

    def __init__(self, pilot):
        self.cid = pilot['cid']
        self.name = pilot['name']
        self.callsign = pilot['callsign']
        self.server = pilot['server']
        self.latitude = pilot['latitude']
        self.longitude = pilot['longitude']
        self.altitude = pilot['altitude']
        self.heading = pilot['heading']
        self.groundspeed = pilot['groundspeed']
        self.transponder = pilot['transponder']
        self.flight_plan = pilot['flight_plan']
        self.logon_time = pilot['logon_time']
        self.last_updated = pilot['last_updated']
        self.departure = '' if self.flight_plan is None else self.flight_plan['departure']
        self.arrival = '' if self.flight_plan is None else self.flight_plan['arrival']
        self.aircraft = '' if self.flight_plan is None else self.flight_plan['aircraft_short']
        self.cruise = '' if self.flight_plan is None else self.flight_plan['altitude']
        self.route = '' if self.flight_plan is None else self.flight_plan['route']


    def get_callsign(self):
        return self.callsign


    def get_position(self):
        return self.latitude, self.longitude, self.altitude


    def get_groundspeed(self):
        return self.groundspeed
