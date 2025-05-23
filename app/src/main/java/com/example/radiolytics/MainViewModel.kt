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
import java.io.File
import java.io.FileOutputStream
import java.util.Locale

class MainViewModel(application: Application) : AndroidViewModel(application) {
    companion object {
        private const val RECORDING_INTERVAL_MS = 7000L // enter value in Milliseconds
        private const val MAX_BUFFER_MINUTES = 1 // Keep 1 minute of fingerprints
    }

    private val audioFingerprinter = AudioFingerprinter()
    private val storage = FirebaseStorage.getInstance()
    private val storageRef = storage.reference
    private var isRecordingContinuously = false
    private var recordingJob: kotlinx.coroutines.Job? = null

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
        if (isRecordingContinuously) {
            stopRecording()
            return
        }

        isRecordingContinuously = true
        _recordingState.value = RecordingState.Recording

        recordingJob = viewModelScope.launch(Dispatchers.IO) {
            try {
                audioFingerprinter.startRecording()
                while (isRecordingContinuously) {
                    val startTime = System.currentTimeMillis()
                    var frameCount = 0
                    while (System.currentTimeMillis() - startTime < RECORDING_INTERVAL_MS) {
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
                    // Start a new recording immediately
                    audioFingerprinter.startRecording()
                }
            } catch (e: Exception) {
                _recordingState.postValue(RecordingState.Error("Recording error: ${e.message}"))
                isRecordingContinuously = false
            }
            _recordingState.postValue(RecordingState.Idle)
        }
    }

    fun stopRecording() {
        isRecordingContinuously = false
        recordingJob?.cancel()
        recordingJob = null
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
                val dateFormat = java.text.SimpleDateFormat("HH-mm-ss", Locale.getDefault())
                val readableTimestamp = dateFormat.format(java.util.Date(timestamp))
                val filePrefix = "AppFingerprint_"
                val fileName = "${readableTimestamp}_${filePrefix}${RECORDING_INTERVAL_MS/1000}s.json"
                // Use absolute path for local storage
                val adminDir = File("/storage/emulated/0/Android/data/com.example.radiolytics/files/ADMIN DO NOT COMMIT")
                if (!adminDir.exists()) adminDir.mkdirs()
                val fingerprintsDir = File(adminDir, "fingerprints")
                if (!fingerprintsDir.exists()) fingerprintsDir.mkdirs()
                val localFile = File(fingerprintsDir, fileName)

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

                // Save locally
                FileOutputStream(localFile).use { it.write(jsonContent.toByteArray()) }
                Log.d("MainViewModel", "Saved fingerprint locally: ${localFile.absolutePath}")

                // Local cleanup: keep only fingerprints from the last MAX_BUFFER_MINUTES
                val cutoffTime = System.currentTimeMillis() - (MAX_BUFFER_MINUTES * 60 * 1000)
                Log.d("MainViewModel", "Cleaning up files older than: ${java.text.SimpleDateFormat("HH-mm-ss", Locale.getDefault()).format(java.util.Date(cutoffTime))}")
                val allLocal = fingerprintsDir.listFiles { f -> f.name.startsWith(filePrefix) && f.name.endsWith(".json") }?.toList() ?: emptyList()
                Log.d("MainViewModel", "Found ${allLocal.size} local files to check for cleanup")
                for (file in allLocal) {
                    val timestamp = Regex("(\\d{2}-\\d{2}-\\d{2})_AppFingerprint_\\d+s\\.json").find(file.name)?.groupValues?.getOrNull(1)?.let { dateStr ->
                        try {
                            val parts = dateStr.split("-")
                            val calendar = java.util.Calendar.getInstance().apply {
                                set(java.util.Calendar.HOUR_OF_DAY, parts[0].toInt())
                                set(java.util.Calendar.MINUTE, parts[1].toInt())
                                set(java.util.Calendar.SECOND, parts[2].toInt())
                                set(java.util.Calendar.MILLISECOND, 0)
                            }
                            calendar.timeInMillis
                        } catch (e: Exception) {
                            null
                        }
                    }
                    if (timestamp != null && timestamp < cutoffTime) {
                        try {
                            file.delete()
                            Log.d("MainViewModel", "Deleted old local fingerprint: ${file.name}")
                        } catch (e: Exception) {
                            Log.e("MainViewModel", "Failed to delete local file: ${file.name}", e)
                        }
                    }
                }

                // Upload to Firebase Storage with prefix
                val fingerprintRef = storageRef.child("fingerprints/${fileName}")
                fingerprintRef.putBytes(jsonContent.toByteArray()).await()
                Log.d("MainViewModel", "Fingerprint uploaded to Firebase: $fileName")

                // Firebase cleanup: keep only fingerprints from the last MAX_BUFFER_MINUTES
                Log.d("MainViewModel", "Starting Firebase cleanup for files older than: ${java.text.SimpleDateFormat("HH-mm-ss", Locale.getDefault()).format(java.util.Date(cutoffTime))}")
                val firebaseFiles = storageRef.child("fingerprints").listAll().await().items
                    .filter { it.name.startsWith(filePrefix) && it.name.endsWith(".json") }
                Log.d("MainViewModel", "Found ${firebaseFiles.size} Firebase files to check for cleanup")
                for (file in firebaseFiles) {
                    val timestamp = Regex("(\\d{2}-\\d{2}-\\d{2})_AppFingerprint_\\d+s\\.json").find(file.name)?.groupValues?.getOrNull(1)?.let { dateStr ->
                        try {
                            val parts = dateStr.split("-")
                            val calendar = java.util.Calendar.getInstance().apply {
                                set(java.util.Calendar.HOUR_OF_DAY, parts[0].toInt())
                                set(java.util.Calendar.MINUTE, parts[1].toInt())
                                set(java.util.Calendar.SECOND, parts[2].toInt())
                                set(java.util.Calendar.MILLISECOND, 0)
                            }
                            calendar.timeInMillis
                        } catch (e: Exception) {
                            null
                        }
                    }
                    if (timestamp != null && timestamp < cutoffTime) {
                        try {
                            file.delete().await()
                            Log.d("MainViewModel", "Deleted old Firebase fingerprint: ${file.name}")
                        } catch (e: Exception) {
                            Log.e("MainViewModel", "Failed to delete Firebase file: ${file.name}", e)
                        }
                    }
                }

                _uploadState.value = UploadState.Success(fingerprintJson)
                Log.d("MainViewModel", "Fingerprint uploaded successfully")
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
        stopRecording()
        currentListener?.invoke()
    }
} 