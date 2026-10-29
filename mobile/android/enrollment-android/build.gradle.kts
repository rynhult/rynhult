plugins {
    id("com.android.library")
    kotlin("android")
}

android {
    namespace = "rynhult.enrollment.android"
    compileSdk = 35

    defaultConfig {
        // 28: StrongBox (setIsStrongBoxBacked) and setUnlockedDeviceRequired. Settle the real minimum in phase 0.
        minSdk = 28
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
    api(project(":enrollment-core"))
    testImplementation(kotlin("test-junit"))
}
