import os
import time
import threading
import base64
import json
from collections import deque
import firebase_admin
from firebase_admin import credentials, storage, firestore
from radio_stream_capture import RadioStreamCapture
from radio_stations import get_all_stations
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# --- CONFIGURATION ---
LIVE_BUFFER_SECONDS = 60  # How many seconds of live fingerprints to keep
FINGERPRINT_SIZE = 32     # bytes
MATCH_THRESHOLD = 32      # Hamming distance threshold

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('radio_capture.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- FIREBASE SETUP ---
cred = credentials.Certificate("service-account.json")
firebase_admin.initialize_app(cred, {
    'storageBucket': 'newbuckettest.firebasestorage.app'
})
db = firestore.client()
bucket = storage.bucket()

# --- RADIO STREAM CAPTURE ---
radio_captures = {}  # station_name -> RadioStreamCapture

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def start_radio_captures():
    """Start capturing audio from all configured radio stations"""
    stations = get_all_stations()
    for station in stations:
        try:
            name = station["name"]
            if name in radio_captures:
                logger.warning(f"Capture already running for {name}")
                continue
                
            logger.info(f"Starting capture for {name}")
            capture = RadioStreamCapture(
                stream_url=station["stream_url"],
                station_name=name,
                headers=station.get("headers", {})
            )
            capture.start()
            radio_captures[name] = capture
            logger.info(f"Successfully started capture for {name}")
            
        except Exception as e:
            logger.error(f"Failed to start capture for {station['name']}: {str(e)}")

def stop_radio_captures():
    """Stop all active radio captures"""
    for name, capture in radio_captures.items():
        try:
            logger.info(f"Stopping capture for {name}")
            capture.stop()
            logger.info(f"Successfully stopped capture for {name}")
        except Exception as e:
            logger.error(f"Error stopping capture for {name}: {str(e)}")
    radio_captures.clear()

def hamming_distance(fp1: bytes, fp2: bytes) -> int:
    assert len(fp1) == len(fp2)
    return sum(bin(b1 ^ b2).count('1') for b1, b2 in zip(fp1, fp2))

def match_fingerprint(uploaded_fp: bytes):
    """Match a fingerprint against all radio stations"""
    best_match = None
    best_distance = 9999
    best_station = None
    
    for station_name, capture in radio_captures.items():
        for ts, live_fp in capture.get_fingerprints():
            dist = hamming_distance(uploaded_fp, live_fp)
            if dist < best_distance:
                best_distance = dist
                best_match = (ts, live_fp)
                best_station = station_name
    
    if best_distance < MATCH_THRESHOLD:
        return best_match[0], best_distance, best_station
    return None, best_distance, None

# --- POLL FOR NEW MOBILE FINGERPRINTS ---
def poll_firebase_storage():
    processed_files = set()
    while True:
        try:
            blobs = bucket.list_blobs(prefix="incoming_fingerprints/")
            for blob in blobs:
                if blob.name in processed_files:
                    continue
                if not blob.name.endswith(".json"):
                    continue
                    
                # Download and parse
                data = json.loads(blob.download_as_text())
                fingerprint_data = data["fingerprint"]
                uploaded_ts = data["timestamp"]
                
                # Convert fingerprint to bytes
                fingerprint_bytes = bytes([int(x) for x in fingerprint_data])
                
                # Match against all stations
                match_ts, distance, station = match_fingerprint(fingerprint_bytes)
                logger.info(f"Processed {blob.name}: match_ts={match_ts}, distance={distance}, station={station}")
                
                # Write result to Firestore
                result = {
                    "matched_at": int(time.time() * 1000),
                    "distance": distance,
                    "match_timestamp": match_ts,
                    "station": station or "Unknown",
                    "confidence": max(0, 1 - distance / FINGERPRINT_SIZE / 8)
                }
                db.collection("results").document(str(uploaded_ts)).set(result)
                processed_files.add(blob.name)
                
                # Delete old fingerprint file after processing
                blob.delete()
                
        except Exception as e:
            logger.error(f"Error in poll loop: {str(e)}")
            
        time.sleep(5)

@app.on_event("startup")
async def startup_event():
    """Start radio captures when the server starts"""
    logger.info("Starting radio captures...")
    start_radio_captures()
    
    # Start polling in a separate thread
    poll_thread = threading.Thread(target=poll_firebase_storage, daemon=True)
    poll_thread.start()
    logger.info("Fingerprint polling started")

@app.on_event("shutdown")
async def shutdown_event():
    """Stop radio captures when the server shuts down"""
    logger.info("Stopping radio captures...")
    stop_radio_captures()
    logger.info("All radio captures stopped")

if __name__ == "__main__":
    try:
        logger.info("Starting Radiolytics backend server...")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except Exception as e:
        logger.error(f"Server error: {str(e)}")
        stop_radio_captures() 