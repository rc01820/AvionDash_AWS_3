"""Static reference data. Same airports as the rest of the AvionDash suite;
AvionPulse fleet uses N-AVP tails so data from the three apps never collides
in a shared Datadog org."""

AIRPORTS = [
    # iata, icao, name, city, country, lat, lon, tz, elevation_ft, runways
    ("JFK", "KJFK", "John F. Kennedy Intl", "New York", "United States", 40.639751, -73.778925, "America/New_York", 13, 4),
    ("LAX", "KLAX", "Los Angeles Intl", "Los Angeles", "United States", 33.942791, -118.410042, "America/Los_Angeles", 125, 4),
    ("ORD", "KORD", "O'Hare Intl", "Chicago", "United States", 41.978603, -87.904842, "America/Chicago", 672, 8),
    ("ATL", "KATL", "Hartsfield-Jackson Atlanta Intl", "Atlanta", "United States", 33.636719, -84.428067, "America/New_York", 1026, 5),
    ("DFW", "KDFW", "Dallas/Fort Worth Intl", "Dallas", "United States", 32.896828, -97.037997, "America/Chicago", 607, 7),
    ("DEN", "KDEN", "Denver Intl", "Denver", "United States", 39.856094, -104.673738, "America/Denver", 5431, 6),
    ("SFO", "KSFO", "San Francisco Intl", "San Francisco", "United States", 37.618972, -122.374889, "America/Los_Angeles", 13, 4),
    ("SEA", "KSEA", "Seattle-Tacoma Intl", "Seattle", "United States", 47.449888, -122.311777, "America/Los_Angeles", 433, 3),
    ("MIA", "KMIA", "Miami Intl", "Miami", "United States", 25.795865, -80.287046, "America/New_York", 8, 4),
    ("BOS", "KBOS", "Boston Logan Intl", "Boston", "United States", 42.364347, -71.005181, "America/New_York", 19, 4),
    ("LHR", "EGLL", "London Heathrow", "London", "United Kingdom", 51.477500, -0.461389, "Europe/London", 83, 2),
    ("CDG", "LFPG", "Charles de Gaulle", "Paris", "France", 49.012779, 2.550000, "Europe/Paris", 392, 4),
    ("FRA", "EDDF", "Frankfurt Airport", "Frankfurt", "Germany", 50.033333, 8.570556, "Europe/Berlin", 364, 4),
    ("AMS", "EHAM", "Amsterdam Schiphol", "Amsterdam", "Netherlands", 52.308056, 4.764167, "Europe/Amsterdam", -11, 6),
    ("MAD", "LEMD", "Madrid-Barajas", "Madrid", "Spain", 40.472222, -3.560833, "Europe/Madrid", 2000, 4),
    ("DXB", "OMDB", "Dubai Intl", "Dubai", "UAE", 25.252778, 55.364444, "Asia/Dubai", 62, 2),
    ("SIN", "WSSS", "Singapore Changi", "Singapore", "Singapore", 1.350189, 103.994433, "Asia/Singapore", 22, 3),
    ("NRT", "RJAA", "Tokyo Narita", "Tokyo", "Japan", 35.765278, 140.385556, "Asia/Tokyo", 141, 2),
    ("HND", "RJTT", "Tokyo Haneda", "Tokyo", "Japan", 35.549444, 139.779167, "Asia/Tokyo", 21, 4),
    ("PEK", "ZBAA", "Beijing Capital Intl", "Beijing", "China", 40.080111, 116.584556, "Asia/Shanghai", 115, 3),
    ("HKG", "VHHH", "Hong Kong Intl", "Hong Kong", "China", 22.308919, 113.914603, "Asia/Hong_Kong", 28, 2),
    ("SYD", "YSSY", "Sydney Kingsford Smith", "Sydney", "Australia", -33.946111, 151.177222, "Australia/Sydney", 21, 3),
    ("YYZ", "CYYZ", "Toronto Pearson Intl", "Toronto", "Canada", 43.677222, -79.630556, "America/Toronto", 569, 5),
    ("GRU", "SBGR", "Sao Paulo Guarulhos Intl", "Sao Paulo", "Brazil", -23.432075, -46.469511, "America/Sao_Paulo", 2459, 3),
    ("JNB", "FAOR", "OR Tambo Intl", "Johannesburg", "South Africa", -26.133694, 28.242317, "Africa/Johannesburg", 5558, 3),
]

