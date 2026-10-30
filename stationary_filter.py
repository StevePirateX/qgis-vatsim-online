"""Decides what to do with each pilot when recording the history.

Pure Python (no QGIS or GDAL), so it can be used from the QGIS plugin, from the separate
history recorder process and from the standalone worker.

Each history row has a start time (`timestamp`) and a `last_seen` time. For a moving aircraft the
two are the same (a single sample). A stationary aircraft gets ONE row, and while nothing about it
changes that row's `last_seen` is pushed forward, so `last_seen - timestamp` is how long it sat
there.
"""
import math

# An aircraft at or below this groundspeed (knots) counts as stationary. Moving aircraft are
# recorded on every update; stationary ones only when something about them has changed.
STATIONARY_MAX_GROUNDSPEED = 2

# A stationary aircraft starts a new row if it ends up more than this many metres from where
# its row was recorded (so a slow pushback or taxi is still captured, while position jitter on a
# parked aircraft is not). It is measured from the RECORDED position, so slow drift adds up.
STATIONARY_MOVE_TOLERANCE_M = 5.0

RECORD = "record"   # write a new row
EXTEND = "extend"   # unchanged and stationary: push the existing row's last_seen forward


def _metres_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance in metres between two nearby points."""
    dlon = (lon2 - lon1 + 180.0) % 360.0 - 180.0  # handles crossing the antimeridian
    dy = (lat2 - lat1) * 111_320.0
    dx = dlon * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dx, dy)


class StationaryFilter:
    """
    * Moving aircraft (above max_groundspeed) always get a new row.
    * A stationary aircraft gets a new row the first time it is seen stationary, and after that
      only when its position (beyond move_tolerance_m), heading or flight plan changes. Until
      then its existing row is extended.

    "Changed" is judged against what was last RECORDED for that callsign, not the last update.
    A row recorded while the aircraft was moving is never extended: stopping starts a new row.
    """

    def __init__(self, max_groundspeed: float = STATIONARY_MAX_GROUNDSPEED,
                 move_tolerance_m: float = STATIONARY_MOVE_TOLERANCE_M):
        self.max_groundspeed = max_groundspeed
        self.move_tolerance_m = move_tolerance_m
        # callsign -> (latitude, longitude, heading, flight plan details, was_stationary)
        self._last = {}
        self._fids = {}  # callsign -> id of the row recorded for it

    def _state_of(self, p) -> tuple:
        return (p.latitude, p.longitude, p.heading,
                (p.departure, p.arrival, p.aircraft, p.cruise, p.route),
                p.groundspeed <= self.max_groundspeed)

    def check(self, p):
        """Return (RECORD or EXTEND, state). Does not change the filter's memory."""
        state = self._state_of(p)
        if not state[4]:  # moving
            return RECORD, state
        prev = self._last.get(p.callsign)
        if (prev is None or not prev[4] or self._fids.get(p.callsign) is None
                or prev[2] != state[2] or prev[3] != state[3]
                or _metres_between(prev[0], prev[1], state[0], state[1]) > self.move_tolerance_m):
            return RECORD, state
        return EXTEND, state

    def open_fid(self, callsign: str):
        """Id of the row to extend for a pilot that check() answered EXTEND for."""
        return self._fids.get(callsign)

    def commit(self, recorded: dict, present_callsigns):
        """Remember the rows that were written (call only after the write succeeded) and forget
        pilots who have disconnected.

        :param recorded: {callsign: (state, row id)} for every row inserted
        """
        for callsign, (state, fid) in recorded.items():
            self._last[callsign] = state
            self._fids[callsign] = fid
        for callsign in [cs for cs in self._last if cs not in present_callsigns]:
            del self._last[callsign]
            self._fids.pop(callsign, None)