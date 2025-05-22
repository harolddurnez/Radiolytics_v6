package com.example.radiolytics

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.viewModelScope
import com.google.firebase.firestore.ktx.firestore
import com.google.firebase.ktx.Firebase
import com.google.firebase.storage.FirebaseStorage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val audioFingerprinter = AudioFingerprinter()
    private val storage = FirebaseStorage.getInstance()
    private val storageRef = storage.reference

    private val _recordingState = MutableLiveData<RecordingState>()
    val recordingState: LiveData<RecordingState> = _recordingState

    private val _uploadState = MutableLiveData<UploadState>()
    val uploadState: LiveData<UploadState> = _uploadState

    var lastUploadTimestamp: Long? = null
        private set

    private val _result = MutableLiveData<String>()
    private val db = Firebase.firestore
    private var currentListener: (() -> Unit)? = null

    sealed class RecordingState {
        object Idle : RecordingState()
        object Recording : RecordingState()
        data class Error(val message: String) : RecordingState()
    }

    sealed class UploadState {
        object Idle : UploadState()
        object Uploading : UploadState()
        data class Success(val fingerprint: String) : UploadState()
        data class Error(val message: String) : UploadState()
    }

    init {
        _recordingState.value = RecordingState.Idle
        _uploadState.value = UploadState.Idle
    }

    fun observeResults(callback: (String) -> Unit) {
        _result.observeForever { callback(it) }
    }

    fun startRecording() {
        _recordingState.value = RecordingState.Recording
        viewModelScope.launch(Dispatchers.IO) {
                try {
                    audioFingerprinter.startRecording()
                // Record for 10 seconds
                    val startTime = System.currentTimeMillis()
                var frameCount = 0
                while (System.currentTimeMillis() - startTime < 10000) {
                        if (!audioFingerprinter.processAudioChunk()) {
                            break
                    }
                    frameCount++
                    kotlinx.coroutines.delay(50)
                }
                Log.d("MainViewModel", "Recorded $frameCount frames")
                    val fingerprint = audioFingerprinter.stopRecording()
                if (frameCount > 0) {
                    uploadFingerprint(fingerprint)
                } else {
                    Log.w("MainViewModel", "No frames recorded, skipping upload")
                }
            } catch (e: Exception) {
                _recordingState.postValue(RecordingState.Error("Recording error: ${e.message}"))
            }
            _recordingState.postValue(RecordingState.Idle)
        }
    }

    fun stopRecording() {
        try {
            audioFingerprinter.stopRecording()
        } catch (_: Exception) {}
        _recordingState.value = RecordingState.Idle
    }

    private fun uploadFingerprint(fingerprint: ByteArray) {
        viewModelScope.launch {
            try {
                _uploadState.value = UploadState.Uploading
                val timestamp = System.currentTimeMillis()
                lastUploadTimestamp = timestamp
                
                // Generate a unique device ID if not exists
                val deviceId = android.provider.Settings.Secure.getString(
                    getApplication<Application>().contentResolver,
                    android.provider.Settings.Secure.ANDROID_ID
                )
                
                // Parse the fingerprint JSON string back to a list of features
                val fingerprintJson = String(fingerprint)
                val jsonArray = org.json.JSONArray(fingerprintJson)
                val fingerprintList = mutableListOf<List<Double>>()
                
                for (i in 0 until jsonArray.length()) {
                    val frameArray = jsonArray.getJSONArray(i)
                    val frame = mutableListOf<Double>()
                    for (j in 0 until frameArray.length()) {
                        frame.add(frameArray.getDouble(j))
                    }
                    fingerprintList.add(frame)
                }
                
                val jsonContent = """
                    {
                        "fingerprint": ${org.json.JSONArray(fingerprintList).toString()},
                        "timestamp": $timestamp,
                        "device_id": "$deviceId"
                    }
                """.trimIndent()
                
                // Upload to incoming_fingerprints with timestamp as filename
                val fingerprintRef = storageRef.child("incoming_fingerprints/${timestamp}.json")
                fingerprintRef.putBytes(jsonContent.toByteArray()).await()
                _uploadState.value = UploadState.Success(fingerprintJson)
                Log.d("MainViewModel", "Fingerprint uploaded successfully")
                
                // Start listening for results
                startListeningForResults(timestamp)
            } catch (e: Exception) {
                _uploadState.value = UploadState.Error("Upload failed: ${e.message}")
                Log.e("MainViewModel", "Upload failed", e)
            }
        }
    }

    private fun startListeningForResults(timestamp: Long) {
        currentListener?.invoke()
        
        val docRef = db.collection("results")
            .document(timestamp.toString())

        val listener = docRef.addSnapshotListener { snapshot, error ->
            if (error != null) {
                Log.e("MainViewModel", "Error listening for results", error)
                _result.value = "Error: ${error.message}"
                return@addSnapshotListener
            }

            if (snapshot != null && snapshot.exists()) {
                val station = snapshot.getString("station") ?: "Unknown"
                val confidence = snapshot.getDouble("confidence") ?: 0.0
                val matchedAt = snapshot.getLong("matched_at") ?: 0L
                val distance = snapshot.getLong("distance") ?: -1L
                val matchTimestamp = snapshot.getLong("match_timestamp") ?: 0L
                
                // Format the time for better readability
                val dateFormat = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())
                val matchedTimeStr = dateFormat.format(java.util.Date(matchedAt))
                val matchTimeStr = dateFormat.format(java.util.Date(matchTimestamp))
                
                val resultText = "Matched: $station (${(confidence * 100).toInt()}%) | Distance: $distance | Matched at: $matchedTimeStr | Stream time: $matchTimeStr"
                _result.value = resultText
                
                // Remove listener after getting result
                currentListener?.invoke()
                currentListener = null
            }
        }

        currentListener = { listener.remove() }
    }

    override fun onCleared() {
        super.onCleared()
        currentListener?.invoke()
    }
} 