AIRCRAFT = [
    # tail, model, manufacturer, capacity, range_nm, status, engine, year, flight_hours
    ("N-AVP001", "B737-800", "Boeing", 189, 3060, "active", "CFM56-7B", 2018, 14230.5),
    ("N-AVP002", "B737-MAX9", "Boeing", 193, 3550, "active", "LEAP-1B", 2022, 5120.0),
    ("N-AVP003", "A320neo", "Airbus", 165, 3400, "active", "LEAP-1A", 2021, 7840.3),
    ("N-AVP004", "A321XLR", "Airbus", 220, 4700, "active", "LEAP-1A", 2023, 2100.8),
    ("N-AVP005", "B787-9", "Boeing", 296, 7635, "active", "GEnx-1B", 2020, 11520.0),
    ("N-AVP006", "B787-10", "Boeing", 330, 6430, "active", "GEnx-1B", 2021, 9800.2),
    ("N-AVP007", "A350-900", "Airbus", 369, 8100, "active", "Trent XWB", 2019, 16000.0),
    ("N-AVP008", "A350-1000", "Airbus", 410, 8700, "maintenance", "Trent XWB", 2020, 12500.0),
    ("N-AVP009", "B777-300ER", "Boeing", 396, 7370, "active", "GE90-115B", 2017, 22300.5),
    ("N-AVP010", "A380-800", "Airbus", 555, 8200, "active", "Trent 970", 2016, 29000.0),
    ("N-AVP011", "E195-E2", "Embraer", 146, 2850, "active", "PW1900G", 2023, 1800.0),
    ("N-AVP012", "CRJ-900", "Bombardier", 90, 1550, "active", "CF34-8C5", 2019, 8900.0),
    ("N-AVP013", "A220-300", "Airbus", 130, 3400, "active", "PW1500G", 2022, 4100.0),
    ("N-AVP014", "B737-700", "Boeing", 140, 3200, "maintenance", "CFM56-7B", 2014, 28000.0),
    ("N-AVP015", "B757-200", "Boeing", 200, 3900, "active", "RR RB211", 2013, 41200.0),
]

# flight number, origin, destination, tail, cruise altitude
FLIGHTS = [
    ("AVP101", "JFK", "LHR", "N-AVP005", 37000),
    ("AVP102", "LAX", "NRT", "N-AVP007", 39000),
    ("AVP103", "LHR", "DXB", "N-AVP009", 36000),
    ("AVP104", "DXB", "SIN", "N-AVP010", 38000),
    ("AVP105", "SFO", "HKG", "N-AVP006", 38000),
    ("AVP106", "ATL", "LHR", "N-AVP005", 36000),
    ("AVP107", "ORD", "LAX", "N-AVP001", 35000),
    ("AVP108", "DFW", "JFK", "N-AVP003", 34000),
    ("AVP109", "BOS", "MIA", "N-AVP011", 33000),
    ("AVP110", "JFK", "LAX", "N-AVP015", 37000),
    ("AVP111", "LHR", "CDG", "N-AVP003", 24000),
    ("AVP112", "LAX", "SFO", "N-AVP001", 28000),
    ("AVP113", "ORD", "BOS", "N-AVP011", 35000),
    ("AVP114", "JFK", "MIA", "N-AVP015", 36000),
    ("AVP115", "FRA", "JFK", "N-AVP009", 38000),
    ("AVP116", "NRT", "SYD", "N-AVP010", 39000),
    ("AVP117", "DFW", "ATL", "N-AVP012", 31000),
    ("AVP118", "SEA", "DEN", "N-AVP013", 35000),
    ("AVP119", "DEN", "ORD", "N-AVP002", 36000),
    ("AVP120", "ATL", "DFW", "N-AVP004", 34000),
    ("AVP121", "AMS", "MAD", "N-AVP013", 36000),
    ("AVP122", "HKG", "PEK", "N-AVP006", 37000),
    ("AVP123", "SFO", "SEA", "N-AVP012", 33000),
    ("AVP124", "GRU", "JNB", "N-AVP007", 38000),
    ("AVP125", "YYZ", "LHR", "N-AVP002", 37000),
]

USERS = [
    # username, password, role, full name
    ("admin", "aviondash123", "admin", "Ops Administrator"),
    ("operator", "operator123", "operator", "Duty Operations Controller"),
    ("viewer", "viewer123", "viewer", "Read-only Observer"),
]

DELAY_REASONS = ["Weather hold", "ATC flow control", "Late inbound aircraft", "Crew rest requirement",
                 "Ground handling", "Fuel truck delay", "De-icing"]
CANCEL_REASONS = ["Mechanical", "Winter storm", "Crew unavailable", "Airport closure"]

OPS_EVENT_TEMPLATES = [
    ("info", "ACARS", "ACARS position report gap > 10 min on {tail}"),
    ("info", "LINE", "Line check completed on {tail} at {apt}"),
    ("warning", "MX", "APU fault message deferred (MEL 49-1) on {tail}"),
    ("warning", "MX", "Bird strike inspection requested for {tail} at {apt}"),
    ("warning", "OPS", "Fuel uplift discrepancy reported on {tail}"),
    ("critical", "MX", "Engine oil pressure caution on {tail} — inspection required"),
    ("info", "OPS", "Gate change at {apt} for {tail}"),
    ("warning", "WX", "Convective SIGMET affecting arrivals into {apt}"),
]
