"""Build a Garmin-acceptable TCX file from Strava activity streams.

TCX carries time, GPS, altitude, distance, heart rate, cadence and — via the
Garmin ActivityExtension — power. That's everything Garmin Connect needs to show
the ride with its real data. Reconstructed from streams, it is not byte-identical
to the Magene original, but it preserves the measurements.
"""
from __future__ import annotations

from datetime import timedelta
from xml.sax.saxutils import escape

TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
AX_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"

# Strava sport_type -> TCX Sport attribute (TCX only allows Running/Biking/Other).
_SPORT = {
    "Ride": "Biking",
    "GravelRide": "Biking",
    "MountainBikeRide": "Biking",
    "EBikeRide": "Biking",
    "VirtualRide": "Biking",
    "Run": "Running",
    "TrailRun": "Running",
    "VirtualRun": "Running",
}


def _sport(sport_type: str) -> str:
    return _SPORT.get(sport_type, "Other")


def _get(streams: dict, key: str):
    v = streams.get(key)
    return v if isinstance(v, list) else None


def build_tcx(activity, streams: dict) -> bytes:
    """activity: strava.Activity; streams: {key: [values]}; returns TCX bytes."""
    time_s = _get(streams, "time") or []
    latlng = _get(streams, "latlng")
    altitude = _get(streams, "altitude")
    distance = _get(streams, "distance")
    hr = _get(streams, "heartrate")
    cadence = _get(streams, "cadence")
    watts = _get(streams, "watts")

    n = len(time_s)
    start = activity.start_dt

    def at(stream, i):
        if stream is None or i >= len(stream):
            return None
        return stream[i]

    points = []
    for i in range(n):
        t = start + timedelta(seconds=int(time_s[i]))
        parts = [f"<Time>{t.strftime('%Y-%m-%dT%H:%M:%SZ')}</Time>"]
        ll = at(latlng, i)
        if isinstance(ll, (list, tuple)) and len(ll) == 2 and ll[0] is not None:
            parts.append(
                f"<Position><LatitudeDegrees>{ll[0]}</LatitudeDegrees>"
                f"<LongitudeDegrees>{ll[1]}</LongitudeDegrees></Position>"
            )
        alt = at(altitude, i)
        if alt is not None:
            parts.append(f"<AltitudeMeters>{alt}</AltitudeMeters>")
        dist = at(distance, i)
        if dist is not None:
            parts.append(f"<DistanceMeters>{dist}</DistanceMeters>")
        h = at(hr, i)
        if h is not None:
            parts.append(f"<HeartRateBpm><Value>{int(h)}</Value></HeartRateBpm>")
        cad = at(cadence, i)
        if cad is not None:
            parts.append(f"<Cadence>{min(254, int(cad))}</Cadence>")
        w = at(watts, i)
        if w is not None:
            parts.append(
                f'<Extensions><TPX xmlns="{AX_NS}"><Watts>{int(w)}</Watts></TPX></Extensions>'
            )
        points.append("<Trackpoint>" + "".join(parts) + "</Trackpoint>")

    total_time = int(time_s[-1]) if n else 0
    total_dist = 0.0
    if distance:
        total_dist = float(distance[-1])
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    name = escape(activity.name or "Ride")

    # Average cadence/HR/watts for the Lap are optional; keep the file lean.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="{TCX_NS}"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xsi:schemaLocation="{TCX_NS} http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd">
 <Activities>
  <Activity Sport="{_sport(activity.sport_type)}">
   <Id>{start_iso}</Id>
   <Lap StartTime="{start_iso}">
    <TotalTimeSeconds>{total_time}</TotalTimeSeconds>
    <DistanceMeters>{total_dist:.1f}</DistanceMeters>
    <Calories>0</Calories>
    <Intensity>Active</Intensity>
    <TriggerMethod>Manual</TriggerMethod>
    <Track>
     {''.join(points)}
    </Track>
   </Lap>
   <Notes>{name}</Notes>
   <Creator xsi:type="Device_t">
    <Name>Magene (via Strava)</Name>
    <UnitId>0</UnitId>
    <ProductID>0</ProductID>
   </Creator>
  </Activity>
 </Activities>
</TrainingCenterDatabase>"""
    return xml.encode("utf-8")
