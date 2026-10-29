package rynhult.enrollment

import java.security.Principal
import java.security.PublicKey
import java.security.cert.X509Certificate
import javax.security.auth.x500.X500Principal

/**
 * The app-held keys. On Android this is the Keystore (enrollment-android's AndroidKeystoreKeys): keys are generated
 * inside it, never exported, and only used through [sign] and the TLS key manager. Tests use a software stand-in.
 */
interface DeviceKeys {
    /** A fresh, non-exportable P-256 signing key under [alias], replacing nothing: the alias must be unused. */
    fun create(alias: String): PublicKey

    /** SHA256withECDSA over [data] with the key under [alias], DER-encoded. */
    fun sign(alias: String, data: ByteArray): ByteArray

    fun publicKey(alias: String): PublicKey?

    /** Store the validated leaf and CA next to the key, so TLS can present them. */
    fun storeChain(alias: String, chain: List<X509Certificate>)

    fun delete(alias: String)

    /** Every alias this store holds that starts with [prefix] (EnrollmentClient.ALIAS_PREFIX), for cleanup. */
    fun aliases(prefix: String): List<String>
}

/** One HTTPS exchange with the configured origin. Implementations never follow redirects (see HttpsTransport). */
data class HttpResponse(val status: Int, val body: String, val setCookies: List<String> = emptyList()) {
    // Bodies and Set-Cookie headers carry receipts and session cookies: describe only their size.
    override fun toString() = "HttpResponse(status=$status, body=${body.length} chars, setCookies=${setCookies.size})"
}

fun interface Transport {
    fun send(method: String, path: String, jsonBody: String?, headers: Map<String, String>): HttpResponse
}

/** Which TLS servers the enrolled identity is offered to (enrollment-android's KeystoreKeyManager). */
object ClientIdentity {
    /**
     * Only to a server that names no acceptable issuers (TLS 1.3 may omit them) or names [deviceCa], the CA that issued
     * our leaf: never to a server that asks for some other CA's clients, even if server pinning were misconfigured.
     */
    fun offeredTo(deviceCa: X500Principal?, issuers: Array<out Principal>?): Boolean {
        if (issuers.isNullOrEmpty()) return true
        if (deviceCa == null) return false
        return issuers.any { issuer ->
            try {
                X500Principal(issuer.name) == deviceCa
            } catch (error: IllegalArgumentException) {
                false
            }
        }
    }
}
