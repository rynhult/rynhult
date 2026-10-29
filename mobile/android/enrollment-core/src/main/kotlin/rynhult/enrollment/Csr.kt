package rynhult.enrollment

import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.security.PublicKey
import java.util.Base64

/**
 * A minimal PKCS#10 request for an app-held P-256 key, signed through [DeviceKeys] so the private key never leaves
 * the Keystore. The server ignores the subject and every extension (the signer assigns them), so the request carries
 * a fixed subject and no attributes. Only the DER the request needs is encoded here.
 */
object Csr {
    private const val SUBJECT = "rynhult-android"
    private val COMMON_NAME = byteArrayOf(0x55, 0x04, 0x03)                                           // 2.5.4.3
    private val ECDSA_WITH_SHA256 = byteArrayOf(0x2A, 0x86.toByte(), 0x48, 0xCE.toByte(), 0x3D, 0x04, 0x03, 0x02) // 1.2.840.10045.4.3.2

    fun pem(publicKey: PublicKey, sign: (ByteArray) -> ByteArray): String {
        require(publicKey.algorithm == "EC" && publicKey.format == "X.509") { "a P-256 public key is required" }
        val name = seq(set(seq(tlv(0x06, COMMON_NAME), tlv(0x0C, SUBJECT.toByteArray()))))
        val info = seq(tlv(0x02, byteArrayOf(0)), name, publicKey.encoded, tlv(0xA0, ByteArray(0)))
        val signature = sign(info)
        val der = seq(info, seq(tlv(0x06, ECDSA_WITH_SHA256)), tlv(0x03, byteArrayOf(0) + signature))
        val body = Base64.getMimeEncoder(64, "\n".toByteArray()).encodeToString(der)
        return "-----BEGIN CERTIFICATE REQUEST-----\n$body\n-----END CERTIFICATE REQUEST-----\n"
    }

    fun sha256Hex(text: String): String =
        MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

    private fun seq(vararg parts: ByteArray) = tlv(0x30, concat(parts))
    private fun set(vararg parts: ByteArray) = tlv(0x31, concat(parts))

    private fun concat(parts: Array<out ByteArray>): ByteArray {
        val out = ByteArrayOutputStream()
        parts.forEach { out.write(it) }
        return out.toByteArray()
    }

    internal fun tlv(tag: Int, value: ByteArray): ByteArray {
        val out = ByteArrayOutputStream()
        out.write(tag)
        val length = value.size
        when {
            length < 0x80 -> out.write(length)
            length <= 0xFF -> { out.write(0x81); out.write(length) }
            length <= 0xFFFF -> { out.write(0x82); out.write(length ushr 8); out.write(length and 0xFF) }
            else -> throw IllegalArgumentException("DER value too long")
        }
        out.write(value)
        return out.toByteArray()
    }
}
