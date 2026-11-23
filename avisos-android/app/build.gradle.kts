plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.adipack.avisos"
    compileSdk = 35
    buildToolsVersion = "35.0.0"  // la instala el workflow (paso «Android SDK»)

    defaultConfig {
        applicationId = "com.adipack.avisos"   // NO CAMBIAR: el MDM lo reconoce por este nombre
        minSdk = 26
        targetSdk = 35
        // En GitHub Actions se toman de AVISOS_VERSION_CODE / AVISOS_VERSION_NAME (cada build sube el número).
        versionCode = (System.getenv("AVISOS_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("AVISOS_VERSION_NAME") ?: "1.0.0"
    }

    buildFeatures {
        buildConfig = true   // APP_VERSION = BuildConfig.VERSION_NAME (se informa al MDM)
    }

    // Misma llave que el agente MDM (mismos Secrets de GitHub). Sin ella se compila sólo el debug.
    val keystorePath = System.getenv("MDM_KEYSTORE") ?: "../adipack-mdm.jks"
    val hasKeystore = file(keystorePath).exists() && !System.getenv("MDM_KEYSTORE_PASS").isNullOrEmpty()

    signingConfigs {
        create("release") {
            storeFile = file(keystorePath)
            storePassword = System.getenv("MDM_KEYSTORE_PASS") ?: ""
            keyAlias = System.getenv("MDM_KEY_ALIAS") ?: "mdm"
            keyPassword = System.getenv("MDM_KEY_PASS") ?: ""
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (hasKeystore) signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    lint {
        // El informe de lint se publica como artefacto en GitHub Actions; no detiene la compilación.
        abortOnError = false
        // El release no repite lint: así reglas pensadas para Google Play (permisos de MDM como
        // QUERY_ALL_PACKAGES) nunca bloquean el APK firmado. El informe sale del lintDebug.
        checkReleaseBuilds = false
    }
}

dependencies {
    // Sin dependencias externas a propósito: el cliente MQTT es propio (MqttClient.kt) y usa sólo java.net.
}
