import pyaudio
import numpy as np
import threading
import time
import requests
from io import BytesIO
import logging
from pydub import AudioSegment
import ffmpeg
import tempfile
import os
from collections import deque

class RadioStreamCapture:
    def __init__(self, stream_url, station_name, headers=None, buffer_seconds=60):
        self.stream_url = stream_url
        self.station_name = station_name
        self.headers = headers or {}
        self.buffer_seconds = buffer_seconds
        self.is_running = False
        self.thread = None
        self.fingerprints = deque()  # (timestamp, fingerprint)
        self.audio = pyaudio.PyAudio()
        
        # Audio parameters (matching Android app)
        self.SAMPLE_RATE = 44100
        self.CHANNELS = 1
        self.CHUNK_SIZE = 4096
        self.FFT_SIZE = 2048
        self.MIN_FREQ = 30
        self.MAX_FREQ = 3000
        self.PEAK_THRESHOLD = 0.1
        self.FINGERPRINT_SIZE = 32
        
        # FFT setup
        self.fft = np.fft.fft
        self.window = np.hanning(self.FFT_SIZE)
        
        # Create a temporary directory for audio processing
        self.temp_dir = tempfile.mkdtemp()
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(f"RadioStreamCapture-{station_name}")

    def start(self):
        if self.is_running:
            return
            
        self.is_running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        self.logger.info(f"Started capturing stream for {self.station_name}")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join()
        self.audio.terminate()
        # Clean up temporary files
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception as e:
            self.logger.error(f"Error cleaning up temp directory: {str(e)}")
        self.logger.info(f"Stopped capturing stream for {self.station_name}")

    def _convert_aac_to_wav(self, aac_data):
        """Convert AAC data to WAV format using ffmpeg"""
        try:
            # Create temporary files
            aac_file = os.path.join(self.temp_dir, f"temp_{int(time.time())}.aac")
            wav_file = os.path.join(self.temp_dir, f"temp_{int(time.time())}.wav")
            
            # Write AAC data to file
            with open(aac_file, 'wb') as f:
                f.write(aac_data)
            
            # Convert to WAV using ffmpeg
            stream = ffmpeg.input(aac_file)
            stream = ffmpeg.output(stream, wav_file, acodec='pcm_s16le', ac=1, ar=self.SAMPLE_RATE)
            ffmpeg.run(stream, capture_stdout=True, capture_stderr=True, overwrite_output=True)
            
            # Read WAV data
            with open(wav_file, 'rb') as f:
                wav_data = f.read()
            
            # Clean up temporary files
            os.remove(aac_file)
            os.remove(wav_file)
            
            return wav_data
            
        except Exception as e:
            self.logger.error(f"Error converting AAC to WAV: {str(e)}")
            return None

    def _capture_loop(self):
        while self.is_running:
            try:
                # Stream the audio using requests with headers
                response = requests.get(self.stream_url, stream=True, headers=self.headers)
                if response.status_code != 200:
                    self.logger.error(f"Failed to connect to stream: {response.status_code}")
                    time.sleep(5)
                    continue

                buffer = BytesIO()
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    if not self.is_running:
                        break
                        
                    if chunk:
                        buffer.write(chunk)
                        
                        if buffer.tell() >= self.CHUNK_SIZE * 2:  # Buffer more data for AAC conversion
                            # Process the audio chunk
                            buffer.seek(0)
                            aac_data = buffer.read()
                            
                            # Convert AAC to WAV
                            wav_data = self._convert_aac_to_wav(aac_data)
                            if wav_data:
                                # Convert to float32 array
                                audio_data = np.frombuffer(wav_data[44:], dtype=np.int16).astype(np.float32) / 32768.0
                                fingerprint = self._process_audio_chunk(audio_data)
                                
                                if fingerprint is not None:
                                    timestamp = int(time.time() * 1000)
                                    self._add_fingerprint(fingerprint, timestamp)
                            
                            buffer = BytesIO()

                # If we get here, the stream ended
                self.logger.warning(f"Stream ended for {self.station_name}, attempting to reconnect...")
                time.sleep(5)

            except Exception as e:
                self.logger.error(f"Error in capture loop: {str(e)}")
                time.sleep(5)  # Wait before retrying

    def _process_audio_chunk(self, audio_data):
        try:
            # Apply window function
            windowed = audio_data[:self.FFT_SIZE] * self.window
            
            # Pad if necessary
            if len(windowed) < self.FFT_SIZE:
                windowed = np.pad(windowed, (0, self.FFT_SIZE - len(windowed)))
            
            # Perform FFT
            fft_result = self.fft(windowed)
            magnitudes = np.abs(fft_result[:self.FFT_SIZE//2])
            
            # Normalize magnitudes to [0,1] range
            if np.max(magnitudes) > 0:
                magnitudes = magnitudes / np.max(magnitudes)
            
            # Find peaks
            peaks = []
            for i, magnitude in enumerate(magnitudes):
                freq = i * self.SAMPLE_RATE / self.FFT_SIZE
                if self.MIN_FREQ <= freq <= self.MAX_FREQ and magnitude > self.PEAK_THRESHOLD:
                    # Store normalized frequency bin (0-1 range)
                    normalized_bin = i / (self.FFT_SIZE // 2)
                    peaks.append((normalized_bin, magnitude))
            
            # Sort by magnitude and take top peaks
            peaks.sort(key=lambda x: x[1], reverse=True)
            top_peaks = peaks[:self.FINGERPRINT_SIZE]
            
            # Create fingerprint - store normalized frequency bins
            fingerprint = bytearray(self.FINGERPRINT_SIZE)
            for i, (norm_bin, _) in enumerate(top_peaks):
                # Convert normalized bin (0-1) to byte (0-255)
                fingerprint[i] = int(norm_bin * 255)
            
            return bytes(fingerprint)
            
        except Exception as e:
            self.logger.error(f"Error processing audio chunk: {str(e)}")
            return None

    def _add_fingerprint(self, fingerprint, timestamp):
        self.fingerprints.append((timestamp, fingerprint))
        # Remove old fingerprints
        cutoff = int(time.time() * 1000) - self.buffer_seconds * 1000
        while self.fingerprints and self.fingerprints[0][0] < cutoff:
            self.fingerprints.popleft()

    def get_fingerprints(self):
        """Return a copy of the current fingerprints buffer"""
        return list(self.fingerprints)

    def get_station_name(self):
        return self.station_name 