"""
Configuration file for radio stations.
Each station is defined by a name, stream URL, and optional headers for authentication.
The stream URL should be a direct audio stream (e.g., MP3, AAC, etc.).
"""

RADIO_STATIONS = [
    {
        "name": "Smile FM",
        "stream_url": "https://edge.iono.fm/xice/212_high.aac",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        },
        "description": "Smile FM - Local radio station"
    },
    {
        "name": "KFM",
        "stream_url": "https://playerservices.streamtheworld.com/api/livestream-redirect/KFMAAC.aac",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        },
        "description": "KFM - Local radio station"
    }
]

def get_station_by_name(name: str) -> dict:
    """Get station configuration by name"""
    return next((s for s in RADIO_STATIONS if s["name"].lower() == name.lower()), None)

def get_all_stations() -> list:
    """Get all station configurations"""
    return RADIO_STATIONS 