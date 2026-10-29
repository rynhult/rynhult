package rynhult.enrollment

import java.io.ByteArrayInputStream
import java.security.MessageDigest
import java.security.PublicKey
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.time.Instant
import java.time.OffsetDateTime
import java.util.Date

/**
 * Checks the issued identity before it is stored, as the server checks its signer's output (server mobile.identity):
 * the leaf is signed by the CA, carries exactly our key, the server-assigned subject and the clientAuth-only profile,
 * and is valid now. On rotation the CA must be the one pinned at first enrollment: a QR never replaces CA trust.
 */
object CredentialValidator {
    private const val CLIENT_AUTH = "1.3.6.1.5.5.7.3.2"
    private val PROFILE = setOf("2.5.29.19", "2.5.29.15", "2.5.29.37") // basic constraints, key usage, extended key usage

    class Identity(val leaf: X509Certificate, val ca: X509Certificate, val caSha256: String)

    fun validate(
        response: CredentialResponse,
        publicKey: PublicKey,
        now: Instant,
        pinnedCaSha256: String?,
        expectedDeviceId: String?,
    ): Identity {
        val leaf = certificates(response.certificate).singleOrNull() ?: refuse("one leaf certificate")
        val ca = certificates(response.chain).singleOrNull() ?: refuse("one CA certificate")
        val caSha256 = sha256Hex(ca.encoded)
        if (pinnedCaSha256 != null && caSha256 != pinnedCaSha256) refuse("the device CA this phone enrolled with")
        if (!Regex("^[0-9a-f]{32}$").matches(response.deviceId)) refuse("a device ID")
        if (expectedDeviceId != null && response.deviceId != expectedDeviceId) refuse("the same device on rotation")
        try {
            ca.verify(ca.publicKey)
            leaf.verify(ca.publicKey)
        } catch (error: Exception) {
            refuse("a leaf signed by the device CA")
        }
        if (ca.basicConstraints < 0) refuse("a CA certificate in the chain")
        if (!leaf.publicKey.encoded.contentEquals(publicKey.encoded)) refuse("this phone's own key")
        if (leaf.subjectX500Principal.getName("RFC2253") != "OU=acc-native,CN=${response.deviceId}") refuse("the native subject")
        if (leaf.basicConstraints != -1) refuse("a leaf that isn't a CA")
        if (leaf.criticalExtensionOIDs != PROFILE || !leaf.nonCriticalExtensionOIDs.isNullOrEmpty()) refuse("the native profile")
        if (leaf.extendedKeyUsage != listOf(CLIENT_AUTH)) refuse("client authentication only")
        val usage = leaf.keyUsage ?: refuse("a key usage")
        if (!usage[0] || usage.drop(1).any { it }) refuse("digital signature only")
        try {
            leaf.checkValidity(Date.from(now))
            ca.checkValidity(Date.from(now))
        } catch (error: Exception) {
            refuse("a certificate that is valid now")
        }
        if (leaf.notAfter.after(ca.notAfter)) refuse("a leaf that ends before its CA")
        if (OffsetDateTime.parse(response.expiresAt).toEpochSecond() != leaf.notAfter.toInstant().epochSecond) {
            refuse("an expiry that matches the certificate")
        }
        return Identity(leaf, ca, caSha256)
    }

    fun certificates(pem: String): List<X509Certificate> = try {
        CertificateFactory.getInstance("X.509")
            .generateCertificates(ByteArrayInputStream(pem.toByteArray(Charsets.US_ASCII)))
            .map { it as X509Certificate }
    } catch (error: Exception) {
        emptyList()
    }

    fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun refuse(expected: String): Nothing =
        throw EnrollmentException("The server's credential isn't usable: expected $expected. Nothing was stored.")
}
