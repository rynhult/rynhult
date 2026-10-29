package rynhult.enrollment

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.util.Base64

/** The server's mobile API v1 (docs/wiki/Internal/Mobile-Devices.md on the server branch), as this client uses it. */
object Api {
    const val PREFIX = "/api/mobile/v1"
    const val PLATFORM = "android"
    val json = Json { ignoreUnknownKeys = true }

    /**
     * Decode a server answer. A decoding error's message can quote the JSON input (a receipt, say), so it is replaced
     * by a fixed message and not attached as the cause: nothing from the body reaches a log or crash report.
     */
    fun <T> decode(serializer: KSerializer<T>, body: String): T = try {
        json.decodeFromString(serializer, body)
    } catch (error: IllegalArgumentException) {  // kotlinx.serialization's SerializationException is one
        throw EnrollmentException("The server's answer wasn't in the expected form.")
    }
}

class EnrollmentException(message: String, val status: Int? = null) : Exception(message)

/**
 * The claim proof: these fields joined by exactly one newline, no trailing newline, signed with the CSR's key
 * (ECDSA/SHA-256, DER) and sent as base64url without padding.
 */
object ClaimProof {
    fun message(link: PairingLink, installationId: String, csrPem: String): ByteArray = listOf(
        "acc-mobile-claim-v1", link.pairingId, link.token, link.origin, installationId, Api.PLATFORM, Csr.sha256Hex(csrPem),
    ).joinToString("\n").toByteArray(Charsets.UTF_8)

    fun encode(signature: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(signature)
}

@Serializable
data class ClaimRequest(
    val token: String,
    @SerialName("installation_id") val installationId: String,
    val platform: String,
    val csr: String,
    val proof: String,
) {
    override fun toString() = "ClaimRequest(installationId=$installationId, platform=$platform, token=<redacted>, proof=<redacted>)"
}

@Serializable
data class ClaimResponse(val receipt: String, @SerialName("matching_code") val matchingCode: String) {
    override fun toString() = "ClaimResponse(matchingCode=$matchingCode, receipt=<redacted>)"
}

@Serializable
data class ReceiptRequest(val receipt: String) {
    override fun toString() = "ReceiptRequest(receipt=<redacted>)"
}

@Serializable
data class CredentialResponse(
    @SerialName("device_id") val deviceId: String,
    val certificate: String,
    val chain: String,
    @SerialName("expires_at") val expiresAt: String,
    val activation: String,
)

@Serializable
data class DeviceRecord(
    val id: String,
    val person: String? = null,
    val kind: String? = null,
    val status: String,
    val activation: String,
    val fingerprint: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
)

@Serializable
data class DeviceList(val devices: List<DeviceRecord>)

/** `/api/auth/state`, read before binding: who is signed in through this identity. */
@Serializable
data class SignInState(
    val authenticated: Boolean = false,
    val username: String? = null,
    @SerialName("step_up_required") val stepUpRequired: Boolean? = null,
)

/** Server activation states: pending edge policy, awaiting the first bound connection, verified, or inactive. */
enum class Activation(val wire: String) {
    PENDING("pending"), AWAITING_CONNECTION("awaiting_connection"), VERIFIED("verified"), INACTIVE("inactive");

    companion object {
        fun of(wire: String): Activation =
            entries.firstOrNull { it.wire == wire } ?: throw EnrollmentException("Unknown activation state.")
    }
}
