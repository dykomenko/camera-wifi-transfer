plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Stand-in for Sony's phone app.  The camera's NFC tag names the package
// com.sony.playmemories.mobile (an Android Application Record); when no app
// with that name is installed, a tap opens Google Play.  This tiny app takes
// that name and hands the tag over to Camera Transfer.  It cannot be
// installed next to Sony's own app.
android {
    namespace = "com.dykomenko.cameratransfer.nfcstub"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.sony.playmemories.mobile"
        minSdk = 29
        targetSdk = 34
        versionCode = 1
        versionName = "1.0-camera-transfer"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
