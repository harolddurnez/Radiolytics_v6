# Radiolytics Python Backend

## Fingerprint JSON Format
```
{
  "fingerprint": [[RMS, centroid, energy, dB], ...],
  "timestamp": <int>,
  "device_id" or "station": <str>
}
```

## Usage

### Run Matcher Service
```
python fingerprint_matcher.py --run-matcher
```

### Analyze Match Log
```
python fingerprint_matcher.py --analyze-log
```

### CLI Help
```
python fingerprint_matcher.py --help
```

## Config
Edit `config.json` to set thresholds and log file:
```
{
  "POLL_INTERVAL": 5,
  "MATCH_THRESHOLD": 0.75,
  "MIN_FRAMES_MATCH": 10,
  "BUFFER_MINUTES": 3,
  "LOG_CSV": "successful_matches.csv"
}
```

## Analytics
- Reports best recording lengths, offsets, and most reliable stations.
- Suggests optimal recording duration based on log data.

## Testing
Run tests with:
```
python -m unittest discover tests
``` 