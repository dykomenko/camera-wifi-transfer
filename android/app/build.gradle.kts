plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.dykomenko.cameratransfer"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.dykomenko.cameratransfer"
        minSdk = 29
        targetSdk = 34
        versionCode = 4
        versionName = "0.4.0-beta"
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Sideloaded test builds: signed with the local debug key.
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

dependencies {
    // QR scanner for the code a Sony camera shows on its "Send to Smartphone" screen.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}

// The camera code is shared with the desktop app: copy it from the
// repository instead of keeping a second copy here.
val repoRoot: File = rootProject.projectDir.parentFile
val sharedPython = layout.buildDirectory.dir("shared-python")
val copySharedPython by tasks.registering(Copy::class) {
    from(File(repoRoot, "camera_session.py"))
    from(File(repoRoot, "sony_soap.py"))
    from(File(repoRoot, "vendor/airmtp")) {
        include("*.py")
        exclude("airmtpcmd_OSX_Frozen_Wrapper.py")
    }
    into(sharedPython)
}
tasks.named("preBuild") { dependsOn(copySharedPython) }
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }.configureEach {
    dependsOn(copySharedPython)
}

chaquopy {
    defaultConfig {
        version = "3.10"
        pyc {
            src = false
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
            srcDir("build/shared-python")
        }
    }
}
