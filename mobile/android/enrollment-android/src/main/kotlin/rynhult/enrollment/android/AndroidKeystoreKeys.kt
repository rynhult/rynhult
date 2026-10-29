package rynhult.enrollment.android

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import rynhult.enrollment.DeviceKeys
import rynhult.enrollment.EnrollmentException
import rynhult.enrollment.FileStateStore
import java.io.File
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec

/** Where the key lives: the phone's secure element (StrongBox), its TEE, or software (refused unless allowed). */
enum class KeyStorage { STRONGBOX, TRUSTED_ENVIRONMENT, SOFTWARE }

/**
 * App-specific Android Keystore keys: P-256, signing only, generated inside the Keystore and never exportable (the
 * Keystore has no export path for them). StrongBox when the phone has one, the TEE otherwise; a software-only
 * Keystore is refused unless [allowSoftware], so the app can state its assurance accurately (docs/MOBILE-APPS.md).
 *
 * Non-exportable isn't protection from a compromised app process using the key; the app lock and server checks
 * cover that. Keys are usable only while the phone is unlocked: no authenticated refresh while locked in v1.
 */
class AndroidKeystoreKeys(private val context: Context, private val allowSoftware: Boolean = false) : DeviceKeys {
    private val keyStore: KeyStore = KeyStore.getInstance(PROVIDER).apply { load(null) }

    override fun create(alias: String): PublicKey {
        if (keyStore.containsAlias(alias)) throw EnrollmentException("That key alias is already in use.")
        val strongBox = context.packageManager.hasSystemFeature(PackageManager.FEATURE_STRONGBOX_KEYSTORE)
        val publicKey = try {
            generate(alias, strongBox)
        } catch (error: StrongBoxUnavailableException) {
            generate(alias, strongBox = false)  // advertised but busy or unusable: fall back to the TEE
        }
        val storage = storage(alias)
        if (storage == KeyStorage.SOFTWARE && !allowSoftware) {
            delete(alias)
            throw EnrollmentException("This phone has no hardware-backed key storage, so it can't hold a Rynhult identity.")
        }
        return publicKey
    }

    private fun generate(alias: String, strongBox: Boolean): PublicKey {
        val spec = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            .setDigests(KeyProperties.DIGEST_SHA256)  // also what TLS 1.2/1.3 ECDSA client authentication uses
            .setUnlockedDeviceRequired(true)
            .setIsStrongBoxBacked(strongBox)
            .build()
        return KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, PROVIDER).run {
            initialize(spec)
            generateKeyPair().public
        }
    }

    /** Report where an existing key actually lives (KeyInfo), never what was requested. */
    fun storage(alias: String): KeyStorage {
        val key = privateKey(alias) ?: throw EnrollmentException("No such key.")
        val info = KeyFactory.getInstance(key.algorithm, PROVIDER).getKeySpec(key, KeyInfo::class.java)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            when (info.securityLevel) {
                KeyProperties.SECURITY_LEVEL_STRONGBOX -> KeyStorage.STRONGBOX
                KeyProperties.SECURITY_LEVEL_TRUSTED_ENVIRONMENT -> KeyStorage.TRUSTED_ENVIRONMENT
                else -> KeyStorage.SOFTWARE
            }
        } else {
            @Suppress("DEPRECATION")
            if (info.isInsideSecureHardware) KeyStorage.TRUSTED_ENVIRONMENT else KeyStorage.SOFTWARE
        }
    }

    override fun sign(alias: String, data: ByteArray): ByteArray {
        val key = privateKey(alias) ?: throw EnrollmentException("The identity key is gone; pair again.")
        return Signature.getInstance("SHA256withECDSA").run {
            initSign(key)
            update(data)
            sign()
        }
    }

    override fun publicKey(alias: String): PublicKey? = keyStore.getCertificate(alias)?.publicKey

    /** Replace the Keystore's placeholder self-signed certificate with the issued leaf and CA; the key stays put. */
    override fun storeChain(alias: String, chain: List<X509Certificate>) {
        val key = privateKey(alias) ?: throw EnrollmentException("The identity key is gone; pair again.")
        keyStore.setKeyEntry(alias, key, null, chain.toTypedArray())
    }

    override fun delete(alias: String) {
        if (keyStore.containsAlias(alias)) keyStore.deleteEntry(alias)
    }

    override fun aliases(prefix: String): List<String> = keyStore.aliases().toList().filter { it.startsWith(prefix) }

    fun privateKey(alias: String): PrivateKey? = keyStore.getKey(alias, null) as PrivateKey?

    fun chain(alias: String): Array<X509Certificate>? =
        keyStore.getCertificateChain(alias)?.map { it as X509Certificate }?.toTypedArray()

    companion object {
        const val PROVIDER = "AndroidKeyStore"

        /** The enrollment record in no-backup storage: backups and device transfers never carry it. */
        fun stateStore(context: Context) = FileStateStore(File(context.noBackupFilesDir, "rynhult-enrollment.json"))
    }
}
