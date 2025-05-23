package com.example.radiolytics

import android.app.Application
import android.util.Log
import com.google.android.gms.common.ConnectionResult
import com.google.android.gms.common.GoogleApiAvailability
import com.google.firebase.FirebaseApp
import com.google.firebase.appcheck.FirebaseAppCheck
import com.google.firebase.appcheck.debug.DebugAppCheckProviderFactory
import com.google.firebase.appcheck.playintegrity.PlayIntegrityAppCheckProviderFactory

class RadiolyticsApp : Application() {
    companion object {
        private const val TAG = "RadiolyticsApp"
    }

    override fun onCreate() {
        super.onCreate()
        
        // Initialize Firebase
        FirebaseApp.initializeApp(this)
        
        // Initialize Firebase App Check with debug provider for development
        val firebaseAppCheck = FirebaseAppCheck.getInstance()
        try {
            // Use debug provider for development
            val debugFactory = DebugAppCheckProviderFactory.getInstance()
            firebaseAppCheck.installAppCheckProviderFactory(debugFactory)
            Log.d(TAG, "Using Debug App Check provider for development")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to initialize debug provider", e)
        }
        
        // Check Google Play Services availability
        val googleApiAvailability = GoogleApiAvailability.getInstance()
        val resultCode = googleApiAvailability.isGooglePlayServicesAvailable(this)
        if (resultCode != ConnectionResult.SUCCESS) {
            // Google Play Services is not available
            googleApiAvailability.showErrorNotification(this, resultCode)
        }
    }
} 