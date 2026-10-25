"""Decides which pilots are worth recording in the history.

Pure Python (no QGIS or GDAL), so it can be used from the QGIS plugin and from the separate
history recorder process.
"""
import math

# An aircraft at or below this groundspeed (knots) counts as stationary. Moving aircraft are
# recorded on every update; stationary ones only when something about them has changed.
STATIONARY_MAX_GROUNDSPEED = 2

# A stationary aircraft is recorded again if it ends up more than this many metres from where
# it was last recorded (so a slow pushback or taxi is still captured, while position jitter on a
# parked aircraft is not). It is measured from the last RECORDED position, so slow drift adds up.
STATIONARY_MOVE_TOLERANCE_M = 5.0


def _metres_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in metres between two nearby points."""
    dlon = (lon2 - lon1 + 180.0) % 360.0 - 180.0  # handles crossing the antimeridian
    dy = (lat2 - lat1) * 111_320.0
    dx = dlon * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dx, dy)


class StationaryFilter:
    """
    * Moving aircraft (above max_groundspeed) are always recorded.
    * Stationary aircraft are recorded the first time they are seen, and after that only when
      their position (beyond move_tolerance_m), heading or flight plan changes.

    "Changed" is judged against what was last RECORDED for that callsign, not the last update.
    """

    def __init__(self, max_groundspeed: float = STATIONARY_MAX_GROUNDSPEED,
                 move_tolerance_m: float = STATIONARY_MOVE_TOLERANCE_M):
        self.max_groundspeed = max_groundspeed
        self.move_tolerance_m = move_tolerance_m
        self._last = {}  # callsign -> (latitude, longitude, heading, flight plan details)

    @staticmethod
    def _state_of(p) -> tuple:
        return (p.latitude, p.longitude, p.heading,
                (p.departure, p.arrival, p.aircraft, p.cruise, p.route))

    def check(self, p):
        """Return (should_record, state). Does not change the filter's memory."""
        state = self._state_of(p)
        if p.groundspeed > self.max_groundspeed:
            return True, state
        prev = self._last.get(p.callsign)
        if prev is None or prev[2] != state[2] or prev[3] != state[3]:
            return True, state
        if _metres_between(prev[0], prev[1], state[0], state[1]) > self.move_tolerance_m:
            return True, state
        return False, state

    def commit(self, recorded: dict, present_callsigns):
        """Remember what was recorded (call only after the write succeeded) and forget
        pilots who have disconnected."""
        self._last.update(recorded)
        for callsign in [cs for cs in self._last if cs not in present_callsigns]:
            del self._last[callsign]