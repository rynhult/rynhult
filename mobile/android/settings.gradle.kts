// Rynhult Android enrollment (docs/MOBILE-APPS.md, mobile phase 1). See README.md for the build and test commands.
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "rynhult-android-enrollment"

// Pure Kotlin/JVM: the protocol, validation and state, unit-tested on any JDK 17.
include(":enrollment-core")

// The Android Keystore side needs the Android SDK; it's left out when there is none, so the JVM tests still run.
if (System.getenv("ANDROID_HOME") != null || file("local.properties").exists()) {
    include(":enrollment-android")
}
